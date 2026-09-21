#!/usr/bin/env python3

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
import unittest

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
        self.assertEqual(module.stale_stage_for_days(3, thresholds), 'escalated')
        self.assertEqual(module.stale_stage_for_days(4, thresholds), 'final-notice')

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

    def test_ensure_stale_labels_creates_severity_colored_labels(self) -> None:
        class DummyClient:
            def __init__(self) -> None:
                self.created: list[tuple[str, str, str]] = []

            def create_label(self, name: str, color: str = 'ededed', description: str = '') -> None:
                self.created.append((name, color, description))

        client = DummyClient()
        existing_labels: set[str] = set()

        module.ensure_stale_labels(client, module.DEFAULT_CONFIG, existing_labels)

        self.assertEqual(client.created, [
            ('stale:warning', 'ffd33d', 'Pull request has been inactive for 2+ days.'),
            ('stale:escalated', 'fb8c00', 'Pull request has been inactive for 3+ days.'),
            ('stale:final-notice', 'd73a49', 'Pull request has been inactive for 4+ days.'),
        ])


if __name__ == '__main__':
    raise SystemExit(unittest.main())
