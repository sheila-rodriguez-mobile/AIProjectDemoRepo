#!/usr/bin/env python3

from __future__ import annotations

import argparse
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

DEFAULT_REPORT_PATH = Path('.github/stale-cleaner-report.json')
DEFAULT_HISTORY_PATH = Path('.github/stale-cleaner-history.jsonl')
MAX_HISTORY_ENTRIES = 30
CATEGORY_PALETTE = ['#60a5fa', '#a78bfa', '#34d399', '#f59e0b', '#f87171', '#22d3ee', '#fb7185']


def load_report(report_path: Path) -> dict[str, Any] | None:
    if not report_path.exists():
        return None
    return json.loads(report_path.read_text(encoding='utf-8'))


def load_history(history_path: Path, max_entries: int = MAX_HISTORY_ENTRIES) -> list[dict[str, Any]]:
    if not history_path.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in history_path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items[-max_entries:]


def render_list(items: list[str]) -> str:
    if not items:
        return '<li class="empty">None</li>'
    return ''.join(f'<li>{html.escape(item)}</li>' for item in items)


def prettify_category(category: str) -> str:
    return category.replace('-', ' ')


def category_color(index: int) -> str:
    return CATEGORY_PALETTE[index % len(CATEGORY_PALETTE)]


def collect_ai_category_counts(decisions: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for decision in decisions:
        category = str(decision.get('category', 'unknown')).strip() or 'unknown'
        counts[category] = counts.get(category, 0) + 1
    return counts


def stale_totals(report: dict[str, Any]) -> tuple[int, int, int, int]:
    counts = report.get('stale_counts', {}) or {}
    active = int(counts.get('active', 0))
    warning = int(counts.get('warning', 0))
    escalated = int(counts.get('escalated', 0))
    final_notice = int(counts.get('final-notice', 0))
    return active, warning, escalated, final_notice


def derive_metrics(report: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    active, warning, escalated, final_notice = stale_totals(report)
    total_stale = warning + escalated + final_notice
    prs_processed = int(report.get('prs_processed', 0))
    ai = report.get('ai', {}) or {}
    ai_reviewed = int(ai.get('reviewed', 0))
    ai_fallbacks = int(ai.get('fallbacks', 0))
    ai_suppressed = int(ai.get('suppressed', 0))
    decisions = ai.get('decisions', []) or []
    delete_candidates = len(report.get('delete_candidates', []))
    stale_branches = len(report.get('stale_branches', []))
    protected_branches = len(report.get('protected_by_labels', []))
    ai_coverage = (ai_reviewed / total_stale * 100.0) if total_stale else 0.0
    fallback_rate = (ai_fallbacks / ai_reviewed * 100.0) if ai_reviewed else 0.0
    stale_share = (total_stale / prs_processed * 100.0) if prs_processed else 0.0

    recent_history = history[-7:]
    average_stale = (
        sum(
            int((item.get('stale_counts', {}) or {}).get('warning', 0))
            + int((item.get('stale_counts', {}) or {}).get('escalated', 0))
            + int((item.get('stale_counts', {}) or {}).get('final-notice', 0))
            for item in recent_history
        ) / len(recent_history)
        if recent_history
        else 0.0
    )
    delta_vs_recent = total_stale - average_stale

    insights: list[str] = []
    if final_notice > 0:
        insights.append(f'{final_notice} PR(s) are at final notice and need immediate action.')
    elif escalated > 0:
        insights.append(f'{escalated} PR(s) are escalated and should be reviewed soon.')
    else:
        insights.append('No escalated or final-notice PRs in the latest run.')

    if delete_candidates > 0:
        insights.append(f'{delete_candidates} branch(es) are eligible for deletion if still safe to remove.')
    else:
        insights.append('No branches are currently marked as delete candidates.')

    if ai_reviewed == 0:
        insights.append('AI did not review any stale PRs in this run.')
    elif ai_fallbacks > 0:
        insights.append(f'AI fallback rate is {fallback_rate:.1f}%; verify local model availability if you expected full AI coverage.')
    else:
        insights.append(f'AI reviewed {ai_reviewed} stale PR(s) with no fallbacks.')

    category_counts = collect_ai_category_counts(decisions)

    if category_counts:
        top_category, top_count = max(category_counts.items(), key=lambda item: item[1])
        insights.append(f'Most common AI category is {top_category} with {top_count} decision(s).')

    if history:
        direction = 'above' if delta_vs_recent > 0 else 'below'
        insights.append(f'Total stale PRs are {abs(delta_vs_recent):.1f} {direction} the recent-run average.')

    return {
        'active': active,
        'warning': warning,
        'escalated': escalated,
        'final_notice': final_notice,
        'total_stale': total_stale,
        'prs_processed': prs_processed,
        'ai_reviewed': ai_reviewed,
        'ai_fallbacks': ai_fallbacks,
        'ai_suppressed': ai_suppressed,
        'delete_candidates': delete_candidates,
        'stale_branches': stale_branches,
        'protected_branches': protected_branches,
        'ai_coverage': ai_coverage,
        'fallback_rate': fallback_rate,
        'stale_share': stale_share,
        'ai_category_counts': category_counts,
        'average_stale': average_stale,
        'delta_vs_recent': delta_vs_recent,
        'insights': insights,
    }


def render_cards(report: dict[str, Any], metrics: dict[str, Any]) -> str:
    cards = [
        ('Run mode', report.get('run_mode', 'unknown')),
        ('PRs processed', str(metrics['prs_processed'])),
        ('Total stale PRs', str(metrics['total_stale'])),
        ('Final notice PRs', str(metrics['final_notice'])),
        ('Delete candidates', str(metrics['delete_candidates'])),
        ('AI reviewed', str(metrics['ai_reviewed'])),
        ('AI fallback rate', f"{metrics['fallback_rate']:.1f}%"),
        ('Protected branches', str(metrics['protected_branches'])),
    ]
    return ''.join(
        '<div class="card"><div class="label">{}</div><div class="value">{}</div></div>'.format(
            html.escape(label), html.escape(value)
        )
        for label, value in cards
    )


def render_stale_counts(report: dict[str, Any]) -> str:
    counts = report.get('stale_counts', {}) or {}
    labeled = report.get('labeled_counts')
    held_by_stage: dict[str, int] = {}
    for item in report.get('held_prs', []) or []:
        stage = str(item.get('stage', ''))
        held_by_stage[stage] = held_by_stage.get(stage, 0) + 1
    verb = 'to label' if report.get('run_mode') == 'dry-run' else 'labeled'
    cells = []
    for stage, value in counts.items():
        detail = ''
        # Older reports have no labeled_counts; keep their original rendering.
        if labeled is not None and stage != 'active':
            detail = '<div class="count-detail">{} {} · {} held</div>'.format(
                html.escape(str(labeled.get(stage, 0))),
                html.escape(verb),
                html.escape(str(held_by_stage.get(stage, 0))),
            )
        cells.append(
            '<div class="count"><span>{}</span><strong>{}</strong>{}</div>'.format(
                html.escape(stage.replace('-', ' ')), html.escape(str(value)), detail
            )
        )
    return ''.join(cells)


def render_context_labels(report: dict[str, Any]) -> str:
    counts = report.get('context_label_counts', {}) or {}
    if not counts:
        return ''
    chips = ''.join(
        '<span class="category-badge" style="border-color:#475569;">{} · {}</span>'.format(
            html.escape(str(label)), html.escape(str(count))
        )
        for label, count in sorted(counts.items())
    )
    return (
        '<div class="count-note">Context labels added next to the stage label:</div>'
        '<div class="category-badges">' + chips + '</div>'
    )


def render_held_prs(report: dict[str, Any]) -> str:
    held = report.get('held_prs', []) or []
    if not held:
        return ''
    items = ''.join(
        '<li>PR #{} ({}) · {} → {} — {}</li>'.format(
            html.escape(str(item.get('pr_number', '?'))),
            html.escape(str(item.get('stage', ''))),
            html.escape(str(item.get('state', ''))),
            html.escape(str(item.get('action', ''))),
            html.escape(str(item.get('reason', ''))),
        )
        for item in held
    )
    return (
        '<div class="count-note">Counts are by days inactive. These PRs reached a stale '
        'stage but were held without a label (human override):</div><ul>' + items + '</ul>'
    )


def render_insights(metrics: dict[str, Any]) -> str:
    return ''.join(f'<li>{html.escape(item)}</li>' for item in metrics['insights'])


def render_ai_decisions(report: dict[str, Any]) -> str:
    decisions = report.get('ai', {}).get('decisions', []) or []
    if not decisions:
        return '<li class="empty">No AI decisions recorded</li>'
    items = []
    for decision in decisions:
        suffix = ''
        if decision.get('fallback_reason'):
            suffix = f"<div class='decision-fallback'>Fallback: {html.escape(str(decision['fallback_reason']))}</div>"
        items.append(
            "<li><div class='decision-title'>PR #{pr_number} · {provider} · {category} · {confidence}</div>"
            "<div class='decision-meta'>Baseline: {baseline} · Final action: {action}</div>"
            "<div class='decision-reason'>{reason}</div>{suffix}</li>".format(
                pr_number=html.escape(str(decision.get('pr_number', 'unknown'))),
                provider=html.escape(str(decision.get('provider', 'unknown'))),
                category=html.escape(str(decision.get('category', 'unknown'))),
                confidence=html.escape(f"{float(decision.get('confidence', 0.0)):.1%}"),
                baseline=html.escape(str(decision.get('baseline_stage', 'unknown'))),
                action=html.escape(str(decision.get('final_action', 'unknown'))),
                reason=html.escape(str(decision.get('reason', ''))),
                suffix=suffix,
            )
        )
    return ''.join(items)


def filtered_ai_decisions(report: dict[str, Any], selected_category: str) -> list[dict[str, Any]]:
    decisions = report.get('ai', {}).get('decisions', []) or []
    if not selected_category or selected_category == 'all':
        return decisions
    return [
        decision
        for decision in decisions
        if str(decision.get('category', '')).strip().lower() == selected_category.strip().lower()
    ]


def render_ai_filter_controls(report: dict[str, Any], selected_category: str) -> str:
    category_counts = collect_ai_category_counts(report.get('ai', {}).get('decisions', []) or [])
    if not category_counts:
        return '<div class="empty">No AI categories available for filtering yet.</div>'

    options = ['<option value="all">All categories</option>']
    badges = []
    for index, (category, count) in enumerate(sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))):
        selected = ' selected' if category == selected_category else ''
        options.append(
            f'<option value="{html.escape(category)}"{selected}>{html.escape(prettify_category(category))} ({count})</option>'
        )
        badges.append(
            "<span class='category-badge' style='border-color:{color}; color:{color};'>{label}: {count}</span>".format(
                color=html.escape(category_color(index)),
                label=html.escape(prettify_category(category)),
                count=html.escape(str(count)),
            )
        )

    return """
        <form method='get' class='filter-form'>
          <label for='category'>Filter AI decisions</label>
          <select id='category' name='category' onchange='this.form.submit()'>
            {options}
          </select>
          <noscript><button type='submit'>Apply</button></noscript>
        </form>
        <div class='category-badges'>{badges}</div>
    """.format(options=''.join(options), badges=''.join(badges))


