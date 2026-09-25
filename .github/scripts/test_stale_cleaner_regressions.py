#!/usr/bin/env python3
"""Regression tests for the stale-cleaner correctness and robustness fixes."""

from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path

HERE = Path(__file__).parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    assert spec.loader is not None
    spec.loader.exec_module(loaded)
    return loaded


# The fixture module loads stale_cleaner itself; share that single instance.
fixtures = _load('stale_cleaner_test_fixtures', 'test_stale_cleaner.py')
module = fixtures.module

NOW = fixtures.NOW
iso = fixtures.iso
PR_THRESHOLDS = module.DEFAULT_CONFIG['pull_request_thresholds']
BRANCH_THRESHOLDS = module.DEFAULT_CONFIG['branch_thresholds']


def heuristic_config() -> dict:
    config = copy.deepcopy(module.DEFAULT_CONFIG)
    config['ai_config']['ai_provider'] = 'heuristic'
    config['ai_config']['log_decisions'] = False
    return config


def single_pr_client(**record_overrides):
    """Fake client with one stale PR (#10, 5 days inactive) and no branches."""
    record = {
        'payload': {
            'number': 10,
            'title': 'Some change',
            'body': '',
            'created_at': iso(2026, 9, 1),
            'user': {'login': 'author'},
            'head': {'ref': 'feature/ten', 'sha': 'sha-ten'},
            'requested_reviewers': [],
            'requested_teams': [],
            'mergeable': True,
            'mergeable_state': 'clean',
        },
        'labels': [],
        'commit_dates': [iso(2026, 9, 20)],
    }
    record.update(record_overrides)
    return fixtures.FakeGitHubClient(
        [record], [], {}, {'sha-ten': iso(2026, 9, 20)}
    )


def run_prs(client, config=None, dry_run=False):
    summary = module.RunSummary(run_mode='dry-run' if dry_run else 'apply')
    module.process_pull_requests(
        client, config or heuristic_config(), NOW, dry_run, summary, 'acme'
    )
    return summary


# ---------------------------------------------------------------------------
# Comment handling
# ---------------------------------------------------------------------------


class CommentDeduplicationTests(unittest.TestCase):
    def test_comment_contains_hidden_marker(self) -> None:
        client = single_pr_client()
        run_prs(client)
        self.assertEqual(len(client.comments), 1)
        self.assertIn('<!-- pr-cleaner:key=stale:final-notice -->', client.comments[0][1])

    def test_same_comment_is_not_reposted_on_next_run(self) -> None:
        first = single_pr_client()
        run_prs(first)
        previous_body = first.comments[0][1]

        second = single_pr_client(
            comments=[
                {
                    'body': previous_body,
                    'created_at': iso(2026, 9, 24),
                    'user': {'login': 'github-actions[bot]', 'type': 'Bot'},
                }
            ]
        )
        summary = run_prs(second)
        self.assertEqual(second.comments, [])
        self.assertEqual(summary.comments_skipped_duplicate, 1)
        # The label is still maintained even when the comment is skipped.
        self.assertIn((10, ('stale:final-notice',)), second.added_labels)

    def test_comment_reposts_when_pr_had_activity_since(self) -> None:
        client = single_pr_client(
            comments=[
                {
                    'body': '<!-- pr-cleaner:key=stale:final-notice -->',
                    'created_at': iso(2026, 9, 10),
                    'user': {'login': 'github-actions[bot]', 'type': 'Bot'},
                }
            ]
        )
        summary = run_prs(client)
        self.assertEqual(len(client.comments), 1)
        self.assertEqual(summary.comments_skipped_duplicate, 0)

    def test_comment_reposts_when_state_changes(self) -> None:
        client = single_pr_client(
            comments=[
                {
                    'body': '<!-- pr-cleaner:key=stale:escalated -->',
                    'created_at': iso(2026, 9, 24),
                    'user': {'login': 'github-actions[bot]', 'type': 'Bot'},
                }
            ]
        )
        run_prs(client)
        self.assertEqual(len(client.comments), 1)

    def test_own_comments_do_not_feed_pattern_analysis(self) -> None:
        # The "blocked" comment text mentions a merge conflict. Before the fix,
        # reading it back made the cleaner classify the PR as blocked forever.
        blocked_comment = module.contextual_comment_body(
            'blocked', 5, ['@author'], 10, [], key='blocked:suppressed'
        )
        self.assertIn('merge conflict', blocked_comment)
        client = single_pr_client(
            payload={
                **single_pr_client()._pull_requests[0]['payload'],
                'mergeable': None,
                'mergeable_state': '',
            },
            comments=[
                {
                    'body': blocked_comment,
                    'created_at': iso(2026, 9, 22),
                    'user': {'login': 'someone', 'type': 'User'},
                }
            ],
        )
        summary = run_prs(client, dry_run=True)
        self.assertEqual(summary.ai_decisions[0].state, 'stale')

    def test_bot_comments_are_ignored(self) -> None:
        client = single_pr_client(
            payload={
                **single_pr_client()._pull_requests[0]['payload'],
                'mergeable': None,
            },
            comments=[
                {
                    'body': 'Detected merge conflict in lockfile',
                    'created_at': iso(2026, 9, 22),
                    'user': {'login': 'renovate[bot]', 'type': 'Bot'},
                }
            ],
        )
        summary = run_prs(client, dry_run=True)
        self.assertEqual(summary.ai_decisions[0].state, 'stale')

    def test_ai_disabled_still_posts_classic_comment(self) -> None:
        config = heuristic_config()
        config['ai_config']['enabled'] = False
        client = single_pr_client()
        run_prs(client, config)
        self.assertEqual(len(client.comments), 1)
        self.assertIn((10, ('stale:final-notice',)), client.added_labels)


