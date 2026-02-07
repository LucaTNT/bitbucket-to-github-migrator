# Bitbucket to GitHub Migrator

Console-based Python app to migrate repositories from Bitbucket Cloud to GitHub. It lists all Bitbucket repos across your workspaces, lets you select which to migrate, provides a bulk edit step for GitHub repo names/owners, then mirrors all branches and tags into GitHub. It also creates a migration report with links to both the original and destination repositories.

## Project Goals
- Enumerate Bitbucket workspaces and repositories, including organizations.
- Allow selective migration with a single recap/confirmation step.
- Mirror **all** Git history (all branches and tags) to GitHub.
- Produce a markdown report mapping old -> new repositories.

## Requirements
- Python 3.8+
- `git` installed and available on PATH
- `git-lfs` installed if you enable LFS migration
- Bitbucket Cloud account with an **API token (with scopes)** created via Atlassian Account settings
- GitHub personal access token (classic or fine-grained with repo create/push permissions)

## Token Setup
### Bitbucket API Token (with scopes)
Create a scoped Bitbucket API token from your Atlassian account:
- Follow: [Create an API token (Bitbucket Cloud)](https://support.atlassian.com/bitbucket-cloud/docs/create-an-api-token/)
- Select **Bitbucket** as the app and choose scopes appropriate for:
  - Workspaces (Read)
  - User data (Read)
  - Repositories (Read)
- See the scope list here: [API token permissions](https://support.atlassian.com/bitbucket-cloud/docs/api-token-permissions/)

### GitHub Personal Access Token
Create a personal access token from your GitHub account:
- Follow: [Creating a personal access token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/creating-a-personal-access-token)
- Ensure it can create repos (if needed) and push to them.

## Usage
```bash
python3 bitbucket-to-github-migrator.py
```

If a `.env` file exists in the project root, it will be loaded automatically.

## Update Existing Repos
To update local git origins from Bitbucket to GitHub based on the migration state:
```bash
python3 update-git-origins.py
```

This script:
- Recursively scans for git repos under a path you provide.
- Uses `migration_state.json` to map Bitbucket repos to GitHub.
- Defaults to the same repo name on GitHub if not found in state (asks for a GitHub owner).
- Updates `origin` to the new GitHub SSH URL.
- If a `pushurl` exists and matches `origin`, it updates it too.
- If a `pushurl` exists and differs, it skips updating it and reports conflicts at the end.

You will be prompted for:
- Atlassian account email (for Bitbucket REST API auth)
- Bitbucket username (for Git auth)
- Bitbucket API token (with scopes)
- GitHub username (for auth)
- GitHub personal access token
- Default GitHub owner (user or org)

### Environment Variables
You can provide credentials and defaults via environment variables to skip prompts:
- `BITBUCKET_EMAIL`
- `BITBUCKET_USERNAME`
- `BITBUCKET_TOKEN`
- `GITHUB_USERNAME`
- `GITHUB_TOKEN`
- `GITHUB_OWNER`
- `LFS_MIGRATE` (set to `1`/`true` to enable LFS migration)
- `LFS_THRESHOLD` (default `5MB`)
- `DRY_RUN` (set to `1`/`true` to only print the plan and exit)

Example:
```bash
export BITBUCKET_EMAIL="you@example.com"
export BITBUCKET_USERNAME="your_bitbucket_username"
export BITBUCKET_TOKEN="bitbucket_api_token"
export GITHUB_USERNAME="your_github_username"
export GITHUB_TOKEN="github_pat"
export GITHUB_OWNER="your_github_org_or_username"
export LFS_MIGRATE="1"
export LFS_THRESHOLD="5MB"
export DRY_RUN="1"
```

Then:
1. Select which Bitbucket repos to migrate.
2. Optionally edit target GitHub repo names/owners using the edit prompt.
3. Review the recap and confirm once.
4. The app will create missing GitHub repos, reuse existing empty ones, and mirror all branches/tags.

## Output
- Terminal summary of migrations
- `migration_report.md` with clickable Bitbucket/GitHub links

## Notes
- Existing non-empty GitHub repos require confirmation before mirroring.
- Repos are created as private by default (adjustable in `app.py`).
- LFS migration rewrites history in the destination only; source repos are not modified.
- The app writes `migration_state.json` to allow resuming interrupted runs.

## Troubleshooting
- **401/invalid token**: Ensure you created a **Bitbucket API token with scopes** and selected the Bitbucket app. Use your Atlassian account email for REST API auth and Bitbucket username for Git auth.
- **Large repo clone fails**: Enable LFS migration and/or increase retries. Large binary history can fail over HTTPS without LFS.
- **GitHub repo exists**: Empty repos are reused automatically; non-empty repos require confirmation before mirroring.

## Safety
- Mirroring with LFS enabled rewrites the destination history. Only use it if you are okay with rewriting the GitHub repo history.

## License
MIT. See `LICENSE`.

## Disclaimer
This project was created with Codex and may contain AI-generated "slop." Review before using in production.
For what is worth, I used it successfully.
