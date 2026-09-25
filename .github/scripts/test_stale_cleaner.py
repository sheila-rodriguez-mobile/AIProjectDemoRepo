#!/usr/bin/env python3

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).with_name('stale_cleaner.py')
spec = importlib.util.spec_from_file_location('stale_cleaner', SCRIPT_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
assert spec.loader is not None
spec.loader.exec_module(module)

PR_THRESHOLDS = module.DEFAULT_CONFIG['pull_request_thresholds']
BRANCH_THRESHOLDS = module.DEFAULT_CONFIG['branch_thresholds']
AI_CONFIG = module.DEFAULT_CONFIG['ai_config']

NOW = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.timezone.utc)


def iso(year: int, month: int, day: int) -> str:
    stamp = dt.datetime(year, month, day, 12, 0, tzinfo=dt.timezone.utc)
    return stamp.isoformat().replace('+00:00', 'Z')


def base_signals(**overrides):
    signals = {
        'pr_number': 42,
        'baseline_stage': 'warning',
        'days_inactive': 3,
        'pr_age_days': 5,
        'branch_age_days': 5,
        'mergeable': True,
        'mergeable_state': 'clean',
        'unresolved_review_threads': 0,
        'latest_review_state': None,
        'check_state': 'success',
        'linked_issues_count': 0,
        'open_review_requests': 0,
        'comment_patterns': {},
        'signal_summary': [],
    }
    signals.update(overrides)
    return signals


# ---------------------------------------------------------------------------
# Baseline behaviour
# ---------------------------------------------------------------------------


class StaleCleanerTests(unittest.TestCase):
    def test_stage_calculation(self) -> None:
        self.assertEqual(module.stale_stage_for_days(0, PR_THRESHOLDS), 'active')
        self.assertEqual(module.stale_stage_for_days(1, PR_THRESHOLDS), 'active')
        self.assertEqual(module.stale_stage_for_days(2, PR_THRESHOLDS), 'active')
        self.assertEqual(module.stale_stage_for_days(3, PR_THRESHOLDS), 'warning')
        self.assertEqual(module.stale_stage_for_days(5, PR_THRESHOLDS), 'warning')
        self.assertEqual(module.stale_stage_for_days(6, PR_THRESHOLDS), 'escalated')
        self.assertEqual(module.stale_stage_for_days(8, PR_THRESHOLDS), 'escalated')
        self.assertEqual(module.stale_stage_for_days(9, PR_THRESHOLDS), 'final-notice')
        self.assertEqual(module.stale_stage_for_days(99, PR_THRESHOLDS), 'final-notice')

    def test_branch_exempt_patterns(self) -> None:
        patterns = module.DEFAULT_CONFIG['exempt_branch_patterns']
        self.assertTrue(module.is_exempt_branch('release/1.2.3', patterns))
        self.assertTrue(module.is_exempt_branch('hotfix/customer-fix', patterns))
        self.assertFalse(module.is_exempt_branch('feature/new-work', patterns))

    def test_days_between_uses_timezone_aware_datetimes(self) -> None:
        now = dt.datetime(2026, 9, 17, 8, 20, tzinfo=dt.timezone.utc)
        earlier = dt.datetime(2026, 9, 10, 8, 20, tzinfo=dt.timezone.utc)
        self.assertEqual(module.days_between(earlier, now), 7)

    def test_protected_label_matching_is_case_insensitive(self) -> None:
        labels = [{'name': 'Do_Not_Delete'}, {'name': 'bug'}]
        self.assertTrue(module.branch_is_protected_by_label(labels, 'do_not_delete'))
        self.assertTrue(module.branch_is_protected_by_label(labels, 'DO_NOT_DELETE'))
        self.assertFalse(module.branch_is_protected_by_label(labels, 'security'))

    def test_latest_activity_picks_most_recent_timestamp(self) -> None:
        now = dt.datetime(2026, 9, 17, 8, 20, tzinfo=dt.timezone.utc)
        commit = dt.datetime(2026, 9, 12, 9, 30, tzinfo=dt.timezone.utc)
        review = dt.datetime(2026, 9, 15, 18, 45, tzinfo=dt.timezone.utc)
        self.assertEqual(module.latest_activity([commit], [review], now), review)

    def test_config_deep_merge_preserves_nested_defaults(self) -> None:
        merged = module.deep_merge(
            module.DEFAULT_CONFIG, {'ai_config': {'confidence_threshold': 0.5}}
        )
        self.assertEqual(merged['ai_config']['confidence_threshold'], 0.5)
        self.assertEqual(merged['ai_config']['ai_provider'], 'copilot_cli')


