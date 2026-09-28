#!/usr/bin/env python3
"""Phase 3 tests: memory, multi-run escalation, feedback loop, branch advisor,
and goal-based optimisation of the stateful repository-hygiene agent."""

from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent


def _load(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    assert spec.loader is not None
    spec.loader.exec_module(loaded)
    return loaded


fixtures = _load('stale_cleaner_test_fixtures', 'test_stale_cleaner.py')
module = fixtures.module
agent = module.agent

NOW = fixtures.NOW
iso = fixtures.iso
DAY = dt.timedelta(days=1)


def config(ai_enabled: bool = True, **agent_overrides) -> dict:
    cfg = copy.deepcopy(module.DEFAULT_CONFIG)
    cfg['ai_config']['ai_provider'] = 'heuristic'
    cfg['ai_config']['log_decisions'] = False
    cfg['ai_config']['enabled'] = ai_enabled
    cfg['agent_config'].update(agent_overrides)
    return cfg


def new_memory(cfg: dict) -> 'agent.AgentMemory':
    return agent.AgentMemory(None, cfg['agent_config'])


class StatefulFakeClient(fixtures.FakeGitHubClient):
    """Fake GitHub whose writes change what later runs observe."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.clock = NOW
        self.commit_messages = {}

    def add_issue_labels(self, number, labels):
        super().add_issue_labels(number, labels)
        record = self._record(number)
        record['labels'] = sorted(set(record.get('labels', [])) | set(labels))
        record['payload']['labels'] = [{'name': name} for name in record['labels']]

    def remove_issue_label(self, number, label):
        super().remove_issue_label(number, label)
        record = self._record(number)
        record['labels'] = [name for name in record.get('labels', []) if name != label]
        record['payload']['labels'] = [{'name': name} for name in record['labels']]

    def add_comment(self, number, body):
        super().add_comment(number, body)
        self._record(number).setdefault('comments', []).append(
            {
                'body': body,
                'created_at': self.clock.isoformat(),
                'user': {'login': 'github-actions[bot]', 'type': 'Bot'},
            }
        )

    def commit_detail(self, sha):
        payload = super().commit_detail(sha)
        if sha in self.commit_messages:
            payload['commit']['message'] = self.commit_messages[sha]
        return payload


def pr_client(commit_day=(2026, 9, 15), reviewers=True, **record_overrides):
    payload = {
        'number': 10,
        'title': 'Some change',
        'body': '',
        'created_at': iso(2026, 9, 1),
        'user': {'login': 'author'},
        'head': {'ref': 'feature/ten', 'sha': 'sha-ten'},
        'requested_reviewers': [{'login': 'rev'}] if reviewers else [],
        'requested_teams': [{'slug': 'core'}] if reviewers else [],
        'mergeable': True,
        'mergeable_state': 'clean',
        'labels': [],
    }
    record = {'payload': payload, 'labels': [], 'commit_dates': [iso(*commit_day)]}
    record.update(record_overrides)
    return StatefulFakeClient([record], [], {}, {'sha-ten': iso(*commit_day)})


def run(client, cfg, memory, now, dry_run=False):
    client.clock = now
    summary = module.RunSummary(run_mode='dry-run' if dry_run else 'apply')
    module.process_pull_requests(client, cfg, now, dry_run, summary, 'acme', memory)
    return summary


def comment_count(client, number=10):
    return sum(1 for n, _ in client.comments if n == number)


# ---------------------------------------------------------------------------
# 1. Decision memory
# ---------------------------------------------------------------------------


class MemoryStoreTests(unittest.TestCase):
    def test_save_and_load_round_trip(self) -> None:
        cfg = config()
        memory = new_memory(cfg)
        agent.record_decision(memory, 7, NOW, 'warning', 'stale', 'add_stale_label', 0.7, 'heuristic', 'r')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'nested' / 'memory.json'
            memory.save(path, NOW)
            loaded = agent.AgentMemory.load(path, cfg['agent_config'])
        self.assertIsNone(loaded.load_error)
        record = loaded.pr(7)
        self.assertEqual(record['last_state'], 'stale')
        self.assertEqual(record['last_provider'], 'heuristic')
        self.assertEqual(len(record['history']), 1)
        self.assertEqual(loaded.data['memory_version'], agent.MEMORY_VERSION)

    def test_missing_file_starts_fresh(self) -> None:
        memory = agent.AgentMemory.load('/nonexistent/memory.json')
        self.assertIsNone(memory.load_error)
        self.assertEqual(memory.data['pull_requests'], {})

    def test_corrupt_or_future_memory_starts_fresh_with_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corrupt = Path(tmp) / 'corrupt.json'
            corrupt.write_text('{not json', encoding='utf-8')
            future = Path(tmp) / 'future.json'
            future.write_text(json.dumps({'memory_version': 99}), encoding='utf-8')
            for path in (corrupt, future):
                memory = agent.AgentMemory.load(path)
                self.assertIsNotNone(memory.load_error)
                self.assertEqual(memory.data['pull_requests'], {})

    def test_history_is_capped(self) -> None:
        memory = agent.AgentMemory(None, {'history_limit': 3})
        for index in range(6):
            agent.record_decision(memory, 1, NOW + index * DAY, 'warning', 'stale', 'add_stale_label')
        self.assertEqual(len(memory.pr(1)['history']), 3)

    def test_ai_context_receives_memory_view(self) -> None:
        cfg = config()
        memory = new_memory(cfg)
        client = pr_client(reviewers=False)
        run(client, cfg, memory, NOW)
        captured = {}
        original = module.evaluate_pr_with_ai

        def spy(pr, stage, ai_config, thresholds=None, context=None):
            captured.update(context or {})
            return original(pr, stage, ai_config, thresholds, context)

        module.evaluate_pr_with_ai = spy
        try:
            run(client, cfg, memory, NOW + DAY)
        finally:
            module.evaluate_pr_with_ai = original
        self.assertEqual(captured['memory']['previous_state'], 'stale')
        self.assertEqual(captured['memory']['escalation_step'], 1)

    def test_prune_drops_records_past_retention(self) -> None:
        memory = agent.AgentMemory(None, {'retention_days': 30})
        memory.pr(1)['last_seen_at'] = agent.to_iso(NOW - 40 * DAY)
        memory.pr(2)['last_seen_at'] = agent.to_iso(NOW - 5 * DAY)
        self.assertEqual(agent.prune_memory(memory, [], [], NOW), 1)
        self.assertFalse(memory.has_pr(1))
        self.assertTrue(memory.has_pr(2))


# ---------------------------------------------------------------------------
# 2. Escalation planning across runs
# ---------------------------------------------------------------------------


class EscalationLadderTests(unittest.TestCase):
    def test_ladder_advances_one_step_per_run_with_scoped_mentions(self) -> None:
        cfg = config(ai_enabled=False)
        cfg['additional_mentions'] = ['@acme/leads']
        memory = new_memory(cfg)
        client = pr_client()

        names = []
        for day in range(6):
            summary = run(client, cfg, memory, NOW + day * DAY)
            plan = summary.escalation_plans[0]
            names.append((plan.step, plan.name, plan.advanced))

        self.assertEqual(
            names,
            [
                (1, 'soft_warning', True),
                (2, 'ping_reviewers', True),
                (3, 'ping_team', True),
                (4, 'recommend_close', True),
                (5, 'branch_recommendation', True),
                (5, 'branch_recommendation', False),
            ],
        )
        bodies = [body for _, body in client.comments]
        self.assertEqual(len(bodies), 5)
        self.assertIn('Mentions: @author', bodies[0])
        self.assertNotIn('@rev', bodies[0])
        self.assertIn('@rev', bodies[1])
        self.assertNotIn('@acme/leads', bodies[1])
        self.assertIn('@acme/core', bodies[2])
        self.assertIn('@acme/leads', bodies[2])
        self.assertIn('close or archive', bodies[3])
        self.assertIn('Branch recommendation: `delete_after_close`', bodies[4])
        self.assertIn('<!-- pr-cleaner:key=escalation:step-5 -->', bodies[4])
        self.assertEqual(memory.pr(10)['branch_recommendation'], 'delete_after_close')

    def test_step_spacing_prevents_double_escalation_in_one_day(self) -> None:
        cfg = config(ai_enabled=False)
        memory = new_memory(cfg)
        client = pr_client()
        run(client, cfg, memory, NOW)
        summary = run(client, cfg, memory, NOW + dt.timedelta(hours=2))
        self.assertFalse(summary.escalation_plans[0].advanced)
        self.assertIn('waiting', summary.escalation_plans[0].reason)
        self.assertEqual(comment_count(client), 1)

    def test_stage_caps_the_ladder(self) -> None:
        # 4 days inactive -> warning stage -> at most step 2.
        cfg = config(ai_enabled=False, min_days_between_steps=0)
        memory = new_memory(cfg)
        client = pr_client(commit_day=(2026, 9, 21))
        steps = [run(client, cfg, memory, NOW).escalation_plans[0] for _ in range(3)]
        self.assertEqual([plan.step for plan in steps], [1, 2, 2])
        self.assertIn('maximum allowed at stage `warning`', steps[2].reason)

    def test_suppressing_state_does_not_advance_ladder(self) -> None:
        plan = agent.plan_escalation(agent.AgentMemory(), 1, 'final-notice', 'awaiting_reviewer', NOW)
        self.assertFalse(plan.advanced)
        self.assertEqual(plan.step, 0)

    def test_dry_run_plans_but_does_not_mutate(self) -> None:
        cfg = config(ai_enabled=False)
        memory = new_memory(cfg)
        client = pr_client()
        summary = run(client, cfg, memory, NOW, dry_run=True)
        self.assertTrue(summary.escalation_plans[0].advanced)
        self.assertEqual(client.comments, [])
        self.assertEqual(client.added_labels, [])


# ---------------------------------------------------------------------------
# 3. Feedback loop
# ---------------------------------------------------------------------------


class FeedbackLoopTests(unittest.TestCase):
    def test_human_label_removal_is_an_override_that_is_respected(self) -> None:
        cfg = config(ai_enabled=False)
        memory = new_memory(cfg)
        client = pr_client()
        run(client, cfg, memory, NOW)
        self.assertEqual(client._record(10)['labels'], ['stale:final-notice'])

        # A human removes the label without any new activity.
        client.remove_issue_label(10, 'stale:final-notice')
        client.added_labels.clear()
        summary = run(client, cfg, memory, NOW + DAY)

        self.assertIn({'pr_number': 10, 'event': 'label_overridden'}, summary.feedback_events)
        self.assertEqual(summary.overrides_respected, 1)
        self.assertEqual(client.added_labels, [])
        self.assertEqual(memory.metrics['label_overrides'], 1)
        self.assertEqual(memory.pr(10)['last_final_action'], 'respect_human_override')

        # After the cooldown the ladder restarts gently at step 1.
        summary = run(client, cfg, memory, NOW + 8 * DAY)
        self.assertEqual(summary.escalation_plans[0].step, 1)
        self.assertIn((10, ('stale:final-notice',)), client.added_labels)

    def test_exempt_label_added_by_human_counts_as_override(self) -> None:
        cfg = config(ai_enabled=False)
        memory = new_memory(cfg)
        client = pr_client()
        run(client, cfg, memory, NOW)
        record = client._record(10)
        record['labels'].append('no-stale')
        record['payload']['labels'] = [{'name': name} for name in record['labels']]
        summary = run(client, cfg, memory, NOW + DAY)
        self.assertEqual(summary.prs_skipped_exempt, 1)
        self.assertIn({'pr_number': 10, 'event': 'label_overridden'}, summary.feedback_events)

    def test_human_response_after_comment_is_recorded(self) -> None:
        cfg = config(ai_enabled=False)
        memory = new_memory(cfg)
        client = pr_client()
        run(client, cfg, memory, NOW)
        client._record(10)['comments'].append(
            {'body': 'Still on it', 'created_at': (NOW + DAY / 2).isoformat(), 'user': {'login': 'author'}}
        )
        summary = run(client, cfg, memory, NOW + DAY)
        self.assertIn({'pr_number': 10, 'event': 'human_responded'}, summary.feedback_events)
        self.assertEqual(memory.metrics['pings_answered'], 1)
        self.assertEqual(memory.pr(10)['outcomes']['human_responses'], 1)

    def test_reactivated_pr_resets_ladder_and_clears_label(self) -> None:
        cfg = config(ai_enabled=False)
        memory = new_memory(cfg)
        client = pr_client()
        run(client, cfg, memory, NOW)
        run(client, cfg, memory, NOW + DAY)
        self.assertEqual(memory.pr(10)['escalation_step'], 2)

        # New commit -> PR is active again.
        client._record(10)['commit_dates'].append((NOW + DAY + dt.timedelta(hours=1)).isoformat())
        summary = run(client, cfg, memory, NOW + DAY + dt.timedelta(hours=2))

        self.assertIn({'pr_number': 10, 'event': 'reactivated'}, summary.feedback_events)
        self.assertEqual(memory.pr(10)['escalation_step'], 0)
        self.assertEqual(memory.metrics['reactivations'], 1)
        self.assertIn((10, 'stale:final-notice'), client.removed_labels)
        # The agent removed its own label, so it is not later seen as an override.
        self.assertIsNone(memory.pr(10)['last_label'])

    def test_suppression_that_lasts_too_long_expires(self) -> None:
        cfg = config(ai_enabled=True, max_suppression_days=14)
        memory = new_memory(cfg)
        client = pr_client(
            reviewers=False,
            graphql={'reviewThreads': {'nodes': [{'isResolved': False}]}},
        )
        first = run(client, cfg, memory, NOW)
        self.assertEqual(first.ai_decisions[0].final_action, 'suppress_stale_label')
        self.assertEqual(client.added_labels, [])

        later = run(client, cfg, memory, NOW + 14 * DAY)
        decision = later.ai_decisions[0]
        self.assertEqual(decision.final_action, 'add_stale_label')
        self.assertIn('suppression expired after 14 day(s)', decision.reason)
        self.assertEqual(later.suppressions_expired, 1)
        self.assertTrue(later.escalation_plans[0].advanced)
        self.assertIn((10, ('stale:final-notice',)), client.added_labels)
        self.assertEqual(memory.metrics['suppressions_expired'], 1)


# ---------------------------------------------------------------------------
# 4. AI-assisted branch decisions
# ---------------------------------------------------------------------------


ADVISOR = agent.DEFAULT_AGENT_CONFIG['branch_advisor']


def advise(name='feature/x', prs=(), open_prs=(), message='', memory_record=None, delete_candidate=True):
    signals = agent.branch_signals(name, 30, list(prs), list(open_prs), message, memory_record, ADVISOR)
    return agent.advise_branch_heuristic(signals, delete_candidate, ADVISOR)


class BranchAdvisorTests(unittest.TestCase):
    def test_merged_branch_is_deletable(self) -> None:
        advice = advise(prs=[{'state': 'closed', 'merged_at': iso(2026, 8, 1)}])
        self.assertEqual(advice.recommendation, 'delete')
        self.assertIn('work was merged through a PR', advice.reasons)

    def test_branch_used_as_base_by_open_pr_is_kept(self) -> None:
        advice = advise(open_prs=[{'number': 5, 'base': {'ref': 'feature/x'}, 'head': {'ref': 'feature/y'}}])
        self.assertEqual(advice.recommendation, 'keep')

    def test_branch_referenced_by_open_pr_is_kept(self) -> None:
        advice = advise(
            open_prs=[{'number': 6, 'title': 'Follow-up', 'body': 'builds on feature/x', 'head': {'ref': 'feature/z'}}]
        )
        self.assertEqual(advice.recommendation, 'keep')
        # Substring matches of longer names must not count as references.
        advice = advise(
            open_prs=[{'number': 6, 'title': '', 'body': 'see feature/x-two', 'head': {'ref': 'feature/z'}}]
        )
        self.assertEqual(advice.recommendation, 'delete')

    def test_naming_and_commit_semantics(self) -> None:
        self.assertEqual(advise(name='archive/2025').recommendation, 'keep')
        self.assertEqual(advise(message='WIP: half done').recommendation, 'review')
        self.assertEqual(advise(message='please do not delete').recommendation, 'keep')
        spike = advise(name='spike/idea', message='experiment with cache')
        self.assertEqual(spike.recommendation, 'delete')
        self.assertGreater(spike.confidence, 0.7)

    def test_related_pr_labels_and_unmerged_history(self) -> None:
        self.assertEqual(
            advise(prs=[{'state': 'closed', 'labels': [{'name': 'pinned'}]}]).recommendation, 'keep'
        )
        self.assertEqual(advise(prs=[{'state': 'closed'}]).recommendation, 'review')

    def test_previously_restored_branch_is_kept(self) -> None:
        self.assertEqual(advise(memory_record={'restored': True}).recommendation, 'keep')

    def test_not_yet_delete_candidate_is_never_delete(self) -> None:
        self.assertNotEqual(advise(delete_candidate=False).recommendation, 'delete')

    def test_ai_cannot_delete_without_agreement(self) -> None:
        heuristic = agent.BranchAdvice('b', 'review', 0.7, ['wip'])
        ai = agent.BranchAdvice('b', 'delete', 0.95, ['old'], provider='copilot_cli')
        self.assertEqual(agent.combine_branch_advice(heuristic, ai, 0.8).recommendation, 'review')
        heuristic = agent.BranchAdvice('b', 'delete', 0.8, ['merged'])
        weak = agent.BranchAdvice('b', 'delete', 0.5, ['old'], provider='copilot_cli')
        self.assertEqual(agent.combine_branch_advice(heuristic, weak, 0.8).recommendation, 'review')

    def test_ai_can_make_decision_more_cautious(self) -> None:
        heuristic = agent.BranchAdvice('b', 'delete', 0.8, ['merged'])
        ai = agent.BranchAdvice('b', 'keep', 0.9, ['referenced in issue'], provider='copilot_cli')
        combined = agent.combine_branch_advice(heuristic, ai, 0.8)
        self.assertEqual(combined.recommendation, 'keep')
        self.assertEqual(combined.provider, 'copilot_cli')

    def test_normalize_branch_ai_result_is_defensive(self) -> None:
        advice = agent.normalize_branch_ai_result('b', {'recommendation': 'nuke', 'confidence': 7, 'reason': 'x'})
        self.assertEqual(advice.recommendation, 'review')
        self.assertEqual(advice.confidence, 1.0)
        with self.assertRaises(ValueError):
            agent.normalize_branch_ai_result('b', {'confidence': 'high'})

    def test_branch_prompt_marks_content_untrusted(self) -> None:
        prompt = agent.build_branch_ai_prompt({'branch_name': 'b'})
        self.assertIn('untrusted', prompt)
        self.assertIn('keep, delete, review', prompt)


class BranchProcessingTests(unittest.TestCase):
    def build(self):
        base = fixtures.build_fake_client()
        client = StatefulFakeClient(base._pull_requests, base._branches, base._branch_prs, base._commit_dates)
        return client

    def run_branches(self, client, cfg, memory, now=NOW, dry_run=False):
        summary = module.RunSummary(run_mode='apply')
        module.process_branches(client, cfg, now, dry_run, summary, 'main', memory)
        return summary

    def test_advisor_agrees_and_branch_is_deleted_and_remembered(self) -> None:
        cfg = config()
        memory = new_memory(cfg)
        client = self.build()
        summary = self.run_branches(client, cfg, memory)
        self.assertEqual(client.deleted_branches, ['feature/old'])
        advice = {item.branch_name: item for item in summary.branch_advice}
        self.assertEqual(advice['feature/old'].recommendation, 'delete')
        self.assertIsNotNone(memory.branch('feature/old')['deleted_at'])
        self.assertEqual(memory.metrics['branches_deleted'], 1)

    def test_advisor_blocks_deletion_of_unfinished_work(self) -> None:
        cfg = config()
        memory = new_memory(cfg)
        client = self.build()
        client.commit_messages['sha-old'] = 'WIP: do not merge yet'
        summary = self.run_branches(client, cfg, memory)
        self.assertEqual(client.deleted_branches, [])
        self.assertEqual(summary.deletions_blocked_by_advisor, ['feature/old'])
        risks = {item.branch_name: item for item in summary.branch_risk_assessments}
        self.assertIn('advisor recommends review', risks['feature/old'].reason)

    def test_restored_branch_is_detected_and_never_deleted_again(self) -> None:
        cfg = config()
        memory = new_memory(cfg)
        client = self.build()
        self.run_branches(client, cfg, memory)
        # The fake still lists the branch -> a human restored it.
        client.deleted_branches.clear()
        summary = self.run_branches(client, cfg, memory, NOW + DAY)
        self.assertIn({'branch': 'feature/old', 'event': 'branch_restored'}, summary.feedback_events)
        self.assertEqual(client.deleted_branches, [])
        self.assertIn('feature/old', summary.deletions_blocked_by_advisor)
        self.assertEqual(memory.metrics['branches_restored'], 1)

        # The optimiser learns caution from the incident (bounded).
        changes = agent.optimize_policy(memory, NOW + DAY)
        self.assertTrue(any('restored' in change for change in changes))
        self.assertEqual(memory.policy['branch_extra_days'], 2)

    def test_learned_caution_delays_deletion(self) -> None:
        cfg = config()
        memory = new_memory(cfg)
        memory.policy['branch_extra_days'] = 60  # feature/old is 55 days old
        client = self.build()
        summary = self.run_branches(client, cfg, memory)
        self.assertEqual(client.deleted_branches, [])
        self.assertIn('feature/old', summary.deletions_blocked_by_advisor)

    def test_copilot_branch_advice_failure_falls_back(self) -> None:
        cfg = config()
        cfg['ai_config']['ai_provider'] = 'copilot_cli'
        memory = new_memory(cfg)
        client = self.build()
        original = module.evaluate_branch_with_copilot_cli

        def boom(signals, ai_config):
            raise RuntimeError('copilot unavailable')

        module.evaluate_branch_with_copilot_cli = boom
        try:
            summary = self.run_branches(client, cfg, memory, dry_run=True)
        finally:
            module.evaluate_branch_with_copilot_cli = original
        advice = summary.branch_advice[0]
        self.assertEqual(advice.provider, 'heuristic')
        self.assertIn('copilot unavailable', advice.fallback_reason)

    def test_copilot_branch_advice_can_keep(self) -> None:
        cfg = config()
        cfg['ai_config']['ai_provider'] = 'copilot_cli'
        memory = new_memory(cfg)
        client = self.build()
        original = module.evaluate_branch_with_copilot_cli
        module.evaluate_branch_with_copilot_cli = lambda signals, ai_config: agent.BranchAdvice(
            signals['branch_name'], 'keep', 0.9, ['referenced in roadmap'], provider='copilot_cli'
        )
        try:
            self.run_branches(client, cfg, memory)
        finally:
            module.evaluate_branch_with_copilot_cli = original
        self.assertEqual(client.deleted_branches, [])

    def test_without_memory_behaviour_is_unchanged(self) -> None:
        client = self.build()
        client.commit_messages['sha-old'] = 'WIP'
        summary = self.run_branches(client, config(), None)
        self.assertEqual(client.deleted_branches, ['feature/old'])
        self.assertEqual(summary.branch_advice, [])
        self.assertFalse(summary.agent_enabled)


# ---------------------------------------------------------------------------
# 5. Goal-based optimisation
# ---------------------------------------------------------------------------


class OptimizationTests(unittest.TestCase):
    def test_false_stale_labels_lower_suppression_bar_within_bounds(self) -> None:
        memory = agent.AgentMemory()
        for round_ in range(1, 6):
            memory.metrics['labels_applied'] = 10 * round_
            memory.metrics['label_overrides'] = 5 * round_
            agent.optimize_policy(memory, NOW)
        self.assertEqual(memory.policy['confidence_offset'], -0.1)
        self.assertAlmostEqual(memory.effective_confidence_threshold(0.8), 0.7)
        self.assertEqual(memory.effective_confidence_threshold(0.52), 0.5)

    def test_adjustment_needs_new_samples(self) -> None:
        memory = agent.AgentMemory()
        memory.metrics.update(labels_applied=10, label_overrides=5)
        agent.optimize_policy(memory, NOW)
        agent.optimize_policy(memory, NOW)  # no new samples -> no second step
        self.assertEqual(memory.policy['confidence_offset'], -0.05)

    def test_unanswered_pings_slow_the_ladder(self) -> None:
        memory = agent.AgentMemory()
        for round_ in range(1, 20):
            memory.metrics['pings_sent'] = 5 * round_
            agent.optimize_policy(memory, NOW)
        self.assertEqual(memory.policy['step_spacing_days'], 7)

    def test_long_suppressions_shorten_window_to_floor(self) -> None:
        memory = agent.AgentMemory()
        for round_ in range(1, 10):
            memory.metrics['suppressions'] = 5 * round_
            memory.metrics['suppressions_expired'] = 5 * round_
            agent.optimize_policy(memory, NOW)
        self.assertEqual(memory.effective_max_suppression_days(), 7)

    def test_branch_caution_is_capped(self) -> None:
        memory = agent.AgentMemory()
        memory.metrics['branches_restored'] = 100
        agent.optimize_policy(memory, NOW)
        self.assertEqual(memory.policy['branch_extra_days'], 30)

    def test_optimization_can_be_disabled(self) -> None:
        memory = agent.AgentMemory(None, {'optimization': {'enabled': False}})
        memory.metrics.update(labels_applied=50, label_overrides=50)
        self.assertEqual(agent.optimize_policy(memory, NOW), [])

    def test_goal_metrics(self) -> None:
        memory = agent.AgentMemory()
        memory.metrics.update(labels_applied=10, label_overrides=2, escalations_started=4, reactivations=1)
        goals = agent.goal_metrics(memory)
        self.assertEqual(goals['false_stale_label_rate'], 0.2)
        self.assertEqual(goals['reactivation_rate'], 0.25)
        self.assertIsNone(goals['ping_response_rate'])


# ---------------------------------------------------------------------------
# Config, reporting, and run finalisation
# ---------------------------------------------------------------------------


class AgentConfigAndReportTests(unittest.TestCase):
    def test_repo_config_has_valid_agent_config(self) -> None:
        repo_config = module.load_config(str(HERE.parent / 'stale-cleaner.json'))
        self.assertIn('agent_config', repo_config)
        self.assertEqual(module.validate_config(repo_config), [])

    def test_invalid_agent_config_is_rejected(self) -> None:
        cfg = config()
        cfg['agent_config']['min_days_between_steps'] = -1
        cfg['agent_config']['optimization'] = {'max_confidence_offset': 0.9}
        errors = module.validate_config(cfg)
        self.assertTrue(any('min_days_between_steps' in error for error in errors))
        self.assertTrue(any('max_confidence_offset' in error for error in errors))
        cfg['agent_config'] = 'nope'
        self.assertIn('agent_config must be an object', module.validate_config(cfg))

    def test_report_and_summary_include_agent_block(self) -> None:
        cfg = config(ai_enabled=False)
        memory = new_memory(cfg)
        client = pr_client()
        summary = run(client, cfg, memory, NOW)
        module.finalize_agent_run(memory, summary, NOW)
        payload = module.summary_as_dict(summary)
        block = payload['agent']
        self.assertTrue(block['enabled'])
        self.assertEqual(block['escalation_plans'][0]['name'], 'soft_warning')
        self.assertIn('false_stale_label_rate', block['goal_metrics'])
        self.assertEqual(block['policy']['step_spacing_days'], 1)
        json.dumps(payload)  # must be serialisable
        lines = '\n'.join(module.agent_summary_lines(summary))
        self.assertIn('## Agent Memory & Planning', lines)
        self.assertIn('advanced to step 1', lines)
        self.assertEqual(memory.metrics['runs'], 1)

    def test_agent_disabled_in_config_ignores_memory(self) -> None:
        cfg = config(ai_enabled=False, enabled=False)
        memory = new_memory(cfg)
        client = pr_client()
        summary = run(client, cfg, memory, NOW)
        self.assertFalse(summary.agent_enabled)
        self.assertEqual(summary.escalation_plans, [])
        self.assertEqual(memory.data['pull_requests'], {})


if __name__ == '__main__':
    raise SystemExit(unittest.main())
