#!/usr/bin/env python3
"""Fetch GitHub activity stats for singlesp and write github-stats.json.

Uses the GitHub **GraphQL** API `contributionsCollection` for accurate
aggregate stats (the same data source that powers GitHub profile graphs),
plus the REST API for per-project detail cards.

Runs in GitHub Actions on a daily schedule, or locally with:
    GITHUB_TOKEN=ghp_... python scripts/update_github_stats.py

Requires a token.  Recommended: classic PAT with scopes
    public_repo + read:org + read:user
or a fine-grained PAT with repository access and read permissions for
"Contents" on the repos you want tracked.
"""

import json
import os
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()
    try:
        urlopen(Request("https://api.github.com"), context=_SSL_CTX)
    except (ssl.SSLCertVerificationError, Exception):
        _SSL_CTX = ssl.create_default_context()
        _SSL_CTX.check_hostname = False
        _SSL_CTX.verify_mode = ssl.CERT_NONE

# ── Config ─────────────────────────────────────────────────────────
USERNAME = "singlesp"
OUTPUT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "github-stats.json")
LOOKBACK_DAYS = 30
MAX_PROJECTS = 8
MAX_COMMITS_DETAIL = 10
API_PAUSE = 0.05

EXTRA_ORGS = [
    "PennLINC",
]


# ── HTTP helpers ───────────────────────────────────────────────────
def _request(url, token=None, method="GET", body=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, headers=headers, method=method,
                  data=json.dumps(body).encode() if body else None)
    try:
        with urlopen(req, context=_SSL_CTX) as r:
            return json.loads(r.read())
    except HTTPError as e:
        print(f"  ⚠  {e.code} {url}", file=sys.stderr)
        try:
            err_body = e.read().decode()[:300]
            print(f"     {err_body}", file=sys.stderr)
        except Exception:
            pass
        return None


def api_get(url, token=None):
    return _request(url, token)


def graphql(query, token, variables=None):
    body = {"query": query}
    if variables:
        body["variables"] = variables
    return _request("https://api.github.com/graphql", token, method="POST", body=body)


# ── GraphQL: aggregate contribution stats ──────────────────────────
CONTRIBUTIONS_QUERY = """
query($username: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $username) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      totalPullRequestContributions
      totalPullRequestReviewContributions
      totalIssueContributions
      totalRepositoriesWithContributedCommits
      commitContributionsByRepository(maxRepositories: 20) {
        contributions {
          totalCount
        }
        repository {
          name
          nameWithOwner
          url
          description
          primaryLanguage { name }
          stargazerCount
          forkCount
          owner { login }
        }
      }
    }
  }
}
"""


