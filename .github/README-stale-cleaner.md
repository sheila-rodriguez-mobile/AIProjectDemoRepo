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
- `STALE_CLEANER_REPORT_PATH` — optional, defaults to `.github/stale-cleaner-report.json`
- `STALE_CLEANER_HISTORY_PATH` — optional, defaults to `.github/stale-cleaner-history.jsonl`

## Behavior summary

The cleaner:

1. Ensures the managed stale labels exist in apply mode.
2. Reviews open pull requests and applies stale labels when inactivity crosses configured thresholds.
3. Posts reminder comments with mentions for the PR author and reviewers.
4. Removes stale labels when activity resumes.
5. Scans branches for stale and delete-candidate ages.
6. Skips protected, exempt, or PR-associated branches.
7. Deletes eligible branches when `DRY_RUN=false`.
8. Writes a structured JSON report for the local dashboard and other tooling.
9. Appends each run to a local history file so the dashboard can show trends.

## Pull request inactivity thresholds

- Warning (yellow): 2-3 days inactive
- Escalated (orange): 4-6 days inactive
- Final notice (red): more than 6 days inactive

## Branch staleness thresholds

- Stale: 8 days without activity
- Delete candidate: 10 days without activity

## AI Agent Capabilities (Phase 1)

Phase 1 now supports a **real local AI model** using the `gemini` CLI available on your machine, with heuristic fallback when the local model is unavailable.

### Supported providers

`ai_config.ai_provider` supports:

- `"gemini_cli"` — real local Gemini model via the installed `gemini` command
- `"heuristic"` — no external/local model, rules-only fallback

## Important note about Copilot

From what is currently available on this machine:

- `gemini` is installed and scriptable from the terminal
- `gh copilot` is **not** currently available as a scriptable CLI command here

That means the cleaner can use **Gemini right now** as the real model.
If your employer later provides a scriptable Copilot CLI command, the cleaner can be extended to call it too.

## AI configuration

Configured in `.github/stale-cleaner.json`:

```json
{
  "ai_config": {
    "enabled": true,
    "ai_provider": "gemini_cli",
    "gemini_model": null,
    "allowed_suppression_categories": [
      "active-review",
      "waiting-for-author",
      "blocked-by-dependency",
      "work-in-progress"
    ],
    "confidence_threshold": 0.8,
    "max_comments_to_inspect": 10,
    "author_only_comments_count": false,
    "log_decisions": true
  }
}
```

### Configuration knobs

- `enabled` — turns AI review on or off
- `ai_provider` — `gemini_cli` or `heuristic`
- `gemini_model` — optional Gemini model name to pass to `gemini --model`
- `allowed_suppression_categories` — categories counted as AI suppression recommendations
- `confidence_threshold` — minimum confidence required before AI recommends suppression
- `max_comments_to_inspect` — reserved for future comment-analysis support
- `author_only_comments_count` — reserved for future activity-analysis support
- `log_decisions` — enables per-PR AI decision logs

## How Gemini is invoked safely

The cleaner runs Gemini in a **temporary directory** instead of your repository so the CLI does not act directly on repo files.
It also asks Gemini to:

- output **JSON only**
- avoid tool use
- make a **conservative** stale-suppression recommendation

If Gemini is unavailable or fails, the cleaner falls back to the heuristic evaluator and increments the fallback metric.

## Summary metrics

The workflow summary includes:

- PRs reviewed by AI
- PRs flagged for suppression
- AI fallback/errors
- PRs AI flagged for suppression, with provider and rationale
- PRs AI reviewed but left stale, with provider and rationale

## Dry-run logging

Example Gemini-backed log output:

```text
[Gemini CLI-AI] PR #42 | Stage: warning | Decision: keep_stale | Category: needs-attention | Confidence: 86.0% | Action: keep_stale | Reason: No strong signal of active review in the PR title or description (DRY_RUN)
```

Example fallback log output:

```text
[Heuristic-AI] PR #42 | Stage: warning | Decision: suppress | Category: work-in-progress | Confidence: 70.0% | Action: defer | Reason: Detected WIP/draft/waiting indicators in PR title or description; fallback from gemini_cli (DRY_RUN) | Fallback: gemini CLI exited with status 1
```

## Local run examples

### Real AI with local Gemini

```bash
export GITHUB_TOKEN="your-token"
export GITHUB_REPOSITORY="sheila-rodriguez-mobile/AIProjectDemoRepo"
export DRY_RUN="true"
python3 .github/scripts/stale_cleaner.py
```

### Force heuristic mode

```bash
python3 - <<'PY'
import json
from pathlib import Path
path = Path('.github/stale-cleaner.json')
config = json.loads(path.read_text())
config['ai_config']['ai_provider'] = 'heuristic'
path.write_text(json.dumps(config, indent=2) + '\n')
PY
python3 .github/scripts/stale_cleaner.py
```

## Important runtime note

A GitHub-hosted Actions runner typically will **not** have your local `gemini` CLI installed or authenticated.
So:

- **local runs on your machine** can use real Gemini AI
- **GitHub workflow runs** will likely fall back to heuristics unless you use a self-hosted runner with Gemini installed and authenticated

## Local validation

Run the included tests with:

```bash
python3 .github/scripts/test_stale_cleaner.py
```

## Local dashboard

The cleaner now writes a structured report to:

```text
.github/stale-cleaner-report.json
```

That JSON includes:

- overall PR/branch metrics
- stale counts
- AI metrics
- AI decision details
- branch lists

The cleaner also appends run snapshots to:

```text
.github/stale-cleaner-history.jsonl
```

That history enables trend charts and recent-run comparisons.

### 1. Run the cleaner

```bash
python3 .github/scripts/stale_cleaner.py
```

### 2. Start the dashboard

```bash
python3 .github/scripts/stale_cleaner_dashboard.py
```

Then open:

```text
http://127.0.0.1:8765
```

### Dashboard features

- summary cards for PR, branch, and AI counts
- executive-summary bullets for regular employees
- PR stale-count breakdown
- visual bar charts for stage distribution, operational pressure, and branch workload
- visual chart for AI decision categories
- recent-run trend chart for stale PRs, delete candidates, and AI fallbacks
- AI decision list with provider, confidence, and rationale
- recent-runs table for lightweight analysis
- stale/protected/delete-candidate branch lists
- raw JSON payload for debugging

### Optional custom report path

```bash
export STALE_CLEANER_REPORT_PATH="/tmp/stale-cleaner-report.json"
export STALE_CLEANER_HISTORY_PATH="/tmp/stale-cleaner-history.jsonl"
python3 .github/scripts/stale_cleaner.py
python3 .github/scripts/stale_cleaner_dashboard.py --report /tmp/stale-cleaner-report.json --history /tmp/stale-cleaner-history.jsonl
```

## Important runtime note for the dashboard

The dashboard only shows what exists in the latest report file.
If the cleaner has not run yet, or if the report path is changed, the dashboard will show that the report is missing until a new run writes it.

Trend charts also depend on the history file.
If you only have one run, the dashboard will still work, but history-based charts will become more useful after multiple runs.