# ---------------------------------------------------------------------------
# Signal interpretation
# ---------------------------------------------------------------------------


class SignalInterpretationTests(unittest.TestCase):
    def test_mergeable_state_blocked_is_not_a_conflict(self) -> None:
        # "blocked" means branch protection (e.g. required review) is unmet.
        decision = module.fallback_state_from_context(
            fixtures.base_signals(mergeable=True, mergeable_state='blocked'),
            PR_THRESHOLDS,
        )
        self.assertNotEqual(decision.state, 'blocked')

    def test_mergeable_state_dirty_is_a_conflict(self) -> None:
        decision = module.fallback_state_from_context(
            fixtures.base_signals(mergeable=None, mergeable_state='dirty'),
            PR_THRESHOLDS,
        )
        self.assertEqual(decision.state, 'blocked')

    def test_graphql_conflicting_string_is_a_conflict(self) -> None:
        self.assertIs(module.normalize_graphql_mergeable('CONFLICTING'), False)
        self.assertIs(module.normalize_graphql_mergeable('MERGEABLE'), True)
        self.assertIsNone(module.normalize_graphql_mergeable('UNKNOWN'))
        decision = module.fallback_state_from_context(
            fixtures.base_signals(mergeable='CONFLICTING', mergeable_state=''),
            PR_THRESHOLDS,
        )
        self.assertEqual(decision.state, 'blocked')

    def test_github_mergeable_true_overrides_conflict_chatter(self) -> None:
        decision = module.fallback_state_from_context(
            fixtures.base_signals(mergeable=True, comment_patterns={'conflict': 3}),
            PR_THRESHOLDS,
        )
        self.assertNotEqual(decision.state, 'blocked')

    def test_failing_ci_with_linked_issue_is_not_external(self) -> None:
        decision = module.fallback_state_from_context(
            fixtures.base_signals(check_state='failure', linked_issues_count=1),
            PR_THRESHOLDS,
        )
        self.assertEqual(decision.state, 'stale')

    def test_explicit_external_discussion_is_external(self) -> None:
        decision = module.fallback_state_from_context(
            fixtures.base_signals(comment_patterns={'external': 1}), PR_THRESHOLDS
        )
        self.assertEqual(decision.state, 'awaiting_external')

    def test_keyword_matching_uses_whole_words(self) -> None:
        self.assertEqual(module.count_keyword_hits(['aqua marine'], ['qa']), 0)
        self.assertEqual(module.count_keyword_hits(['waiting on QA'], ['qa']), 1)
        self.assertEqual(module.count_keyword_hits(['preview build'], ['review']), 0)

    def test_combined_status_with_no_statuses_is_not_pending(self) -> None:
        self.assertEqual(
            module.combine_check_state({'state': 'pending', 'total_count': 0}, None),
            'unknown',
        )

    def test_failing_status_is_not_masked_by_passing_check_runs(self) -> None:
        state = module.combine_check_state(
            {'state': 'failure', 'total_count': 1},
            {'check_runs': [{'status': 'completed', 'conclusion': 'success'}]},
        )
        self.assertEqual(state, 'failure')

    def test_in_progress_check_run_is_pending(self) -> None:
        state = module.combine_check_state(
            {'state': 'pending', 'total_count': 0},
            {'check_runs': [{'status': 'in_progress', 'conclusion': None}]},
        )
        self.assertEqual(state, 'pending')

    def test_all_passing_checks_are_success(self) -> None:
        state = module.combine_check_state(
            {'state': 'success', 'total_count': 2},
            {'check_runs': [{'status': 'completed', 'conclusion': 'skipped'}]},
        )
        self.assertEqual(state, 'success')

    def test_outdated_review_threads_are_not_counted(self) -> None:
        client = single_pr_client(
            graphql={
                'reviewThreads': {
                    'nodes': [{'isResolved': False, 'isOutdated': True}]
                },
                'closingIssuesReferences': {'nodes': []},
            }
        )
        context = module.build_pr_context(
            client, client._pull_requests[0]['payload'], 10, NOW, heuristic_config()
        )
        self.assertEqual(context['unresolved_review_threads'], 0)


