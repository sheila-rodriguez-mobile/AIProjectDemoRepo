#!/usr/bin/env python3

from __future__ import annotations

import datetime as dt
import fnmatch
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_CONFIG = {
    'pull_request_thresholds': {
        'warning_days': 2,
        'escalated_days': 3,
        'final_notice_days': 4,
    },
    'exempt_pr_labels': ['no-stale', 'security', 'blocked'],
    'managed_labels': ['stale:warning', 'stale:escalated', 'stale:final-notice'],
    'branch_thresholds': {
        'stale_days': 30,
        'delete_candidate_days': 60,
    },
    'exempt_branch_patterns': ['main', 'master', 'develop', 'release/*', 'hotfix/*'],
    'protected_label': 'Do_Not_Delete',
    'additional_mentions': [],
}


@dataclass
class RunSummary:
    run_mode: str
    prs_processed: int = 0
    prs_skipped_exempt: int = 0
    stale_counts: dict[str, int] | None = None
    cleared_stale_labels: int = 0
    branches_processed: int = 0
    stale_branches: list[str] | None = None
    delete_candidates: list[str] | None = None
    deleted_branches: list[str] | None = None
    protected_by_labels: list[str] | None = None

    def __post_init__(self) -> None:
        if self.stale_counts is None:
            self.stale_counts = {'active': 0, 'warning': 0, 'escalated': 0, 'final-notice': 0}
        if self.stale_branches is None:
            self.stale_branches = []
        if self.delete_candidates is None:
            self.delete_candidates = []
        if self.deleted_branches is None:
            self.deleted_branches = []
        if self.protected_by_labels is None:
            self.protected_by_labels = []


class GitHubClient:
    def __init__(self, token: str, repository: str, api_url: str = 'https://api.github.com') -> None:
        self.token = token
        self.repository = repository
        self.api_url = api_url.rstrip('/')
        self.owner, self.repo = repository.split('/', 1)

    def request(self, method: str, path: str, params: dict[str, Any] | None = None, data: Any = None) -> Any:
        url = f'{self.api_url}{path}'
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            url = f'{url}?{query}'
        headers = {
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {self.token}',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'AIProjectDemoRepo-PR-Cleaner',
        }
        body = None
        if data is not None:
            body = json.dumps(data).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                payload = response.read().decode('utf-8')
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'GitHub API request failed: {method} {path} ({exc.code}) {detail}') from exc

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        items: list[Any] = []
        page = 1
        while True:
            query = dict(params or {})
            query.setdefault('per_page', 100)
            query['page'] = page
            response = self.request('GET', path, query)
            if not response:
                break
            if not isinstance(response, list):
                return response
            items.extend(response)
            if len(response) < int(query['per_page']):
                break
            page += 1
        return items

    def repo_info(self) -> dict[str, Any]:
        return self.request('GET', f'/repos/{self.repository}')

    def repo_labels(self) -> list[dict[str, Any]]:
        return self.paginate(f'/repos/{self.repository}/labels')

    def create_label(self, name: str, color: str = 'ededed', description: str = '') -> None:
        self.request('POST', f'/repos/{self.repository}/labels', {'name': name, 'color': color, 'description': description})

    def open_pull_requests(self) -> list[dict[str, Any]]:
        return self.paginate(f'/repos/{self.repository}/pulls', {'state': 'open', 'sort': 'updated', 'direction': 'desc'})

    def pull_request_commits(self, number: int) -> list[dict[str, Any]]:
        return self.paginate(f'/repos/{self.repository}/pulls/{number}/commits')

    def pull_request_reviews(self, number: int) -> list[dict[str, Any]]:
        return self.paginate(f'/repos/{self.repository}/pulls/{number}/reviews')

    def issue_labels(self, number: int) -> list[dict[str, Any]]:
        return self.paginate(f'/repos/{self.repository}/issues/{number}/labels')

    def add_issue_labels(self, number: int, labels: list[str]) -> None:
        if labels:
            self.request('POST', f'/repos/{self.repository}/issues/{number}/labels', labels)

    def remove_issue_label(self, number: int, label: str) -> None:
        encoded = urllib.parse.quote(label, safe='')
        self.request('DELETE', f'/repos/{self.repository}/issues/{number}/labels/{encoded}')

    def add_comment(self, number: int, body: str) -> None:
        self.request('POST', f'/repos/{self.repository}/issues/{number}/comments', {'body': body})

    def branches(self) -> list[dict[str, Any]]:
        return self.paginate(f'/repos/{self.repository}/branches')

    def branch_detail(self, branch: str) -> dict[str, Any]:
        encoded = urllib.parse.quote(branch, safe='')
        return self.request('GET', f'/repos/{self.repository}/branches/{encoded}')

    def commit_detail(self, sha: str) -> dict[str, Any]:
        return self.request('GET', f'/repos/{self.repository}/commits/{sha}')

    def pull_requests_for_branch(self, branch: str) -> list[dict[str, Any]]:
        return self.paginate(f'/repos/{self.repository}/pulls', {'state': 'all', 'head': f'{self.owner}:{branch}'})

    def delete_branch(self, branch: str) -> None:
        encoded = urllib.parse.quote(branch, safe='')
        self.request('DELETE', f'/repos/{self.repository}/git/refs/heads/{encoded}')


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str | None) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if path:
        config_path = Path(path)
        if config_path.exists():
            with config_path.open('r', encoding='utf-8') as handle:
                loaded = json.load(handle)
            config = deep_merge(config, loaded)
    return config


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {'1', 'true', 'yes', 'y', 'on'}:
        return True
    if normalized in {'0', 'false', 'no', 'n', 'off'}:
        return False
    return default


