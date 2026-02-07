#!/usr/bin/env python3
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from origin_updater import apply_updates, build_updates, list_git_repos, recap_updates
BITBUCKET_API = "https://api.bitbucket.org/2.0"
GITHUB_API = "https://api.github.com"
STATE_FILE = "migration_state.json"


@dataclass
class BitbucketRepo:
    workspace: str
    slug: str
    name: str
    https_clone: str
    web_url: str


@dataclass
class RepoPlan:
    source: BitbucketRepo
    target_owner: str
    target_name: str
    status: str = "pending"

def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


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
        eprint(f"Warning: failed to read .env file: {exc}")


def env_bool(name: str) -> Optional[bool]:
    value = env_value(name)
    if value is None:
        return None
    return value.lower() in {"1", "true", "yes", "y", "on"}


def http_json(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Dict] = None,
) -> Tuple[int, Dict]:
    data = None
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    request = Request(url, method=method, headers=req_headers, data=data)
    try:
        with urlopen(request) as resp:
            status = resp.status
            payload = resp.read().decode("utf-8")
            return status, json.loads(payload) if payload else {}
    except HTTPError as err:
        payload = err.read().decode("utf-8")
        try:
            data = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            data = {"message": payload}
        return err.code, data
    except URLError as err:
        raise RuntimeError(f"Network error calling {url}: {err}") from err