def render_filtered_ai_decisions(decisions: list[dict[str, Any]], selected_category: str) -> str:
    if not decisions:
        if selected_category and selected_category != 'all':
            return '<li class="empty">No AI decisions match the selected category.</li>'
        return '<li class="empty">No AI decisions recorded</li>'
    items = []
    for decision in decisions:
        suffix = ''
        if decision.get('fallback_reason'):
            suffix = f"<div class='decision-fallback'>Fallback: {html.escape(str(decision['fallback_reason']))}</div>"
        items.append(
            "<li><div class='decision-title'>PR #{pr_number} · {provider} · {category} · {confidence}</div>"
            "<div class='decision-meta'>Baseline: {baseline} · Final action: {action}</div>"
            "<div class='decision-reason'>{reason}</div>{suffix}</li>".format(
                pr_number=html.escape(str(decision.get('pr_number', 'unknown'))),
                provider=html.escape(str(decision.get('provider', 'unknown'))),
                category=html.escape(str(decision.get('category', 'unknown'))),
                confidence=html.escape(f"{float(decision.get('confidence', 0.0)):.1%}"),
                baseline=html.escape(str(decision.get('baseline_stage', 'unknown'))),
                action=html.escape(str(decision.get('final_action', 'unknown'))),
                reason=html.escape(str(decision.get('reason', ''))),
                suffix=suffix,
            )
        )
    return ''.join(items)


def render_ai_category_chart(metrics: dict[str, Any]) -> str:
    category_counts = metrics.get('ai_category_counts', {}) or {}
    if not category_counts:
        return "<div class='chart-block'><h3>AI decision categories</h3><div class='empty'>No AI categories recorded for this run.</div></div>"

    palette = [
        '#60a5fa',
        '#a78bfa',
        '#34d399',
        '#f59e0b',
        '#f87171',
        '#22d3ee',
        '#fb7185',
    ]
    items = sorted(category_counts.items(), key=lambda item: (-item[1], item[0].lower()))
    chart_data = [
        (label.replace('-', ' '), float(value), palette[index % len(palette)])
        for index, (label, value) in enumerate(items)
    ]
    return render_bar_chart('AI decision categories', chart_data)


def render_ai_category_donut(metrics: dict[str, Any]) -> str:
    category_counts = metrics.get('ai_category_counts', {}) or {}
    if not category_counts:
        return "<div class='chart-block'><h3>AI category share</h3><div class='empty'>No AI category distribution yet.</div></div>"

    items = sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))
    total = sum(category_counts.values()) or 1
    cx = cy = 90
    radius = 58
    circumference = 2 * 3.141592653589793 * radius
    offset = 0.0
    segments = []
    legend = []
    for index, (category, count) in enumerate(items):
        color = category_color(index)
        length = circumference * (count / total)
        segments.append(
            "<circle cx='{cx}' cy='{cy}' r='{r}' fill='none' stroke='{color}' stroke-width='18' stroke-dasharray='{length:.2f} {rest:.2f}' stroke-dashoffset='-{offset:.2f}' transform='rotate(-90 {cx} {cy})' />".format(
                cx=cx,
                cy=cy,
                r=radius,
                color=html.escape(color),
                length=length,
                rest=max(circumference - length, 0.0),
                offset=offset,
            )
        )
        legend.append(
            "<div class='legend-item'><span class='legend-swatch' style='background:{color};'></span>{label} ({count})</div>".format(
                color=html.escape(color),
                label=html.escape(prettify_category(category)),
                count=html.escape(str(count)),
            )
        )
        offset += length

    return """
      <div class='chart-block'>
        <h3>AI category share</h3>
        <div class='donut-wrap'>
          <svg viewBox='0 0 180 180' class='donut-chart' role='img' aria-label='AI category share'>
            <circle cx='90' cy='90' r='58' fill='none' stroke='#1e293b' stroke-width='18' />
            {segments}
            <text x='90' y='84' text-anchor='middle' class='donut-total'>{total}</text>
            <text x='90' y='102' text-anchor='middle' class='donut-subtitle'>AI decisions</text>
          </svg>
          <div class='legend donut-legend'>{legend}</div>
        </div>
      </div>
    """.format(segments=''.join(segments), total=html.escape(str(total)), legend=''.join(legend))


