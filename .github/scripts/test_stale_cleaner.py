#!/usr/bin/env python3

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
import unittest
from unittest import mock

SCRIPT_PATH = Path(__file__).with_name('stale_cleaner.py')
spec = importlib.util.spec_from_file_location('stale_cleaner', SCRIPT_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
assert spec.loader is not None
spec.loader.exec_module(module)


class StaleCleanerTests(unittest.TestCase):
    def test_stage_calculation(self) -> None:
        thresholds = module.DEFAULT_CONFIG['pull_request_thresholds']
        self.assertEqual(module.stale_stage_for_days(0, thresholds), 'active')
        self.assertEqual(module.stale_stage_for_days(1, thresholds), 'active')
        self.assertEqual(module.stale_stage_for_days(2, thresholds), 'warning')
        self.assertEqual(module.stale_stage_for_days(3, thresholds), 'warning')
        self.assertEqual(module.stale_stage_for_days(4, thresholds), 'escalated')
        self.assertEqual(module.stale_stage_for_days(6, thresholds), 'escalated')
        self.assertEqual(module.stale_stage_for_days(7, thresholds), 'final-notice')

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

    def test_ai_decision_disabled_returns_none(self) -> None:
        pr = {
            'number': 42,
            'title': 'Test PR',
            'body': 'Test body',
        }
        ai_config = {'enabled': False}
        decision = module.evaluate_pr_with_ai(pr, 'warning', ai_config)
        self.assertIsNone(decision)

    def test_ai_decision_detects_wip_indicator(self) -> None:
        pr = {
            'number': 42,
            'title': '[WIP] Feature under review',
            'body': 'Still working on this',
        }
        ai_config = {
            'enabled': True,
            'allowed_suppression_categories': ['work-in-progress'],
            'confidence_threshold': 0.6,
            'log_decisions': False,
        }
        decision = module.evaluate_pr_with_ai(pr, 'warning', ai_config)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.category, 'work-in-progress')
        self.assertGreaterEqual(decision.confidence, 0.6)
        self.assertEqual(decision.provider, 'heuristic')

    def test_ai_decision_detects_active_review(self) -> None:
        pr = {
            'number': 43,
            'title': 'Add new feature',
            'body': 'Addressing review feedback',
        }
        ai_config = {
            'enabled': True,
            'allowed_suppression_categories': ['active-review'],
            'confidence_threshold': 0.5,
            'log_decisions': False,
        }
        decision = module.evaluate_pr_with_ai(pr, 'escalated', ai_config)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.decision, 'suppress')
        self.assertEqual(decision.category, 'active-review')

    def test_ai_respects_confidence_threshold(self) -> None:
        pr = {
            'number': 44,
            'title': 'Update docs',
            'body': 'Minor update',
        }
        ai_config = {
            'enabled': True,
            'allowed_suppression_categories': ['insufficient-evidence'],
            'confidence_threshold': 0.9,  # High threshold
            'log_decisions': False,
        }
        decision = module.evaluate_pr_with_ai(pr, 'final-notice', ai_config)
        self.assertIsNotNone(decision)
        self.assertNotEqual(decision.final_action, 'suppress')  # Low confidence action

    def test_run_summary_initializes_ai_fields(self) -> None:
        summary = module.RunSummary(run_mode='dry-run')
        self.assertEqual(summary.ai_reviewed, 0)
        self.assertEqual(summary.ai_suppressed, 0)
        self.assertEqual(summary.ai_fallbacks, 0)
        self.assertIsNotNone(summary.ai_decisions)
        self.assertEqual(len(summary.ai_decisions), 0)

    def test_default_ai_provider_is_gemini_cli(self) -> None:
        self.assertEqual(module.DEFAULT_CONFIG['ai_config']['ai_provider'], 'gemini_cli')

    def test_extract_json_object_parses_wrapped_content(self) -> None:
        payload = 'noise {"is_active": true, "category": "active-review", "confidence": 0.91, "reason": "recent updates"} tail'
        parsed = module.extract_json_object(payload)
        self.assertTrue(parsed['is_active'])
        self.assertEqual(parsed['category'], 'active-review')

    @mock.patch.object(module.shutil, 'which', return_value='/opt/homebrew/bin/gemini')
    @mock.patch.object(module.subprocess, 'run')
    def test_gemini_cli_decision_parses_json_output(self, mock_run: mock.Mock, _mock_which: mock.Mock) -> None:
        mock_run.return_value = mock.Mock(
            returncode=0,
            stdout='{"is_active": true, "category": "active-review", "confidence": 0.93, "reason": "Review is ongoing"}',
            stderr='',
        )
        pr = {
            'number': 101,
            'title': 'Feature PR',
            'body': 'Implements feature',
        }
        ai_config = {
            'enabled': True,
            'ai_provider': 'gemini_cli',
            'confidence_threshold': 0.8,
        }
        decision = module.evaluate_pr_with_gemini_cli(pr, 'warning', ai_config)
        self.assertEqual(decision.provider, 'gemini_cli')
        self.assertEqual(decision.decision, 'suppress')
        self.assertEqual(decision.category, 'active-review')
        self.assertEqual(decision.final_action, 'suppress')

    def test_summary_report_payload_contains_ai_details(self) -> None:
        summary = module.RunSummary(run_mode='dry-run')
        summary.prs_processed = 3
        summary.stale_counts['warning'] = 2
        summary.stale_branches.append('feature/old-work')
        summary.ai_reviewed = 2
        summary.ai_suppressed = 1
        summary.ai_decisions.append(
            module.AIDecision(
                pr_number=42,
                baseline_stage='warning',
                decision='suppress',
                category='active-review',
                confidence=0.91,
                reason='Review is ongoing',
                final_action='suppress',
                provider='gemini_cli',
            )
        )
        payload = module.summary_report_payload(summary)
        self.assertEqual(payload['run_mode'], 'dry-run')
        self.assertEqual(payload['prs_processed'], 3)
        self.assertEqual(payload['stale_counts']['warning'], 2)
        self.assertEqual(payload['stale_branches'], ['feature/old-work'])
        self.assertEqual(payload['ai']['reviewed'], 2)
        self.assertEqual(payload['ai']['suppressed'], 1)
        self.assertEqual(payload['ai']['decisions'][0]['provider'], 'gemini_cli')
        self.assertIn('generated_at', payload)

    def test_process_pr_handles_key_error_gracefully(self) -> None:
        class MockGitHubClient:
            def open_pull_requests(self):
                return [
                    {'number': 1, 'created_at': '2026-09-15T12:00:00Z'},
                    {'number': 2}  # Missing 'created_at'
                ]
            def issue_labels(self, number):
                return []
            def pull_request_commits(self, number):
                return []
            def pull_request_reviews(self, number):
                return []
            def repo_labels(self):
                return []
            def ensure_stale_labels(self, client, config, existing_labels, dry_run):
                pass

        client = MockGitHubClient()
        config = module.DEFAULT_CONFIG
        now = dt.datetime(2026, 9, 17, 8, 20, tzinfo=dt.timezone.utc)
        summary = module.RunSummary(run_mode='dry-run')

        module.process_pull_requests(client, config, now, True, summary, 'owner')

        self.assertEqual(summary.prs_processed, 2)
        self.assertEqual(summary.ai_fallbacks, 1)


if __name__ == '__main__':
    raise SystemExit(unittest.main())
