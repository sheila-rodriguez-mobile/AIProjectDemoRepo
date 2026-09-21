# PR Cleaner

This repository includes a GitHub Actions workflow that manages stale pull requests and stale branches.

## Workflow

- File: `.github/workflows/stale-cleaner.yml`
- Schedule: weekdays at 08:20 UTC
- Manual runs default to real mode

## Runtime configuration

Environment variables used by `.github/scripts/stale_cleaner.py`:

- `GITHUB_TOKEN` — required
- `GITHUB_REPOSITORY` — required
- `CONFIG_PATH` — optional, defaults to `.github/stale-cleaner.json`
- `DRY_RUN` — optional, defaults to `false` for scheduled runs and for manual runs unless `dry_run` is explicitly enabled
- `GITHUB_API_URL` — optional, defaults to `https://api.github.com`
- `GITHUB_STEP_SUMMARY` — optional

## Behavior summary

The cleaner:

1. Creates any missing managed stale labels using severity-specific colors.
2. Reviews open pull requests and applies stale labels when inactivity crosses configured thresholds.
3. Posts reminder comments with mentions for the PR author and reviewers.
4. Removes stale labels when activity resumes.
5. Scans branches for stale and delete-candidate ages.
6. Skips protected, exempt, or PR-associated branches.
7. Deletes eligible branches when `DRY_RUN=false`.

Managed label colors:

- `stale:warning` — yellow (`#ffd33d`)
- `stale:escalated` — orange (`#fb8c00`)
- `stale:final-notice` — red (`#d73a49`)

## Pull request inactivity thresholds

- Warning (yellow): 2 days inactive
- Escalated (orange): 3 days inactive
- Final notice (red): 4 days inactive

## Local validation

Run the included tests with:

```bash
python3 .github/scripts/test_stale_cleaner.py
```