# ---------------------------------------------------------------------------
# Phase 2: multi-state classification
# ---------------------------------------------------------------------------


class MultiStateClassificationTests(unittest.TestCase):
    def test_all_states_have_policies_and_comment_styles(self) -> None:
        for state in module.DECISION_STATES:
            policy = module.STATE_POLICIES[state]
            self.assertIn(policy['comment_style'], module.COMMENT_STYLES)
            self.assertTrue(policy['actions'])
            for action in policy['actions']:
                self.assertIn(action, module.VALID_ACTIONS)

    def test_expected_states_exist(self) -> None:
        self.assertEqual(
            module.DECISION_STATES,
            [
                'stale',
                'active_discussion',
                'awaiting_external',
                'awaiting_reviewer',
                'blocked',
                'candidate_for_closure',
            ],
        )

    def test_normalize_state_maps_aliases_and_unknowns(self) -> None:
        self.assertEqual(module.normalize_state('Awaiting Reviewer'), 'awaiting_reviewer')
        self.assertEqual(module.normalize_state('active-review'), 'active_discussion')
        self.assertEqual(
            module.normalize_state('blocked_by_dependency'), 'awaiting_external'
        )
        self.assertEqual(module.normalize_state('abandoned'), 'candidate_for_closure')
        self.assertEqual(module.normalize_state('nonsense'), 'stale')
        self.assertEqual(module.normalize_state(None), 'stale')
        self.assertEqual(module.normalize_state(''), 'stale')

    def test_normalize_actions_filters_invalid_and_falls_back(self) -> None:
        self.assertEqual(
            module.normalize_actions(['post_comment', 'nuke_repo'], 'stale'),
            ['post_comment'],
        )
        self.assertEqual(
            module.normalize_actions([], 'blocked'),
            module.STATE_POLICIES['blocked']['actions'],
        )
        self.assertEqual(
            module.normalize_actions('ping_author, defer', 'blocked'),
            ['ping_author', 'defer'],
        )
        self.assertEqual(
            module.normalize_actions(None, 'stale'),
            module.STATE_POLICIES['stale']['actions'],
        )

    def test_decision_from_ai_result_high_confidence_suppresses(self) -> None:
        decision = module.decision_from_ai_result(
            42,
            'warning',
            AI_CONFIG,
            'copilot_cli',
            {
                'state': 'awaiting_reviewer',
                'actions': ['suppress_stale_label', 'post_comment', 'ping_reviewers'],
                'confidence': 0.91,
                'reason': 'Waiting on reviewer response',
            },
        )
        self.assertEqual(decision.state, 'awaiting_reviewer')
        self.assertEqual(decision.decision, 'suppress')
        self.assertEqual(decision.final_action, 'suppress_stale_label')
        self.assertIn('ping_reviewers', decision.actions)

    def test_decision_from_ai_result_low_confidence_defers(self) -> None:
        decision = module.decision_from_ai_result(
            7,
            'escalated',
            AI_CONFIG,
            'copilot_cli',
            {
                'state': 'active_discussion',
                'actions': ['suppress_stale_label', 'post_comment'],
                'confidence': 0.4,
                'reason': 'Weak signal',
            },
        )
        self.assertEqual(decision.final_action, 'defer')

    def test_decision_from_ai_result_keeps_stale_for_closure_candidate(self) -> None:
        decision = module.decision_from_ai_result(
            9,
            'final-notice',
            AI_CONFIG,
            'copilot_cli',
            {
                'state': 'candidate_for_closure',
                'actions': ['add_stale_label', 'post_comment', 'ping_author'],
                'confidence': 0.95,
                'reason': 'Long inactivity',
            },
        )
        self.assertEqual(decision.decision, 'keep_stale')
        self.assertEqual(decision.final_action, 'add_stale_label')
        self.assertEqual(decision.mention_scope, 'author')

    def test_decision_rejects_suppression_for_disallowed_state(self) -> None:
        strict_config = dict(AI_CONFIG)
        strict_config['allowed_suppression_states'] = ['blocked']
        decision = module.decision_from_ai_result(
            11,
            'warning',
            strict_config,
            'copilot_cli',
            {
                'state': 'awaiting_reviewer',
                'actions': ['suppress_stale_label'],
                'confidence': 0.99,
                'reason': 'Reviewer pending',
            },
        )
        self.assertEqual(decision.final_action, 'defer')

    def test_decision_handles_garbage_model_output(self) -> None:
        decision = module.decision_from_ai_result(
            1, 'warning', AI_CONFIG, 'copilot_cli', {'state': '???', 'confidence': 0.99}
        )
        self.assertEqual(decision.state, 'stale')
        self.assertEqual(decision.decision, 'keep_stale')
        self.assertEqual(decision.final_action, 'add_stale_label')

    def test_extract_json_object_handles_surrounding_text(self) -> None:
        payload = 'Here you go:\n```json\n{"state": "blocked", "confidence": 0.9}\n```'
        parsed = module.extract_json_object(payload)
        self.assertEqual(parsed['state'], 'blocked')
        with self.assertRaises(ValueError):
            module.extract_json_object('no json here')

    def test_build_ai_prompt_lists_all_states_and_actions(self) -> None:
        prompt = module.build_ai_prompt(base_signals())
        for state in module.DECISION_STATES:
            self.assertIn(state, prompt)
        for action in module.VALID_ACTIONS:
            self.assertIn(action, prompt)
        self.assertIn('Output JSON only', prompt)
        self.assertIn('Suppressing stale handling requires stronger evidence', prompt)


