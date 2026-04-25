"""Generate weekly release notes locally without posting to Mattermost.

Usage:
1. Copy .env.example -> .env and fill in values (GITHUB_TOKEN, FE_REPO, BE_REPO, optional LLM_*).
2. Install deps: `pip install -r requirements.txt`
3. Run: `python generate_local_release_notes.py`
"""
from dotenv import load_dotenv
import os
import sys
import urllib.parse
from github import GithubException

load_dotenv()

from release_note_generator import run_generator


def normalize_repo_name(repo: str) -> str:
    """Normalize input like a full GitHub URL or 'owner/repo' into 'owner/repo'."""
    if not repo:
        return repo

    repo = repo.strip()
    # If looks like a URL, parse path
    if repo.startswith("http://") or repo.startswith("https://"):
        try:
            parsed = urllib.parse.urlparse(repo)
            path = parsed.path
            # strip leading/trailing slashes and possible .git
            path = path.lstrip('/').rstrip('/')
            if path.endswith('.git'):
                path = path[:-4]
            return path
        except Exception:
            return repo

    # If it's a git@github.com:owner/repo.git form
    if repo.startswith('git@') and ':' in repo:
        parts = repo.split(':', 1)[1]
        parts = parts.rstrip('/').rstrip('.git')
        return parts

    # Already owner/repo
    return repo


def main():
    token = os.environ.get("GITHUB_TOKEN", "")
    fe_repo = os.environ.get("FE_REPO", "")
    be_repo = os.environ.get("BE_REPO", "")
    llm_key = os.environ.get("LLM_API_KEY", "")
    llm_url = os.environ.get("LLM_API_URL", "")

    if not token or not fe_repo or not be_repo:
        print("Missing required environment variables: GITHUB_TOKEN, FE_REPO, BE_REPO")
        sys.exit(1)

    try:
        message = run_generator(token, fe_repo, be_repo, llm_key, llm_url)
    except GithubException as e:
        print("Error accessing GitHub repository:")
        print(f"  FE_REPO={fe_repo!s}")
        print(f"  BE_REPO={be_repo!s}")
        print("\nPossible causes:")
        print("- Repository name is incorrect; use the 'owner/repo' format.")
        print("- The token has insufficient permissions (needs 'repo' scope for private repos).")
        print("- The token is for a different account that doesn't have access to the repo.")
        print("\nQuick checks you can run in your shell:")
        print("- Verify repo exists and is reachable:")
        print("  curl -s -o /dev/null -w \"%{http_code}\n\" -H \"Authorization: token $GITHUB_TOKEN\" https://api.github.com/repos/OWNER/REPO")
        print("- Check token scopes:")
        print("  curl -sI -H \"Authorization: token $GITHUB_TOKEN\" https://api.github.com/user | grep -i x-oauth-scopes")
        print("\nFull error from GitHub API:")
        print(str(e))
        sys.exit(1)

    print("\n--- Generated Release Notes ---\n")
    print(message)


if __name__ == "__main__":
    main()
