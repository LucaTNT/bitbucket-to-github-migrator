#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


STATE_FILE = "migration_state.json"


@dataclass
class RepoUpdate:
    path: str
    current_origin: str
    current_pushurl: Optional[str]
    source_key: str
    target_owner: str
    target_name: str
    new_origin: str
    from_state: bool
    update_pushurl: bool
    pushurl_conflict: bool


def run_git(command: List[str], cwd: Optional[str] = None) -> str:
    result = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(command)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout.strip()


def run_git_optional(command: List[str], cwd: Optional[str] = None) -> Optional[str]:
    result = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def load_state(path: str) -> Dict[str, Dict[str, str]]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        return {}
    state: Dict[str, Dict[str, str]] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            state[str(key)] = {
                "status": str(value.get("status", "")),
                "target_owner": str(value.get("target_owner", "")),
                "target_name": str(value.get("target_name", "")),
            }
    return state


def is_git_repo(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git"))


def list_git_repos(root: str) -> List[str]:
    repos: List[str] = []
    for dirpath, dirnames, _filenames in os.walk(root):
        if ".git" in dirnames:
            repos.append(dirpath)
            dirnames[:] = []
    return repos


def parse_bitbucket_origin(origin: str) -> Optional[Tuple[str, str]]:
    # Accepts https://bitbucket.org/workspace/repo(.git) or git@bitbucket.org:workspace/repo(.git)
    https_match = re.match(r"https://([^@]+@)?bitbucket\.org/([^/]+)/([^/]+?)(\.git)?$", origin)
    if https_match:
        workspace = https_match.group(2)
        repo = https_match.group(3)
        return workspace, repo
    ssh_match = re.match(r"git@bitbucket\.org:([^/]+)/([^/]+?)(\.git)?$", origin)
    if ssh_match:
        workspace = ssh_match.group(1)
        repo = ssh_match.group(2)
        return workspace, repo
    return None


def build_updates(
    repos: List[str], state: Dict[str, Dict[str, str]], default_owner: str
) -> List[RepoUpdate]:
    updates: List[RepoUpdate] = []
    for repo_path in repos:
        try:
            origin = run_git(["git", "remote", "get-url", "origin"], cwd=repo_path)
        except RuntimeError:
            continue
        pushurl = run_git_optional(["git", "remote", "get-url", "--push", "origin"], cwd=repo_path)
        parsed = parse_bitbucket_origin(origin)
        if not parsed:
            continue
        workspace, repo = parsed
        source_key = f"{workspace}/{repo}"
        target_owner = default_owner
        target_name = repo
        from_state = False
        if source_key in state:
            entry = state[source_key]
            if entry.get("target_owner"):
                target_owner = entry["target_owner"]
            if entry.get("target_name"):
                target_name = entry["target_name"]
            from_state = True
        new_origin = f"git@github.com:{target_owner}/{target_name}.git"
        update_pushurl = False
        pushurl_conflict = False
        if pushurl:
            if pushurl == origin:
                update_pushurl = True
            else:
                pushurl_conflict = True
        updates.append(
            RepoUpdate(
                path=repo_path,
                current_origin=origin,
                current_pushurl=pushurl,
                source_key=source_key,
                target_owner=target_owner,
                target_name=target_name,
                new_origin=new_origin,
                from_state=from_state,
                update_pushurl=update_pushurl,
                pushurl_conflict=pushurl_conflict,
            )
        )
    return updates


def recap(updates: List[RepoUpdate]) -> None:
    print("\nPlanned origin updates:")
    highlight = sys.stdout.isatty()
    green = "\033[32m" if highlight else ""
    reset = "\033[0m" if highlight else ""
    for idx, item in enumerate(updates, start=1):
        state_note = f"{green} [from state]{reset}" if item.from_state else ""
        print(f"{idx:3d}. {item.path}")
        print(f"     {item.source_key}{state_note}")
        print(f"     {item.current_origin} -> {item.new_origin}")


def apply_updates(updates: List[RepoUpdate]) -> List[RepoUpdate]:
    conflicts: List[RepoUpdate] = []
    for idx, item in enumerate(updates, start=1):
        print(f"\n[{idx}/{len(updates)}] {item.path}")
        run_git(["git", "remote", "set-url", "origin", item.new_origin], cwd=item.path)
        if item.update_pushurl:
            run_git(["git", "remote", "set-url", "--push", "origin", item.new_origin], cwd=item.path)
        if item.pushurl_conflict:
            conflicts.append(item)
        print("Updated.")
    return conflicts


def prompt(text: str, default: Optional[str] = None) -> str:
    if default is not None:
        text = f"{text} [{default}] "
    else:
        text = f"{text} "
    while True:
        value = input(text).strip()
        if value:
            return value
        if default is not None:
            return default


def prompt_yes_no(text: str, default: bool = False) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        value = input(text + suffix).strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False


def env_value(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value:
        value = value.strip()
    return value or None


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as exc:
        print(f"Warning: failed to read .env file: {exc}", file=sys.stderr)


def print_env_usage(values: Dict[str, Optional[str]]) -> None:
    used = {k: v for k, v in values.items() if v}
    if not used:
        return
    print("\nUsing values from .env/environment:")
    for key, value in used.items():
        print(f"- {key}={value}")


def main() -> None:
    load_dotenv()
    env_root = env_value("SCAN_ROOT")
    env_owner = env_value("GITHUB_OWNER")
    env_state = env_value("STATE_FILE")
    print_env_usage(
        {
            "SCAN_ROOT": env_root,
            "GITHUB_OWNER": env_owner,
            "STATE_FILE": env_state,
        }
    )

    root = prompt("Root path to scan", env_root or os.getcwd())
    print(f"Scanning for git repos under {root}")
    state_path = env_state or os.path.join(os.getcwd(), STATE_FILE)
    state = load_state(state_path)
    default_owner = prompt("Default GitHub owner (user or org)", env_owner)

    repos = list_git_repos(root)
    updates = build_updates(repos, state, default_owner)
    if not updates:
        print("No Bitbucket origins found.")
        return

    recap(updates)
    if not prompt_yes_no("Apply these origin updates?", default=False):
        print("Cancelled.")
        return

    apply_updates(updates)
    conflicts = [u for u in updates if u.pushurl_conflict]
    if conflicts:
        print("\nPushurl conflicts (left unchanged):")
        for item in conflicts:
            print(f"- {item.path}: {item.current_pushurl}")
    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
