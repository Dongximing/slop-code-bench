#!/usr/bin/env python3
"""
Backup Scheduler - CLI-driven backup scheduler with YAML schedule parsing,
exclusion rules, backup strategies, and JSON Lines event history output.
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
    parser.add_argument(
        "--backup",
        help="Path to the backup destination directory (treated as backup:// root)"
    )
    return parser.parse_args()


def parse_iso8601(timestamp_str: str) -> datetime:
    """Parse ISO-8601/RFC 3339 timestamp string to a timezone-aware datetime."""
    ts = timestamp_str.replace('Z', '+00:00')
    dt = datetime.fromisoformat(ts)
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
    """Get all trigger times for a job within the given window."""
    when = job.get('when', {})
    kind = when.get('kind')
    at_str = when.get('at')

    triggers = []

    if kind == 'once':
        naive_dt = datetime.fromisoformat(at_str)
        local_dt = schedule_tz.localize(naive_dt)
        utc_dt = floor_to_minute(local_dt.astimezone(pytz.UTC))
        if window_start <= utc_dt <= window_end:
            triggers.append(utc_dt)

    elif kind == 'daily':
        hour, minute = map(int, at_str.split(':'))
        current = window_start.astimezone(schedule_tz)
        current = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        current_utc = current.astimezone(pytz.UTC)
        if current_utc < window_start:
            current = current + timedelta(days=1)

        while True:
            current_utc = floor_to_minute(current.astimezone(pytz.UTC))
            if current_utc > window_end:
                break
            if window_start <= current_utc <= window_end:
                triggers.append(current_utc)
            current = current + timedelta(days=1)

    elif kind == 'weekly':
        hour, minute = map(int, at_str.split(':'))
        days_set = parse_days(when.get('days', []))
        current = window_start.astimezone(schedule_tz)
        current = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        current_utc = current.astimezone(pytz.UTC)
        if current_utc < window_start:
            current = current + timedelta(days=1)

        while True:
            current_utc = floor_to_minute(current.astimezone(pytz.UTC))
            if current_utc > window_end:
                break
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
    """Check if a job is due within the window. Returns (is_due, trigger_times)."""
    if not job.get('enabled', True):
        return False, []

    triggers = get_trigger_times(job, schedule_tz, window_start, window_end)
    return (True, triggers) if triggers else (False, [])


def compile_glob_pattern(pattern: str) -> re.Pattern:
    """Compile a glob pattern to a regex. Supports: *, ?, **, []."""
    result = []
    i = 0
    while i < len(pattern):
        c = pattern[i]

        if c == '*' and i + 1 < len(pattern) and pattern[i + 1] == '*':
            if i + 2 < len(pattern) and pattern[i + 2] == '/':
                result.append('(.*/)?')
                i += 3
            else:
                result.append('.*')
                i += 2
        elif c == '*':
            result.append('[^/]*')
            i += 1
        elif c == '?':
            result.append('[^/]')
            i += 1
        elif c == '[':
            j = i + 1
            while j < len(pattern) and pattern[j] != ']':
                if pattern[j] == '\\' and j + 1 < len(pattern):
                    j += 2
                else:
                    j += 1
            if j < len(pattern):
                j += 1
            result.append(pattern[i:j])
            i = j
        elif c in '.^$+{}()|\\':
            result.append('\\' + c)
            i += 1
        elif c == '/':
            result.append('/')
            i += 1
        else:
            result.append(c)
            i += 1

    return re.compile('^' + ''.join(result) + '$')


def matches_pattern(path: str, pattern: str) -> bool:
    """Check if a path matches a glob pattern."""
    return compile_glob_pattern(pattern).match(path) is not None


def find_matching_pattern(path: str, patterns: List[str]) -> Optional[str]:
    """Find the first pattern that matches the path."""
    for pattern in patterns:
        if matches_pattern(path, pattern):
            return pattern
    return None


def get_all_files(root_path: Path) -> List[str]:
    """Get all files in the directory tree, sorted lexicographically by relative path."""
    files = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        for filename in filenames:
            full_path = Path(dirpath) / filename
            rel_path = full_path.relative_to(root_path)
            files.append(str(rel_path).replace(os.sep, '/'))
    files.sort()
    return files


def emit_event(event: Dict[str, Any]) -> None:
    """Emit a JSON event to stdout (compact format, newline-terminated)."""
    print(json.dumps(event, separators=(',', ':')))


def format_local_time(dt: datetime, tz: pytz.BaseTzInfo) -> str:
    """Format a datetime in local time with timezone offset."""
    local_dt = dt.astimezone(tz)
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
    """Resolve the mount:// source to an actual filesystem path."""
    if source.startswith('mount://'):
        subpath = source[8:]
        if subpath:
            return Path(mount_path) / subpath
        return Path(mount_path)
    raise ValueError(f"Invalid source scheme: {source}")