def render_ai_category_trend_chart(history: list[dict[str, Any]]) -> str:
    if not history:
        return "<div class='chart-block'><h3>AI category trends</h3><div class='empty'>Run the cleaner a few times to unlock category trends.</div></div>"

    recent = history[-10:]
    aggregate: dict[str, int] = {}
    for item in recent:
        counts = collect_ai_category_counts((item.get('ai', {}) or {}).get('decisions', []) or [])
        for category, count in counts.items():
            aggregate[category] = aggregate.get(category, 0) + count

    top_categories = [category for category, _ in sorted(aggregate.items(), key=lambda item: (-item[1], item[0]))[:3]]
    if not top_categories:
        return "<div class='chart-block'><h3>AI category trends</h3><div class='empty'>No AI decision history available yet.</div></div>"

    labels = [str(index) for index, _ in enumerate(recent, start=1)]
    series: list[tuple[str, list[int], str]] = []
    for index, category in enumerate(top_categories):
        color = category_color(index)
        points = []
        for item in recent:
            counts = collect_ai_category_counts((item.get('ai', {}) or {}).get('decisions', []) or [])
            points.append(int(counts.get(category, 0)))
        series.append((category, points, color))

    max_value = max((max(points) for _, points, _ in series if points), default=1) or 1
    width = 640
    height = 220
    left = 28
    bottom = 28
    top = 18
    right = 18
    inner_width = width - left - right
    inner_height = height - top - bottom

    def point_string(points: list[int]) -> str:
        if len(points) == 1:
            x = left + inner_width / 2
            y = top + inner_height - (points[0] / max_value * inner_height)
            return f"{x:.1f},{y:.1f}"
        coords = []
        for idx, value in enumerate(points):
            x = left + (idx / (len(points) - 1)) * inner_width
            y = top + inner_height - (value / max_value * inner_height)
            coords.append(f"{x:.1f},{y:.1f}")
        return ' '.join(coords)

    grid_lines = ''.join(
        f"<line x1='{left}' y1='{top + inner_height * step / 4:.1f}' x2='{width - right}' y2='{top + inner_height * step / 4:.1f}' class='grid-line' />"
        for step in range(5)
    )
    label_marks = ''.join(
        f"<text x='{left + (idx / max(1, len(labels) - 1)) * inner_width:.1f}' y='{height - 6}' class='axis-label'>{html.escape(label)}</text>"
        for idx, label in enumerate(labels)
    )
    polylines = ''.join(
        f"<polyline points='{point_string(points)}' fill='none' stroke='{color}' stroke-width='3' stroke-linecap='round' stroke-linejoin='round' />"
        for _, points, color in series
    )
    legend = ''.join(
        f"<div class='legend-item'><span class='legend-swatch' style='background:{html.escape(color)};'></span>{html.escape(prettify_category(name))}</div>"
        for name, _, color in series
    )
    return f"""
      <div class='chart-block'>
        <h3>AI category trends</h3>
        <svg viewBox='0 0 {width} {height}' class='trend-chart' role='img' aria-label='AI category trends'>
          {grid_lines}
          <line x1='{left}' y1='{top + inner_height}' x2='{width - right}' y2='{top + inner_height}' class='axis-line' />
          {polylines}
          {label_marks}
        </svg>
        <div class='legend'>{legend}</div>
        <div class='chart-footnote'>Tracks the top AI categories across recent runs.</div>
      </div>
    """


def render_bar_chart(title: str, data: list[tuple[str, float, str]]) -> str:
    maximum = max((value for _, value, _ in data), default=0.0) or 1.0
    rows = []
    for label, value, color in data:
        width = (value / maximum) * 100.0
        rows.append(
            "<div class='bar-row'><div class='bar-label'>{label}</div><div class='bar-track'><div class='bar-fill' style='width:{width:.2f}%; background:{color};'></div></div><div class='bar-value'>{value}</div></div>".format(
                label=html.escape(label),
                width=width,
                color=html.escape(color),
                value=html.escape(f'{value:.1f}' if isinstance(value, float) and not value.is_integer() else str(int(value) if float(value).is_integer() else value)),
            )
        )
    return f"<div class='chart-block'><h3>{html.escape(title)}</h3>{''.join(rows)}</div>"


def render_trend_chart(history: list[dict[str, Any]]) -> str:
    if not history:
        return "<div class='empty'>Run the cleaner a few times to unlock trend graphics.</div>"

    recent = history[-10:]
    stale_points = []
    delete_points = []
    fallback_points = []
    labels = []
    for index, item in enumerate(recent, start=1):
        counts = item.get('stale_counts', {}) or {}
        stale_total = int(counts.get('warning', 0)) + int(counts.get('escalated', 0)) + int(counts.get('final-notice', 0))
        stale_points.append(stale_total)
        delete_points.append(len(item.get('delete_candidates', [])))
        fallback_points.append(int((item.get('ai', {}) or {}).get('fallbacks', 0)))
        labels.append(str(index))

    series = [
        ('Stale PRs', stale_points, '#60a5fa'),
        ('Delete candidates', delete_points, '#f59e0b'),
        ('AI fallbacks', fallback_points, '#f87171'),
    ]
    max_value = max((max(points) for _, points, _ in series if points), default=1) or 1
    width = 640
    height = 220
    left = 28
    bottom = 28
    top = 18
    right = 18
    inner_width = width - left - right
    inner_height = height - top - bottom

    def point_string(points: list[int]) -> str:
        if len(points) == 1:
            x = left + inner_width / 2
            y = top + inner_height - (points[0] / max_value * inner_height)
            return f"{x:.1f},{y:.1f}"
        coords = []
        for idx, value in enumerate(points):
            x = left + (idx / (len(points) - 1)) * inner_width
            y = top + inner_height - (value / max_value * inner_height)
            coords.append(f"{x:.1f},{y:.1f}")
        return ' '.join(coords)

    grid_lines = ''.join(
        f"<line x1='{left}' y1='{top + inner_height * step / 4:.1f}' x2='{width - right}' y2='{top + inner_height * step / 4:.1f}' class='grid-line' />"
        for step in range(5)
    )
    label_marks = ''.join(
        f"<text x='{left + (idx / max(1, len(labels) - 1)) * inner_width:.1f}' y='{height - 6}' class='axis-label'>{html.escape(label)}</text>"
        for idx, label in enumerate(labels)
    )
    polylines = ''.join(
        f"<polyline points='{point_string(points)}' fill='none' stroke='{color}' stroke-width='3' stroke-linecap='round' stroke-linejoin='round' />"
        for _, points, color in series
    )
    legend = ''.join(
        f"<div class='legend-item'><span class='legend-swatch' style='background:{html.escape(color)};'></span>{html.escape(name)}</div>"
        for name, _, color in series
    )
    return f"""
        <div class='chart-block'>
          <h3>Recent-run trends</h3>
          <svg viewBox='0 0 {width} {height}' class='trend-chart' role='img' aria-label='Recent run trends'>
            {grid_lines}
            <line x1='{left}' y1='{top + inner_height}' x2='{width - right}' y2='{top + inner_height}' class='axis-line' />
            {polylines}
            {label_marks}
          </svg>
          <div class='legend'>{legend}</div>
          <div class='chart-footnote'>Each point represents one cleaner run from the local history file.</div>
        </div>
    """


# ---------------------------------------------------------------------------
# Team dashboard helpers (tables and graphics)
# ---------------------------------------------------------------------------

STAGE_ORDER = ['active', 'warning', 'escalated', 'final-notice']
STAGE_COLORS = {
    'active': '#34d399',
    'warning': '#fbbf24',
    'escalated': '#fb923c',
    'final-notice': '#f87171',
}
STATE_COLORS = {
    'stale': '#f87171',
    'active_discussion': '#34d399',
    'awaiting_external': '#22d3ee',
    'awaiting_reviewer': '#fbbf24',
    'blocked': '#fb7185',
    'candidate_for_closure': '#a78bfa',
    'active': '#34d399',
}
RISK_COLORS = {
    'likely_safe_to_delete': '#f87171',
    'maybe_preserve': '#34d399',
    'requires_review': '#60a5fa',
}
ADVICE_COLORS = {'keep': '#34d399', 'review': '#fbbf24', 'delete': '#f87171'}
LADDER_STEPS = [
    (1, 'soft_warning', 'Soft warning', 'author'),
    (2, 'ping_reviewers', 'Ping reviewers', 'reviewers'),
    (3, 'ping_team', 'Ping team', 'owning team + extras'),
    (4, 'recommend_close', 'Recommend close', 'author'),
    (5, 'branch_recommendation', 'Branch recommendation', 'author'),
]
# Defaults and bounds of the agent's self-tuning (see agent_config.optimization).
GOAL_TARGETS = {
    'false_stale_label_rate': ('False stale label rate', 'max', 0.20, 'Stale labels removed by humans'),
    'ping_response_rate': ('Ping response rate', 'min', 0.20, 'Pinged people who responded'),
    'reactivation_rate': ('Reactivation rate', None, None, 'Escalated PRs that became active'),
    'suppression_expiry_rate': ('Suppression expiry rate', 'max', 0.30, 'Suppressions that lasted too long'),
    'accidental_deletion_rate': ('Accidental deletion rate', 'max', 0.0, 'Deleted branches restored by humans'),
    'accidental_deletions': ('Accidental deletions', 'max', 0, 'Branches restored after deletion'),
}
POLICY_ROWS = [
    ('step_spacing_days', 'Days between escalation steps', 1, 'max 7', lambda v: f'{int(v)} day(s)'),
    ('confidence_offset', 'Suppression confidence offset', 0.0, 'min -0.10', lambda v: f'{float(v):+.2f}'),
    ('branch_extra_days', 'Extra branch age before deletion', 0, 'max +30', lambda v: f'+{int(v)} day(s)'),
    ('suppression_days_offset', 'Suppression window reduction', 0, 'max 7', lambda v: f'-{int(v)} day(s)'),
]