# ---------------------------------------------------------------------------
# Decision gating
# ---------------------------------------------------------------------------


class DecisionGatingTests(unittest.TestCase):
    def test_action_order_does_not_flip_suppression(self) -> None:
        decision = module.decision_from_ai_result(
            1,
            'warning',
            module.DEFAULT_CONFIG['ai_config'],
            'copilot_cli',
            {
                'state': 'awaiting_reviewer',
                'actions': ['post_comment', 'suppress_stale_label'],
                'confidence': 0.95,
            },
        )
        self.assertEqual(decision.final_action, 'suppress_stale_label')

    def test_suppress_action_on_non_suppressing_state_keeps_stale(self) -> None:
        decision = module.decision_from_ai_result(
            1,
            'warning',
            module.DEFAULT_CONFIG['ai_config'],
            'copilot_cli',
            {'state': 'stale', 'actions': ['suppress_stale_label'], 'confidence': 0.99},
        )
        self.assertEqual(decision.decision, 'keep_stale')
        self.assertEqual(decision.final_action, 'add_stale_label')

    def test_confidence_is_clamped_and_validated(self) -> None:
        decision = module.decision_from_ai_result(
            1,
            'warning',
            module.DEFAULT_CONFIG['ai_config'],
            'copilot_cli',
            {'state': 'blocked', 'actions': ['suppress_stale_label'], 'confidence': 7},
        )
        self.assertEqual(decision.confidence, 1.0)
        with self.assertRaises(ValueError):
            module.decision_from_ai_result(
                1,
                'warning',
                module.DEFAULT_CONFIG['ai_config'],
                'copilot_cli',
                {'state': 'blocked', 'confidence': 'very high'},
            )

    def test_fallback_respects_configured_threshold(self) -> None:
        signals = fixtures.base_signals(comment_patterns={'review': 1})  # 0.62
        strict = module.fallback_state_from_context(signals, PR_THRESHOLDS)
        self.assertEqual(strict.final_action, 'defer')

        lenient_config = heuristic_config()['ai_config']
        lenient_config['confidence_threshold'] = 0.5
        lenient = module.fallback_state_from_context(
            signals, PR_THRESHOLDS, lenient_config
        )
        self.assertEqual(lenient.final_action, 'suppress_stale_label')

    def test_fallback_respects_allowed_suppression_states(self) -> None:
        config = heuristic_config()['ai_config']
        config['allowed_suppression_states'] = ['blocked']
        decision = module.fallback_state_from_context(
            fixtures.base_signals(open_review_requests=1), PR_THRESHOLDS, config
        )
        self.assertEqual(decision.state, 'awaiting_reviewer')
        self.assertEqual(decision.final_action, 'defer')

    def test_deferral_is_capped_at_final_notice(self) -> None:
        # Weak review chatter (0.62) would defer forever; at final-notice it must label.
        client = single_pr_client(
            comments=[
                {
                    'body': 'can someone review this?',
                    'created_at': iso(2026, 9, 21),
                    'user': {'login': 'teammate', 'type': 'User'},
                }
            ]
        )
        summary = run_prs(client)
        decision = summary.ai_decisions[0]
        self.assertEqual(decision.state, 'active_discussion')
        self.assertEqual(decision.final_action, 'add_stale_label')
        self.assertIn('deferral limit reached', decision.reason)
        self.assertIn((10, ('stale:final-notice',)), client.added_labels)

    def test_deferral_still_applies_before_final_notice(self) -> None:
        client = single_pr_client(
            commit_dates=[iso(2026, 9, 22)],  # 3 days -> escalated
            comments=[
                {
                    'body': 'can someone review this?',
                    'created_at': iso(2026, 9, 23),
                    'user': {'login': 'teammate', 'type': 'User'},
                }
            ],
        )
        summary = run_prs(client)
        self.assertEqual(summary.ai_decisions[0].final_action, 'defer')
        self.assertEqual(client.added_labels, [])
        self.assertEqual(client.comments, [])


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ConfigTests(unittest.TestCase):
    def test_default_config_and_repo_config_are_valid(self) -> None:
        self.assertEqual(module.validate_config(module.DEFAULT_CONFIG), [])
        repo_config = module.load_config(str(HERE.parent / 'stale-cleaner.json'))
        self.assertEqual(module.validate_config(repo_config), [])

    def test_validation_catches_bad_values(self) -> None:
        config = copy.deepcopy(module.DEFAULT_CONFIG)
        config['pull_request_thresholds']['warning_days'] = 9
        config['branch_thresholds']['stale_days'] = 20
        config['managed_labels'] = ['only-one']
        config['ai_config']['confidence_threshold'] = 1.5
        config['ai_config']['allowed_suppression_states'] = ['stale']
        errors = module.validate_config(config)
        joined = ' | '.join(errors)
        self.assertIn('warning_days <= escalated_days', joined)
        self.assertIn('stale_days must be <=', joined)
        self.assertIn('managed_labels', joined)
        self.assertIn('confidence_threshold', joined)
        self.assertIn('non-suppressing states', joined)

    def test_validation_rejects_non_integer_thresholds(self) -> None:
        config = copy.deepcopy(module.DEFAULT_CONFIG)
        config['pull_request_thresholds']['warning_days'] = '2'
        self.assertTrue(module.validate_config(config))

    def test_load_config_does_not_mutate_defaults(self) -> None:
        before = json.dumps(module.DEFAULT_CONFIG, sort_keys=True)
        config = module.load_config(None)
        config['ai_config']['ai_provider'] = 'changed'
        config['managed_labels'].append('extra')
        self.assertEqual(json.dumps(module.DEFAULT_CONFIG, sort_keys=True), before)

    def test_custom_managed_labels_are_used(self) -> None:
        config = heuristic_config()
        config['managed_labels'] = ['idle:1', 'idle:2', 'idle:3']
        client = single_pr_client(labels=['idle:1'])
        run_prs(client, config)
        self.assertIn((10, ('idle:3',)), client.added_labels)
        self.assertIn((10, 'idle:1'), client.removed_labels)
        self.assertNotIn('stale:final-notice', str(client.added_labels))


