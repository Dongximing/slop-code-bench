#!/usr/bin/env python3
"""
Backup Scheduler - A CLI-driven backup scheduler that parses YAML schedule files,
determines which jobs are due, simulates running them, and emits event history as JSON Lines.
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tarfile
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import yaml


def glob_to_regex(pattern: str) -> str:
    """
    Convert a glob pattern to a regex pattern.

    Glob rules:
    - `*` matches any sequence of characters except `/`
    - `?` matches any single character except `/`
    - `**` matches any sequence of characters including `/`
    - `[...]` character classes
    """
    result = []
    i = 0
    n = len(pattern)

    while i < n:
        c = pattern[i]

        if c == '*':
            # Check for **
            if i + 1 < n and pattern[i + 1] == '*':
                # ** matches anything including /
                result.append('.*')
                i += 2
                # Skip any following /
                if i < n and pattern[i] == '/':
                    i += 1
            else:
                # * matches anything except /
                result.append('[^/]*')
                i += 1
        elif c == '?':
            # ? matches any single character except /
            result.append('[^/]')
            i += 1
        elif c == '[':
            # Character class - find the closing ]
            j = i + 1
            if j < n and pattern[j] == '!':
                j += 1
            if j < n and pattern[j] == ']':
                j += 1
            while j < n and pattern[j] != ']':
                j += 1
            if j < n:
                result.append(pattern[i:j + 1])
                i = j + 1
            else:
                result.append(re.escape(c))
                i += 1
        elif c == '/':
            result.append('/')
            i += 1
        else:
            result.append(re.escape(c))
            i += 1

    return '^' + ''.join(result) + '$'


def matches_glob(path: str, pattern: str) -> bool:
    """
    Check if a path matches a glob pattern.

    The pattern is matched against the full path.
    """
    regex = glob_to_regex(pattern)
    return re.match(regex, path) is not None


def matches_any_pattern(path: str, patterns: list[str]) -> tuple[bool, str | None]:
    """
    Check if a path matches any of the given patterns.

    Returns (matched, first_matching_pattern).
    """
    for pattern in patterns:
        if matches_glob(path, pattern):
            return True, pattern
    return False, None


def floor_to_minute(dt: datetime) -> datetime:
    """Floor a datetime to the minute (remove seconds and microseconds)."""
    return dt.replace(second=0, microsecond=0)


def parse_iso_datetime(dt_str: str, tz: ZoneInfo | None = None) -> datetime:
    """Parse an ISO 8601 / RFC 3339 datetime string."""
    # Handle 'Z' suffix for UTC
    if dt_str.endswith('Z'):
        dt_str = dt_str[:-1] + '+00:00'

    # Try parsing with timezone info
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None and tz is not None:
            dt = dt.replace(tzinfo=tz)
        return dt
    except ValueError:
        pass

    # Try without timezone (will use provided tz)
    try:
        dt = datetime.fromisoformat(dt_str)
        if tz is not None:
            dt = dt.replace(tzinfo=tz)
        return dt
    except ValueError:
        raise ValueError(f"Invalid datetime format: {dt_str}")


def parse_time(time_str: str) -> tuple[int, int]:
    """Parse a time string in HH:MM format."""
    parts = time_str.split(':')
    if len(parts) != 2:
        raise ValueError(f"Invalid time format: {time_str}")
    return int(parts[0]), int(parts[1])


def get_weekday_number(day_name: str) -> int:
    """Convert 3-letter day name to weekday number (Mon=0, Sun=6)."""
    days = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
    return days[day_name.lower()]


def generate_trigger_times(job: dict[str, Any], schedule_tz: ZoneInfo,
                           start: datetime, end: datetime) -> list[datetime]:
    """Generate all trigger times for a job within the given window."""
    triggers = []

    when = job.get('when', {})
    kind = when.get('kind')

    if kind == 'once':
        # One-time job - parse the 'at' as ISO timestamp in schedule timezone
        at_str = when.get('at')
        if not at_str:
            return []

        # Parse without timezone, then apply schedule timezone
        try:
            trigger = datetime.fromisoformat(at_str)
            if trigger.tzinfo is None:
                trigger = trigger.replace(tzinfo=schedule_tz)
            trigger = floor_to_minute(trigger)

            # Check if within window (inclusive)
            if start <= trigger <= end:
                triggers.append(trigger)
        except ValueError:
            pass

    elif kind == 'daily':
        at_str = when.get('at')
        if not at_str:
            return []

        hour, minute = parse_time(at_str)

        # Convert start and end to schedule timezone for iteration
        start_local = start.astimezone(schedule_tz)
        end_local = end.astimezone(schedule_tz)

        # Start from the beginning of the day of start_local
        current_date = start_local.date()
        end_date = end_local.date()

        # Iterate day by day
        while True:
            trigger = datetime(current_date.year, current_date.month, current_date.day,
                             hour, minute, tzinfo=schedule_tz)
            trigger = floor_to_minute(trigger)

            # Check if within window (inclusive)
            if trigger > end:
                break
            if trigger >= start:
                triggers.append(trigger)

            current_date += timedelta(days=1)
            if current_date > end_date + timedelta(days=1):
                break

    elif kind == 'weekly':
        at_str = when.get('at')
        days = when.get('days', [])
        if not at_str or not days:
            return []

        hour, minute = parse_time(at_str)
        target_weekdays = {get_weekday_number(d) for d in days}

        # Convert start and end to schedule timezone for iteration
        start_local = start.astimezone(schedule_tz)
        end_local = end.astimezone(schedule_tz)

        # Start from the beginning of the day of start_local
        current_date = start_local.date()
        end_date = end_local.date()

        # Iterate day by day
        while True:
            if current_date.weekday() in target_weekdays:
                trigger = datetime(current_date.year, current_date.month, current_date.day,
                                 hour, minute, tzinfo=schedule_tz)
                trigger = floor_to_minute(trigger)

                # Check if within window (inclusive)
                if trigger > end:
                    break
                if trigger >= start:
                    triggers.append(trigger)

            current_date += timedelta(days=1)
            if current_date > end_date + timedelta(days=1):
                break

    return triggers


def format_datetime_with_tz(dt: datetime) -> str:
    """Format datetime as ISO 8601 with timezone (Z or ±HH:MM)."""
    # Convert to the datetime's timezone for display
    dt = dt.astimezone(dt.tzinfo)

    # Get the UTC offset
    offset = dt.utcoffset()
    if offset is None:
        # No timezone info, assume UTC
        return dt.strftime('%Y-%m-%dT%H:%M:%SZ')

    if offset == timedelta(0):
        # UTC - use 'Z'
        return dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    else:
        # Non-UTC - use ±HH:MM format
        sign = '+' if offset >= timedelta(0) else '-'
        offset = abs(offset)
        hours, remainder = divmod(offset.seconds, 3600)
        minutes = remainder // 60
        return dt.strftime('%Y-%m-%dT%H:%M:%S') + f'{sign}{hours:02d}:{minutes:02d}'


def emit_event(event: dict[str, Any]) -> None:
    """Emit a JSON event to stdout."""
    print(json.dumps(event, separators=(',', ':')), flush=True)


def get_all_files(mount_path: str, source_path: str) -> list[str]:
    """Get all files in the source directory tree, relative to source path."""
    files = []
    source_full = os.path.join(mount_path, source_path)

    if not os.path.exists(source_full):
        return files

    for root, _, filenames in os.walk(source_full):
        for filename in filenames:
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, source_full)
            files.append(rel_path)

    # Sort lexicographically
    files.sort()
    return files


def compute_file_checksum(file_path: str) -> str:
    """Compute SHA-256 checksum of a file."""
    sha256 = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            sha256.update(chunk)
    return f'sha256:{sha256.hexdigest()}'


def get_file_size(file_path: str) -> int:
    """Get the size of a file in bytes."""
    return os.path.getsize(file_path)


class PackBuilder:
    """Builds tar archives for the pack strategy."""

    def __init__(self, pack_id: int, trigger_time: datetime):
        self.pack_id = pack_id
        self.trigger_time = trigger_time
        self.files: list[tuple[str, int]] = []  # (path, size)
        self.total_content_size = 0
        self.tar_buffer = io.BytesIO()

    def add_file(self, path: str, size: int, content: bytes) -> None:
        """Add a file to the current pack."""
        self.files.append((path, size))
        self.total_content_size += size

        # Create tar entry with deterministic metadata
        info = tarfile.TarInfo(name=path)
        info.size = size
        info.mtime = 0
        info.mode = 0o644
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""

        # We need to write to the tar buffer
        if not hasattr(self, '_tar'):
            self._tar = tarfile.open(fileobj=self.tar_buffer, mode='w', format=tarfile.GNU_FORMAT)

        self._tar.addfile(info, io.BytesIO(content))

    def current_size(self) -> int:
        """Return current content size in pack."""
        return self.total_content_size

    def finalize(self) -> tuple[str, int, str, int]:
        """Finalize the pack and return (name, content_size, checksum, tar_size)."""
        if hasattr(self, '_tar'):
            self._tar.close()

        tar_bytes = self.tar_buffer.getvalue()
        tar_size = len(tar_bytes)

        sha256 = hashlib.sha256()
        sha256.update(tar_bytes)
        checksum = f'sha256:{sha256.hexdigest()}'

        name = f'pack-{self.pack_id}.tar'
        return name, self.total_content_size, checksum, tar_size


def run_full_strategy(job_id: str, mount_path: str, source_path: str, file_path: str) -> tuple[int, int]:
    """Run the 'full' backup strategy for a single file."""
    full_path = os.path.join(mount_path, source_path, file_path)
    size = get_file_size(full_path)
    checksum = compute_file_checksum(full_path)

    emit_event({
        'event': 'FILE_BACKED_UP',
        'job_id': job_id,
        'path': file_path,
        'size': size,
        'checksum': checksum
    })

    return size


def run_verify_strategy(job_id: str, mount_path: str, source_path: str, file_path: str) -> tuple[int, int]:
    """Run the 'verify' strategy for a single file."""
    full_path = os.path.join(mount_path, source_path, file_path)
    size = get_file_size(full_path)
    checksum = compute_file_checksum(full_path)

    emit_event({
        'event': 'FILE_VERIFIED',
        'job_id': job_id,
        'path': file_path,
        'size': size,
        'checksum': checksum
    })

    return size


def main() -> None:
    parser = argparse.ArgumentParser(description='Backup Scheduler')
    parser.add_argument('--schedule', required=True, help='Path to YAML schedule file')
    parser.add_argument('--now', required=True, help='Current time in ISO 8601 format')
    parser.add_argument('--duration', type=float, default=24, help='Duration in hours (default: 24)')
    parser.add_argument('--mount', required=True, help='Path to mounted files location')

    args = parser.parse_args()

    # Parse the 'now' time
    now = parse_iso_datetime(args.now)
    now = floor_to_minute(now)

    # Calculate end time (inclusive)
    duration_delta = timedelta(hours=args.duration)
    end = floor_to_minute(now + duration_delta)

    # Parse the YAML schedule
    with open(args.schedule, 'r') as f:
        schedule = yaml.safe_load(f)

    # Get schedule timezone
    tz_name = schedule.get('timezone', 'UTC')
    schedule_tz = ZoneInfo(tz_name)

    jobs = schedule.get('jobs', [])
    jobs_total = len(jobs)

    # Emit SCHEDULE_PARSED event
    emit_event({
        'event': 'SCHEDULE_PARSED',
        'timezone': tz_name,
        'jobs_total': jobs_total
    })

    # Collect all due jobs with their trigger times
    due_jobs = []  # List of (job, trigger_time)

    for job in jobs:
        # Skip disabled jobs
        if not job.get('enabled', True):
            continue

        job_id = job.get('id')
        triggers = generate_trigger_times(job, schedule_tz, now, end)

        for trigger in triggers:
            due_jobs.append((job, trigger))

    # Sort by job id (ascending), then by trigger time
    due_jobs.sort(key=lambda x: (x[0].get('id', ''), x[1]))

    # Process each due job
    for job, trigger_time in due_jobs:
        job_id = job.get('id')
        when = job.get('when', {})
        kind = when.get('kind')

        # Emit JOB_ELIGIBLE event
        now_local = trigger_time.astimezone(schedule_tz)
        emit_event({
            'event': 'JOB_ELIGIBLE',
            'job_id': job_id,
            'kind': kind,
            'now_local': format_datetime_with_tz(now_local)
        })

        # Get exclude patterns
        exclude_patterns = job.get('exclude', [])
        exclude_count = len(exclude_patterns)

        # Emit JOB_STARTED event
        emit_event({
            'event': 'JOB_STARTED',
            'job_id': job_id,
            'exclude_count': exclude_count
        })

        # Get source path
        source = job.get('source', 'mount://')
        if source.startswith('mount://'):
            source_path = source[len('mount://'):]
        else:
            source_path = source

        # Get all files in source directory
        files = get_all_files(args.mount, source_path)

        # Get strategy configuration
        strategy = job.get('strategy', {})
        strategy_kind = strategy.get('kind') if strategy else None

        # Emit STRATEGY_SELECTED event if strategy is present
        if strategy_kind:
            emit_event({
                'event': 'STRATEGY_SELECTED',
                'job_id': job_id,
                'kind': strategy_kind
            })

        selected_count = 0
        excluded_count = 0
        total_size = 0
        pack_count = 0

        # Initialize pack strategy state if needed
        current_pack: PackBuilder | None = None
        if strategy_kind == 'pack':
            options = strategy.get('options', {})
            max_pack_bytes = options.get('max_pack_bytes', 1048576)

        def finalize_current_pack():
            nonlocal pack_count
            if current_pack is None:
                return
            name, content_size, checksum, tar_size = current_pack.finalize()
            emit_event({
                'event': 'PACK_CREATED',
                'job_id': job_id,
                'name': name,
                'size': content_size,
                'timestamp': format_datetime_with_tz(trigger_time),
                'checksum': checksum,
                'tar_size': tar_size
            })

        for file_path in files:
            # Check if file is excluded
            is_excluded, matching_pattern = matches_any_pattern(file_path, exclude_patterns)

            if is_excluded:
                emit_event({
                    'event': 'FILE_EXCLUDED',
                    'job_id': job_id,
                    'path': file_path,
                    'pattern': matching_pattern
                })
                excluded_count += 1
            else:
                # Emit FILE_SELECTED event
                emit_event({
                    'event': 'FILE_SELECTED',
                    'job_id': job_id,
                    'path': file_path
                })
                selected_count += 1

                # Execute strategy for this file
                if strategy_kind == 'full':
                    size = run_full_strategy(job_id, args.mount, source_path, file_path)
                    total_size += size
                elif strategy_kind == 'verify':
                    size = run_verify_strategy(job_id, args.mount, source_path, file_path)
                    total_size += size
                elif strategy_kind == 'pack':
                    full_path = os.path.join(args.mount, source_path, file_path)
                    with open(full_path, 'rb') as f:
                        content = f.read()
                    size = len(content)
                    total_size += size

                    # Check if we need to finalize current pack before adding this file
                    if current_pack is not None:
                        if current_pack.current_size() + size > max_pack_bytes and current_pack.current_size() > 0:
                            finalize_current_pack()
                            current_pack = None

                    # Create new pack if needed
                    if current_pack is None:
                        pack_count += 1
                        current_pack = PackBuilder(pack_count, trigger_time)

                    # Add file to current pack
                    current_pack.add_file(file_path, size, content)

                    emit_event({
                        'event': 'FILE_PACKED',
                        'job_id': job_id,
                        'path': file_path,
                        'size': size,
                        'pack_id': current_pack.pack_id
                    })

        # Finalize last pack if any
        if current_pack is not None:
            finalize_current_pack()

        # Emit JOB_COMPLETED event
        completed_event: dict[str, Any] = {
            'event': 'JOB_COMPLETED',
            'job_id': job_id,
            'selected': selected_count,
            'excluded': excluded_count
        }

        if strategy_kind:
            completed_event['total_size'] = total_size
            if strategy_kind == 'pack':
                completed_event['packs'] = pack_count

        emit_event(completed_event)


if __name__ == '__main__':
    main()