def esc(value: Any) -> str:
    return html.escape(str(value))


def pretty(value: Any) -> str:
    return str(value or '').replace('_', ' ').replace('-', ' ')


def badge(text: Any, color: str, solid: bool = False) -> str:
    style = (
        f'background:{color}; color:#0b1220; border-color:{color};'
        if solid
        else f'border-color:{color}; color:{color};'
    )
    return f'<span class="badge" style="{style}">{esc(text)}</span>'


def format_time(value: Any) -> str:
    text = str(value or 'unknown')
    return text[:19].replace('T', ' ') + (' UTC' if len(text) >= 19 else '')


def agent_block(report: dict[str, Any]) -> dict[str, Any]:
    return report.get('agent') or {}


def render_mode_badge(report: dict[str, Any]) -> str:
    mode = str(report.get('run_mode', 'unknown'))
    if mode == 'apply':
        return badge('APPLY', '#34d399', solid=True)
    if mode == 'dry-run':
        return badge('DRY RUN · no changes made', '#fbbf24', solid=True)
    return badge(mode, '#94a3b8')


def render_repo_line(report: dict[str, Any]) -> str:
    repo = str(report.get('repository') or '')
    if not repo:
        return 'Repository: n/a'
    return f'Repository: <a href="https://github.com/{esc(repo)}">{esc(repo)}</a>'


def pr_link(report: dict[str, Any], number: Any) -> str:
    repo = str(report.get('repository') or '')
    if repo and str(number).isdigit():
        return f'<a href="https://github.com/{esc(repo)}/pull/{esc(number)}">#{esc(number)}</a>'
    return f'#{esc(number)}'


def branch_link(report: dict[str, Any], name: str) -> str:
    repo = str(report.get('repository') or '')
    if repo:
        return f'<a href="https://github.com/{esc(repo)}/tree/{esc(name)}"><code>{esc(name)}</code></a>'
    return f'<code>{esc(name)}</code>'


def label_coverage(report: dict[str, Any]) -> float:
    counts = report.get('stale_counts', {}) or {}
    stale = sum(int(counts.get(stage, 0)) for stage in STAGE_ORDER[1:])
    labeled = sum(int(v) for v in (report.get('labeled_counts') or {}).values())
    return (labeled / stale) if stale else 0.0


def confidence_bar(value: Any) -> str:
    try:
        number = max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return '<span class="muted">n/a</span>'
    color = '#34d399' if number >= 0.8 else ('#fbbf24' if number >= 0.65 else '#f87171')
    return (
        f'<div class="mini-bar" title="{number:.0%}"><div style="width:{number * 100:.0f}%; '
        f'background:{color};"></div></div><span class="mini-value">{number:.0%}</span>'
    )


def step_meter(step: Any, advanced: bool) -> str:
    try:
        current = int(step)
    except (TypeError, ValueError):
        current = 0
    dots = ''.join(
        f'<span class="dot{" on" if index <= current else ""}"></span>' for index in range(1, 6)
    )
    note = ' <span class="new">new</span>' if advanced else ''
    return f'<span class="dots">{dots}</span> <span class="mini-value">{current}/5</span>{note}'


# ------------------------------------------------------------------ KPIs
def render_kpis(report: dict[str, Any], metrics: dict[str, Any]) -> str:
    agent = agent_block(report)
    labeled = sum(int(v) for v in (report.get('labeled_counts') or {}).values())
    stale = metrics['total_stale']
    context_total = sum(int(v) for v in (report.get('context_label_counts') or {}).values())
    errors = len(report.get('errors', []) or [])
    advanced = sum(1 for plan in agent.get('escalation_plans', []) or [] if plan.get('advanced'))
    verb = 'planned' if report.get('run_mode') == 'dry-run' else 'labelled'
    cards = [
        ('Open PRs analysed', metrics['prs_processed'], f"{report.get('prs_skipped_exempt', 0)} exempt skipped", '#60a5fa'),
        ('Stale PRs', stale, f"{metrics['stale_share']:.0f}% of analysed PRs", '#fbbf24'),
        ('Stale PRs ' + verb, f'{labeled}/{stale}', f'{label_coverage(report):.0%} coverage', '#34d399'),
        ('Final notice', metrics['final_notice'], 'need action now', '#f87171'),
        ('Context labels', context_total, 'why PRs are stuck', '#a78bfa'),
        ('Escalations this run', advanced, 'ladder steps advanced', '#fb923c'),
        ('Comments posted', report.get('comments_posted', 0), f"{report.get('comments_skipped_duplicate', 0)} duplicates skipped", '#22d3ee'),
        ('AI reviewed', metrics['ai_reviewed'], f"{metrics['fallback_rate']:.0f}% fallback", '#a78bfa'),
        ('Branches', report.get('branches_processed', 0), f"{metrics['stale_branches']} stale · {len(report.get('deleted_branches', []) or [])} deleted", '#60a5fa'),
        ('Errors', errors, 'clean run' if not errors else 'see Errors below', '#34d399' if not errors else '#f87171'),
    ]
    return ''.join(
        f'<div class="kpi" style="border-top-color:{color};"><div class="label">{esc(label)}</div>'
        f'<div class="value">{esc(value)}</div><div class="sub">{esc(sub)}</div></div>'
        for label, value, sub, color in cards
    )


def render_extra_insights(report: dict[str, Any]) -> str:
    items: list[str] = []
    context = report.get('context_label_counts') or {}
    if context:
        top, count = max(context.items(), key=lambda item: item[1])
        items.append(f'Most common blocker label: {top} ({count} PR(s)).')
    coverage = label_coverage(report)
    if report.get('labeled_counts') is not None:
        items.append(f'{coverage:.0%} of stale PRs carry a stage label.')
    held = report.get('held_prs') or []
    if held:
        items.append(f'{len(held)} stale PR(s) left unlabelled because a human overrode the label.')
    blocked = agent_block(report).get('deletions_blocked_by_advisor') or []
    if blocked:
        items.append(f'{len(blocked)} branch deletion(s) blocked by the advisor or learned caution.')
    errors = report.get('errors') or []
    if errors:
        items.append(f'{len(errors)} error(s) were recorded; see the Errors panel.')
    return ''.join(f'<li>{esc(item)}</li>' for item in items)


# ------------------------------------------------------------------ PRs
def render_stage_bar(report: dict[str, Any]) -> str:
    counts = report.get('stale_counts', {}) or {}
    total = sum(int(counts.get(stage, 0)) for stage in STAGE_ORDER) or 1
    labeled = report.get('labeled_counts') or {}
    segments = ''.join(
        f'<div class="seg" style="width:{int(counts.get(stage, 0)) / total * 100:.2f}%; '
        f'background:{STAGE_COLORS[stage]};" title="{esc(pretty(stage))}: {int(counts.get(stage, 0))}"></div>'
        for stage in STAGE_ORDER
        if int(counts.get(stage, 0))
    )
    rows = []
    for stage in STAGE_ORDER:
        count = int(counts.get(stage, 0))
        detail = '' if stage == 'active' else f'{int(labeled.get(stage, 0))} labelled'
        rows.append(
            f'<tr><td><span class="swatch" style="background:{STAGE_COLORS[stage]}"></span>{esc(pretty(stage))}</td>'
            f'<td class="num">{count}</td><td class="muted">{esc(detail)}</td></tr>'
        )
    return (
        '<h3>PR stage distribution</h3>'
        f'<div class="stack">{segments}</div>'
        '<table class="compact"><tbody>' + ''.join(rows) + '</tbody></table>'
        '<div class="chart-footnote">0-2 active · 3-5 warning · 6-8 escalated · 9+ final notice (days inactive)</div>'
    )


