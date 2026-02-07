#!/usr/bin/env python3
import json
import os
import sys
from typing import Dict, Optional

from origin_updater import apply_updates, build_updates, list_git_repos, recap_updates
STATE_FILE = "migration_state.json"


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

    recap_updates(updates)
    if not prompt_yes_no("Apply these origin updates?", default=False):
        print("Cancelled.")
        return

    conflicts = apply_updates(updates)
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
