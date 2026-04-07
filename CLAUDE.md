# Project notes for Claude

## Refreshing GitHub activity stats

When the user asks to "refresh github activity" / "update my github stats" / similar, run from the repo root:

```
GITHUB_TOKEN=$(/opt/homebrew/bin/gh auth token) python scripts/update_github_stats.py
```

Notes:
- The script reads `GITHUB_TOKEN` from env and uses GitHub's GraphQL `contributionsCollection` API.
- We pipe in the `gh` CLI's OAuth token (not a classic PAT) because PennLINC enforces SAML SSO and `gh`'s OAuth flow handles SSO authorization automatically. Classic PATs can't be SSO-authorized on this org from the user's account.
- After running, the console output's `projects:` section should list at least one `PennLINC/...` entry. If it doesn't, the user's `gh` token has likely lost SSO authorization — tell them to run `gh auth login` again and re-approve PennLINC in the browser.
- Show the user the diff of `github-stats.json`, then on approval commit with message `chore: refresh github activity stats` and push.

## No cron workflow

There is intentionally no GitHub Actions workflow for stats. The Actions runner can only hold a classic PAT, which can't be SSO-authorized for PennLINC, so the daily run produced empty PennLINC results and overwrote good data. The Claude-on-demand workflow above replaces it. Do not re-add `.github/workflows/update-github-stats.yml`.

## Rebuilding cv.pdf and resume.pdf

Both are built from their `.tex` sources with `pdflatex`. Activate mamba base first so we don't pollute other envs:

```
source ~/mambaforge/etc/profile.d/conda.sh 2>/dev/null || source ~/miniforge3/etc/profile.d/conda.sh && mamba activate base
pdflatex -interaction=nonstopmode cv.tex && pdflatex -interaction=nonstopmode cv.tex
pdflatex -interaction=nonstopmode resume.tex && pdflatex -interaction=nonstopmode resume.tex
```

(Two passes each so the page-count macro `\pageref{LastPage}` resolves.) Clean up `*.aux *.log *.out` afterward.