def render_context_label_chart(report: dict[str, Any]) -> str:
    counts = report.get('context_label_counts') or {}
    if not counts:
        return (
            "<div class='chart-block'><h3>Context labels</h3>"
            "<div class='empty'>No context labels this run (plain inactivity only).</div></div>"
        )
    palette = ['#fb7185', '#fbbf24', '#22d3ee', '#34d399', '#a78bfa', '#60a5fa']
    data = [
        (label, float(count), palette[index % len(palette)])
        for index, (label, count) in enumerate(sorted(counts.items(), key=lambda item: -item[1]))
    ]
    return render_bar_chart('Context labels (why PRs are stuck)', data)


def pr_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}

    def row(number: Any) -> dict[str, Any]:
        key = int(number)
        return rows.setdefault(key, {'pr_number': key, 'labels': [], 'step': None, 'advanced': False})

    for item in report.get('labeled_prs', []) or []:
        row(item['pr_number']).update(item)
    for decision in (report.get('ai', {}) or {}).get('decisions', []) or report.get('ai_decisions', []) or []:
        entry = row(decision.get('pr_number', 0))
        entry.setdefault('stage', decision.get('baseline_stage'))
        entry.setdefault('action', decision.get('final_action'))
        entry.update(
            {
                'state': decision.get('state') or decision.get('category'),
                'confidence': decision.get('confidence'),
                'provider': decision.get('provider'),
                'reason': decision.get('reason'),
                'fallback_reason': decision.get('fallback_reason'),
            }
        )
        if 'days_inactive' not in entry:
            for signal in decision.get('signal_summary', []) or []:
                if str(signal).startswith('days_inactive='):
                    entry['days_inactive'] = int(str(signal).split('=', 1)[1] or 0)
    for item in report.get('held_prs', []) or []:
        entry = row(item['pr_number'])
        entry.update({'stage': item.get('stage'), 'action': item.get('action'), 'held': True})
    for plan in agent_block(report).get('escalation_plans', []) or []:
        entry = row(plan.get('pr_number', 0))
        entry['step'] = plan.get('step')
        entry['step_name'] = plan.get('name')
        entry['advanced'] = bool(plan.get('advanced'))
        entry['branch_recommendation'] = plan.get('branch_recommendation')
    return sorted(
        rows.values(),
        key=lambda item: (
            -STAGE_ORDER.index(item.get('stage')) if item.get('stage') in STAGE_ORDER else 0,
            -int(item.get('days_inactive') or 0),
        ),
    )


def render_pr_table(report: dict[str, Any], selected_state: str = 'all') -> str:
    rows = pr_rows(report)
    if selected_state and selected_state != 'all':
        rows = [item for item in rows if str(item.get('state')) == selected_state]
    if not rows:
        return '<div class="empty">No stale pull requests in this run.</div>'
    stage_labels = {'stale:warning', 'stale:escalated', 'stale:final-notice'}
    body = []
    for item in rows:
        stage = str(item.get('stage') or '')
        state = str(item.get('state') or '')
        labels = ''.join(
            badge(label, STAGE_COLORS.get(label.split(':', 1)[-1], '#94a3b8'), solid=label in stage_labels)
            if label in stage_labels
            else badge(label, '#c4b5fd')
            for label in item.get('labels') or []
        ) or ('<span class="muted">held (override)</span>' if item.get('held') else '<span class="muted">none</span>')
        reason = esc(item.get('reason') or '')
        if item.get('fallback_reason'):
            reason += f'<div class="muted small">Fallback: {esc(item["fallback_reason"])}</div>'
        if item.get('branch_recommendation'):
            reason += f'<div class="muted small">Branch: {esc(item["branch_recommendation"])}</div>'
        title = esc(item.get('title') or '')
        author = esc(item.get('author') or '')
        body.append(
            '<tr>'
            f'<td class="nowrap">{pr_link(report, item["pr_number"])}</td>'
            f'<td>{title}<div class="muted small">{("@" + author) if author else ""}</div></td>'
            f'<td class="num">{esc(item.get("days_inactive", ""))}</td>'
            f'<td>{badge(pretty(stage), STAGE_COLORS.get(stage, "#94a3b8"), solid=True) if stage else ""}</td>'
            f'<td>{badge(pretty(state), STATE_COLORS.get(state, "#94a3b8")) if state else ""}</td>'
            f'<td>{labels}</td>'
            f'<td class="nowrap">{esc(pretty(item.get("action") or ""))}</td>'
            f'<td class="nowrap">{step_meter(item.get("step"), item.get("advanced", False)) if item.get("step") is not None else "<span class=muted>n/a</span>"}</td>'
            f'<td class="nowrap">{confidence_bar(item.get("confidence")) if item.get("confidence") is not None else "<span class=muted>n/a</span>"}</td>'
            f'<td class="reason">{reason}</td>'
            '</tr>'
        )
    return (
        '<div class="table-wrap"><table class="data"><thead><tr>'
        '<th>PR</th><th>Title / author</th><th>Days idle</th><th>Stage</th><th>AI state</th>'
        '<th>Labels on PR</th><th>Action</th><th>Escalation</th><th>Confidence</th><th>Reason</th>'
        '</tr></thead><tbody>' + ''.join(body) + '</tbody></table></div>'
    )


# ------------------------------------------------------------------ escalation
def render_escalation_ladder(report: dict[str, Any]) -> str:
    plans = agent_block(report).get('escalation_plans', []) or []
    if not agent_block(report).get('enabled'):
        return '<div class="empty">Agent memory is disabled; no multi-run escalation.</div>'
    at_step = {step: [] for step, *_ in LADDER_STEPS}
    for plan in plans:
        step = int(plan.get('step') or 0)
        if step in at_step:
            at_step[step].append(plan)
    held = [plan for plan in plans if int(plan.get('step') or 0) == 0]
    boxes = []
    for step, _key, title, audience in LADDER_STEPS:
        items = at_step[step]
        new = sum(1 for plan in items if plan.get('advanced'))
        prs = ' '.join(pr_link(report, plan.get('pr_number')) for plan in items) or '<span class="muted">none</span>'
        boxes.append(
            f'<div class="rung{" active" if items else ""}"><div class="rung-step">Step {step}</div>'
            f'<div class="rung-title">{esc(title)}</div><div class="muted small">pings {esc(audience)}</div>'
            f'<div class="rung-count">{len(items)}</div>'
            f'<div class="small">{prs}</div>'
            f'{"<div class=new>+" + str(new) + " this run</div>" if new else ""}</div>'
        )
    note = ''
    if held:
        note = (
            '<div class="chart-footnote">Not escalating (situation is not the author\'s fault, or capped): '
            + ' '.join(pr_link(report, plan.get('pr_number')) for plan in held)
            + '</div>'
        )
    return (
        '<div class="ladder">' + '<div class="arrow">›</div>'.join(boxes) + '</div>' + note
        + '<div class="chart-footnote">At most one step per run. The stage caps the ladder: '
        'warning ≤ step 2, escalated ≤ step 3, final notice ≤ step 5.</div>'
    )