def resolve_destination_path(destination: str, backup_path: str) -> Path:
    """Resolve the backup:// destination to an actual filesystem path."""
    if destination.startswith('backup://'):
        subpath = destination[9:]
        if subpath:
            return Path(backup_path) / subpath
        return Path(backup_path)
    raise ValueError(f"Invalid destination scheme: {destination}")


def load_destination_state(job_id: str, dest_path: Path) -> Dict[str, str]:
    """Load existing backup state for a job. Returns dict of {relative_path: checksum}."""
    state: Dict[str, str] = {}
    if not dest_path.exists():
        return state

    for dirpath, dirnames, filenames in os.walk(dest_path):
        for filename in filenames:
            full_path = Path(dirpath) / filename
            rel_path = full_path.relative_to(dest_path)
            path_str = str(rel_path).replace(os.sep, '/')
            _, checksum = compute_file_checksum(full_path)
            state[path_str] = checksum

    return state


def compute_file_checksum(file_path: Path) -> Tuple[int, str]:
    """Compute SHA-256 checksum of a file. Returns (size, 'sha256:{hex}')."""
    sha256_hash = hashlib.sha256()
    size = 0
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            sha256_hash.update(chunk)
            size += len(chunk)
    return size, f'sha256:{sha256_hash.hexdigest()}'


def compute_tar_checksum(tar_bytes: bytes) -> str:
    """Compute SHA-256 checksum of tar archive bytes. Returns 'sha256:{hex}'."""
    return f'sha256:{hashlib.sha256(tar_bytes).hexdigest()}'


def process_job(
    job: Dict[str, Any],
    mount_path: str,
    backup_path: Optional[str],
    trigger_time: datetime,
    schedule_tz: pytz.BaseTzInfo
) -> None:
    """Process a single job: emit events for files and summary."""
    job_id = job['id']
    exclude_patterns = job.get('exclude', [])
    source = job.get('source', 'mount://')
    destination = job.get('destination', 'backup://')
    strategy = job.get('strategy')

    now_local = format_local_time(trigger_time, schedule_tz)
    kind = job.get('when', {}).get('kind', 'daily')
    emit_event({
        'event': 'JOB_ELIGIBLE',
        'job_id': job_id,
        'kind': kind,
        'now_local': now_local
    })

    emit_event({
        'event': 'JOB_STARTED',
        'job_id': job_id,
        'exclude_count': len(exclude_patterns)
    })

    strategy_kind = None
    strategy_options = {}
    if strategy:
        strategy_kind = strategy.get('kind') if isinstance(strategy, dict) else strategy
        strategy_options = strategy.get('options', {}) if isinstance(strategy, dict) else {}
        emit_event({
            'event': 'STRATEGY_SELECTED',
            'job_id': job_id,
            'kind': strategy_kind
        })

    source_path = resolve_source_path(source, mount_path)

    # Determine destination path for this job
    dest_state: Dict[str, str] = {}
    dest_state_files = 0
    dest_path = None

    if backup_path and destination:
        dest_base = resolve_destination_path(destination, backup_path)
        dest_path = dest_base / job_id

        # Load existing backup state for incremental backups (only for non-pack strategies)
        if dest_path.exists() and strategy_kind != 'pack':
            dest_state = load_destination_state(job_id, dest_path)
            dest_state_files = len(dest_state)
            if dest_state_files > 0:
                emit_event({
                    'event': 'DEST_STATE_LOADED',
                    'job_id': job_id,
                    'files_total': dest_state_files
                })

    selected_count = 0
    excluded_count = 0
    total_size = 0
    packs = 0
    files_skipped_unchanged = 0

    if source_path.exists() and source_path.is_dir():
        files = get_all_files(source_path)

        if strategy_kind == 'pack':
            packs, total_size, selected_count, excluded_count = process_pack_strategy_inline(
                job_id, source_path, files, exclude_patterns, strategy_options, trigger_time, schedule_tz
            )
        else:
            selected_files: List[str] = []
            for file_path in files:
                matching_pattern = find_matching_pattern(file_path, exclude_patterns)

                if matching_pattern:
                    emit_event({
                        'event': 'FILE_EXCLUDED',
                        'job_id': job_id,
                        'path': file_path,
                        'pattern': matching_pattern
                    })
                    excluded_count += 1
                else:
                    emit_event({
                        'event': 'FILE_SELECTED',
                        'job_id': job_id,
                        'path': file_path
                    })
                    selected_files.append(file_path)

            selected_count = len(selected_files)

            if strategy_kind == 'full':
                total_size, files_skipped_unchanged = process_full_strategy(
                    job_id, source_path, selected_files, dest_path, dest_state
                )
            elif strategy_kind == 'verify':
                total_size = process_verify_strategy(job_id, source_path, selected_files)

    completed_event = {
        'event': 'JOB_COMPLETED',
        'job_id': job_id,
        'selected': selected_count,
        'excluded': excluded_count
    }

    if strategy and (strategy.get('kind') if isinstance(strategy, dict) else strategy) == 'pack':
        completed_event['packs'] = packs
        completed_event['total_size'] = total_size
    elif strategy:
        strategy_kind_check = strategy.get('kind') if isinstance(strategy, dict) else strategy
        if strategy_kind_check in ('full', 'verify'):
            completed_event['total_size'] = total_size
            completed_event['files_skipped_unchanged'] = files_skipped_unchanged
            completed_event['dest_state_files'] = dest_state_files

    emit_event(completed_event)


