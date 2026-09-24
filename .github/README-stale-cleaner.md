{
  "enabled": true,
  "allowed_suppression_categories": ["active-review", "waiting-for-author", "blocked-by-dependency", "work-in-progress"],
  "confidence_threshold": 0.8,
  "max_comments_to_inspect": 10,
  "author_only_comments_count": false,
  "log_decisions": true
}››# PR Cleaner

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

1. Ensures the managed stale labels exist.
2. Reviews open pull requests and applies stale labels when inactivity crosses configured thresholds.
3. Posts reminder comments with mentions for the PR author and reviewers.
4. Removes stale labels when activity resumes.
5. Scans branches for stale and delete-candidate ages.
6. Skips protected, exempt, or PR-associated branches.
7. Deletes eligible branches when `DRY_RUN=false`.

## Pull request inactivity thresholds

- Warning (yellow): 2-3 days inactive
- Escalated (orange): 4-6 days inactive
- Final notice (red): more than 6 days inactive

## AI Agent Capabilities (Phase 1)

### Overview

Phase 1 adds **observable and safe** AI capabilities to the PR Cleaner. The AI assists with PR stale-management decisions while maintaining full transparency and requiring explicit configuration.

### What the AI does

**In Phase 1, the AI:**
- Reviews stale PRs for activity indicators in titles and descriptions
- Detects patterns like WIP (work-in-progress), draft, waiting, or blocked markers
- Flags PRs that appear to still be under active review despite age
- Logs all decisions for transparency and auditing
- Does **not** automatically suppress stale labels (only logs recommendations)
- Falls back gracefully to default behavior on low confidence

**In future phases, the AI will:**
- Analyze PR comments and review threads
- Build more sophisticated activity models
- Optionally suppress stale labels for high-confidence cases
- Learn from human overrides

### Configuration

AI settings live in `.github/stale-cleaner.json`:

```json
{
  "ai_config": {
    "enabled": true,
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

**Configuration options:**

- `enabled` — Set to `false` to disable AI evaluation entirely
- `allowed_suppression_categories` — Categories the AI can recommend suppressing (Phase 1: logged only)
- `confidence_threshold` — Minimum confidence (0.0–1.0) required for AI to recommend action
- `max_comments_to_inspect` — Limit API calls by inspecting only recent comments
- `author_only_comments_count` — Whether to count only author comments as activity
- `log_decisions` — Set to `false` to suppress AI decision logging

### AI Summary Section

The workflow summary now includes an AI Agent Metrics section:

```
## AI Agent Metrics
- PRs reviewed by AI: 5
- PRs flagged for suppression: 2
- AI fallback/errors: 0

## AI Decision Details

### PRs AI Flagged for Suppression
- PR #42: work-in-progress (confidence: 70%) - Detected WIP/draft/waiting indicators in PR title or description
- PR #43: active-review (confidence: 60%) - Detected recent activity indicators in PR metadata

### PRs AI Reviewed But Left Stale
- PR #44: Insufficient evidence (confidence: 50%) - Phase 1: Conservative mode
```

### Logging and Observability

When `log_decisions` is enabled, each AI decision generates a log line:

```
[AI] PR #42 | Stage: warning | Decision: suppress | Category: work-in-progress | Confidence: 70% | Action: suppress | Reason: Detected WIP/draft/waiting indicators... (DRY_RUN)
```

This makes it easy to:
- Audit AI behavior
- Debug why a PR was or wasn't marked stale
- Adjust thresholds based on real-world results

### Safety by Design

Phase 1 prioritizes **safety and transparency** over automation:

1. **No mutations in dry-run** — AI decisions are logged but never applied during dry runs
2. **Conservative defaults** — Confidence thresholds are high (0.8) so only clear cases trigger recommendations
3. **Explicit allow-lists** — Only configured suppression categories can trigger recommendations
4. **Fallback on error** — Any AI failure gracefully reverts to default behavior
5. **Human-in-the-loop** — Even when confident, Phase 1 AI only logs, doesn't suppress

### Testing AI Decisions

Run the test suite to validate AI behavior:

```bash
python3 .github/scripts/test_stale_cleaner.py
```

Tests include:
- AI disabling with `enabled: false`
- WIP/draft/waiting detection
- Active-review indicator detection
- Confidence threshold enforcement
- RunSummary AI field initialization

### Tuning AI for Your Repo

1. **Enable logging** and run a few cycles to see what decisions the AI makes
2. **Review the summary** to understand which PRs get flagged and why
3. **Adjust `confidence_threshold`** if too many false positives or false negatives
4. **Add/remove categories** from `allowed_suppression_categories` based on your workflow
5. **Gradually migrate to suppression** (Phase 2+) once you trust the AI

## Local validation

Run the included tests with:

```bash
python3 .github/scripts/test_stale_cleaner.py
```
