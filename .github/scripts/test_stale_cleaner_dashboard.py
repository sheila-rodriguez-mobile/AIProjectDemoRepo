#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
import unittest

SCRIPT_PATH = Path(__file__).with_name('stale_cleaner_dashboard.py')
spec = importlib.util.spec_from_file_location('stale_cleaner_dashboard', SCRIPT_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
assert spec.loader is not None
spec.loader.exec_module(module)


class StaleCleanerDashboardTests(unittest.TestCase):
    def test_collect_ai_category_counts(self) -> None:
        counts = module.collect_ai_category_counts([
            {'category': 'active-review'},
            {'category': 'work-in-progress'},
            {'category': 'active-review'},
        ])
        self.assertEqual(counts['active-review'], 2)
        self.assertEqual(counts['work-in-progress'], 1)

    def test_derive_metrics_collects_ai_category_counts(self) -> None:
        report = {
            'prs_processed': 5,
            'stale_counts': {'active': 1, 'warning': 2, 'escalated': 1, 'final-notice': 0},
            'delete_candidates': ['feature/delete-me'],
            'stale_branches': ['feature/stale-a'],
            'protected_by_labels': ['feature/protected-a'],
            'ai': {
                'reviewed': 3,
                'suppressed': 1,
                'fallbacks': 0,
                'decisions': [
                    {'category': 'active-review'},
                    {'category': 'work-in-progress'},
                    {'category': 'active-review'},
                ],
            },
        }
        metrics = module.derive_metrics(report, [])
        self.assertEqual(metrics['ai_category_counts']['active-review'], 2)
        self.assertEqual(metrics['ai_category_counts']['work-in-progress'], 1)
        self.assertTrue(any('Most common AI category is active-review' in item for item in metrics['insights']))

    def test_render_ai_category_chart_handles_empty_state(self) -> None:
        html = module.render_ai_category_chart({'ai_category_counts': {}})
        self.assertIn('No AI categories recorded for this run.', html)

    def test_render_ai_category_chart_renders_bar_chart(self) -> None:
        html = module.render_ai_category_chart(
            {'ai_category_counts': {'active-review': 3, 'work-in-progress': 1}}
        )
        self.assertIn('AI decision categories', html)
        self.assertIn('active review', html)
        self.assertIn('work in progress', html)

    def test_render_ai_category_donut_renders_segments(self) -> None:
        html = module.render_ai_category_donut(
            {'ai_category_counts': {'active-review': 2, 'work-in-progress': 1}}
        )
        self.assertIn('AI category share', html)
        self.assertIn('AI decisions', html)
        self.assertIn('active review', html)

    def test_filtered_ai_decisions_respects_selected_category(self) -> None:
        report = {
            'ai': {
                'decisions': [
                    {'category': 'active-review', 'pr_number': 1},
                    {'category': 'work-in-progress', 'pr_number': 2},
                ]
            }
        }
        filtered = module.filtered_ai_decisions(report, 'active-review')
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]['pr_number'], 1)

    def test_render_ai_category_trend_chart_renders_top_categories(self) -> None:
        history = [
            {'ai': {'decisions': [{'category': 'active-review'}, {'category': 'work-in-progress'}]}},
            {'ai': {'decisions': [{'category': 'active-review'}, {'category': 'active-review'}]}},
        ]
        html = module.render_ai_category_trend_chart(history)
        self.assertIn('AI category trends', html)
        self.assertIn('active review', html)


if __name__ == '__main__':
    raise SystemExit(unittest.main())