# ---------------------------------------------------------------------------
# Failure isolation and fail-safe branch deletion
# ---------------------------------------------------------------------------


class FailureIsolationTests(unittest.TestCase):
    def test_one_failing_pr_does_not_abort_the_run(self) -> None:
        client = fixtures.build_fake_client()

        original = module.build_pr_context

        def flaky(client_, pr, number, now, config):
            if number == 3:
                raise RuntimeError('simulated API outage')
            return original(client_, pr, number, now, config)

        module.build_pr_context = flaky
        try:
            summary = run_prs(client)
        finally:
            module.build_pr_context = original

        self.assertEqual(summary.prs_processed, 4)
        self.assertEqual(summary.prs_failed, 1)
        self.assertEqual(summary.ai_fallbacks, 0)
        self.assertTrue(any('PR #3' in message for message in summary.errors))
        # PRs before and after the failing one are still processed.
        self.assertIn(2, [decision.pr_number for decision in summary.ai_decisions])

    def test_branch_with_failed_pr_lookup_is_never_deleted(self) -> None:
        base = fixtures.build_fake_client()

        class LookupFails(type(base)):
            def pull_requests_for_branch(self, branch):
                raise RuntimeError('rate limited')

        client = LookupFails(
            base._pull_requests, base._branches, base._branch_prs, base._commit_dates
        )
        summary = module.RunSummary(run_mode='apply')
        module.process_branches(client, heuristic_config(), NOW, False, summary, 'main')
        self.assertEqual(client.deleted_branches, [])
        risks = {item.branch_name: item for item in summary.branch_risk_assessments}
        self.assertEqual(risks['feature/old'].risk_state, 'requires_review')
        self.assertIn('Could not verify', risks['feature/old'].reason)

    def test_branch_with_unknown_age_is_never_deleted(self) -> None:
        base = fixtures.build_fake_client()
        dates = dict(base._commit_dates)
        dates.pop('sha-old')
        client = type(base)(base._pull_requests, base._branches, base._branch_prs, dates)
        summary = module.RunSummary(run_mode='apply')
        module.process_branches(client, heuristic_config(), NOW, False, summary, 'main')
        self.assertNotIn('feature/old', client.deleted_branches)

    def test_failed_delete_is_recorded_and_run_continues(self) -> None:
        base = fixtures.build_fake_client()
        branches = base._branches + [
            {'name': 'feature/old2', 'protected': False, 'commit': {'sha': 'sha-old'}}
        ]

        class DeleteFails(type(base)):
            def delete_branch(self, branch):
                if branch == 'feature/old':
                    raise RuntimeError('422 protected by ruleset')
                super().delete_branch(branch)

        client = DeleteFails(base._pull_requests, branches, base._branch_prs, base._commit_dates)
        summary = module.RunSummary(run_mode='apply')
        module.process_branches(client, heuristic_config(), NOW, False, summary, 'main')
        self.assertEqual(summary.branch_delete_failures, ['feature/old'])
        self.assertEqual(client.deleted_branches, ['feature/old2'])
        self.assertEqual(summary.deleted_branches, ['feature/old2'])

    def test_open_pr_head_protects_branch_even_if_lookup_is_empty(self) -> None:
        risk = module.assess_branch_risk(
            'feature/x', 99, [], {'feature/x'}, BRANCH_THRESHOLDS, default_branch='main'
        )
        self.assertEqual(risk.risk_state, 'requires_review')


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode('utf-8')

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(code: int):
    return urllib.error.HTTPError(
        'https://api.github.com/x', code, 'err', {}, io.BytesIO(b'{"message":"err"}')
    )


class HttpClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls = []
        self.original = module.urllib.request.urlopen

    def tearDown(self) -> None:
        module.urllib.request.urlopen = self.original

    def _script(self, outcomes):
        def fake_urlopen(request, timeout=None):
            self.calls.append((request.get_method(), request.full_url, timeout))
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return _FakeResponse(outcome)

        module.urllib.request.urlopen = fake_urlopen

    def client(self, **kwargs):
        return module.GitHubClient('t', 'acme/demo', retry_base_delay=0, **kwargs)

    def test_get_is_retried_on_server_error(self) -> None:
        self._script([_http_error(503), {'default_branch': 'main'}])
        self.assertEqual(self.client().repo_info(), {'default_branch': 'main'})
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][2], 30.0)  # timeout applied

    def test_comment_post_is_not_retried_on_server_error(self) -> None:
        self._script([_http_error(502), None])
        with self.assertRaises(RuntimeError):
            self.client().add_comment(1, 'hi')
        self.assertEqual(len(self.calls), 1)

    def test_comment_post_is_retried_on_rate_limit(self) -> None:
        self._script([_http_error(429), {}])
        self.client().add_comment(1, 'hi')
        self.assertEqual(len(self.calls), 2)

    def test_retries_are_bounded(self) -> None:
        self._script([_http_error(503)] * 10)
        with self.assertRaises(RuntimeError):
            self.client(max_retries=2).repo_info()
        self.assertEqual(len(self.calls), 3)

    def test_client_errors_are_not_retried(self) -> None:
        self._script([_http_error(404)])
        with self.assertRaises(RuntimeError):
            self.client().repo_info()
        self.assertEqual(len(self.calls), 1)

    def test_delete_branch_keeps_slashes_in_ref(self) -> None:
        self._script([None])
        self.client().delete_branch('feature/nested/name')
        self.assertTrue(
            self.calls[0][1].endswith('/git/refs/heads/feature/nested/name')
        )

    def test_graphql_url_for_enterprise_server(self) -> None:
        cloud = module.GitHubClient('t', 'a/b')
        enterprise = module.GitHubClient('t', 'a/b', api_url='https://ghe.corp/api/v3')
        self.assertEqual(cloud.graphql_url, 'https://api.github.com/graphql')
        self.assertEqual(enterprise.graphql_url, 'https://ghe.corp/api/graphql')


