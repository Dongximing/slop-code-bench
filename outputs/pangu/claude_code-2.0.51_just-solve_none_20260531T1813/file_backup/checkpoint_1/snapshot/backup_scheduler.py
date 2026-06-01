#!/usr/bin/env python3
"""
Backup scheduler that parses a YAML schedule, evaluates which jobs are due,
and simulates running those jobs with exclusion rules, emitting event history as JSON Lines.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch, translate
from pathlib import Path
from typing import Any

import yaml
from dateutil import tz


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Simulate backup jobs based on a YAML schedule."
    )
    parser.add_argument(
        "--schedule",
        required=True,
        help="Path to YAML schedule file."
    )
    parser.add_argument(
        "--now",
        required=True,
        help="Wall clock for scheduling decisions (ISO 8601 format, e.g., 2025-09-10T13:45:00Z)."
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=24.0,
        help="Duration to simulate in hours. Default is 24."
    )
    parser.add_argument(
        "--mount",
        required=True,
        help="Path to the location where files are mounted (treated as mount:// root)."
    )
    return parser.parse_args()


def parse_iso_timestamp(ts_str: str) -> datetime:
    """Parse an ISO 8601 timestamp, returning a timezone-aware datetime."""
    # Handle 'Z' suffix
    if ts_str.endswith('Z'):
        ts_str = ts_str[:-1] + '+00:00'
    # datetime.fromisoformat handles timezone-aware strings
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        raise ValueError(f"Timestamp {ts_str} is missing timezone info.")
    return dt


def load_schedule(schedule_path: str) -> dict:
    """Load and parse the YAML schedule."""
    with open(schedule_path, 'r') as f:
        return yaml.safe_load(f)


def get_timezone(tz_name: str) -> tz.tzfile:
    """Get a timezone object from an IANA timezone name."""
    return tz.gettz(tz_name)


def parse_time_of_day(time_str: str) -> tuple[int, int]:
    """Parse HH:MM format into (hour, minute)."""
    parts = time_str.split(':')
    if len(parts) != 2:
        raise ValueError(f"Invalid time format: {time_str}")
    hour = int(parts[0])
    minute = int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Time out of range: {time_str}")
    return hour, minute


def get_next_trigger_time(
    job: dict,
    tz_info: tz.tzfile,
    start_dt: datetime
) -> list[datetime]:
    """
    Calculate all trigger times for a job within the simulation window.

    Returns a list of datetime objects in UTC (all floored to minute).
    """
    kind = job['when']['kind']
    triggers = []

    if kind == 'daily':
        hour, minute = parse_time_of_day(job['when']['at'])
        window_end = start_dt + timedelta(hours=job.get('_duration_hours', 24))

        # Generate trigger times for each day in the window
        start_date = start_dt.astimezone(tz_info).replace(hour=0, minute=0, second=0, microsecond=0)
        current = start_date
        while current <= window_end.astimezone(tz_info):
            trigger = current.replace(hour=hour, minute=minute)
            trigger_utc = trigger.astimezone(timezone.utc)
            triggers.append(trigger_utc)
            current += timedelta(days=1)

    elif kind == 'weekly':
        hour, minute = parse_time_of_day(job['when']['at'])
        days = job['when'].get('days', [])
        day_map = {'Mon': 0, 'Tue': 1, 'Wed': 2, 'Thu': 3, 'Fri': 4, 'Sat': 5, 'Sun': 6}
        weekday_nums = []
        for d in days:
            d_upper = d.strip().capitalize()
            if d_upper not in day_map:
                raise ValueError(f"Invalid day name: {d}")
            weekday_nums.append(day_map[d_upper])

        window_end = start_dt + timedelta(hours=job.get('_duration_hours', 24))

        # Find all occurrences within the window
        start_date = start_dt.astimezone(tz_info).replace(hour=0, minute=0, second=0, microsecond=0)
        current = start_date
        while current <= window_end.astimezone(tz_info):
            for wd in weekday_nums:
                days_ahead = wd - current.weekday()
                if days_ahead < 0:
                    days_ahead += 7
                trigger = current + timedelta(days=days_ahead)
                trigger = trigger.replace(hour=hour, minute=minute)
                trigger_utc = trigger.astimezone(timezone.utc)
                triggers.append(trigger_utc)
            current += timedelta(days=7)

        triggers = sorted(set(triggers))

    elif kind == 'once':
        at_str = job['when']['at']
        dt_naive = datetime.fromisoformat(at_str)
        dt_local = dt_naive.replace(tzinfo=tz_info)
        trigger_utc = dt_local.astimezone(timezone.utc)
        window_end = start_dt + timedelta(hours=job.get('_duration_hours', 24))
        if start_dt <= trigger_utc <= window_end:
            triggers.append(trigger_utc)
    else:
        raise ValueError(f"Unknown job kind: {kind}")

    return triggers


def find_due_jobs(
    schedule: dict,
    now_utc: datetime,
    duration_hours: float
) -> list[tuple[str, dict, datetime, str]]:
    """
    Find all jobs that are due within the simulation window.

    Returns a list of tuples: (job_id, job_dict, trigger_time_utc, now_local_str)
    Sorted by job_id ascending.
    """
    timezone_name = schedule.get('timezone', 'UTC')
    tz_info = get_timezone(timezone_name)

    due_jobs = []
    for job in schedule.get('jobs', []):
        if not job.get('enabled', True):
            continue

        job['_duration_hours'] = duration_hours

        triggers = get_next_trigger_time(job, tz_info, now_utc)
        window_end = now_utc + timedelta(hours=duration_hours)

        for trigger in triggers:
            if now_utc <= trigger <= window_end:
                trigger_local = trigger.astimezone(tz_info)
                now_local_str = trigger_local.strftime('%Y-%m-%dT%H:%M:%SZ')
                due_jobs.append((job['id'], job, trigger, now_local_str))

    due_jobs.sort(key=lambda x: x[0])
    return due_jobs


def match_pattern(path: str, pattern: str) -> bool:
    """
    Check if a path matches a glob pattern.

    Supports:
    - * matches any sequence of characters except /
    - ? matches any single character except /
    - ** matches any sequence of characters including /
    - [abc] character classes

    Paths use forward slashes.
    """
    # Normalize to forward slashes
    path = path.replace('\\', '/')
    pattern = pattern.replace('\\', '/')

    # Handle the case of just **
    if pattern == '**':
        return True

    # Handle ** patterns specially since fnmatch doesn't handle them correctly
    if '**' in pattern:
        return _match_pattern_with_star_star(path, pattern)

    # For regular patterns without **, use fnmatch but ensure * doesn't cross /
    return _fnmatch_no_slash_crossing(path, pattern)


def _fnmatch_no_slash_crossing(path: str, pattern: str) -> bool:
    """
    Use fnmatch but enforce that * does not match /.

    Key rules:
    - * matches any sequence of non-/ characters within a segment
    - A/* means: path has 2 segments, first must be 'A', second must match any non-/ chars
    - A/*/* means: path has 3 segments, first='A', second any, third any
    """
    # Split both path and pattern by /
    path_parts = path.split('/')
    pattern_parts = pattern.split('/')

    # Must have the same number of segments
    # Unless the pattern ends with * - but * without ** should NOT cross / boundaries
    # So A/*/* means exactly 3 segments
    if len(path_parts) != len(pattern_parts):
        return False

    # Match each segment
    for p_part, pt_part in zip(path_parts, pattern_parts):
        if not fnmatch(p_part, pt_part):
            return False

    return True


def _match_pattern_with_star_star(path: str, pattern: str) -> bool:
    """
    Match a pattern containing ** against a path.
    """
    # Split pattern by **
    parts = pattern.split('**')

    if not parts[0] and not parts[-1]:
        # Pattern is like **something**
        # Remove leading and trailing empty parts
        parts = parts[1:-1]
        # This becomes something like [something, something]
        # Pattern is: ** middle **
        # We need to check if path contains middle as a substring
        if not parts:
            # Just **
            return True
        middle = parts[0]
        # For simplicity, treat as: ** + middle + **
        # This should match if path contains the middle pattern
        # Build a pattern: */*/*/.../middle/*/*/*
        # For now, just check if the pattern matches somewhere
        return _match_star_star_middle(path, middle)

    if not parts[0]:
        # Pattern starts with **, like **/something or **something
        suffix = parts[1] if len(parts) > 1 else ''
        # Pattern is: **suffix
        # Check if path ends with suffix
        if suffix:
            # Need to check if path ends with suffix
            # The middle part (before suffix) can contain /
            return path.endswith(suffix)
        else:
            # Pattern is just **
            return True

    if not parts[-1]:
        # Pattern ends with **, like something/**
        prefix = parts[0]
        # Pattern is: prefix**
        # Check if path starts with prefix
        return path.startswith(prefix)

    # Pattern is: prefix**suffix
    prefix = parts[0]
    suffix = parts[1] if len(parts) > 1 else ''

    if not prefix:
        # Shouldn't happen due to above checks
        return path.endswith(suffix) if suffix else True

    if not suffix:
        return path.startswith(prefix)

    # Both prefix and suffix: path must start with prefix and end with suffix
    if not path.startswith(prefix) or not path.endswith(suffix):
        return False

    # The middle part (after prefix, before suffix) is what ** matches
    middle = path[len(prefix):-len(suffix)] if suffix else path[len(prefix):]

    # For our purposes, this is always valid since ** can match anything including /
    return True


def _match_star_star_middle(path: str, middle: str) -> bool:
    """
    Check if path contains middle pattern anywhere.
    """
    # Simple approach: check each prefix of path
    # Actually, let's use a regex approach
    # Escape middle and replace * with .* for regex
    import re
    escaped = re.escape(middle)
    pattern = escaped.replace(r'\*', '.*').replace(r'\?', '.')
    regex = pattern + r'.*' + pattern
    return bool(re.search(pattern, path))


def apply_exclusion_rules(file_path: str, exclude_patterns: list[str]) -> tuple[bool, str | None]:
    """
    Apply exclusion patterns to a file path.

    Returns (is_excluded, first_matching_pattern).
    If not excluded, returns (False, None).
    """
    for pattern in exclude_patterns:
        if match_pattern(file_path, pattern):
            return True, pattern
    return False, None


def traverse_source(
    mount_path: str,
    source_path: str,
    exclude_patterns: list[str],
    job_id: str
) -> tuple[list[str], list[tuple[str, str]]]:
    """
    Traverse the source directory and apply exclusion rules.
    """
    full_path = Path(mount_path) / source_path if source_path else Path(mount_path)

    if not full_path.exists():
        return [], []

    selected = []
    excluded = []

    all_files = []
    for root, dirs, files in os.walk(full_path):
        root_path = Path(root)
        if root_path == full_path:
            rel_root = ''
        else:
            rel_root = str(root_path.relative_to(full_path))

        for file in sorted(files):
            if rel_root:
                rel_path = f"{rel_root}/{file}"
            else:
                rel_path = file
            all_files.append(rel_path)

    all_files.sort()

    for rel_path in all_files:
        is_excluded, pattern = apply_exclusion_rules(rel_path, exclude_patterns)
        if is_excluded:
            excluded.append((rel_path, pattern))
        else:
            selected.append(rel_path)

    return selected, excluded


def emit_event(event_type: str, **kwargs):
    """Emit a JSON Lines event to stdout."""
    event = {'event': event_type}
    event.update(kwargs)
    print(json.dumps(event, separators=(',', ':')))


def run_job(job_id: str, job: dict, now_local: str, mount_path: str):
    """Execute a single job, emitting events as it runs."""
    kind = job['when']['kind']
    emit_event('JOB_ELIGIBLE', job_id=job_id, kind=kind, now_local=now_local)

    exclude_count = len(job.get('exclude', []))
    emit_event('JOB_STARTED', job_id=job_id, exclude_count=exclude_count)

    source = job.get('source', 'mount://')
    if source.startswith('mount://'):
        source_path = source[8:]
    else:
        source_path = source

    exclude_patterns = job.get('exclude', [])
    selected, excluded = traverse_source(mount_path, source_path, exclude_patterns, job_id)

    for path in selected:
        emit_event('FILE_SELECTED', job_id=job_id, path=path)

    for path, pattern in excluded:
        emit_event('FILE_EXCLUDED', job_id=job_id, path=path, pattern=pattern)

    emit_event('JOB_COMPLETED', job_id=job_id, selected=len(selected), excluded=len(excluded))


def main():
    args = parse_args()

    now_utc = parse_iso_timestamp(args.now)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    now_utc = now_utc.replace(second=0, microsecond=0)

    schedule = load_schedule(args.schedule)

    timezone_name = schedule.get('timezone', 'UTC')
    jobs_total = len(schedule.get('jobs', []))
    emit_event('SCHEDULE_PARSED', timezone=timezone_name, jobs_total=jobs_total)

    due_jobs = find_due_jobs(schedule, now_utc, args.duration)

    for job_id, job, trigger_time, now_local in due_jobs:
        run_job(job_id, job, now_local, args.mount)


if __name__ == '__main__':
    main()
