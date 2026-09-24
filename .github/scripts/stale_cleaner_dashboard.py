#!/usr/bin/env python3

from __future__ import annotations

import argparse
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_REPORT_PATH = Path('.github/stale-cleaner-report.json')


def load_report(report_path: Path) -> dict[str, Any] | None:
    if not report_path.exists():
        return None
    return json.loads(report_path.read_text(encoding='utf-8'))


def render_list(items: list[str]) -> str:
    if not items:
        return '<li class="empty">None</li>'
    return ''.join(f'<li>{html.escape(item)}</li>' for item in items)


def render_cards(report: dict[str, Any]) -> str:
    cards = [
        ('Run mode', report.get('run_mode', 'unknown')),
        ('PRs processed', str(report.get('prs_processed', 0))),
        ('PRs skipped', str(report.get('prs_skipped_exempt', 0))),
        ('Cleared stale labels', str(report.get('cleared_stale_labels', 0))),
        ('Branches processed', str(report.get('branches_processed', 0))),
        ('Delete candidates', str(len(report.get('delete_candidates', [])))),
        ('AI reviewed', str(report.get('ai', {}).get('reviewed', 0))),
        ('AI fallbacks', str(report.get('ai', {}).get('fallbacks', 0))),
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
            html.escape(stage), html.escape(str(value))
        )
        for stage, value in counts.items()
    )


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


def render_dashboard(report_path: Path) -> str:
    report = load_report(report_path)
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
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: linear-gradient(180deg, #020617, #0f172a); color: var(--text); }}
    .container {{ max-width: 1240px; margin: 0 auto; padding: 32px 20px 48px; }}
    .header {{ display:flex; justify-content:space-between; align-items:flex-end; gap:16px; margin-bottom: 24px; }}
    .subtitle {{ color: var(--muted); font-size: 14px; }}
    .cards {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom:24px; }}
    .card, .panel {{ background: rgba(17,24,39,0.92); border:1px solid var(--border); border-radius: 18px; box-shadow: 0 12px 30px rgba(0,0,0,0.28); }}
    .card {{ padding: 18px; }}
    .label {{ color: var(--muted); font-size: 13px; margin-bottom: 10px; text-transform: uppercase; letter-spacing: .04em; }}
    .value {{ font-size: 28px; font-weight: 700; }}
    .grid {{ display:grid; grid-template-columns: 1.15fr 1fr; gap: 16px; }}
    .panel {{ padding: 20px; }}
    .counts {{ display:grid; grid-template-columns: repeat(2, minmax(120px,1fr)); gap: 12px; }}
    .count {{ background:#0b1220; border:1px solid var(--border); border-radius:14px; padding:16px; }}
    .count span {{ color: var(--muted); display:block; margin-bottom:8px; text-transform:capitalize; }}
    .count strong {{ font-size:24px; }}
    ul {{ margin: 0; padding-left: 20px; }}
    li {{ margin: 8px 0; }}
    .empty {{ color: var(--muted); }}
    pre {{ background:#020617; border:1px solid var(--border); border-radius:14px; padding:16px; overflow:auto; color:#cbd5e1; font-size:12px; }}
    .decision-title {{ font-weight: 600; margin-bottom: 4px; }}
    .decision-meta, .decision-fallback {{ color: var(--muted); font-size: 13px; margin-top: 4px; }}
    .decision-reason {{ margin-top: 6px; }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} .header {{ flex-direction:column; align-items:flex-start; }} }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div>
        <h1>PR Cleaner Dashboard</h1>
        <div class="subtitle">Auto-refreshes every 10 seconds • Report: {html.escape(str(report_path))}</div>
      </div>
      <div class="subtitle">Generated at: {html.escape(str(report.get('generated_at', 'unknown')))}</div>
    </div>

    <div class="cards">{render_cards(report)}</div>

    <div class="grid">
      <div class="panel">
        <h2>PR stale counts</h2>
        <div class="counts">{render_stale_counts(report)}</div>
      </div>
      <div class="panel">
        <h2>AI decisions</h2>
        <ul>{render_ai_decisions(report)}</ul>
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
        <h2>Raw report</h2>
        <pre>{html.escape(json.dumps(report, indent=2))}</pre>
      </div>
    </div>
  </div>
</body>
</html>'''


class DashboardHandler(BaseHTTPRequestHandler):
    report_path: Path = DEFAULT_REPORT_PATH

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

        if parsed.path not in {'/', '/index.html'}:
            self.send_response(404)
            self.end_headers()
            return

        page = render_dashboard(self.report_path)
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
    parser.add_argument('--host', default='127.0.0.1', help='Host to bind')
    parser.add_argument('--port', type=int, default=8765, help='Port to bind')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report_path = Path(args.report)

    class BoundHandler(DashboardHandler):
        pass

    BoundHandler.report_path = report_path
    server = ThreadingHTTPServer((args.host, args.port), BoundHandler)
    print(f'PR Cleaner dashboard serving http://{args.host}:{args.port} using report {report_path}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
