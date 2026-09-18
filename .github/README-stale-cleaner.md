# PR Cleaner

This repository includes a GitHub Actions workflow that manages stale pull requests and stale branches.

## Workflow

- File: `.github/workflows/stale-cleaner.yml`
- Schedule: weekdays at 08:20 UTC
- Manual runs default to dry-run mode

## Runtime configuration

Environment variables used by `.github/scripts/stale_cleaner.py`:

- `GITHUB_TOKEN` — required
- `GITHUB_REPOSITORY` — required
- `CONFIG_PATH` — optional, defaults to `.github/stale-cleaner.json`
- `DRY_RUN` — optional, defaults to `true`
- `GITHUB_API_URL` — optional, defaults to `https://api.github.com`
- `GITHUB_STEP_SUMMARY` — optional

## Behavior summary

The cleaner:

1. Ensures the managed stale labels exist.
2. Reviews open pull requests and applies stale labels when inactivity crosses configured thresholds.
3. Posts reminder comments with mentions for the PR author and reviewers.
4. Removes stale labels when activity resumes.
5. Scans branches for stale and delete-candidate ages.
6. Skips protected, exempt, or PR-associated branches.
7. Deletes eligible branches when `DRY_RUN=false`.

## Pull request inactivity thresholds

- Warning: 2 days inactive
- Escalated: 3 days inactive
- Final notice: 4 days inactive

## Local validation

Run the included tests with:

```bash
python3 .github/scripts/test_stale_cleaner.py
```