def bitbucket_auth_header(username: str, api_token: str) -> Dict[str, str]:
    token = base64.b64encode(f"{username}:{api_token}".encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {token}"}


def github_auth_header(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class WorkspaceAuthError(RuntimeError):
    pass


def source_key(repo: BitbucketRepo) -> str:
    return f"{repo.workspace}/{repo.slug}"


def load_state() -> Dict[str, Dict[str, str]]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            state: Dict[str, Dict[str, str]] = {}
            for key, value in data.items():
                if isinstance(value, dict):
                    state[str(key)] = {
                        "status": str(value.get("status", "pending")),
                        "target_owner": str(value.get("target_owner", "")),
                        "target_name": str(value.get("target_name", "")),
                    }
            return state
    except Exception:
        return {}
    return {}


def save_state(plans: List[RepoPlan]) -> None:
    data: Dict[str, Dict[str, str]] = {}
    for plan in plans:
        data[source_key(plan.source)] = {
            "status": plan.status,
            "target_owner": plan.target_owner,
            "target_name": plan.target_name,
        }
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)


def apply_existing_state(plans: List[RepoPlan]) -> List[RepoPlan]:
    state = load_state()
    if not state:
        return plans
    for plan in plans:
        key = source_key(plan.source)
        if key in state:
            plan.status = state[key].get("status", plan.status)
            existing_owner = state[key].get("target_owner")
            existing_name = state[key].get("target_name")
            if existing_owner and existing_name:
                plan.target_owner = existing_owner
                plan.target_name = existing_name
    return plans


def fetch_bitbucket_workspaces(user_email: str, api_token: str) -> List[str]:
    headers = bitbucket_auth_header(user_email, api_token)
    workspaces: List[str] = []
    url = f"{BITBUCKET_API}/workspaces?role=member&pagelen=100"
    while url:
        status, data = http_json(url, headers=headers)
        if status != 200:
            raise WorkspaceAuthError(f"Failed to fetch workspaces (status {status}): {data}")
        for item in data.get("values", []):
            slug = item.get("slug")
            if slug:
                workspaces.append(slug)
        url = data.get("next")
    return workspaces


def prompt_workspaces_manual() -> List[str]:
    print(
        textwrap.dedent(
            """
We couldn't list workspaces automatically. You can:
- Re-enter a user API token that supports the /workspaces endpoint
- Or enter workspace slugs manually (comma-separated)
"""
        ).strip()
    )
    raw = prompt("Workspace slugs (comma-separated, e.g., myteam,easypodcast)")
    return [slug.strip() for slug in raw.split(",") if slug.strip()]


def fetch_bitbucket_repos(user_email: str, api_token: str, workspace: str) -> List[BitbucketRepo]:
    headers = bitbucket_auth_header(user_email, api_token)
    repos: List[BitbucketRepo] = []
    url = f"{BITBUCKET_API}/repositories/{workspace}?pagelen=100"
    while url:
        status, data = http_json(url, headers=headers)
        if status != 200:
            raise RuntimeError(f"Failed to fetch repos for {workspace} (status {status}): {data}")
        for item in data.get("values", []):
            slug = item.get("slug")
            name = item.get("name") or slug
            links = item.get("links", {})
            clone_links = links.get("clone", [])
            https_clone = ""
            for clone in clone_links:
                if clone.get("name") == "https":
                    https_clone = clone.get("href", "")
                    break
            web_url = ""
            if links.get("html"):
                web_url = links["html"].get("href", "")
            if slug and https_clone and web_url:
                repos.append(
                    BitbucketRepo(
                        workspace=workspace,
                        slug=slug,
                        name=name,
                        https_clone=https_clone,
                        web_url=web_url,
                    )
                )
        url = data.get("next")
    return repos


def pick_repos(repos: List[BitbucketRepo], state: Dict[str, Dict[str, str]]) -> List[BitbucketRepo]:
    print("\nRepositories found:")
    for idx, repo in enumerate(repos, start=1):
        note = ""
        key = source_key(repo)
        if key in state and state[key].get("status") == "done":
            target_owner = state[key].get("target_owner", "")
            target_name = state[key].get("target_name", "")
            if target_owner and target_name:
                note = f" [migrated -> {target_owner}/{target_name}]"
            else:
                note = " [migrated]"
        print(f"{idx:3d}. {repo.workspace}/{repo.slug} ({repo.name}){note}")

    print(
        textwrap.dedent(
            """

Selection tips:
- Enter numbers or ranges separated by commas (e.g., 1,3,5-7)
- Type 'all' to select everything
- Type 'none' to clear selection
- Type 'done' when finished
"""
        ).strip()
    )

    selected: Dict[int, BitbucketRepo] = {}
    total = len(repos)
    while True:
        choice = input("Selection> ").strip().lower()
        if not choice:
            continue
        if choice == "all":
            selected = {i: repos[i - 1] for i in range(1, total + 1)}
            print(f"Selected {len(selected)} repositories.")
            continue
        if choice == "none":
            selected = {}
            print("Selection cleared.")
            continue
        if choice == "done":
            if selected:
                return [selected[i] for i in sorted(selected)]
            print("No repositories selected yet.")
            continue

        for part in choice.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start_str, end_str = part.split("-", 1)
                if start_str.isdigit() and end_str.isdigit():
                    start = int(start_str)
                    end = int(end_str)
                    for i in range(start, end + 1):
                        if 1 <= i <= total:
                            selected[i] = repos[i - 1]
            elif part.isdigit():
                i = int(part)
                if 1 <= i <= total:
                    selected[i] = repos[i - 1]
        print(f"Selected {len(selected)} repositories.")


def create_github_repo(
    token: str, owner: str, owner_is_user: bool, repo_name: str
) -> Tuple[bool, str]:
    headers = github_auth_header(token)
    body = {"name": repo_name, "private": True}
    if owner_is_user:
        url = f"{GITHUB_API}/user/repos"
    else:
        url = f"{GITHUB_API}/orgs/{owner}/repos"

    status, data = http_json(url, method="POST", headers=headers, body=body)
    if status in {201, 202}:
        return True, "created"
    if status == 422 and isinstance(data, dict):
        message = data.get("message", "")
        errors = data.get("errors", [])
        if "already exists" in message.lower():
            return False, "exists"
        for err in errors:
            if isinstance(err, dict):
                if "already exists" in str(err.get("message", "")).lower():
                    return False, "exists"
            return False, "exists"
    raise RuntimeError(f"Failed to create GitHub repo {owner}/{repo_name} (status {status}): {data}")


def fetch_github_repo_info(token: str, owner: str, repo_name: str) -> Dict:
    headers = github_auth_header(token)
    status, data = http_json(f"{GITHUB_API}/repos/{owner}/{repo_name}", headers=headers)
    if status != 200:
        raise RuntimeError(
            f"Failed to fetch GitHub repo info {owner}/{repo_name} (status {status}): {data}"
        )
    return data


def run_git(command: List[str], cwd: Optional[str] = None) -> None:
    result = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(command)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def run_git_with_retry(
    command: List[str],
    cwd: Optional[str] = None,
    retries: int = 3,
    delay_seconds: int = 10,
) -> None:
    last_error: Optional[RuntimeError] = None
    for attempt in range(1, retries + 1):
        try:
            run_git(command, cwd=cwd)
            return
        except RuntimeError as exc:
            last_error = exc
            if attempt >= retries:
                break
            print(f"Git failed (attempt {attempt}/{retries}). Retrying in {delay_seconds}s...")
            time.sleep(delay_seconds)
    if last_error:
        raise last_error


def git_lfs_available() -> bool:
    try:
        run_git(["git", "lfs", "version"])
        return True
    except RuntimeError:
        return False


def inject_basic_auth(url: str, username: str, password: str) -> str:
    parsed = urlparse(url)
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = netloc.split("@", 1)[1]
    user = quote(username, safe="")
    pwd = quote(password, safe="")
    netloc = f"{user}:{pwd}@{netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


def mirror_repo(
    repo: BitbucketRepo,
    bitbucket_username: str,
    bitbucket_api_token: str,
    github_username: str,
    github_token: str,
    target_owner: str,
    target_name: str,
    lfs_migrate: bool,
    lfs_threshold: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="bb-mirror-") as tmpdir:
        bb_url = inject_basic_auth(repo.https_clone, bitbucket_username, bitbucket_api_token)
        run_git_with_retry(["git", "clone", "--mirror", bb_url, tmpdir], retries=3, delay_seconds=15)

        gh_url = inject_basic_auth(
            f"https://github.com/{target_owner}/{target_name}.git",
            github_username,
            github_token,
        )
        run_git(["git", "remote", "set-url", "origin", gh_url], cwd=tmpdir)
        if lfs_migrate:
            run_git(["git", "lfs", "install", "--local"], cwd=tmpdir)
            run_git(
                [
                    "git",
                    "lfs",
                    "migrate",
                    "import",
                    "--everything",
                    f"--above={lfs_threshold}",
                ],
                cwd=tmpdir,
            )
        run_git_with_retry(["git", "push", "--mirror"], cwd=tmpdir, retries=3, delay_seconds=15)


def recap(plans: List[RepoPlan]) -> None:
    print("\nRecap: repositories to mirror")
    for plan in plans:
        print(
            f"- {plan.source.workspace}/{plan.source.slug} -> "
            f"{plan.target_owner}/{plan.target_name} [{plan.status}]"
        )


def edit_plans(plans: List[RepoPlan]) -> List[RepoPlan]:
    print("\nEdit target names (optional).")
    print("Current plan:")
    for idx, plan in enumerate(plans, start=1):
        print(f"{idx:3d}. {plan.source.workspace}/{plan.source.slug} -> {plan.target_owner}/{plan.target_name}")

    print(
        textwrap.dedent(
            """

Edit commands:
- Enter: <index> <new_repo_name>          (keeps owner)
- Enter: <index> <owner>/<new_repo_name>
- Type 'done' when finished
"""
        ).strip()
    )

    while True:
        raw = input("Edit> ").strip()
        if not raw:
            continue
        if raw.lower() == "done":
            return plans

        parts = raw.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            print("Invalid input. Use: <index> <new_repo_name> or <index> <owner>/<new_repo_name>")
            continue

        idx = int(parts[0])
        if not (1 <= idx <= len(plans)):
            print("Invalid index.")
            continue

        value = parts[1].strip()
        if "/" in value:
            owner, name = value.split("/", 1)
            owner = owner.strip()
            name = name.strip()
            if not owner or not name:
                print("Invalid owner/name format.")
                continue
            plans[idx - 1].target_owner = owner
            plans[idx - 1].target_name = name
        else:
            plans[idx - 1].target_name = value

        plan = plans[idx - 1]
        print(f"Updated {idx}: {plan.source.workspace}/{plan.source.slug} -> {plan.target_owner}/{plan.target_name}")


def write_report(plans: List[RepoPlan], path: str) -> None:
    lines = [
        "# Bitbucket to GitHub Migration Report",
        "",
        "| Bitbucket Repo | GitHub Repo |",
        "| --- | --- |",
    ]
    for plan in plans:
        bb = f"[{plan.source.workspace}/{plan.source.slug}]({plan.source.web_url})"
        gh_url = f"https://github.com/{plan.target_owner}/{plan.target_name}"
        gh = f"[{plan.target_owner}/{plan.target_name}]({gh_url})"
        lines.append(f"| {bb} | {gh} |")

    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main() -> None:
    print("Bitbucket -> GitHub migration helper")
    load_dotenv()

    bb_email = env_value("BITBUCKET_EMAIL") or prompt("Atlassian account email (token owner)")
    bb_username = env_value("BITBUCKET_USERNAME") or prompt("Bitbucket username (from profile settings)")
    bb_api_token = env_value("BITBUCKET_TOKEN") or prompt("Bitbucket API token with scopes (Bitbucket app)")

    try:
        workspaces = fetch_bitbucket_workspaces(bb_email, bb_api_token)
    except WorkspaceAuthError as exc:
        eprint(f"{exc}")
        eprint(
            "Tip: Ensure this is a Bitbucket API token (created in Bitbucket settings), "
            "created via Atlassian account settings > Security > Create API token with scopes, "
            "and that Bitbucket was selected as the app. The token must include "
            "read:user:bitbucket and read:workspace:bitbucket scopes."
        )
        if not prompt_yes_no("Enter workspaces manually instead?", default=True):
            print("Cancelled.")
            return
        workspaces = prompt_workspaces_manual()
    if "easypodcast" not in workspaces:
        eprint(
            "Note: workspace 'easypodcast' not found in your Bitbucket memberships. "
            "If you expect it, double-check access."
        )
    if not workspaces:
        raise RuntimeError("No workspaces found for this Bitbucket account.")

    all_repos: List[BitbucketRepo] = []
    for workspace in workspaces:
        repos = fetch_bitbucket_repos(bb_email, bb_api_token, workspace)
        all_repos.extend(repos)

    if not all_repos:
        raise RuntimeError("No repositories found across your workspaces.")

    all_repos.sort(key=lambda r: (r.workspace, r.slug))
    state = load_state()
    selected = pick_repos(all_repos, state)

    gh_username = env_value("GITHUB_USERNAME") or prompt("GitHub username (for auth)")
    gh_token = env_value("GITHUB_TOKEN") or prompt("GitHub personal access token")
    default_owner = env_value("GITHUB_OWNER") or prompt("Default GitHub owner (user or org)", gh_username)
    lfs_env = env_bool("LFS_MIGRATE")
    lfs_migrate = lfs_env if lfs_env is not None else prompt_yes_no(
        "Enable Git LFS migration for large files (destination history only)?",
        default=False,
    )
    lfs_threshold = env_value("LFS_THRESHOLD") or "5MB"
    if lfs_migrate and not git_lfs_available():
        raise RuntimeError("git-lfs is required for LFS migration but was not found on PATH.")
    dry_run_env = env_bool("DRY_RUN")
    dry_run = dry_run_env if dry_run_env is not None else False
    plans: List[RepoPlan] = []
    for repo in selected:
        target_name = repo.slug
        target_owner = default_owner
        plans.append(RepoPlan(source=repo, target_owner=target_owner, target_name=target_name))

    plans = edit_plans(plans)
    plans = apply_existing_state(plans)
    recap(plans)
    if dry_run:
        print("\nDry run enabled. Exiting without mirroring.")
        return
    if not prompt_yes_no("Proceed with mirroring?"):
        print("Cancelled.")
        return

    total = len(plans)
    for idx, plan in enumerate(plans, start=1):
        if plan.status == "done":
            print(f"\n[{idx}/{total}] {plan.source.workspace}/{plan.source.slug} -> {plan.target_owner}/{plan.target_name}")
            print("Already completed. Skipping.")
            continue
        print(f"\n[{idx}/{total}] {plan.source.workspace}/{plan.source.slug} -> {plan.target_owner}/{plan.target_name}")
        plan.status = "in_progress"
        save_state(plans)
        owner_is_user = plan.target_owner == gh_username
        try:
            created, status = create_github_repo(
                gh_token, plan.target_owner, owner_is_user, plan.target_name
            )
            if status == "exists":
                info = fetch_github_repo_info(gh_token, plan.target_owner, plan.target_name)
                is_empty = bool(info.get("empty")) or int(info.get("size", 0)) == 0
                if is_empty:
                    print("Using existing empty GitHub repository.")
                else:
                    proceed = prompt_yes_no(
                        f"GitHub repo {plan.target_owner}/{plan.target_name} is not empty. Push mirror anyway?",
                        default=False,
                    )
                    if not proceed:
                        print("Skipped.")
                        plan.status = "pending"
                        save_state(plans)
                        continue
            elif created:
                print("Created GitHub repository.")

            print("Mirroring...")
            mirror_repo(
                plan.source,
                bb_username,
                bb_api_token,
                gh_username,
                gh_token,
                plan.target_owner,
                plan.target_name,
                lfs_migrate,
                lfs_threshold,
            )
            print("Done.")
            plan.status = "done"
            save_state(plans)
        except Exception as exc:
            eprint(f"Error while mirroring {plan.source.workspace}/{plan.source.slug}: {exc}")
            plan.status = "pending"
            save_state(plans)
            print("Continuing to next repository.")
            continue

    report_path = os.path.join(os.getcwd(), "migration_report.md")
    write_report(plans, report_path)

    print("\nMigration summary:")
    for plan in plans:
        print(f"{plan.source.workspace}/{plan.source.slug} -> {plan.target_owner}/{plan.target_name}")
    print(f"\nReport saved to {report_path}")

    if prompt_yes_no("\nUpdate local git origins now?", default=False):
        state = load_state()
        root = os.getcwd()
        updates = build_updates(list_git_repos(root), state, default_owner)
        if not updates:
            print("No Bitbucket origins found to update.")
        else:
            recap_updates(updates)
            if prompt_yes_no("Apply these origin updates?", default=False):
                conflicts = apply_updates(updates)
                if conflicts:
                    print("\nPushurl conflicts (left unchanged):")
                    for item in conflicts:
                        print(f"- {item.path}: {item.current_pushurl}")
            else:
                print("Skipped local origin updates.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
    except Exception as exc:
        eprint(f"Error: {exc}")
        sys.exit(1)