def parse_datetime(value: str | None) -> dt.datetime:
    if not value:
        raise ValueError('datetime value is required')
    normalized = value.replace('Z', '+00:00')
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def days_between(start: dt.datetime, end: dt.datetime) -> int:
    return max(0, int((end - start).total_seconds() // 86400))


def stale_stage_for_days(days: int, thresholds: dict[str, Any]) -> str:
    if days >= int(thresholds['final_notice_days']):
        return 'final-notice'
    if days >= int(thresholds['escalated_days']):
        return 'escalated'
    if days >= int(thresholds['warning_days']):
        return 'warning'
    return 'active'


def is_exempt_branch(branch_name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(branch_name, pattern) for pattern in patterns)


def branch_is_protected_by_label(labels: Iterable[dict[str, Any]], protected_label: str) -> bool:
    protected = protected_label.strip().lower()
    return any(str(label.get('name', '')).strip().lower() == protected for label in labels)


def label_names(labels: Iterable[dict[str, Any]]) -> set[str]:
    return {str(label.get('name', '')).strip().lower() for label in labels}


def latest_activity(commit_times: list[dt.datetime], review_times: list[dt.datetime], fallback: dt.datetime) -> dt.datetime:
    timestamps = commit_times + review_times
    return max(timestamps) if timestamps else fallback


def most_recent_timestamps(entries: Iterable[dict[str, Any]], key: str) -> list[dt.datetime]:
    results: list[dt.datetime] = []
    for entry in entries:
        timestamp = entry.get(key)
        if timestamp:
            results.append(parse_datetime(timestamp))
    return results


def unique_mentions(repo_owner: str, pr: dict[str, Any], additional_mentions: Iterable[str]) -> list[str]:
    mentions: list[str] = []
    seen: set[str] = set()

    def add_mention(value: str | None) -> None:
        if not value:
            return
        cleaned = value.strip()
        if not cleaned:
            return
        if cleaned.startswith('@'):
            key = cleaned.lower()
            mention = cleaned
        else:
            key = cleaned.lower()
            mention = f'@{cleaned}'
        if key not in seen:
            seen.add(key)
            mentions.append(mention)

    author = pr.get('user', {}).get('login')
    add_mention(author)
    for reviewer in pr.get('requested_reviewers', []) or []:
        add_mention(reviewer.get('login'))
    for team in pr.get('requested_teams', []) or []:
        slug = team.get('slug') or team.get('name')
        if slug:
            add_mention(f'{repo_owner}/{slug}')
    for mention in additional_mentions:
        add_mention(mention)
    return mentions


def comment_body(stage: str, days: int, mentions: list[str], pr_number: int) -> str:
    title = {
        'warning': 'stale warning',
        'escalated': 'stale escalation',
        'final-notice': 'final stale notice',
    }.get(stage, 'stale update')
    lines = [
        f'### PR Cleaner: {title}',
        '',
        f'This pull request has been inactive for {days} day(s).',
        f'PR #{pr_number} is now in the `{stage}` stage.',
    ]
    if mentions:
        lines.extend(['', 'Mentions: ' + ' '.join(mentions)])
    return '\n'.join(lines)


def ensure_stale_labels(client: GitHubClient, config: dict[str, Any], existing_labels: set[str]) -> None:
    for label in config['managed_labels']:
        if label.lower() not in existing_labels:
            client.create_label(label)


def process_pull_requests(client: GitHubClient, config: dict[str, Any], now: dt.datetime, dry_run: bool, summary: RunSummary, repo_owner: str) -> None:
    managed_labels = [label.lower() for label in config['managed_labels']]
    exempt_labels = {label.lower() for label in config['exempt_pr_labels']}

    ensure_stale_labels(client, config, {label.get('name', '').strip().lower() for label in client.repo_labels()})

    for pr in client.open_pull_requests():
        summary.prs_processed += 1
        number = int(pr['number'])
        current_labels = client.issue_labels(number)
        current_label_names = label_names(current_labels)

        if current_label_names & exempt_labels:
            summary.prs_skipped_exempt += 1
            continue

        commit_times = most_recent_timestamps(client.pull_request_commits(number), 'commit.commit.author.date')
        # Pull request commits nest the timestamp in commit.commit.author.date, so parse explicitly.
        commit_times = []
        for commit in client.pull_request_commits(number):
            commit_data = commit.get('commit', {}).get('commit', {})
            for candidate in ('author', 'committer'):
                timestamp = commit_data.get(candidate, {}).get('date')
                if timestamp:
                    commit_times.append(parse_datetime(timestamp))
        review_times = []
        for review in client.pull_request_reviews(number):
            if review.get('submitted_at'):
                review_times.append(parse_datetime(review['submitted_at']))

        created_at = parse_datetime(pr['created_at'])
        last_activity = latest_activity(commit_times, review_times, created_at)
        days_inactive = days_between(last_activity, now)
        stage = stale_stage_for_days(days_inactive, config['pull_request_thresholds'])
        summary.stale_counts[stage] += 1

        target_label = {
            'warning': 'stale:warning',
            'escalated': 'stale:escalated',
            'final-notice': 'stale:final-notice',
        }.get(stage)

        managed_current = [label for label in config['managed_labels'] if label.lower() in current_label_names]
        if stage == 'active':
            if managed_current:
                summary.cleared_stale_labels += 1
                if not dry_run:
                    for label in managed_current:
                        client.remove_issue_label(number, label)
            continue

        if managed_current != [target_label]:
            if not dry_run:
                for label in managed_current:
                    if label != target_label:
                        client.remove_issue_label(number, label)
                if target_label not in current_label_names:
                    client.add_issue_labels(number, [target_label])

        mentions = unique_mentions(repo_owner, pr, config.get('additional_mentions', []))
        if not dry_run:
            client.add_comment(number, comment_body(stage, days_inactive, mentions, number))


def branch_age_days(client: GitHubClient, branch: dict[str, Any], now: dt.datetime) -> int:
    sha = branch['commit']['sha']
    commit = client.commit_detail(sha)
    timestamp = commit.get('commit', {}).get('committer', {}).get('date') or commit.get('commit', {}).get('author', {}).get('date')
    if not timestamp:
        return 0
    return days_between(parse_datetime(timestamp), now)


def process_branches(client: GitHubClient, config: dict[str, Any], now: dt.datetime, dry_run: bool, summary: RunSummary, default_branch: str) -> None:
    thresholds = config['branch_thresholds']
    protected_label = config['protected_label']

    open_pr_heads = {str(pr.get('head', {}).get('ref', '')) for pr in client.open_pull_requests()}

    for branch in client.branches():
        summary.branches_processed += 1
        name = branch['name']
        if name == default_branch or branch.get('protected'):
            continue
        if is_exempt_branch(name, config['exempt_branch_patterns']):
            continue

        prs_for_branch = client.pull_requests_for_branch(name)
        if any(branch_is_protected_by_label(pr.get('labels', []), protected_label) for pr in prs_for_branch):
            if name not in summary.protected_by_labels:
                summary.protected_by_labels.append(name)
            continue
        if any(pr.get('state') == 'open' or str(pr.get('head', {}).get('ref', '')) in open_pr_heads for pr in prs_for_branch):
            continue

        age = branch_age_days(client, branch, now)
        if age >= int(thresholds['stale_days']):
            summary.stale_branches.append(name)
        if age >= int(thresholds['delete_candidate_days']):
            summary.delete_candidates.append(name)
            if not dry_run:
                client.delete_branch(name)
                summary.deleted_branches.append(name)


def write_summary(summary: RunSummary) -> None:
    lines = [
        '# PR Cleaner Summary',
        '',
        f'- Run mode: `{summary.run_mode}`',
        f'- PRs processed: {summary.prs_processed}',
        f'- PRs skipped by exemption: {summary.prs_skipped_exempt}',
        f'- Cleared stale labels: {summary.cleared_stale_labels}',
        f'- Branches processed: {summary.branches_processed}',
        f'- Stale branches: {len(summary.stale_branches)}',
        f'- Delete candidates: {len(summary.delete_candidates)}',
        f'- Deleted branches: {len(summary.deleted_branches)}',
        f'- Branches protected by labels: {len(summary.protected_by_labels)}',
        '',
        '## PR Stale Counts',
    ]
    for stage, count in summary.stale_counts.items():
        lines.append(f'- {stage}: {count}')
    lines.extend(['', '## Stale Branches'])
    lines.extend(f'- {name}' for name in summary.stale_branches or ['_None_'])
    lines.extend(['', '## Deleted Branches'])
    lines.extend(f'- {name}' for name in summary.deleted_branches or ['_None_'])
    lines.extend(['', '## Branches Kept By Protected PR Labels'])
    lines.extend(f'- {name}' for name in summary.protected_by_labels or ['_None_'])

    text = '\n'.join(lines) + '\n'
    summary_path = os.getenv('GITHUB_STEP_SUMMARY')
    if summary_path:
        Path(summary_path).write_text(text, encoding='utf-8')
    print(text)


def main() -> int:
    token = os.getenv('GITHUB_TOKEN')
    repository = os.getenv('GITHUB_REPOSITORY')
    if not token or not repository:
        print('GITHUB_TOKEN and GITHUB_REPOSITORY are required', file=sys.stderr)
        return 2

    config_path = os.getenv('CONFIG_PATH', '.github/stale-cleaner.json')
    dry_run = parse_bool(os.getenv('DRY_RUN'), default=True)
    api_url = os.getenv('GITHUB_API_URL', 'https://api.github.com')
    config = load_config(config_path)

    client = GitHubClient(token, repository, api_url=api_url)
    now = dt.datetime.now(dt.timezone.utc)
    repo_info = client.repo_info()
    summary = RunSummary(run_mode='dry-run' if dry_run else 'apply')

    process_pull_requests(client, config, now, dry_run, summary, client.owner)
    process_branches(client, config, now, dry_run, summary, repo_info.get('default_branch', 'main'))
    write_summary(summary)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