def fetch_contributions_graphql(token, since, until):
    """Use GitHub's contributionsCollection for accurate totals."""
    result = graphql(CONTRIBUTIONS_QUERY, token, variables={
        "username": USERNAME,
        "from": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

    if not result or "data" not in result:
        print("  ⚠  GraphQL query failed, falling back to REST", file=sys.stderr)
        if result and "errors" in result:
            for err in result["errors"]:
                print(f"     {err.get('message', err)}", file=sys.stderr)
        return None

    cc = result["data"]["user"]["contributionsCollection"]
    return {
        "commits": cc["totalCommitContributions"],
        "pull_requests": cc["totalPullRequestContributions"],
        "reviews": cc["totalPullRequestReviewContributions"],
        "issues": cc["totalIssueContributions"],
        "repos_active": cc["totalRepositoriesWithContributedCommits"],
        "repos_with_commits": cc["commitContributionsByRepository"],
    }


# ── REST helpers (for per-project line stats) ──────────────────────
def fetch_line_stats(repos_gql, token, since_iso):
    """Given GraphQL commitContributionsByRepository entries, fetch
    per-project line-change stats via REST and return project dicts."""
    projects = []
    total_added = 0
    total_removed = 0

    for entry in repos_gql:
        repo = entry["repository"]
        commit_count = entry["contributions"]["totalCount"]
        full = repo["nameWithOwner"]

        commits = api_get(
            f"https://api.github.com/repos/{full}/commits"
            f"?author={USERNAME}&since={since_iso}&per_page=30",
            token,
        ) or []

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

        clean = [m for m in messages if len(m) > 3 and m != "."]
        summary = "; ".join(clean[:5])
        if len(summary) > 220:
            summary = summary[:217] + "…"

        owner = repo["owner"]["login"]
        is_org = owner.lower() != USERNAME.lower()
        lang = (repo.get("primaryLanguage") or {}).get("name", "")

        projects.append({
            "name": repo["name"],
            "url": repo["url"],
            "description": repo.get("description") or "",
            "language": lang,
            "stars": repo.get("stargazerCount", 0),
            "forks": repo.get("forkCount", 0),
            "recent_commits": commit_count,
            "lines_added": added,
            "lines_removed": removed,
            "summary": summary,
            "org": owner if is_org else "",
        })

    projects.sort(key=lambda p: p["recent_commits"], reverse=True)
    return projects[:MAX_PROJECTS], total_added, total_removed


# ── REST-only fallback (original logic, for when no token) ─────────
def fetch_stats_rest_only(token, since_iso):
    """Fallback when GraphQL is unavailable (no token)."""
    print("Fetching events (REST fallback)…")
    events = []
    for page in range(1, 4):
        data = api_get(
            f"https://api.github.com/users/{USERNAME}/events?per_page=100&page={page}",
            token,
        )
        if not data:
            break
        events.extend(data)
        if len(data) < 100:
            break

    commit_count = 0
    pr_count = 0
    active_repos = set()

    for ev in events:
        if ev.get("created_at", "") < since_iso:
            continue
        etype = ev["type"]
        repo_name = ev.get("repo", {}).get("name", "")
        payload = ev.get("payload", {})

        if etype == "PushEvent":
            commit_count += len(payload.get("commits", []))
            active_repos.add(repo_name)
        elif etype == "PullRequestEvent":
            if payload.get("action") in ("opened", "closed", "reopened"):
                pr_count += 1
            active_repos.add(repo_name)
        elif etype in ("CreateEvent", "DeleteEvent", "ForkEvent",
                        "IssuesEvent", "IssueCommentEvent",
                        "PullRequestReviewEvent"):
            active_repos.add(repo_name)

    print("Fetching personal repos…")
    personal_repos = api_get(
        f"https://api.github.com/users/{USERNAME}/repos?sort=pushed&per_page=20",
        token,
    ) or []

    all_orgs = set(EXTRA_ORGS)
    org_repos = []
    for org in sorted(all_orgs):
        repos = api_get(
            f"https://api.github.com/orgs/{org}/repos?sort=pushed&per_page=20",
            token,
        ) or []
        org_repos.extend(repos)

    all_repos = personal_repos + org_repos
    seen = set()
    projects = []
    total_added = 0
    total_removed = 0

    for repo in all_repos:
        full = repo["full_name"]
        if full in seen:
            continue

        commits = api_get(
            f"https://api.github.com/repos/{full}/commits"
            f"?author={USERNAME}&since={since_iso}&per_page=30",
            token,
        ) or []
        if not commits:
            continue
        seen.add(full)

        added = removed = 0
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
        clean = [m for m in messages if len(m) > 3 and m != "."]
        summary = "; ".join(clean[:5])
        if len(summary) > 220:
            summary = summary[:217] + "…"

        owner = repo.get("owner", {}).get("login", "")
        is_org = owner.lower() != USERNAME.lower()
        projects.append({
            "name": repo["name"],
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

    projects.sort(key=lambda p: p["recent_commits"], reverse=True)
    projects = projects[:MAX_PROJECTS]

    repo_total = sum(p["recent_commits"] for p in projects)
    if commit_count < repo_total:
        commit_count = repo_total
    if not active_repos:
        active_repos = {p["name"] for p in projects}

    return {
        "commits": commit_count,
        "pull_requests": pr_count,
        "repos_active": len(active_repos),
    }, projects, total_added, total_removed


# ── Main ───────────────────────────────────────────────────────────
def main():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("⚠  No GITHUB_TOKEN – using unauthenticated REST fallback (60 req/hr)")
        print("   Org/private-repo contributions WILL be missing.")
        print("   Set a classic PAT with: public_repo + read:org + read:user")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    since_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    if token:
        print("Fetching contribution stats via GraphQL…")
        gql = fetch_contributions_graphql(token, cutoff, now)
    else:
        gql = None

    if gql:
        stats = {
            "commits": gql["commits"],
            "pull_requests": gql["pull_requests"],
            "repos_active": gql["repos_active"],
        }

        print(f"GraphQL: {stats['commits']} commits, {stats['pull_requests']} PRs, "
              f"{stats['repos_active']} active repos")

        print("Fetching per-project line stats via REST…")
        projects, total_added, total_removed = fetch_line_stats(
            gql["repos_with_commits"], token, since_iso
        )
        stats["lines_added"] = total_added
        stats["lines_removed"] = total_removed

    else:
        rest_stats, projects, total_added, total_removed = fetch_stats_rest_only(
            token, since_iso
        )
        stats = rest_stats
        stats["lines_added"] = total_added
        stats["lines_removed"] = total_removed

    result = {
        "last_updated": now.strftime("%Y-%m-%d"),
        "period": f"Last {LOOKBACK_DAYS} days",
        "stats": stats,
        "projects": projects,
    }

    with open(OUTPUT, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\n✓  Wrote {OUTPUT}")
    for k, v in result["stats"].items():
        print(f"   {k}: {v}")
    print(f"   projects: {len(projects)}")
    for p in projects:
        tag = f" ({p['org']})" if p.get("org") else ""
        print(f"     · {p['name']}{tag}: {p['recent_commits']} commits")


if __name__ == "__main__":
    main()
