# PR Cleaner

This repository includes a GitHub Actions workflow that manages stale pull requests and stale branches.

Phase 2 upgrades the cleaner from a binary stale/not-stale check into a **context-aware decision engine**.

## Workflow

- File: `.github/workflows/stale-cleaner.yml`
- Schedule: weekdays at 08:20 UTC
- Manual runs default to real mode

## Runtime configuration

Environment variables used by `.github/scripts/stale_cleaner.py`:

- `GITHUB_TOKEN` — required
- `GITHUB_REPOSITORY` — required
- `CONFIG_PATH` — optional, defaults to `.github/stale-cleaner.json`
- `DRY_RUN` — optional, defaults to `true` when unset
- `GITHUB_API_URL` — optional, defaults to `https://api.github.com`
- `GITHUB_STEP_SUMMARY` — optional
- `STALE_CLEANER_REPORT_PATH` — optional, defaults to `.github/stale-cleaner-report.json`
- `STALE_CLEANER_HISTORY_PATH` — optional, defaults to `.github/stale-cleaner-history.jsonl`

## Behavior summary

The cleaner:

1. Ensures the managed stale labels exist.
2. Gathers rich context for every open pull request.
3. Classifies each pull request into one of six context states.
4. Chooses a context-appropriate action plan instead of always labelling.
5. Posts a tailored comment scoped to the right people.
6. Removes stale labels when activity resumes or when context justifies suppression.
7. Scans branches for stale and delete-candidate ages.
8. Scores every branch with a deletion risk rating.
9. Deletes eligible branches when `DRY_RUN=false`.

## Phase 2: context-aware PR states

Instead of only `stale` / `not stale`, each PR is classified as one of:

| State | Meaning |
| --- | --- |
| `stale` | No review, dependency, or blocker signals found |
| `active_discussion` | Unresolved review threads or an ongoing conversation |
| `awaiting_external` | Failing/pending checks, linked issues, QA, or dependencies |
| `awaiting_reviewer` | An open review request is the missing signal |
| `blocked` | Merge conflict or an explicit blocker |
| `candidate_for_closure` | Long inactivity for both PR and branch, no active signals |

## Phase 2: context-aware actions

Each state maps to an action plan drawn from:

- `add_stale_label`
- `suppress_stale_label`
- `post_comment`
- `ping_author`
- `ping_reviewers`
- `ping_owning_team`
- `defer` (take no action until the next run)

Suppression is deliberately conservative: it requires both a high-confidence
classification and an allow-listed state, otherwise the engine defers.

## Phase 2: richer signal gathering

For each pull request the engine inspects:

- unresolved review threads (GraphQL)
- CI/check status and check-run conclusions
- merge conflict / mergeable state
- linked issues
- recent review-request changes from the issue timeline
- comment patterns (review, waiting, blocked, external, conflict)
- branch age and PR age together

## Phase 2: tailored comments

Comments are written per state, for example:

- "waiting on reviewer response"
- "waiting on external QA"
- "inactive with no reviewer engagement"

Mentions are also scoped by state: reviewers for review-driven states, the owning
team for external dependencies, and the author for blocked or closure cases.

## Phase 2: branch risk scoring

Branch deletion stays rule-based, but every branch also receives an AI-style risk rating:

- `likely_safe_to_delete`
- `maybe_preserve`
- `requires_review`

These ratings appear in the workflow summary and the JSON report so humans can
review cleanup decisions without changing the deletion rules.

## Pull request inactivity thresholds

- Warning: 2 days inactive
- Escalated: 3 days inactive
- Final notice: 4 days inactive

## Branch thresholds

- Stale: 8 days without activity
- Delete candidate: 10 days without activity

## Reports

Each run writes:

- `.github/stale-cleaner-report.json` — latest structured run report
- `.github/stale-cleaner-history.jsonl` — one JSON line appended per run

Both include AI state counts, per-PR decisions, and branch risk assessments.

## Safety and reliability behaviour

- **No repeated comments.** Every cleaner comment carries a hidden
  `<!-- pr-cleaner:key=... -->` marker. The same message is not posted again
  unless the PR's state or stage changes, or the PR had new activity since.
- **Ignores its own comments.** Cleaner and bot comments are excluded from
  comment-pattern analysis, so the cleaner never reacts to its own wording.
- **Bounded deferral.** Weak suppression evidence can defer stale handling, but
  once a PR reaches `final-notice` it is labelled anyway.
- **Accurate GitHub signals.** Only `mergeable=false` / `mergeable_state=dirty`
  count as merge conflicts (`blocked` just means branch protection is unmet).
  Commits with no status checks are not treated as "pending", and a failing
  status is never hidden by passing check runs.
- **Fail-safe branch deletion.** A branch is never deleted when its PRs or age
  cannot be verified. A failed delete is recorded and the run continues.
- **Per-PR isolation.** One failing PR is logged under *Errors* and counted in
  `prs_failed`; the rest of the run continues.
- **HTTP resilience.** 30s timeouts, bounded retries with backoff for
  reads/deletes on 429/5xx. Comment posts are only retried on 429 to avoid
  duplicates.
- **Config validation.** Invalid thresholds, label lists, or AI settings stop
  the run with exit code 2 before anything touches GitHub.
- **Custom labels.** `managed_labels` (warning, escalated, final-notice order)
  are now honoured when applying stage labels.
- **AI timeout.** `ai_config.ai_timeout_seconds` (default 120) bounds the
  Copilot CLI call; on timeout the heuristic classifier is used.

## Local validation

Run the included tests with:

```bash
python3 -m unittest discover -s .github/scripts -p "test_*.py"
```

Run a safe local dry run (no writes to GitHub):

```bash
export GITHUB_TOKEN="<your token>"
export GITHUB_REPOSITORY="<owner>/<repo>"
export DRY_RUN=true
python3 .github/scripts/stale_cleaner.py
```