# ---------------------------------------------------------------------------
# Phase 2: deterministic fallback classifier
# ---------------------------------------------------------------------------


class FallbackClassifierTests(unittest.TestCase):
    def test_detects_blocked_on_merge_conflict(self) -> None:
        decision = module.fallback_state_from_context(
            base_signals(mergeable=False, mergeable_state='dirty'), PR_THRESHOLDS
        )
        self.assertEqual(decision.state, 'blocked')
        self.assertEqual(decision.decision, 'suppress')

    def test_detects_blocked_from_comment_patterns(self) -> None:
        decision = module.fallback_state_from_context(
            base_signals(mergeable=None, comment_patterns={'conflict': 1}), PR_THRESHOLDS
        )
        self.assertEqual(decision.state, 'blocked')

    def test_detects_active_discussion(self) -> None:
        decision = module.fallback_state_from_context(
            base_signals(unresolved_review_threads=3), PR_THRESHOLDS
        )
        self.assertEqual(decision.state, 'active_discussion')
        self.assertEqual(decision.final_action, 'suppress_stale_label')

    def test_detects_active_discussion_from_changes_requested(self) -> None:
        decision = module.fallback_state_from_context(
            base_signals(
                latest_review_state='CHANGES_REQUESTED', comment_patterns={'review': 1}
            ),
            PR_THRESHOLDS,
        )
        self.assertEqual(decision.state, 'active_discussion')

    def test_detects_awaiting_external(self) -> None:
        decision = module.fallback_state_from_context(
            base_signals(check_state='pending', linked_issues_count=2), PR_THRESHOLDS
        )
        self.assertEqual(decision.state, 'awaiting_external')

    def test_detects_awaiting_reviewer(self) -> None:
        decision = module.fallback_state_from_context(
            base_signals(open_review_requests=1), PR_THRESHOLDS
        )
        self.assertEqual(decision.state, 'awaiting_reviewer')

    def test_detects_candidate_for_closure(self) -> None:
        decision = module.fallback_state_from_context(
            base_signals(days_inactive=60, pr_age_days=90, branch_age_days=90),
            PR_THRESHOLDS,
        )
        self.assertEqual(decision.state, 'candidate_for_closure')
        self.assertEqual(decision.decision, 'keep_stale')

    def test_defaults_to_stale(self) -> None:
        decision = module.fallback_state_from_context(base_signals(), PR_THRESHOLDS)
        self.assertEqual(decision.state, 'stale')
        self.assertEqual(decision.final_action, 'add_stale_label')

    def test_low_confidence_suppression_defers(self) -> None:
        decision = module.fallback_state_from_context(
            base_signals(comment_patterns={'review': 2}), PR_THRESHOLDS
        )
        self.assertEqual(decision.state, 'active_discussion')
        self.assertEqual(decision.final_action, 'defer')

    def test_blocked_takes_priority_over_review_threads(self) -> None:
        decision = module.fallback_state_from_context(
            base_signals(mergeable=False, unresolved_review_threads=5), PR_THRESHOLDS
        )
        self.assertEqual(decision.state, 'blocked')

    def test_tolerates_missing_signal_keys(self) -> None:
        decision = module.fallback_state_from_context({}, PR_THRESHOLDS)
        self.assertIn(decision.state, module.DECISION_STATES)

    def test_evaluate_pr_with_ai_returns_none_when_disabled(self) -> None:
        self.assertIsNone(
            module.evaluate_pr_with_ai({'number': 1}, 'warning', {'enabled': False})
        )

    def test_evaluate_pr_with_ai_uses_heuristic_provider(self) -> None:
        decision = module.evaluate_pr_with_ai(
            {'number': 5, 'title': 'x', 'body': ''},
            'warning',
            {'enabled': True, 'ai_provider': 'heuristic', 'confidence_threshold': 0.8},
            PR_THRESHOLDS,
            context=base_signals(unresolved_review_threads=1),
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision.provider, 'heuristic')
        self.assertEqual(decision.state, 'active_discussion')