def copy_file_to_destination(source_file: Path, dest_file: Path) -> None:
    """Copy a file to the destination directory, creating directories as needed."""
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    with open(source_file, 'rb') as src:
        with open(dest_file, 'wb') as dst:
            dst.write(src.read())


def process_full_strategy(
    job_id: str,
    source_path: Path,
    selected_files: List[str],
    dest_path: Optional[Path],
    dest_state: Dict[str, str]
) -> Tuple[int, int]:
    """Process files with 'full' strategy - backs up each file individually.
    Returns (total_size, files_skipped_unchanged)."""
    total_size = 0
    files_skipped_unchanged = 0

    for file_path in selected_files:
        full_path = source_path / file_path
        size, checksum = compute_file_checksum(full_path)

        # Check if file already exists in destination with same checksum (incremental)
        if dest_path and file_path in dest_state and dest_state[file_path] == checksum:
            emit_event({
                'event': 'FILE_SKIPPED_UNCHANGED',
                'job_id': job_id,
                'path': file_path,
                'hash': checksum
            })
            files_skipped_unchanged += 1
        else:
            total_size += size
            emit_event({
                'event': 'FILE_BACKED_UP',
                'job_id': job_id,
                'path': file_path,
                'size': size,
                'checksum': checksum
            })
            # Copy file to destination if dest_path is provided
            if dest_path:
                dest_file = dest_path / file_path
                copy_file_to_destination(full_path, dest_file)

    return total_size, files_skipped_unchanged


def process_verify_strategy(job_id: str, source_path: Path, selected_files: List[str]) -> int:
    """Process files with 'verify' strategy - verifies each file without copying."""
    total_size = 0
    for file_path in selected_files:
        full_path = source_path / file_path
        size, checksum = compute_file_checksum(full_path)
        total_size += size
        emit_event({
            'event': 'FILE_VERIFIED',
            'job_id': job_id,
            'path': file_path,
            'size': size,
            'checksum': checksum
        })
    return total_size


def process_pack_strategy_inline(
    job_id: str,
    source_path: Path,
    files: List[str],
    exclude_patterns: List[str],
    options: Dict[str, Any],
    trigger_time: datetime,
    schedule_tz: pytz.BaseTzInfo
) -> Tuple[int, int, int, int]:
    """Process files with 'pack' strategy. Returns (packs, total_size, selected, excluded)."""
    max_pack_bytes = options.get('max_pack_bytes', 1048576)
    pack_index = 1
    total_size = 0
    selected_count = 0
    excluded_count = 0

    current_pack_files: List[Tuple[str, int]] = []
    current_pack_size = 0

    for file_path in files:
        matching_pattern = find_matching_pattern(file_path, exclude_patterns)

        if matching_pattern:
            emit_event({
                'event': 'FILE_EXCLUDED',
                'job_id': job_id,
                'path': file_path,
                'pattern': matching_pattern
            })
            excluded_count += 1
        else:
            emit_event({
                'event': 'FILE_SELECTED',
                'job_id': job_id,
                'path': file_path
            })
            selected_count += 1

            full_path = source_path / file_path
            file_size = full_path.stat().st_size

            if current_pack_files and current_pack_size + file_size > max_pack_bytes:
                pack_name, pack_content_size, tar_size, checksum = create_pack_archive(
                    source_path, current_pack_files, pack_index
                )
                timestamp = format_local_time(trigger_time, schedule_tz)
                emit_event({
                    'event': 'PACK_CREATED',
                    'job_id': job_id,
                    'name': pack_name,
                    'size': pack_content_size,
                    'timestamp': timestamp,
                    'checksum': checksum,
                    'tar_size': tar_size
                })
                pack_index += 1
                current_pack_files = []
                current_pack_size = 0

            emit_event({
                'event': 'FILE_PACKED',
                'job_id': job_id,
                'pack_id': pack_index,
                'path': file_path,
                'size': file_size
            })
            current_pack_files.append((file_path, file_size))
            current_pack_size += file_size
            total_size += file_size

    if current_pack_files:
        pack_name, pack_content_size, tar_size, checksum = create_pack_archive(
            source_path, current_pack_files, pack_index
        )
        timestamp = format_local_time(trigger_time, schedule_tz)
        emit_event({
            'event': 'PACK_CREATED',
            'job_id': job_id,
            'name': pack_name,
            'size': pack_content_size,
            'timestamp': timestamp,
            'checksum': checksum,
            'tar_size': tar_size
        })

    return pack_index, total_size, selected_count, excluded_count


