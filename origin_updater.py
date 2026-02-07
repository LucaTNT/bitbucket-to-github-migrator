import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


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


def run_git_capture(command: List[str], cwd: Optional[str] = None) -> str:
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


def list_git_repos(root: str) -> List[str]:
    repos: List[str] = []
    for dirpath, dirnames, _filenames in os.walk(root):
        if ".git" in dirnames:
            repos.append(dirpath)
            dirnames[:] = []
    return repos


def parse_bitbucket_origin(origin: str) -> Optional[Tuple[str, str]]:
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
            origin = run_git_capture(["git", "remote", "get-url", "origin"], cwd=repo_path)
        except RuntimeError:
            continue
        parsed = parse_bitbucket_origin(origin)
        if not parsed:
            continue
        pushurl = run_git_optional(["git", "remote", "get-url", "--push", "origin"], cwd=repo_path)
        workspace, repo = parsed
        key = f"{workspace}/{repo}"
        target_owner = default_owner
        target_name = repo
        from_state = False
        if key in state:
            entry = state[key]
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
                source_key=key,
                target_owner=target_owner,
                target_name=target_name,
                new_origin=new_origin,
                from_state=from_state,
                update_pushurl=update_pushurl,
                pushurl_conflict=pushurl_conflict,
            )
        )
    return updates


def recap_updates(updates: List[RepoUpdate]) -> None:
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
        run_git_capture(["git", "remote", "set-url", "origin", item.new_origin], cwd=item.path)
        if item.update_pushurl:
            run_git_capture(["git", "remote", "set-url", "--push", "origin", item.new_origin], cwd=item.path)
        if item.pushurl_conflict:
            conflicts.append(item)
        print("Updated.")
    return conflicts