# ------------------------------------------------------------------ branches
def render_branch_table(report: dict[str, Any]) -> str:
    assessments = report.get('branch_risk_assessments', []) or []
    if not assessments:
        return '<div class="empty">No branch assessments in this run.</div>'
    agent = agent_block(report)
    advice = {item.get('branch_name'): item for item in agent.get('branch_advice', []) or []}
    deleted = set(report.get('deleted_branches', []) or [])
    blocked = set(agent.get('deletions_blocked_by_advisor', []) or [])
    candidates = set(report.get('delete_candidates', []) or [])
    stale = set(report.get('stale_branches', []) or [])
    protected_label = set(report.get('protected_by_labels', []) or [])
    failures = set(report.get('branch_delete_failures', []) or [])

    def status(name: str, item: dict[str, Any]) -> str:
        if name in deleted:
            return badge('deleted', '#f87171', solid=True)
        if name in failures:
            return badge('delete failed', '#f87171')
        if name in blocked:
            return badge('deletion blocked', '#fbbf24')
        if name in candidates:
            return badge('delete candidate', '#fb923c')
        if name in protected_label:
            return badge('Do_Not_Delete', '#34d399')
        if item.get('protected') or item.get('exempt'):
            return badge('protected/exempt', '#34d399')
        if item.get('open_prs'):
            return badge('open PR', '#60a5fa')
        if name in stale:
            return badge('stale', '#fbbf24')
        return badge('recent', '#94a3b8')

    def order(item: dict[str, Any]) -> tuple:
        name = item.get('branch_name', '')
        rank = 0 if name in deleted | blocked | candidates else (1 if name in stale else 2)
        return (rank, bool(item.get('protected') or item.get('exempt')), -int(item.get('age_days') or 0))

    body = []
    for item in sorted(assessments, key=order):
        name = str(item.get('branch_name', ''))
        risk = str(item.get('risk_state', ''))
        tip = advice.get(name)
        age = '' if (item.get('protected') or item.get('exempt')) else esc(item.get('age_days', ''))
        advice_cell = (
            f'{badge(tip.get("recommendation"), ADVICE_COLORS.get(str(tip.get("recommendation")), "#94a3b8"), solid=True)}'
            f'<div class="muted small">{esc("; ".join((tip.get("reasons") or [])[:2]))}</div>'
            if tip
            else '<span class="muted">n/a</span>'
        )
        body.append(
            '<tr>'
            f'<td>{branch_link(report, name)}</td>'
            f'<td>{status(name, item)}</td>'
            f'<td class="num">{age}</td>'
            f'<td class="num">{esc(item.get("associated_prs", 0))} / {esc(item.get("open_prs", 0))}</td>'
            f'<td>{badge(pretty(risk), RISK_COLORS.get(risk, "#94a3b8"))}</td>'
            f'<td class="nowrap">{confidence_bar(item.get("score"))}</td>'
            f'<td>{advice_cell}</td>'
            f'<td class="reason">{esc(item.get("reason", ""))}</td>'
            '</tr>'
        )
    return (
        '<div class="table-wrap"><table class="data"><thead><tr>'
        '<th>Branch</th><th>Status</th><th>Age (days)</th><th>PRs (all / open)</th>'
        '<th>Risk</th><th>Score</th><th>Advisor</th><th>Reason</th>'
        '</tr></thead><tbody>' + ''.join(body) + '</tbody></table></div>'
    )


# ------------------------------------------------------------------ agent
def render_goal_table(report: dict[str, Any]) -> str:
    goals = agent_block(report).get('goal_metrics') or {}
    if not goals:
        return '<div class="empty">No goal metrics yet (agent memory disabled or first run).</div>'
    rows = []
    for key, (title, direction, target, meaning) in GOAL_TARGETS.items():
        if key not in goals:
            continue
        value = goals.get(key)
        if value is None:
            shown, verdict = 'n/a', badge('not enough data', '#94a3b8')
        else:
            number = float(value)
            shown = f'{number:.0%}' if isinstance(value, float) or key.endswith('rate') else str(value)
            if direction == 'max':
                ok = number <= float(target)
            elif direction == 'min':
                ok = number >= float(target)
            else:
                ok = None
            verdict = (
                badge('tracked', '#94a3b8') if ok is None
                else badge('on target', '#34d399', solid=True) if ok
                else badge('off target', '#f87171', solid=True)
            )
        if target is None:
            target_text = 'tracked'
        elif key.endswith('rate'):
            target_text = f"{'≤' if direction == 'max' else '≥'} {float(target):.0%}"
        else:
            target_text = f'{"≤" if direction == "max" else "≥"} {target}'
        rows.append(
            f'<tr><td>{esc(title)}<div class="muted small">{esc(meaning)}</div></td>'
            f'<td class="num">{esc(shown)}</td><td class="num">{esc(target_text)}</td><td>{verdict}</td></tr>'
        )
    return (
        '<table class="data"><thead><tr><th>Goal</th><th>Current</th><th>Target</th><th>Status</th></tr></thead>'
        '<tbody>' + ''.join(rows) + '</tbody></table>'
    )


def render_policy_table(report: dict[str, Any]) -> str:
    agent = agent_block(report)
    policy = agent.get('policy') or {}
    rows = []
    for key, title, default, bound, fmt in POLICY_ROWS:
        value = policy.get(key, default)
        if value is None:
            value = default
        changed = float(value) != float(default)
        rows.append(
            f'<tr><td>{esc(title)}</td><td class="num">{esc(fmt(value))}</td>'
            f'<td class="num muted">{esc(fmt(default))}</td><td class="muted">{esc(bound)}</td>'
            f'<td>{badge("adapted", "#fbbf24", solid=True) if changed else badge("default", "#94a3b8")}</td></tr>'
        )
    counters = (
        f'<div class="chart-footnote">This run: {esc(agent.get("overrides_respected", 0))} human override(s) respected · '
        f'{esc(agent.get("suppressions_expired", 0))} suppression(s) expired · '
        f'{esc(len(agent.get("deletions_blocked_by_advisor", []) or []))} deletion(s) blocked</div>'
    )
    notes = ''.join(f'<div class="chart-footnote">Note: {esc(note)}</div>' for note in agent.get('memory_notes', []) or [])
    return (
        '<table class="data"><thead><tr><th>Setting</th><th>Current</th><th>Default</th><th>Bound</th><th></th></tr></thead>'
        '<tbody>' + ''.join(rows) + '</tbody></table>' + counters + notes
    )


FEEDBACK_TEXT = {
    'human_responded': ('A human responded after the cleaner commented', '#34d399'),
    'reactivated': ('PR became active again after escalation', '#34d399'),
    'label_overridden': ('A human removed the stale label (false positive)', '#fbbf24'),
    'suppression_expired': ('A suppression lasted too long and expired', '#fb923c'),
    'branch_restored': ('A deleted branch was restored by a human', '#f87171'),
}


def render_feedback_table(report: dict[str, Any]) -> str:
    events = agent_block(report).get('feedback_events', []) or []
    if not events:
        return '<div class="empty">No feedback events this run.</div>'
    rows = []
    for event in events:
        kind = str(event.get('event', ''))
        text, color = FEEDBACK_TEXT.get(kind, (pretty(kind), '#94a3b8'))
        subject = pr_link(report, event['pr_number']) if 'pr_number' in event else branch_link(report, str(event.get('branch', '')))
        rows.append(f'<tr><td>{subject}</td><td>{badge(pretty(kind), color)}</td><td>{esc(text)}</td></tr>')
    return (
        '<table class="data"><thead><tr><th>PR / branch</th><th>Event</th><th>Meaning</th></tr></thead>'
        '<tbody>' + ''.join(rows) + '</tbody></table>'
    )


def render_adjustments(report: dict[str, Any]) -> str:
    changes = agent_block(report).get('policy_adjustments', []) or []
    if not changes:
        return '<div class="empty">No policy changes this run; settings stay within their bounds.</div>'
    return '<ul>' + ''.join(f'<li>{esc(change)}</li>' for change in changes) + '</ul>'


def render_errors(report: dict[str, Any]) -> str:
    errors = report.get('errors', []) or []
    if not errors:
        return ''
    return (
        '<section id="errors"><h2 class="section-title">Errors</h2><div class="panel error-panel"><ul>'
        + ''.join(f'<li>{esc(message)}</li>' for message in errors)
        + '</ul></div></section>'
    )