# ---------------------------------------------------------------------------
# Report compatibility
# ---------------------------------------------------------------------------


class ReportCompatibilityTests(unittest.TestCase):
    def test_report_keeps_nested_ai_block_for_dashboard(self) -> None:
        summary = module.RunSummary(run_mode='dry-run')
        summary.ai_reviewed = 2
        summary.ai_fallbacks = 1
        summary.ai_decisions.append(
            module.fallback_state_from_context(fixtures.base_signals(), PR_THRESHOLDS)
        )
        payload = module.summary_as_dict(summary)
        self.assertEqual(payload['ai']['reviewed'], 2)
        self.assertEqual(payload['ai']['fallbacks'], 1)
        self.assertEqual(payload['ai']['decisions'], payload['ai_decisions'])
        self.assertIn('category', payload['ai']['decisions'][0])

    def test_dashboard_reads_ai_metrics_from_report(self) -> None:
        dashboard_path = HERE / 'stale_cleaner_dashboard.py'
        if not dashboard_path.exists():
            self.skipTest('dashboard not present')
        dashboard = _load('stale_cleaner_dashboard', 'stale_cleaner_dashboard.py')
        summary = module.RunSummary(run_mode='dry-run')
        summary.ai_reviewed = 3
        summary.ai_decisions.append(
            module.fallback_state_from_context(fixtures.base_signals(), PR_THRESHOLDS)
        )
        report = module.summary_as_dict(summary)
        counts = dashboard.collect_ai_category_counts(report['ai']['decisions'])
        self.assertEqual(counts, {'stale': 1})


if __name__ == '__main__':
    raise SystemExit(unittest.main())
