#!/usr/bin/env python3
"""Fetch GitHub activity stats for singlesp and write github-stats.json.

Runs in GitHub Actions on a weekly schedule, or locally with:
    GITHUB_TOKEN=ghp_... python scripts/update_github_stats.py

Works without a token (public data only, 60 req/hr).
Set a classic PAT with scopes  public_repo + read:org  for full
coverage of org repos (5 000 req/hr).
"""

import json
import os
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# macOS Python often ships without system CA certs; fall back gracefully.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()
    try:
        urlopen(Request("https://api.github.com"), context=_SSL_CTX)
    except (ssl.SSLCertVerificationError, Exception):
        # Last resort: unverified context (still encrypted, just no cert check).
        _SSL_CTX = ssl.create_default_context()
        _SSL_CTX.check_hostname = False
        _SSL_CTX.verify_mode = ssl.CERT_NONE

# ── Config ─────────────────────────────────────────────────────────
USERNAME = "singlesp"
OUTPUT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "github-stats.json")
LOOKBACK_DAYS = 30
MAX_PROJECTS = 8               # total projects shown (personal + org)
MAX_COMMITS_DETAIL = 10        # commits to inspect per repo for line stats
API_PAUSE = 0.05               # seconds between detail requests

# Orgs to explicitly check for contributions.  The script also auto-
# discovers orgs via the API when a token with read:org is provided,
# but listing them here guarantees they're included even if membership
# is private.
EXTRA_ORGS = [
    "PennLINC",
    # Add more orgs here as needed, e.g. "my-other-org",
]