def render_recent_runs(history: list[dict[str, Any]]) -> str:
    if not history:
        return '<tr><td colspan="10" class="empty">No recent runs available</td></tr>'
    rows = []
    for item in reversed(history[-10:]):
        counts = item.get('stale_counts', {}) or {}
        stale_total = sum(int(counts.get(stage, 0)) for stage in STAGE_ORDER[1:])
        labeled = item.get('labeled_counts')
        labeled_text = str(sum(int(v) for v in labeled.values())) if isinstance(labeled, dict) else 'n/a'
        mode = str(item.get('run_mode', 'unknown'))
        mode_badge = badge(mode, '#34d399' if mode == 'apply' else '#fbbf24')
        errors = len(item.get('errors', []) or [])
        rows.append(
            '<tr>'
            f'<td class="nowrap">{esc(format_time(item.get("generated_at")))}</td>'
            f'<td>{mode_badge}</td>'
            f'<td class="num">{esc(item.get("prs_processed", 0))}</td>'
            f'<td class="num">{stale_total}</td>'
            f'<td class="num">{esc(labeled_text)}</td>'
            f'<td class="num">{esc(item.get("comments_posted", 0))}</td>'
            f'<td class="num">{len(item.get("delete_candidates", []) or [])}</td>'
            f'<td class="num">{len(item.get("deleted_branches", []) or [])}</td>'
            f'<td class="num">{esc((item.get("ai", {}) or {}).get("fallbacks", 0))}</td>'
            f'<td class="num">{badge(errors, "#f87171") if errors else "0"}</td>'
            '</tr>'
        )
    return ''.join(rows)


DASHBOARD_CSS = """
    :root { --bg:#0f172a; --panel:#111827; --muted:#94a3b8; --text:#e5e7eb; --border:#1f2937; --panel-2:#0b1220; --link:#93c5fd; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:linear-gradient(180deg,#020617,#0f172a); color:var(--text); }
    a { color:var(--link); text-decoration:none; } a:hover { text-decoration:underline; }
    code { font-size:12px; }
    .container { max-width:1400px; margin:0 auto; padding:28px 20px 48px; }
    .header { display:flex; justify-content:space-between; align-items:flex-end; gap:16px; margin-bottom:14px; }
    .header h1 { margin:0 0 6px; display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
    .subtitle { color:var(--muted); font-size:13px; } .right { text-align:right; }
    .nav { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:18px; position:sticky; top:0; padding:10px 0; background:rgba(2,6,23,0.92); z-index:5; }
    .nav a { border:1px solid var(--border); border-radius:999px; padding:6px 14px; font-size:13px; color:var(--text); background:var(--panel-2); }
    section { margin-top:26px; scroll-margin-top:60px; }
    .section-title { font-size:20px; margin:0 0 12px; padding-left:10px; border-left:4px solid #60a5fa; }
    .kpis { display:grid; grid-template-columns:repeat(auto-fit, minmax(170px, 1fr)); gap:14px; margin-bottom:18px; }
    .kpi { background:rgba(17,24,39,0.92); border:1px solid var(--border); border-top:4px solid; border-radius:14px; padding:14px 16px; }
    .kpi .label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
    .kpi .value { font-size:28px; font-weight:700; margin:6px 0 2px; }
    .kpi .sub { color:var(--muted); font-size:12px; }
    .panel { background:rgba(17,24,39,0.92); border:1px solid var(--border); border-radius:16px; padding:18px; margin-bottom:16px; box-shadow:0 12px 30px rgba(0,0,0,0.25); }
    .panel h2, .panel h3 { margin-top:0; }
    .panel-head { display:flex; justify-content:space-between; align-items:baseline; gap:12px; flex-wrap:wrap; }
    .hint, .muted { color:var(--muted); } .small { font-size:12px; margin-top:3px; }
    .grid-2 { display:grid; grid-template-columns:repeat(2, minmax(300px, 1fr)); gap:16px; }
    .grid-3 { display:grid; grid-template-columns:repeat(3, minmax(260px, 1fr)); gap:16px; margin-bottom:16px; }
    .chart-block { background:var(--panel-2); border:1px solid var(--border); border-radius:14px; padding:14px; }
    .chart-block h3 { margin:0 0 14px; font-size:16px; }
    .bar-row { display:grid; grid-template-columns:130px 1fr 56px; align-items:center; gap:10px; margin:10px 0; }
    .bar-label, .bar-value { font-size:13px; color:var(--muted); }
    .bar-track { width:100%; height:12px; background:#1e293b; border-radius:999px; overflow:hidden; }
    .bar-fill { height:100%; border-radius:999px; }
    .stack { display:flex; height:22px; border-radius:999px; overflow:hidden; background:#1e293b; margin-bottom:10px; }
    .seg { height:100%; }
    .swatch { display:inline-block; width:10px; height:10px; border-radius:3px; margin-right:8px; }
    .insights { margin:0; padding-left:20px; } li { margin:6px 0; } ul { margin:0; padding-left:20px; }
    .empty { color:var(--muted); padding:8px 0; }
    .table-wrap { overflow-x:auto; }
    table { width:100%; border-collapse:collapse; font-size:13px; }
    th, td { padding:9px 8px; border-bottom:1px solid var(--border); text-align:left; vertical-align:top; }
    th { color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.03em; position:sticky; top:0; background:#111827; }
    table.data tbody tr:hover { background:rgba(96,165,250,0.06); }
    table.compact td { padding:5px 4px; border:none; }
    .num { text-align:right; font-variant-numeric:tabular-nums; } .nowrap { white-space:nowrap; }
    .reason { color:#cbd5e1; max-width:420px; }
    .badge { display:inline-block; border:1px solid; border-radius:999px; padding:2px 9px; font-size:11px; font-weight:600; margin:1px 4px 1px 0; white-space:nowrap; }
    .mini-bar { display:inline-block; width:60px; height:8px; background:#1e293b; border-radius:999px; overflow:hidden; vertical-align:middle; }
    .mini-bar div { height:100%; }
    .mini-value { color:var(--muted); font-size:12px; margin-left:6px; }
    .dots { display:inline-flex; gap:3px; vertical-align:middle; }
    .dot { width:9px; height:9px; border-radius:50%; background:#1e293b; border:1px solid #334155; }
    .dot.on { background:#fb923c; border-color:#fb923c; }
    .new { display:inline-block; color:#0b1220; background:#34d399; border-radius:6px; font-size:10px; font-weight:700; padding:1px 5px; margin-top:4px; }
    .ladder { display:flex; align-items:stretch; gap:6px; flex-wrap:wrap; }
    .rung { flex:1; min-width:150px; background:var(--panel-2); border:1px solid var(--border); border-radius:14px; padding:12px; }
    .rung.active { border-color:#fb923c; box-shadow:0 0 0 1px rgba(251,146,60,0.3) inset; }
    .rung-step { color:#fb923c; font-size:11px; font-weight:700; text-transform:uppercase; }
    .rung-title { font-weight:700; margin:2px 0; }
    .rung-count { font-size:26px; font-weight:700; margin:6px 0 2px; }
    .arrow { align-self:center; color:var(--muted); font-size:24px; }
    .trend-chart { width:100%; height:auto; background:#020617; border-radius:14px; }
    .grid-line { stroke:#1f2937; stroke-width:1; } .axis-line { stroke:#475569; stroke-width:1; }
    .axis-label { fill:#94a3b8; font-size:11px; text-anchor:middle; }
    .legend { display:flex; gap:14px; flex-wrap:wrap; margin-top:12px; }
    .legend-item { display:flex; align-items:center; gap:8px; color:var(--muted); font-size:13px; }
    .legend-swatch { width:12px; height:12px; border-radius:999px; display:inline-block; }
    .chart-footnote { margin-top:8px; color:var(--muted); font-size:12px; }
    .donut-wrap { display:flex; gap:18px; align-items:center; flex-wrap:wrap; }
    .donut-chart { width:200px; height:200px; }
    .donut-total { fill:var(--text); font-size:26px; font-weight:700; } .donut-subtitle { fill:var(--muted); font-size:12px; }
    .donut-legend { flex-direction:column; align-items:flex-start; }
    .filter-form { display:flex; gap:10px; align-items:center; margin:10px 0; flex-wrap:wrap; }
    .filter-form label { color:var(--muted); font-size:13px; }
    .filter-form select, .filter-form button { background:#020617; color:var(--text); border:1px solid var(--border); border-radius:10px; padding:7px 10px; }
    .category-badges { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
    .category-badge { border:1px solid; border-radius:999px; padding:3px 10px; font-size:12px; }
    .count-note { color:var(--muted); font-size:13px; margin:14px 0 6px; }
    .error-panel { border-color:#7f1d1d; }
    details summary { cursor:pointer; font-weight:600; }
    pre { background:#020617; border:1px solid var(--border); border-radius:14px; padding:16px; overflow:auto; color:#cbd5e1; font-size:12px; max-height:480px; }
    @media (max-width:1100px) { .grid-3 { grid-template-columns:1fr; } }
    @media (max-width:900px) { .grid-2 { grid-template-columns:1fr; } .header { flex-direction:column; align-items:flex-start; } .right { text-align:left; } }
"""


