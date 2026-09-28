# PR Cleaner

This repository includes a GitHub Actions workflow that manages stale pull requests and stale branches.

Phase 2 upgrades the cleaner from a binary stale/not-stale check into a **context-aware decision engine**.

## Workflow

- File: `.github/workflows/stale-cleaner.yml`
- Schedule: daily at 08:20 UTC (every day, including weekends, so no stale
  stage window can be skipped over a multi-day gap between scans)
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

## Phase 3: stateful repository-hygiene agent

Implemented in `.github/scripts/stale_cleaner_agent.py` and wired into
`stale_cleaner.py`. It is controlled by `agent_config` in
`.github/stale-cleaner.json`. Set `agent_config.enabled` to `false` to get
Phase 2 behaviour back unchanged.

### Decision memory

The agent keeps a JSON memory at `.github/stale-cleaner-memory.json`. You can
override the location with `STALE_CLEANER_MEMORY_PATH`. The workflow keeps it
between runs with `actions/cache` (restore the newest snapshot, then save a new
one), and it is also uploaded with the run report.

For each PR it stores:
- the previous AI classification, confidence, and provider;
- the previous final action;
- the label it applied and when;
- the escalation step, plus when it last commented;
- suppression state and reason;
- a capped decision history;
- outcome counters: human responses, reactivations, label overrides, expired
  suppressions, pings sent and answered.

For each branch it stores the advisor's recommendation, when it was deleted,
and whether it was restored.

A memory file that is missing, corrupt, or from a newer version is replaced
with a fresh memory, and the run notes this. Dry runs never save memory unless
`persist_in_dry_run` is `true`. Records that have not been seen for
`retention_days` are pruned.

### Escalation planning across runs

| Step | Name | Who is mentioned |
|------|------|------------------|
| 1 | `soft_warning` | author |
| 2 | `ping_reviewers` | requested reviewers (falls back to the author) |
| 3 | `ping_team` | author, reviewers, owning teams, and `additional_mentions` |
| 4 | `recommend_close` | author (recommends closing or archiving) |
| 5 | `branch_recommendation` | author (`preserve` or `delete_after_close` for the head branch) |

Limits on the ladder:
- It moves at most one step per run.
- It waits at least `step_spacing_days` between steps.
- Only the `stale` and `candidate_for_closure` states move up the ladder.
- The stale stage caps the step: `warning` allows up to step 2, `escalated` up
  to step 3, and `final-notice` up to step 5.

Each step posts one comment with its own hidden marker
(`escalation:step-N`). A step is never pinged twice.

### Feedback loop

On every run the agent compares what it sees now with its memory:
- **A human removes the stale label with no new activity.** This is recorded
  as a false stale label (an override). The agent will not re-label or ping
  that PR for `override_cooldown_days`, and then it starts again at step 1.
- **A human adds an exempt label to a PR the agent labelled.** This also counts
  as an override.
- **A human comments, commits, or reviews after the agent's comment.** This is
  recorded as a response, and as an answered ping if people were mentioned.
- **The PR becomes active after escalation.** This is recorded as a
  reactivation. The ladder resets and the agent removes its own label; that
  removal is not counted as an override.
- **Suppression lasts longer than `max_suppression_days`.** The suppression
  expires and normal stale handling resumes.
- **A branch the agent deleted shows up again.** This is recorded as an
  accidental deletion. That branch is never deleted automatically again.

### AI-assisted branch decisions

For stale branches the advisor recommends `keep`, `delete`, or `review`. It
looks at:
- **Naming patterns:** `keep_patterns` and `delete_patterns`.
- **Linked PR history:** merged, or closed without merging.
- **Labels on related PRs:** `keep_labels` and `delete_labels`.
- **Head commit message:** for example `WIP`, "do not delete", `experiment`.
- **References from open PRs:** the branch is the base of an open PR, or is
  mentioned in an open PR's title or body.

When `ai_provider` is `copilot_cli` and `branch_advisor.use_ai` is on, Copilot
checks the heuristic result. The more cautious answer always wins. A branch is
deleted only if every one of these is true:
- the Phase 2 risk check says it is deletable;
- the advisor says `delete`, and if AI was used it agrees with at least the
  confidence threshold;
- the branch is older than `delete_candidate_days + branch_extra_days`;
- it was never restored.

Branches held back for any of these reasons are listed under
`deletions_blocked_by_advisor`.

### Goal-based optimisation

At the end of each run, `optimize_policy` adjusts a few settings toward the
goals below. Each setting has hard limits, and each goal changes only after
`min_samples` new observations.

| Goal | Signal | Adjustment (bounded) |
|------|--------|----------------------|
| Fewer false stale labels | label overrides ÷ labels applied is above target | lower the suppression confidence bar by 0.05 (at most `max_confidence_offset`, never below 0.5) |
| Fewer noisy pings | pings answered ÷ pings sent is below target | wait one more day between steps (at most `max_step_spacing_days`) |
| Fewer accidental deletions | restored branches | add `branch_extra_days_per_incident` days before deleting (at most `max_branch_extra_days`) |
| Suppressions that don't drag on | expired ÷ total suppressions is above target | shorten the suppression window (down to `min_suppression_days`) |
| More reactivated PRs | reactivations ÷ escalations started | reported as a goal metric |

The report (`report_version: 3`) has an `agent` block containing the
escalation plans, feedback events, branch advice, blocked deletions, goal
metrics, the current policy, and any adjustments made. The step summary has a
matching **Agent Memory & Planning** section.

## Pull request inactivity thresholds

- Active: 0-2 days inactive (no label; any stale label is cleared)
- Warning: 3-5 days inactive (`stale:warning`)
- Escalated: 6-8 days inactive (`stale:escalated`)
- Final notice: 9+ days inactive (`stale:final-notice`)
- Closure candidate (heuristic): 27+ days inactive with the branch also idle 27+ days
  (`max(final_notice_days * 3, 12)`)

## Stale context labels

Every PR that reaches a stale stage gets its **stage label**
(`stale:warning`, `stale:escalated`, or `stale:final-notice`), whatever the
AI decides. When the context engine also detects another situation, a
second **context label** is added next to it:

| Detected situation | Extra label |
|--------------------|-------------|
| Merge conflict (GitHub reports the PR as conflicting) | `merge_conflicts` |
| Other explicit blocker | `blocker` |
| Waiting on a requested reviewer | `awaiting_reviewer` |
| Waiting on QA, pending checks, or an external dependency | `awaiting_external` |
| Unresolved review threads / ongoing review discussion | `active_discussion` |
| Long inactivity for both PR and branch | `closure_candidate` |
| Plain inactivity | _(stage label only)_ |

Behaviour:
- The context label is swapped when the situation changes and removed with
  the stage label as soon as the PR becomes active again.
- Suppressing states (review pending, conflict, external wait, discussion)
  are labelled but do **not** climb the escalation ladder; they get their
  tailored comment instead. Deferred PRs are labelled without a comment.
- The only time a stale PR is left unlabelled is during the human-override
  cooldown after someone removed the stale label.
- Missing labels are created in the repository automatically.
- Configure names in `context_labels.labels`, or set
  `context_labels.enabled` to `false` to use stage labels only.
- The report has `context_label_counts`, and the step summary and dashboard
  show how many PRs got each context label.

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
