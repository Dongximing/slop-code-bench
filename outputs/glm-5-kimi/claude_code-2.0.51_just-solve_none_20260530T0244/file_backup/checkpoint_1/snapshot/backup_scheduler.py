#!/usr/bin/env python3
"""
Backup Scheduler - CLI-driven backup scheduler with YAML schedule parsing,
exclusion rules, and JSON Lines event history output.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
import yaml


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="CLI-driven backup scheduler with YAML schedule parsing"
    )
    parser.add_argument(
        "--schedule",
        required=True,
        help="Path to YAML schedule file"
    )
    parser.add_argument(
        "--now",
        required=True,
        help="Wall clock time in ISO-8601 format (e.g., 2025-09-10T13:45:00Z)"
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=24,
        help="Duration to simulate in hours (default: 24, inclusive bound)"
    )
    parser.add_argument(
        "--mount",
        required=True,
        help="Path to the location where files are mounted (treated as mount:// root)"
    )
    return parser.parse_args()


def parse_iso8601(timestamp_str: str) -> datetime:
    """
    Parse ISO-8601/RFC 3339 timestamp string to a timezone-aware datetime.
    Handles both 'Z' suffix and '+/-HH:MM' offsets.
    """
    # Replace 'Z' with '+00:00' for consistent parsing
    ts = timestamp_str.replace('Z', '+00:00')

    # Handle timezone offset formats
    # Python's fromisoformat can handle +00:00 format in Python 3.7+
    dt = datetime.fromisoformat(ts)

    # Ensure it's timezone-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=pytz.UTC)

    return dt


def load_schedule(schedule_path: str) -> Dict[str, Any]:
    """Load and parse the YAML schedule file."""
    with open(schedule_path, 'r') as f:
        return yaml.safe_load(f)


def floor_to_minute(dt: datetime) -> datetime:
    """Floor a datetime to the nearest minute (truncate seconds/microseconds)."""
    return dt.replace(second=0, microsecond=0)


def get_job_timezone(schedule: Dict[str, Any]) -> pytz.BaseTzInfo:
    """Get the timezone from the schedule, defaulting to UTC."""
    tz_name = schedule.get('timezone', 'UTC')
    return pytz.timezone(tz_name)


def parse_days(days: List[str]) -> set:
    """Convert 3-letter day names to set of weekday numbers (0=Monday)."""
    day_map = {
        'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3,
        'fri': 4, 'sat': 5, 'sun': 6
    }
    result = set()
    for day in days:
        result.add(day_map[day.lower()])
    return result


def get_trigger_times(
    job: Dict[str, Any],
    schedule_tz: pytz.BaseTzInfo,
    window_start: datetime,
    window_end: datetime
) -> List[datetime]:
    """
    Get all trigger times for a job within the given window.

    Returns a list of timezone-aware datetimes (in UTC) when the job should trigger.
    """
    when = job.get('when', {})
    kind = when.get('kind')
    at_str = when.get('at')

    triggers = []

    if kind == 'once':
        # For 'once', at_str is an ISO timestamp without timezone, interpreted in schedule's timezone
        # Parse the timestamp and localize to schedule timezone
        # Format: "YYYY-MM-DDTHH:MM"
        naive_dt = datetime.fromisoformat(at_str)
        # Localize to schedule timezone
        local_dt = schedule_tz.localize(naive_dt)
        # Convert to UTC for comparison
        utc_dt = local_dt.astimezone(pytz.UTC)
        utc_dt = floor_to_minute(utc_dt)

        if window_start <= utc_dt <= window_end:
            triggers.append(utc_dt)

    elif kind == 'daily':
        # Parse HH:MM
        hour, minute = map(int, at_str.split(':'))

        # Iterate through each day in the window
        current = window_start.astimezone(schedule_tz)
        current = current.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # If current is before window_start in local time, move to next occurrence
        current_utc = current.astimezone(pytz.UTC)
        if current_utc < window_start:
            current = current + timedelta(days=1)

        # Loop until we pass window_end
        while True:
            current_utc = current.astimezone(pytz.UTC)
            current_utc = floor_to_minute(current_utc)

            if current_utc > window_end:
                break

            if window_start <= current_utc <= window_end:
                triggers.append(current_utc)

            current = current + timedelta(days=1)

    elif kind == 'weekly':
        # Parse HH:MM and days
        hour, minute = map(int, at_str.split(':'))
        days_set = parse_days(when.get('days', []))

        # Iterate through each day in the window
        current = window_start.astimezone(schedule_tz)
        current = current.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # If current is before window_start in local time, move to next occurrence
        current_utc = current.astimezone(pytz.UTC)
        if current_utc < window_start:
            current = current + timedelta(days=1)

        # Loop until we pass window_end
        while True:
            current_utc = current.astimezone(pytz.UTC)
            current_utc = floor_to_minute(current_utc)

            if current_utc > window_end:
                break

            # Check if this day matches one of the specified days
            if current.weekday() in days_set:
                if window_start <= current_utc <= window_end:
                    triggers.append(current_utc)

            current = current + timedelta(days=1)

    return triggers


def is_job_due(
    job: Dict[str, Any],
    schedule_tz: pytz.BaseTzInfo,
    window_start: datetime,
    window_end: datetime
) -> Tuple[bool, List[datetime]]:
    """
    Check if a job is due within the window.

    Returns (is_due, trigger_times) where trigger_times is a list of all triggers in the window.
    """
    # Check if job is enabled (default is True)
    if not job.get('enabled', True):
        return False, []

    triggers = get_trigger_times(job, schedule_tz, window_start, window_end)

    if triggers:
        return True, triggers

    return False, []


def compile_glob_pattern(pattern: str) -> re.Pattern:
    """
    Compile a glob pattern to a regex pattern.

    Supports: *, ?, **, []
    ** can cross /
    """
    # Escape regex special characters except glob wildcards
    result = []
    i = 0
    while i < len(pattern):
        c = pattern[i]

        if c == '*' and i + 1 < len(pattern) and pattern[i + 1] == '*':
            # ** - match any sequence including path separators
            if i + 2 < len(pattern) and pattern[i + 2] == '/':
                # **/ - match any sequence (including empty) followed by /
                result.append('(.*/)?')
                i += 3
            else:
                # ** at end or followed by non-/
                result.append('.*')
                i += 2
        elif c == '*':
            # * - match any sequence except /
            result.append('[^/]*')
            i += 1
        elif c == '?':
            # ? - match any single character except /
            result.append('[^/]')
            i += 1
        elif c == '[':
            # Character class
            j = i + 1
            while j < len(pattern) and pattern[j] != ']':
                if pattern[j] == '\\' and j + 1 < len(pattern):
                    j += 2
                else:
                    j += 1
            # Include the closing bracket
            if j < len(pattern):
                j += 1
            result.append(pattern[i:j])
            i = j
        elif c in '.^$+{}()|\\':
            # Escape regex special characters
            result.append('\\' + c)
            i += 1
        elif c == '/':
            result.append('/')
            i += 1
        else:
            result.append(c)
            i += 1

    # Add anchors to ensure full path match
    regex_str = '^' + ''.join(result) + '$'
    return re.compile(regex_str)


def matches_pattern(path: str, pattern: str) -> bool:
    """
    Check if a path matches a glob pattern.

    Paths use '/' as separator.
    Pattern is evaluated relative to the job's source.
    """
    regex = compile_glob_pattern(pattern)
    return regex.match(path) is not None


def find_matching_pattern(path: str, patterns: List[str]) -> Optional[str]:
    """
    Find the first pattern that matches the path.

    Returns the pattern string, or None if no match.
    """
    for pattern in patterns:
        if matches_pattern(path, pattern):
            return pattern
    return None


def get_all_files(root_path: Path) -> List[str]:
    """
    Get all files in the directory tree, sorted lexicographically by relative path.

    Returns paths relative to root_path, using '/' as separator.
    """
    files = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        for filename in filenames:
            full_path = Path(dirpath) / filename
            rel_path = full_path.relative_to(root_path)
            # Use '/' as path separator for consistency
            files.append(str(rel_path).replace(os.sep, '/'))

    files.sort()
    return files


def emit_event(event: Dict[str, Any]) -> None:
    """Emit a JSON event to stdout (compact format, newline-terminated)."""
    print(json.dumps(event, separators=(',', ':')))


def format_local_time(dt: datetime, tz: pytz.BaseTzInfo) -> str:
    """
    Format a datetime in local time with timezone offset.

    Returns ISO-8601 format with timezone (Z for UTC, or +HH:MM offset).
    """
    local_dt = dt.astimezone(tz)

    # Get the offset
    offset = local_dt.utcoffset()
    offset_hours = offset.total_seconds() / 3600

    if offset_hours == 0:
        suffix = 'Z'
    else:
        sign = '+' if offset_hours >= 0 else '-'
        hours = int(abs(offset_hours))
        minutes = int(abs(offset.total_seconds()) % 3600 // 60)
        suffix = f'{sign}{hours:02d}:{minutes:02d}'

    return local_dt.strftime('%Y-%m-%dT%H:%M:%S') + suffix


def resolve_source_path(source: str, mount_path: str) -> Path:
    """
    Resolve the mount:// source to an actual filesystem path.

    - mount:// means the root of mount
    - mount://some/path means a subdirectory within mount
    """
    if source.startswith('mount://'):
        subpath = source[8:]  # Remove 'mount://'
        if subpath:
            return Path(mount_path) / subpath
        else:
            return Path(mount_path)
    else:
        raise ValueError(f"Invalid source scheme: {source}")


def process_job(
    job: Dict[str, Any],
    mount_path: str,
    trigger_time: datetime,
    schedule_tz: pytz.BaseTzInfo
) -> None:
    """Process a single job: emit events for files and summary."""
    job_id = job['id']
    exclude_patterns = job.get('exclude', [])
    source = job.get('source', 'mount://')

    # Emit JOB_ELIGIBLE event
    now_local = format_local_time(trigger_time, schedule_tz)
    kind = job.get('when', {}).get('kind', 'daily')
    emit_event({
        'event': 'JOB_ELIGIBLE',
        'job_id': job_id,
        'kind': kind,
        'now_local': now_local
    })

    # Emit JOB_STARTED event
    emit_event({
        'event': 'JOB_STARTED',
        'job_id': job_id,
        'exclude_count': len(exclude_patterns)
    })

    # Get all files in source directory
    source_path = resolve_source_path(source, mount_path)

    selected_count = 0
    excluded_count = 0

    if source_path.exists() and source_path.is_dir():
        files = get_all_files(source_path)

        for file_path in files:
            matching_pattern = find_matching_pattern(file_path, exclude_patterns)

            if matching_pattern:
                # File is excluded
                emit_event({
                    'event': 'FILE_EXCLUDED',
                    'job_id': job_id,
                    'path': file_path,
                    'pattern': matching_pattern
                })
                excluded_count += 1
            else:
                # File is selected
                emit_event({
                    'event': 'FILE_SELECTED',
                    'job_id': job_id,
                    'path': file_path
                })
                selected_count += 1

    # Emit JOB_COMPLETED event
    emit_event({
        'event': 'JOB_COMPLETED',
        'job_id': job_id,
        'selected': selected_count,
        'excluded': excluded_count
    })


def main():
    args = parse_args()

    # Parse the 'now' timestamp
    now_dt = parse_iso8601(args.now)
    now_dt = floor_to_minute(now_dt)

    # Calculate window end (inclusive)
    duration_hours = args.duration
    window_end = now_dt + timedelta(hours=duration_hours)
    window_end = floor_to_minute(window_end)

    # Load schedule
    schedule = load_schedule(args.schedule)

    # Get schedule timezone
    schedule_tz = get_job_timezone(schedule)

    # Emit SCHEDULE_PARSED event (always first)
    jobs = schedule.get('jobs', [])
    emit_event({
        'event': 'SCHEDULE_PARSED',
        'timezone': str(schedule_tz),
        'jobs_total': len(jobs)
    })

    # Find all due jobs with their trigger times
    due_jobs = []
    for job in jobs:
        is_due, trigger_times = is_job_due(job, schedule_tz, now_dt, window_end)
        if is_due:
            for trigger_time in trigger_times:
                due_jobs.append((job, trigger_time))

    # Sort jobs by id ascending, then by trigger time
    due_jobs.sort(key=lambda x: (x[0]['id'], x[1]))

    # Process each due job
    for job, trigger_time in due_jobs:
        process_job(job, args.mount, trigger_time, schedule_tz)


if __name__ == '__main__':
    main()
