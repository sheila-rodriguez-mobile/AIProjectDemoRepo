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
    return ''.join(
        '<div class="count"><span>{}</span><strong>{}</strong></div>'.format(
            html.escape(stage.replace('-', ' ')), html.escape(str(value))
        )
        for stage, value in counts.items()
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


def render_recent_runs(history: list[dict[str, Any]]) -> str:
    if not history:
        return '<tr><td colspan="6" class="empty">No recent runs available</td></tr>'
    rows = []
    for item in reversed(history[-8:]):
        counts = item.get('stale_counts', {}) or {}
        stale_total = int(counts.get('warning', 0)) + int(counts.get('escalated', 0)) + int(counts.get('final-notice', 0))
        rows.append(
            '<tr><td>{generated_at}</td><td>{mode}</td><td>{prs}</td><td>{stale}</td><td>{delete_candidates}</td><td>{fallbacks}</td></tr>'.format(
                generated_at=html.escape(str(item.get('generated_at', 'unknown')))[:19].replace('T', ' '),
                mode=html.escape(str(item.get('run_mode', 'unknown'))),
                prs=html.escape(str(item.get('prs_processed', 0))),
                stale=html.escape(str(stale_total)),
                delete_candidates=html.escape(str(len(item.get('delete_candidates', [])))),
                fallbacks=html.escape(str((item.get('ai', {}) or {}).get('fallbacks', 0))),
            )
        )
    return ''.join(rows)


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
    stage_chart = render_bar_chart(
        'PR stage distribution',
        [
            ('Warning', float(metrics['warning']), '#fbbf24'),
            ('Escalated', float(metrics['escalated']), '#fb923c'),
            ('Final notice', float(metrics['final_notice']), '#f87171'),
            ('Active', float(metrics['active']), '#34d399'),
        ],
    )
    operational_chart = render_bar_chart(
        'Operational pressure',
        [
            ('Stale PR share %', metrics['stale_share'], '#60a5fa'),
            ('AI coverage %', metrics['ai_coverage'], '#a78bfa'),
            ('AI fallback %', metrics['fallback_rate'], '#f87171'),
        ],
    )
    branch_chart = render_bar_chart(
        'Branch workload',
        [
            ('Stale branches', float(metrics['stale_branches']), '#60a5fa'),
            ('Delete candidates', float(metrics['delete_candidates']), '#f59e0b'),
            ('Protected branches', float(metrics['protected_branches']), '#34d399'),
        ],
    )
    ai_category_chart = render_ai_category_chart(metrics)
    ai_category_donut = render_ai_category_donut(metrics)
    ai_category_trend_chart = render_ai_category_trend_chart(history)
    visible_ai_decisions = filtered_ai_decisions(report, selected_category)
    filter_controls = render_ai_filter_controls(report, selected_category)

    return f'''<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="10">
  <title>PR Cleaner Dashboard</title>
  <style>
    :root {{
      --bg: #0f172a;
      --panel: #111827;
      --muted: #94a3b8;
      --text: #e5e7eb;
      --border: #1f2937;
      --panel-2: #0b1220;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: linear-gradient(180deg, #020617, #0f172a); color: var(--text); }}
    .container {{ max-width: 1320px; margin: 0 auto; padding: 32px 20px 48px; }}
    .header {{ display:flex; justify-content:space-between; align-items:flex-end; gap:16px; margin-bottom: 24px; }}
    .subtitle {{ color: var(--muted); font-size: 14px; }}
    .cards {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom:24px; }}
    .card, .panel {{ background: rgba(17,24,39,0.92); border:1px solid var(--border); border-radius: 18px; box-shadow: 0 12px 30px rgba(0,0,0,0.28); }}
    .card {{ padding: 18px; }}
    .label {{ color: var(--muted); font-size: 13px; margin-bottom: 10px; text-transform: uppercase; letter-spacing: .04em; }}
    .value {{ font-size: 28px; font-weight: 700; }}
    .grid {{ display:grid; grid-template-columns: 1.15fr 1fr; gap: 16px; }}
    .panel {{ padding: 20px; }}
    .panel h2 {{ margin-top: 0; }}
    .counts {{ display:grid; grid-template-columns: repeat(2, minmax(120px,1fr)); gap: 12px; }}
    .count {{ background:var(--panel-2); border:1px solid var(--border); border-radius:14px; padding:16px; }}
    .count span {{ color: var(--muted); display:block; margin-bottom:8px; text-transform:capitalize; }}
    .count strong {{ font-size:24px; }}
    .insights {{ margin:0; padding-left:20px; }}
    ul {{ margin: 0; padding-left: 20px; }}
    li {{ margin: 8px 0; }}
    .empty {{ color: var(--muted); }}
    pre {{ background:#020617; border:1px solid var(--border); border-radius:14px; padding:16px; overflow:auto; color:#cbd5e1; font-size:12px; }}
    .decision-title {{ font-weight: 600; margin-bottom: 4px; }}
    .decision-meta, .decision-fallback {{ color: var(--muted); font-size: 13px; margin-top: 4px; }}
    .decision-reason {{ margin-top: 6px; }}
    .chart-grid {{ display:grid; grid-template-columns: repeat(3, minmax(220px, 1fr)); gap:16px; margin-bottom:16px; }}
    .chart-grid-wide {{ display:grid; grid-template-columns: repeat(2, minmax(300px, 1fr)); gap:16px; margin-bottom:16px; }}
    .chart-block {{ background:var(--panel-2); border:1px solid var(--border); border-radius:14px; padding:14px; }}
    .chart-block h3 {{ margin:0 0 14px 0; font-size:16px; }}
    .bar-row {{ display:grid; grid-template-columns: 120px 1fr 56px; align-items:center; gap:10px; margin:10px 0; }}
    .bar-label, .bar-value {{ font-size:13px; color:var(--muted); }}
    .bar-track {{ width:100%; height:12px; background:#1e293b; border-radius:999px; overflow:hidden; }}
    .bar-fill {{ height:100%; border-radius:999px; }}
    .trend-chart {{ width:100%; height:auto; background:#020617; border-radius:14px; }}
    .grid-line {{ stroke:#1f2937; stroke-width:1; }}
    .axis-line {{ stroke:#475569; stroke-width:1; }}
    .axis-label {{ fill:#94a3b8; font-size:11px; text-anchor:middle; }}
    .legend {{ display:flex; gap:14px; flex-wrap:wrap; margin-top:12px; }}
    .legend-item {{ display:flex; align-items:center; gap:8px; color:var(--muted); font-size:13px; }}
    .legend-swatch {{ width:12px; height:12px; border-radius:999px; display:inline-block; }}
    .chart-footnote {{ margin-top:8px; color:var(--muted); font-size:12px; }}
    .donut-wrap {{ display:flex; gap:18px; align-items:center; flex-wrap:wrap; }}
    .donut-chart {{ width:220px; height:220px; }}
    .donut-total {{ fill:var(--text); font-size:26px; font-weight:700; }}
    .donut-subtitle {{ fill:var(--muted); font-size:12px; }}
    .donut-legend {{ flex-direction:column; align-items:flex-start; }}
    .filter-form {{ display:flex; gap:10px; align-items:center; margin-bottom:14px; flex-wrap:wrap; }}
    .filter-form label {{ color:var(--muted); font-size:13px; }}
    .filter-form select, .filter-form button {{ background:#020617; color:var(--text); border:1px solid var(--border); border-radius:10px; padding:8px 10px; }}
    .category-badges {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }}
    .category-badge {{ border:1px solid; border-radius:999px; padding:4px 10px; font-size:12px; }}
    table {{ width:100%; border-collapse: collapse; font-size:14px; }}
    th, td {{ padding:10px 8px; border-bottom:1px solid var(--border); text-align:left; }}
    th {{ color:var(--muted); font-weight:600; }}
    @media (max-width: 1100px) {{ .chart-grid {{ grid-template-columns: 1fr; }} .chart-grid-wide {{ grid-template-columns: 1fr; }} }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} .header {{ flex-direction:column; align-items:flex-start; }} }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div>
        <h1>PR Cleaner Dashboard</h1>
        <div class="subtitle">Auto-refreshes every 10 seconds • Report: {html.escape(str(report_path))} • History: {html.escape(str(history_path))}</div>
      </div>
      <div class="subtitle">Generated at: {html.escape(str(report.get('generated_at', 'unknown')))}</div>
    </div>

    <div class="cards">{render_cards(report, metrics)}</div>

    <div class="panel" style="margin-bottom:16px;">
      <h2>Executive summary</h2>
      <ul class="insights">{render_insights(metrics)}</ul>
    </div>

    <div class="chart-grid">
      {stage_chart}
      {operational_chart}
      {branch_chart}
      {ai_category_chart}
    </div>

    <div class="chart-grid-wide">
      {ai_category_donut}
      {ai_category_trend_chart}
    </div>

    <div class="panel" style="margin-bottom:16px;">
      {render_trend_chart(history)}
    </div>

    <div class="grid">
      <div class="panel">
        <h2>PR stale counts</h2>
        <div class="counts">{render_stale_counts(report)}</div>
      </div>
      <div class="panel">
        <h2>AI decisions</h2>
        {filter_controls}
        <ul>{render_filtered_ai_decisions(visible_ai_decisions, selected_category)}</ul>
      </div>
      <div class="panel">
        <h2>Protected branches</h2>
        <ul>{render_list(report.get('protected_by_labels', []))}</ul>
      </div>
      <div class="panel">
        <h2>Stale branches</h2>
        <ul>{render_list(report.get('stale_branches', []))}</ul>
      </div>
      <div class="panel">
        <h2>Delete candidates</h2>
        <ul>{render_list(report.get('delete_candidates', []))}</ul>
      </div>
      <div class="panel">
        <h2>Deleted branches</h2>
        <ul>{render_list(report.get('deleted_branches', []))}</ul>
      </div>
      <div class="panel" style="grid-column: 1 / -1;">
        <h2>Recent runs</h2>
        <table>
          <thead>
            <tr><th>Generated</th><th>Mode</th><th>PRs processed</th><th>Total stale PRs</th><th>Delete candidates</th><th>AI fallbacks</th></tr>
          </thead>
          <tbody>
            {render_recent_runs(history)}
          </tbody>
        </table>
      </div>
      <div class="panel" style="grid-column: 1 / -1;">
        <h2>Raw report</h2>
        <pre>{html.escape(json.dumps(report, indent=2))}</pre>
      </div>
    </div>
  </div>
</body>
</html>'''


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