def process_pack_strategy(
    job_id: str,
    source_path: Path,
    selected_files: List[str],
    options: Dict[str, Any],
    trigger_time: datetime,
    schedule_tz: pytz.BaseTzInfo
) -> Tuple[int, int]:
    """Process files with 'pack' strategy. Returns (packs, total_size)."""
    max_pack_bytes = options.get('max_pack_bytes', 1048576)
    pack_index = 1
    total_size = 0

    current_pack_files: List[Tuple[str, int]] = []
    current_pack_size = 0

    for file_path in selected_files:
        full_path = source_path / file_path
        file_size = full_path.stat().st_size

        if current_pack_files and current_pack_size + file_size > max_pack_bytes:
            pack_name, pack_content_size, tar_size, checksum = create_pack_archive(
                source_path, current_pack_files, pack_index
            )
            timestamp = format_local_time(trigger_time, schedule_tz)
            emit_event({
                'event': 'PACK_CREATED',
                'job_id': job_id,
                'name': pack_name,
                'size': pack_content_size,
                'timestamp': timestamp,
                'checksum': checksum,
                'tar_size': tar_size
            })
            pack_index += 1
            current_pack_files = []
            current_pack_size = 0

        emit_event({
            'event': 'FILE_PACKED',
            'job_id': job_id,
            'pack_id': pack_index,
            'path': file_path,
            'size': file_size
        })
        current_pack_files.append((file_path, file_size))
        current_pack_size += file_size
        total_size += file_size

    if current_pack_files:
        pack_name, pack_content_size, tar_size, checksum = create_pack_archive(
            source_path, current_pack_files, pack_index
        )
        timestamp = format_local_time(trigger_time, schedule_tz)
        emit_event({
            'event': 'PACK_CREATED',
            'job_id': job_id,
            'name': pack_name,
            'size': pack_content_size,
            'timestamp': timestamp,
            'checksum': checksum,
            'tar_size': tar_size
        })

    return pack_index, total_size


def create_pack_archive(
    source_path: Path,
    files: List[Tuple[str, int]],
    pack_index: int
) -> Tuple[str, int, int, str]:
    """Create a tar archive in memory. Returns (name, content_size, tar_size, checksum)."""
    tar_buffer = io.BytesIO()

    with tarfile.open(fileobj=tar_buffer, mode='w', format=tarfile.GNU_FORMAT) as tar:
        for file_path, _ in files:
            full_path = source_path / file_path

            tarinfo = tarfile.TarInfo(name=file_path)
            tarinfo.mtime = 0
            tarinfo.mode = 0o644
            tarinfo.uid = 0
            tarinfo.gid = 0
            tarinfo.uname = ''
            tarinfo.gname = ''

            with open(full_path, 'rb') as f:
                data = f.read()
                tarinfo.size = len(data)
                tar.addfile(tarinfo, io.BytesIO(data))

    tar_bytes = tar_buffer.getvalue()
    tar_size = len(tar_bytes)
    checksum = compute_tar_checksum(tar_bytes)
    content_size = sum(size for _, size in files)

    return f'pack-{pack_index}.tar', content_size, tar_size, checksum


def main():
    args = parse_args()

    now_dt = floor_to_minute(parse_iso8601(args.now))
    window_end = floor_to_minute(now_dt + timedelta(hours=args.duration))

    schedule = load_schedule(args.schedule)
    schedule_tz = get_job_timezone(schedule)

    jobs = schedule.get('jobs', [])
    emit_event({
        'event': 'SCHEDULE_PARSED',
        'timezone': str(schedule_tz),
        'jobs_total': len(jobs)
    })

    due_jobs = []
    for job in jobs:
        is_due, trigger_times = is_job_due(job, schedule_tz, now_dt, window_end)
        if is_due:
            for trigger_time in trigger_times:
                due_jobs.append((job, trigger_time))

    due_jobs.sort(key=lambda x: (x[0]['id'], x[1]))

    backup_path = getattr(args, 'backup', None)
    for job, trigger_time in due_jobs:
        process_job(job, args.mount, backup_path, trigger_time, schedule_tz)


if __name__ == '__main__':
    main()