# ---------------------------------------------------------------------------
# Phase 2: tailored comments and mention routing
# ---------------------------------------------------------------------------


class TailoredCommunicationTests(unittest.TestCase):
    def test_contextual_comment_body_is_state_specific(self) -> None:
        body = module.contextual_comment_body(
            'awaiting_external', 6, ['@team'], 21, ['check_state=failure']
        )
        self.assertIn('waiting on external dependency', body)
        self.assertIn('`awaiting_external`', body)
        self.assertIn('check_state=failure', body)
        self.assertIn('@team', body)

    def test_contextual_comment_bodies_differ_per_state(self) -> None:
        bodies = {
            state: module.contextual_comment_body(state, 5, [], 1)
            for state in module.DECISION_STATES
        }
        self.assertEqual(len(set(bodies.values())), len(module.DECISION_STATES))

    def test_reviewer_wording_for_awaiting_reviewer(self) -> None:
        body = module.contextual_comment_body('awaiting_reviewer', 5, [], 1)
        self.assertIn('waiting on reviewer response', body)

    def test_mention_targets_are_scoped_by_state(self) -> None:
        pr = {
            'user': {'login': 'author'},
            'requested_reviewers': [{'login': 'reviewer'}],
            'requested_teams': [{'slug': 'platform'}],
        }
        self.assertEqual(
            module.mention_targets_for_state(pr, 'acme', 'awaiting_reviewer'),
            ['@reviewer'],
        )
        self.assertEqual(
            module.mention_targets_for_state(pr, 'acme', 'blocked'), ['@author']
        )
        self.assertEqual(
            module.mention_targets_for_state(pr, 'acme', 'awaiting_external'),
            ['@author', '@acme/platform'],
        )
        self.assertEqual(
            module.mention_targets_for_state(pr, 'acme', 'stale'),
            ['@author', '@reviewer', '@acme/platform'],
        )

    def test_mention_targets_fall_back_to_author_without_reviewers(self) -> None:
        pr = {'user': {'login': 'author'}, 'requested_reviewers': [], 'requested_teams': []}
        self.assertEqual(
            module.mention_targets_for_state(pr, 'acme', 'awaiting_reviewer'), ['@author']
        )

    def test_mention_targets_deduplicate_and_include_extras(self) -> None:
        pr = {
            'user': {'login': 'author'},
            'requested_reviewers': [{'login': 'author'}],
            'requested_teams': [],
        }
        self.assertEqual(
            module.mention_targets_for_state(pr, 'acme', 'stale', ['@oncall', 'author']),
            ['@author', '@oncall'],
        )

    def test_analyze_comment_patterns_counts_signals(self) -> None:
        patterns = module.analyze_comment_patterns(
            ['Please review this', 'still waiting on QA', 'merge conflict here']
        )
        self.assertGreater(patterns['review'], 0)
        self.assertGreater(patterns['waiting'], 0)
        self.assertGreater(patterns['conflict'], 0)

    def test_latest_review_state_picks_newest(self) -> None:
        reviews = [
            {'state': 'APPROVED', 'submitted_at': '2026-09-01T00:00:00Z'},
            {'state': 'CHANGES_REQUESTED', 'submitted_at': '2026-09-10T00:00:00Z'},
        ]
        self.assertEqual(module.latest_review_state(reviews), 'CHANGES_REQUESTED')
        self.assertIsNone(module.latest_review_state([]))


# ---------------------------------------------------------------------------
# Phase 2: branch risk scoring
# ---------------------------------------------------------------------------