def render_dashboard(report_path: Path, history_path: Path, selected_category: str = 'all') -> str:
    report = load_report(report_path)
    history = load_history(history_path)
    if report is None:
        return f'''<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="5">
  <title>PR Cleaner Dashboard</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background:#111827; color:#f9fafb; padding:40px; }}
    .panel {{ background:#1f2937; border-radius:16px; padding:24px; max-width:900px; margin:0 auto; }}
    code {{ color:#93c5fd; }}
  </style>
</head>
<body>
  <div class="panel">
    <h1>PR Cleaner Dashboard</h1>
    <p>No report found at <code>{html.escape(str(report_path))}</code>.</p>
    <p>Run the cleaner once, then refresh this page.</p>
  </div>
</body>
</html>'''

    metrics = derive_metrics(report, history)
    branch_chart = render_bar_chart(
        'Branch risk',
        [
            ('Likely safe', float((report.get('branch_risk_counts') or {}).get('likely_safe_to_delete', 0)), '#f87171'),
            ('Maybe preserve', float((report.get('branch_risk_counts') or {}).get('maybe_preserve', 0)), '#34d399'),
            ('Requires review', float((report.get('branch_risk_counts') or {}).get('requires_review', 0)), '#60a5fa'),
        ],
    )
    health_chart = render_bar_chart(
        'Run health (%)',
        [
            ('Stale share', metrics['stale_share'], '#fbbf24'),
            ('Labelled', label_coverage(report) * 100.0, '#34d399'),
            ('AI coverage', metrics['ai_coverage'], '#a78bfa'),
            ('AI fallback', metrics['fallback_rate'], '#f87171'),
        ],
    )
    raw = html.escape(json.dumps(report, indent=2))
    workload_chart = render_bar_chart(
        'Branch workload',
        [
            ('Processed', float(report.get('branches_processed', 0)), '#60a5fa'),
            ('Stale', float(metrics['stale_branches']), '#fbbf24'),
            ('Delete candidates', float(metrics['delete_candidates']), '#fb923c'),
            ('Deleted', float(len(report.get('deleted_branches', []) or [])), '#f87171'),
            ('Protected by label', float(metrics['protected_branches']), '#34d399'),
        ],
    )

    sections = [
        f'''<div class="header">
      <div>
        <h1>PR Cleaner Dashboard {render_mode_badge(report)}</h1>
        <div class="subtitle">{render_repo_line(report)} • Auto-refreshes every 30 seconds</div>
      </div>
      <div class="subtitle right">Last run: <strong>{html.escape(format_time(report.get('generated_at')))}</strong><br>
        Report: {html.escape(str(report_path))}</div>
    </div>
    <nav class="nav">
      <a href="#prs">Pull requests</a><a href="#escalation">Escalation</a>
      <a href="#branches">Branches</a><a href="#agent">Agent learning</a>
      <a href="#trends">Trends</a><a href="#raw">Raw report</a>
    </nav>''',
        f'<div class="kpis">{render_kpis(report, metrics)}</div>',
        f'''<div class="panel">
      <h2>Executive summary</h2>
      <ul class="insights">{render_insights(metrics)}{render_extra_insights(report)}</ul>
    </div>''',
        f'''<section id="prs">
      <h2 class="section-title">Pull requests</h2>
      <div class="grid-3">
        <div class="chart-block">{render_stage_bar(report)}</div>
        {render_ai_category_donut(metrics)}
        {render_context_label_chart(report)}
      </div>
      <div class="panel">
        <div class="panel-head"><h3>PR triage board</h3>
          <span class="hint">Sorted by days inactive. Links open the PR on GitHub.</span></div>
        {render_ai_filter_controls(report, selected_category)}
        {render_pr_table(report, selected_category)}
        {render_held_prs(report)}
      </div>
    </section>''',
        f'''<section id="escalation">
      <h2 class="section-title">Escalation ladder</h2>
      <div class="panel">{render_escalation_ladder(report)}</div>
    </section>''',
        f'''<section id="branches">
      <h2 class="section-title">Branches</h2>
      <div class="grid-3">
        {branch_chart}
        {workload_chart}
        {health_chart}
      </div>
      <div class="panel">
        <div class="panel-head"><h3>Branch assessment</h3>
          <span class="hint">Non-default branches first; deletion needs every rule and the advisor to agree.</span></div>
        {render_branch_table(report)}
      </div>
    </section>''',
        f'''<section id="agent">
      <h2 class="section-title">Agent learning</h2>
      <div class="grid-2">
        <div class="panel"><h3>Goal metrics (cumulative)</h3>{render_goal_table(report)}</div>
        <div class="panel"><h3>Adaptive policy</h3>{render_policy_table(report)}</div>
      </div>
      <div class="grid-2">
        <div class="panel"><h3>Feedback observed this run</h3>{render_feedback_table(report)}</div>
        <div class="panel"><h3>Policy adjustments this run</h3>{render_adjustments(report)}</div>
      </div>
    </section>''',
        f'''<section id="trends">
      <h2 class="section-title">Trends</h2>
      <div class="grid-2">
        <div class="chart-block">{render_trend_chart(history)}</div>
        {render_ai_category_trend_chart(history)}
      </div>
      <div class="panel">
        <h3>Recent runs</h3>
        <table>
          <thead><tr><th>Generated</th><th>Mode</th><th>PRs</th><th>Stale PRs</th><th>Labelled</th>
          <th>Comments</th><th>Delete candidates</th><th>Deleted</th><th>AI fallbacks</th><th>Errors</th></tr></thead>
          <tbody>{render_recent_runs(history)}</tbody>
        </table>
      </div>
    </section>''',
        render_errors(report),
        f'''<section id="raw">
      <details class="panel"><summary>Raw report JSON</summary><pre>{raw}</pre></details>
    </section>''',
    ]

    return (
        '<!doctype html>\n<html>\n<head>\n  <meta charset="utf-8">\n'
        '  <meta http-equiv="refresh" content="30">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '  <title>PR Cleaner Dashboard</title>\n'
        f'  <style>{DASHBOARD_CSS}</style>\n</head>\n<body>\n  <div class="container">\n'
        + '\n'.join(sections)
        + '\n  </div>\n</body>\n</html>'
    )


class DashboardHandler(BaseHTTPRequestHandler):
    report_path: Path = DEFAULT_REPORT_PATH
    history_path: Path = DEFAULT_HISTORY_PATH

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == '/api/report':
            report = load_report(self.report_path)
            payload = json.dumps(report or {'error': 'report_not_found'})
            status = 200 if report is not None else 404
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload.encode('utf-8'))
            return

        if parsed.path == '/api/history':
            payload = json.dumps(load_history(self.history_path))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload.encode('utf-8'))
            return

        if parsed.path not in {'/', '/index.html'}:
            self.send_response(404)
            self.end_headers()
            return

        selected_category = parse_qs(parsed.query).get('category', ['all'])[0]
        page = render_dashboard(self.report_path, self.history_path, selected_category=selected_category)
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(page.encode('utf-8'))

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Local dashboard for PR Cleaner reports')
    parser.add_argument('--report', default=str(DEFAULT_REPORT_PATH), help='Path to stale cleaner JSON report')
    parser.add_argument('--history', default=str(DEFAULT_HISTORY_PATH), help='Path to stale cleaner run history JSONL file')
    parser.add_argument('--host', default='127.0.0.1', help='Host to bind')
    parser.add_argument('--port', type=int, default=8765, help='Port to bind')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report_path = Path(args.report)
    history_path = Path(args.history)

    class BoundHandler(DashboardHandler):
        pass

    BoundHandler.report_path = report_path
    BoundHandler.history_path = history_path
    server = ThreadingHTTPServer((args.host, args.port), BoundHandler)
    print(f'PR Cleaner dashboard serving http://{args.host}:{args.port} using report {report_path} and history {history_path}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