# ── Helpers ────────────────────────────────────────────────────────
def api_get(url, token=None):
    """GET from GitHub REST API. Returns parsed JSON or None on error."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    try:
        with urlopen(req, context=_SSL_CTX) as r:
            return json.loads(r.read())
    except HTTPError as e:
        print(f"  ⚠  {e.code} {url}", file=sys.stderr)
        return None


def paginated(path, token, pages=3, per_page=100):
    """Fetch multiple pages from the REST API."""
    out = []
    for page in range(1, pages + 1):
        sep = "&" if "?" in path else "?"
        data = api_get(
            f"https://api.github.com{path}{sep}per_page={per_page}&page={page}",
            token,
        )
        if not data:
            break
        out.extend(data)
        if len(data) < per_page:
            break
    return out


def fetch_repo_details(repos, token, since_iso, seen_fullnames):
    """For a list of repo dicts, fetch per-user commit stats.

    Returns (projects_list, total_added, total_removed).
    Skips repos whose full_name is already in *seen_fullnames* (set).
    """
    projects = []
    total_added = 0
    total_removed = 0

    for repo in repos:
        full = repo["full_name"]
        if full in seen_fullnames:
            continue

        commits = api_get(
            f"https://api.github.com/repos/{full}/commits"
            f"?author={USERNAME}&since={since_iso}&per_page=30",
            token,
        ) or []

        if not commits:
            continue

        seen_fullnames.add(full)

        added = 0
        removed = 0
        messages = []

        for c in commits[:MAX_COMMITS_DETAIL]:
            detail = api_get(
                f"https://api.github.com/repos/{full}/commits/{c['sha']}", token
            )
            if detail and "stats" in detail:
                added += detail["stats"].get("additions", 0)
                removed += detail["stats"].get("deletions", 0)
            msg = c.get("commit", {}).get("message", "").split("\n")[0]
            if msg and not msg.lower().startswith("merge"):
                messages.append(msg)
            time.sleep(API_PAUSE)

        total_added += added
        total_removed += removed

        # Brief summary from recent commit messages (skip very short/junk msgs)
        clean = [m for m in messages if len(m) > 3 and m != "."]
        summary = "; ".join(clean[:5])
        if len(summary) > 220:
            summary = summary[:217] + "…"

        owner = repo.get("owner", {}).get("login", "")
        is_org = owner.lower() != USERNAME.lower()

        projects.append({
            "name": repo["name"],
            "full_name": full,
            "url": repo.get("html_url", ""),
            "description": repo.get("description") or "",
            "language": repo.get("language") or "",
            "stars": repo.get("stargazers_count", 0),
            "forks": repo.get("forks_count", 0),
            "recent_commits": len(commits),
            "lines_added": added,
            "lines_removed": removed,
            "summary": summary,
            "org": owner if is_org else "",
        })

    return projects, total_added, total_removed


# ── Main ───────────────────────────────────────────────────────────
def main():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("⚠  No GITHUB_TOKEN – using unauthenticated requests (60 req/hr)")
        print("   Org repos may be missing. Set a PAT with public_repo + read:org.")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    since_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── 1. Recent public events (already includes org activity) ────
    print("Fetching events…")
    events = paginated(f"/users/{USERNAME}/events", token, pages=3)

    commit_count = 0
    pr_count = 0
    active_repos = set()

    for ev in events:
        if ev.get("created_at", "") < since_iso:
            continue
        etype = ev["type"]
        repo = ev.get("repo", {}).get("name", "")
        payload = ev.get("payload", {})

        if etype == "PushEvent":
            commit_count += len(payload.get("commits", []))
            active_repos.add(repo)
        elif etype == "PullRequestEvent":
            if payload.get("action") in ("opened", "closed", "reopened"):
                pr_count += 1
            active_repos.add(repo)
        elif etype in (
            "CreateEvent", "DeleteEvent", "ForkEvent",
            "IssuesEvent", "IssueCommentEvent",
            "PullRequestReviewEvent",
        ):
            active_repos.add(repo)

    # ── 2. Personal repos (sorted by most-recently pushed) ─────────
    print("Fetching personal repos…")
    personal_repos = api_get(
        f"https://api.github.com/users/{USERNAME}/repos?sort=pushed&per_page=20",
        token,
    ) or []

    # ── 3. Org repos ───────────────────────────────────────────────
    # Auto-discover orgs (requires read:org for private memberships)
    print("Discovering orgs…")
    discovered_orgs = set()
    orgs_data = api_get(
        f"https://api.github.com/users/{USERNAME}/orgs", token
    ) or []
    for o in orgs_data:
        discovered_orgs.add(o["login"])

    # Merge with explicitly listed orgs
    all_orgs = discovered_orgs | set(EXTRA_ORGS)
    print(f"  Orgs to check: {', '.join(sorted(all_orgs)) or '(none)'}")

    org_repos = []
    for org in sorted(all_orgs):
        print(f"  Fetching repos for {org}…")
        repos = api_get(
            f"https://api.github.com/orgs/{org}/repos?sort=pushed&per_page=20",
            token,
        ) or []
        org_repos.extend(repos)

    # Also pull in any org repos we saw in events but didn't fetch yet
    event_org_repos_names = {
        r for r in active_repos
        if "/" in r and r.split("/")[0].lower() != USERNAME.lower()
    }
    fetched_fullnames = {r["full_name"] for r in org_repos}
    for full in event_org_repos_names:
        if full not in fetched_fullnames:
            repo_data = api_get(
                f"https://api.github.com/repos/{full}", token
            )
            if repo_data:
                org_repos.append(repo_data)

    # ── 4. Per-repo commit details ─────────────────────────────────
    print("Fetching per-repo commit details (personal)…")
    seen = set()
    projects_personal, added_p, removed_p = fetch_repo_details(
        personal_repos, token, since_iso, seen
    )

    print("Fetching per-repo commit details (org)…")
    projects_org, added_o, removed_o = fetch_repo_details(
        org_repos, token, since_iso, seen
    )

    total_added = added_p + added_o
    total_removed = removed_p + removed_o

    # Combine & sort by recent commits
    all_projects = projects_personal + projects_org
    all_projects.sort(key=lambda p: p["recent_commits"], reverse=True)
    all_projects = all_projects[:MAX_PROJECTS]

    # ── 5. Reconcile stats ─────────────────────────────────────────
    repo_commit_total = sum(p["recent_commits"] for p in all_projects)
    if commit_count < repo_commit_total:
        commit_count = repo_commit_total

    if not active_repos:
        active_repos = {p["full_name"] for p in all_projects}

    # ── 6. Write JSON ──────────────────────────────────────────────
    # Strip internal field before writing
    for p in all_projects:
        p.pop("full_name", None)

    result = {
        "last_updated": now.strftime("%Y-%m-%d"),
        "period": f"Last {LOOKBACK_DAYS} days",
        "stats": {
            "commits": commit_count,
            "pull_requests": pr_count,
            "lines_added": total_added,
            "lines_removed": total_removed,
            "repos_active": len(active_repos),
        },
        "projects": all_projects,
    }

    with open(OUTPUT, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\n✓  Wrote {OUTPUT}")
    for k, v in result["stats"].items():
        print(f"   {k}: {v}")
    print(f"   projects: {len(all_projects)}")
    for p in all_projects:
        tag = f" ({p['org']})" if p.get("org") else ""
        print(f"     · {p['name']}{tag}: {p['recent_commits']} commits")


if __name__ == "__main__":
    main()