class BranchRiskTests(unittest.TestCase):
    def test_likely_safe_to_delete(self) -> None:
        risk = module.assess_branch_risk(
            'feature/very-old', 80, [], set(), BRANCH_THRESHOLDS, default_branch='main'
        )
        self.assertEqual(risk.risk_state, 'likely_safe_to_delete')

    def test_maybe_preserve_between_thresholds(self) -> None:
        age = int(BRANCH_THRESHOLDS['stale_days'])
        self.assertLess(age, int(BRANCH_THRESHOLDS['delete_candidate_days']))
        risk = module.assess_branch_risk(
            'feature/mid-age', age, [], set(), BRANCH_THRESHOLDS, default_branch='main'
        )
        self.assertEqual(risk.risk_state, 'maybe_preserve')

    def test_requires_review_for_open_pr(self) -> None:
        risk = module.assess_branch_risk(
            'feature/with-pr',
            99,
            [{'state': 'open', 'head': {'ref': 'feature/with-pr'}}],
            {'feature/with-pr'},
            BRANCH_THRESHOLDS,
            default_branch='main',
        )
        self.assertEqual(risk.risk_state, 'requires_review')
        self.assertEqual(risk.open_prs, 1)

    def test_protects_default_and_exempt_branches(self) -> None:
        default_risk = module.assess_branch_risk(
            'main', 999, [], set(), BRANCH_THRESHOLDS, default_branch='main'
        )
        exempt_risk = module.assess_branch_risk(
            'release/1.0',
            999,
            [],
            set(),
            BRANCH_THRESHOLDS,
            exempt=True,
            default_branch='main',
        )
        self.assertEqual(default_risk.risk_state, 'maybe_preserve')
        self.assertEqual(exempt_risk.risk_state, 'maybe_preserve')

    def test_respects_do_not_delete_label(self) -> None:
        risk = module.assess_branch_risk(
            'feature/protected',
            999,
            [{'state': 'closed', 'head': {'ref': 'feature/protected'}}],
            set(),
            BRANCH_THRESHOLDS,
            default_branch='main',
            protected_by_label=True,
        )
        self.assertEqual(risk.risk_state, 'maybe_preserve')
        self.assertIn('Do_Not_Delete', risk.reason)

    def test_recent_branch_requires_review(self) -> None:
        risk = module.assess_branch_risk(
            'feature/new', 1, [], set(), BRANCH_THRESHOLDS, default_branch='main'
        )
        self.assertEqual(risk.risk_state, 'requires_review')

    def test_every_risk_state_is_known(self) -> None:
        valid = {'likely_safe_to_delete', 'maybe_preserve', 'requires_review'}
        for age in (0, 5, 8, 10, 50):
            risk = module.assess_branch_risk(
                'feature/x', age, [], set(), BRANCH_THRESHOLDS, default_branch='main'
            )
            self.assertIn(risk.risk_state, valid)
            self.assertGreaterEqual(risk.score, 0.0)
            self.assertLessEqual(risk.score, 1.0)


# ---------------------------------------------------------------------------
# Phase 2: reporting
# ---------------------------------------------------------------------------


class ReportingTests(unittest.TestCase):
    def test_summary_defaults_include_phase_two_buckets(self) -> None:
        summary = module.RunSummary(run_mode='dry-run')
        for state in module.DECISION_STATES:
            self.assertEqual(summary.ai_state_counts[state], 0)
        self.assertEqual(summary.branch_risk_counts['likely_safe_to_delete'], 0)
        self.assertEqual(summary.branch_risk_assessments, [])

    def test_summary_as_dict_contains_phase_two_fields(self) -> None:
        summary = module.RunSummary(run_mode='dry-run')
        summary.ai_state_counts['blocked'] = 1
        summary.branch_risk_counts['requires_review'] = 2
        summary.ai_decisions.append(
            module.fallback_state_from_context(
                base_signals(unresolved_review_threads=1), PR_THRESHOLDS
            )
        )
        summary.branch_risk_assessments.append(
            module.assess_branch_risk(
                'feature/x', 80, [], set(), BRANCH_THRESHOLDS, default_branch='main'
            )
        )

        payload = module.summary_as_dict(summary)
        self.assertEqual(payload['ai_state_counts']['blocked'], 1)
        self.assertEqual(payload['branch_risk_counts']['requires_review'], 2)
        self.assertEqual(payload['ai_decisions'][0]['state'], 'active_discussion')
        self.assertEqual(
            payload['branch_risk_assessments'][0]['risk_state'], 'likely_safe_to_delete'
        )
        self.assertIn('actions', payload['ai_decisions'][0])
        json.dumps(payload)

    def test_write_summary_writes_report_history_and_markdown(self) -> None:
        summary = module.RunSummary(run_mode='dry-run')
        summary.ai_decisions.append(
            module.fallback_state_from_context(base_signals(), PR_THRESHOLDS)
        )
        summary.branch_risk_assessments.append(
            module.assess_branch_risk(
                'feature/x', 80, [], set(), BRANCH_THRESHOLDS, default_branch='main'
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / 'report.json'
            history = Path(tmp) / 'history.jsonl'
            step_summary = Path(tmp) / 'step-summary.md'
            keys = (
                'STALE_CLEANER_REPORT_PATH',
                'STALE_CLEANER_HISTORY_PATH',
                'GITHUB_STEP_SUMMARY',
            )
            previous = {key: os.environ.get(key) for key in keys}
            os.environ['STALE_CLEANER_REPORT_PATH'] = str(report)
            os.environ['STALE_CLEANER_HISTORY_PATH'] = str(history)
            os.environ['GITHUB_STEP_SUMMARY'] = str(step_summary)
            try:
                module.write_summary(summary)
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

            payload = json.loads(report.read_text())
            self.assertEqual(payload['report_version'], 2)
            self.assertIn('ai_state_counts', payload)
            self.assertEqual(len(history.read_text().strip().splitlines()), 1)

            markdown = step_summary.read_text()
            self.assertIn('## AI Context States', markdown)
            self.assertIn('## Branch Risk Scores', markdown)
            self.assertIn('## Branch Risk Details', markdown)


# ---------------------------------------------------------------------------
# Phase 2: end-to-end engine behaviour with a fake GitHub API
# ---------------------------------------------------------------------------


class FakeGitHubClient:
    """In-memory GitHub stand-in so the engine can be exercised offline."""

    def __init__(self, pull_requests, branches, branch_prs=None, commit_dates=None):
        self.owner = 'acme'
        self.repo = 'demo'
        self.repository = 'acme/demo'
        self._pull_requests = pull_requests
        self._branches = branches
        self._branch_prs = branch_prs or {}
        self._commit_dates = commit_dates or {}
        self.created_labels = []
        self.added_labels = []
        self.removed_labels = []
        self.comments = []
        self.deleted_branches = []

    # --- read paths -------------------------------------------------
    def repo_info(self):
        return {'default_branch': 'main'}

    def repo_labels(self):
        return [{'name': 'stale:warning'}]

    def open_pull_requests(self):
        return [pr['payload'] for pr in self._pull_requests]

    def _record(self, number):
        for pr in self._pull_requests:
            if int(pr['payload']['number']) == int(number):
                return pr
        raise KeyError(number)

    def issue_labels(self, number):
        return [{'name': name} for name in self._record(number).get('labels', [])]

    def pull_request_detail(self, number):
        return self._record(number)['payload']

    def pull_request_commits(self, number):
        return [
            {'commit': {'author': {'date': date}, 'committer': {'date': date}}}
            for date in self._record(number).get('commit_dates', [])
        ]

    def pull_request_reviews(self, number):
        return self._record(number).get('reviews', [])

    def issue_comments(self, number):
        return self._record(number).get('comments', [])

    def pull_request_review_comments(self, number):
        return []

    def issue_timeline(self, number):
        return self._record(number).get('timeline', [])

    def pull_request_context_graphql(self, number):
        return self._record(number).get('graphql', {})

    def commit_status(self, sha):
        return {'state': 'success'}

    def commit_check_runs(self, sha):
        return {'check_runs': []}

    def branches(self):
        return self._branches

    def branch_detail(self, branch):
        for item in self._branches:
            if item['name'] == branch:
                return item
        raise RuntimeError(f'unknown branch {branch}')

    def commit_detail(self, sha):
        date = self._commit_dates.get(sha)
        if not date:
            raise RuntimeError(f'unknown sha {sha}')
        return {'commit': {'committer': {'date': date}, 'author': {'date': date}}}

    def pull_requests_for_branch(self, branch):
        return self._branch_prs.get(branch, [])

    # --- write paths ------------------------------------------------
    def create_label(self, name, color='ededed', description=''):
        self.created_labels.append(name)

    def add_issue_labels(self, number, labels):
        self.added_labels.append((number, tuple(labels)))

    def remove_issue_label(self, number, label):
        self.removed_labels.append((number, label))

    def add_comment(self, number, body):
        self.comments.append((number, body))

    def delete_branch(self, branch):
        self.deleted_branches.append(branch)


def build_fake_client():
    pull_requests = [
        {
            # Recently active -> stale label should be cleared.
            'payload': {
                'number': 1,
                'title': 'Fresh work',
                'body': '',
                'created_at': iso(2026, 9, 25),
                'user': {'login': 'alice'},
                'head': {'ref': 'feature/one', 'sha': 'sha-one'},
                'requested_reviewers': [],
                'requested_teams': [],
                'mergeable': True,
                'mergeable_state': 'clean',
            },
            'labels': ['stale:warning'],
            'commit_dates': [iso(2026, 9, 25)],
        },
        {
            # Stale by age but has unresolved review threads -> suppress.
            'payload': {
                'number': 2,
                'title': 'Under review',
                'body': '',
                'created_at': iso(2026, 9, 1),
                'user': {'login': 'bob'},
                'head': {'ref': 'feature/two', 'sha': 'sha-two'},
                'requested_reviewers': [{'login': 'carol'}],
                'requested_teams': [],
                'mergeable': True,
                'mergeable_state': 'clean',
            },
            'labels': ['stale:warning'],
            'commit_dates': [iso(2026, 9, 18)],
            'graphql': {
                'reviewThreads': {'nodes': [{'isResolved': False}, {'isResolved': False}]},
                'closingIssuesReferences': {'nodes': []},
            },
        },
        {
            # Genuinely stale -> keep stale handling.
            'payload': {
                'number': 3,
                'title': 'Quiet change',
                'body': '',
                'created_at': iso(2026, 9, 1),
                'user': {'login': 'dave'},
                'head': {'ref': 'feature/three', 'sha': 'sha-three'},
                'requested_reviewers': [],
                'requested_teams': [],
                'mergeable': True,
                'mergeable_state': 'clean',
            },
            'labels': [],
            'commit_dates': [iso(2026, 9, 10)],
        },
        {
            # Exempt label -> skipped entirely.
            'payload': {
                'number': 4,
                'title': 'Security fix',
                'body': '',
                'created_at': iso(2026, 8, 1),
                'user': {'login': 'erin'},
                'head': {'ref': 'feature/four', 'sha': 'sha-four'},
                'requested_reviewers': [],
                'requested_teams': [],
            },
            'labels': ['security'],
            'commit_dates': [iso(2026, 8, 1)],
        },
    ]

    branches = [
        {'name': 'main', 'protected': True, 'commit': {'sha': 'sha-main'}},
        {'name': 'release/1.0', 'protected': False, 'commit': {'sha': 'sha-release'}},
        {'name': 'feature/old', 'protected': False, 'commit': {'sha': 'sha-old'}},
        {'name': 'feature/openpr', 'protected': False, 'commit': {'sha': 'sha-openpr'}},
        {'name': 'feature/keep', 'protected': False, 'commit': {'sha': 'sha-keep'}},
    ]

    branch_prs = {
        'feature/old': [],
        'feature/openpr': [
            {'state': 'open', 'head': {'ref': 'feature/openpr'}, 'labels': []}
        ],
        'feature/keep': [
            {
                'state': 'closed',
                'head': {'ref': 'feature/keep'},
                'labels': [{'name': 'Do_Not_Delete'}],
            }
        ],
    }

    commit_dates = {
        'sha-main': iso(2026, 9, 24),
        'sha-release': iso(2026, 9, 1),
        'sha-old': iso(2026, 8, 1),
        'sha-openpr': iso(2026, 8, 1),
        'sha-keep': iso(2026, 8, 1),
        'sha-one': iso(2026, 9, 25),
        'sha-two': iso(2026, 9, 18),
        'sha-three': iso(2026, 9, 20),
        'sha-four': iso(2026, 8, 1),
    }

    return FakeGitHubClient(pull_requests, branches, branch_prs, commit_dates)


def heuristic_config():
    config = json.loads(json.dumps(module.DEFAULT_CONFIG))
    config['ai_config']['ai_provider'] = 'heuristic'
    config['ai_config']['log_decisions'] = False
    return config


class EndToEndEngineTests(unittest.TestCase):
    def run_engine(self, dry_run: bool):
        client = build_fake_client()
        config = heuristic_config()
        summary = module.RunSummary(run_mode='dry-run' if dry_run else 'apply')
        module.process_pull_requests(client, config, NOW, dry_run, summary, 'acme')
        module.process_branches(client, config, NOW, dry_run, summary, 'main')
        return client, summary

    def test_apply_mode_routes_each_pull_request_by_context(self) -> None:
        client, summary = self.run_engine(dry_run=False)

        self.assertEqual(summary.prs_processed, 4)
        self.assertEqual(summary.prs_skipped_exempt, 1)

        # PR 1 is active: stale label cleared, no comment.
        self.assertIn((1, 'stale:warning'), client.removed_labels)
        self.assertNotIn(1, [number for number, _ in client.comments])

        decisions = {decision.pr_number: decision for decision in summary.ai_decisions}

        # PR 2 has unresolved review threads -> suppressed with tailored comment.
        self.assertEqual(decisions[2].state, 'active_discussion')
        self.assertEqual(decisions[2].final_action, 'suppress_stale_label')
        self.assertIn((2, 'stale:warning'), client.removed_labels)
        pr2_comment = next(body for number, body in client.comments if number == 2)
        self.assertIn('`active_discussion`', pr2_comment)
        self.assertIn('@carol', pr2_comment)

        # PR 3 has no active signals -> stale label plus stale comment.
        self.assertEqual(decisions[3].state, 'stale')
        self.assertIn((3, ('stale:final-notice',)), client.added_labels)
        pr3_comment = next(body for number, body in client.comments if number == 3)
        self.assertIn('`stale`', pr3_comment)

        # PR 4 is exempt and never reaches the AI engine.
        self.assertNotIn(4, decisions)
        self.assertEqual(summary.ai_reviewed, 2)
        self.assertEqual(summary.ai_suppressed, 1)
        self.assertEqual(summary.ai_fallbacks, 0)
        self.assertEqual(summary.ai_state_counts['active_discussion'], 1)
        self.assertEqual(summary.ai_state_counts['stale'], 1)

    def test_apply_mode_branch_cleanup_and_risk_scoring(self) -> None:
        client, summary = self.run_engine(dry_run=False)

        self.assertEqual(summary.branches_processed, 5)
        self.assertEqual(client.deleted_branches, ['feature/old'])
        self.assertEqual(summary.delete_candidates, ['feature/old'])
        self.assertIn('feature/keep', summary.protected_by_labels)

        risks = {item.branch_name: item for item in summary.branch_risk_assessments}
        self.assertEqual(risks['main'].risk_state, 'maybe_preserve')
        self.assertEqual(risks['release/1.0'].risk_state, 'maybe_preserve')
        self.assertEqual(risks['feature/old'].risk_state, 'likely_safe_to_delete')
        self.assertEqual(risks['feature/openpr'].risk_state, 'requires_review')
        self.assertEqual(risks['feature/keep'].risk_state, 'maybe_preserve')
        self.assertEqual(summary.branch_risk_counts['likely_safe_to_delete'], 1)

    def test_dry_run_makes_no_mutations(self) -> None:
        client, summary = self.run_engine(dry_run=True)

        self.assertEqual(client.created_labels, [])
        self.assertEqual(client.added_labels, [])
        self.assertEqual(client.removed_labels, [])
        self.assertEqual(client.comments, [])
        self.assertEqual(client.deleted_branches, [])

        # Analysis still happens so the summary stays useful.
        self.assertEqual(summary.prs_processed, 4)
        self.assertEqual(summary.ai_reviewed, 2)
        self.assertEqual(summary.delete_candidates, ['feature/old'])
        self.assertEqual(summary.deleted_branches, [])

    def test_copilot_failure_falls_back_to_heuristic(self) -> None:
        client = build_fake_client()
        config = heuristic_config()
        config['ai_config']['ai_provider'] = 'copilot_cli'
        summary = module.RunSummary(run_mode='dry-run')

        original = module.evaluate_pr_with_copilot_cli

        def boom(*args, **kwargs):
            raise RuntimeError('copilot unavailable')

        module.evaluate_pr_with_copilot_cli = boom
        try:
            module.process_pull_requests(client, config, NOW, True, summary, 'acme')
        finally:
            module.evaluate_pr_with_copilot_cli = original

        self.assertEqual(summary.ai_reviewed, 2)
        self.assertEqual(summary.ai_fallbacks, 2)
        for decision in summary.ai_decisions:
            self.assertEqual(decision.provider, 'heuristic')
            self.assertIn('fallback from copilot_cli', decision.reason)

    def test_ai_disabled_falls_back_to_plain_stale_labelling(self) -> None:
        client = build_fake_client()
        config = heuristic_config()
        config['ai_config']['enabled'] = False
        summary = module.RunSummary(run_mode='apply')

        module.process_pull_requests(client, config, NOW, False, summary, 'acme')

        self.assertEqual(summary.ai_reviewed, 0)
        self.assertEqual(summary.ai_decisions, [])
        # PR 2 and PR 3 both get labelled because there is no context engine.
        labelled = {number for number, _ in client.added_labels}
        self.assertIn(2, labelled)
        self.assertIn(3, labelled)


if __name__ == '__main__':
    raise SystemExit(unittest.main())
