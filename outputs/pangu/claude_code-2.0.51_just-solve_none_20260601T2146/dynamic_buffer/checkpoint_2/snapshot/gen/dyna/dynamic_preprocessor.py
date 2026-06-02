"""Auto-generated DynamicPreprocessor module with stateful transforms."""

import csv
import hashlib
import json
import os
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Any


def _infer_value_type(value: Any) -> Any:
    if value is None or value == "":
        return None
    lower = str(value).lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return str(value)


def _apply_filter(row: Dict[str, Any], row_index: int) -> bool:
    return str(row.get('active', '')) == 'true'


def _apply_transform(row: Dict[str, Any], row_index: int, state_manager: '_StatefulStateManager' = None) -> Optional[Dict[str, Any]]:
    output_row = {}
    output_row['id'] = _infer_value_type(str(row.get('id', '')))
    output_row['name'] = _infer_value_type(str(row.get('name', '')))
    return output_row



class _StatefulStateManager:
    """Manages state for all stateful transforms."""

    def __init__(self):
        self._prefix_sums = {}
        self._prefix_counts = {}
        self._prefix_avgs = {}
        self._window_buffers = {}
        self._state_machine_states = {}
        self._deferred_rows = []

    def update_prefix_sum(self, col: str, value: float) -> float:
        if col not in self._prefix_sums:
            self._prefix_sums[col] = 0.0
        self._prefix_sums[col] += value
        return self._prefix_sums[col]

    def update_prefix_avg(self, col: str, value: float) -> float:
        if col not in self._prefix_counts:
            self._prefix_counts[col] = 0
            self._prefix_sums[col] = 0.0
        self._prefix_counts[col] += 1
        self._prefix_sums[col] += value
        return self._prefix_sums[col] / self._prefix_counts[col]

    def update_window(self, col: str, value: float, window_size: int) -> float:
        if col not in self._window_buffers:
            self._window_buffers[col] = {}
        if window_size not in self._window_buffers[col]:
            self._window_buffers[col][window_size] = []
        buffer = self._window_buffers[col][window_size]
        buffer.append(value)
        if len(buffer) > window_size:
            buffer.pop(0)
        return sum(buffer) / len(buffer)

    def update_state_machine(self, col: str, value: float, threshold: float, current_state: int) -> int:
        if value >= threshold:
            return current_state + 1
        return current_state

    def get_state_machine_initial(self, col: str, initial: int) -> int:
        if col not in self._state_machine_states:
            self._state_machine_states[col] = initial
        return self._state_machine_states[col]

    def set_state_machine_state(self, col: str, state: int):
        self._state_machine_states[col] = state

    def get_state(self) -> dict:
        return {
            'prefix_sums': self._prefix_sums,
            'prefix_counts': self._prefix_counts,
            'prefix_avgs': self._prefix_avgs,
            'window_buffers': self._window_buffers,
            'state_machine_states': self._state_machine_states,
            'deferred_rows': self._deferred_rows,
        }

    def set_state(self, state: dict):
        self._prefix_sums = state.get('prefix_sums', {})
        self._prefix_counts = state.get('prefix_counts', {})
        self._prefix_avgs = state.get('prefix_avgs', {})
        self._window_buffers = state.get('window_buffers', {})
        self._state_machine_states = state.get('state_machine_states', {})
        self._deferred_rows = state.get('deferred_rows', [])


def _apply_stateful_transform(row: Dict[str, Any], state_manager: '_StatefulStateManager', row_index: int) -> Optional[Dict[str, Any]]:
    output_row = dict(row)
    if 'id' in output_row:
        del output_row['id']
    output_row['id'] = state_manager.update_window('id', float(row.get('id', 0) or 0), 1)
    return output_row


class DynamicPreprocessor:
    def __init__(self, buffer: int, cache_dir: Optional[str] = None):
        self.buffer = buffer
        self.cache_dir = cache_dir
        self._format = 'csv'
        self._output_columns = ['id', 'name']
        if cache_dir:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)

    def __call__(self, path: str) -> Iterator[Dict[str, Any]]:
        return self._process(path)

    def _get_cache_path(self, input_path: str) -> str:
        key = hashlib.md5(input_path.encode()).hexdigest()
        return os.path.join(self.cache_dir, f"{key}_state.json")

    def _load_from_cache(self, input_path: str) -> dict:
        if not self.cache_dir:
            return {}
        cache_path = self._get_cache_path(input_path)
        if not os.path.exists(cache_path):
            return {}
        try:
            with open(cache_path, 'r') as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_to_cache(self, input_path: str, state: dict):
        if not self.cache_dir:
            return
        cache_path = self._get_cache_path(input_path)
        with open(cache_path, 'w') as f:
            json.dump(state, f)

    def _process(self, path: str) -> Iterator[Dict[str, Any]]:
        # Load saved state
        saved_state = self._load_from_cache(path)
        state_manager = _StatefulStateManager()
        state_manager.set_state(saved_state.get('state_manager', {}))

        # Get next row index to process
        next_row_idx = saved_state.get('next_row_idx', 0)

        buffer_rows = []
        buffer_indices = []

        def save_state():
            state_dict = {
                'state_manager': state_manager.get_state(),
                'next_row_idx': next_row_idx + len(buffer_rows),
            }
            self._save_to_cache(path, state_dict)

        if self._format == 'csv':
            with open(path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for idx, row in enumerate(reader):
                    if idx < next_row_idx:
                        continue
                    buffer_rows.append(row)
                    buffer_indices.append(idx)
                    if len(buffer_rows) >= self.buffer:
                        for row_data, row_idx in zip(buffer_rows, buffer_indices):
                            result = _apply_stateful_transform(row_data, idx, state_manager)
                            if result:
                                yield result
                        next_row_idx = idx + 1
                        buffer_rows, buffer_indices = [], []
                        save_state()
                # Flush remaining
                for row_data, row_idx in zip(buffer_rows, buffer_indices):
                    result = _apply_stateful_transform(row_data, idx, state_manager)
                    if result:
                        yield result

        elif self._format == 'tsv':
            with open(path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f, delimiter='\t')
                for idx, row in enumerate(reader):
                    if idx < next_row_idx:
                        continue
                    buffer_rows.append(row)
                    buffer_indices.append(idx)
                    if len(buffer_rows) >= self.buffer:
                        for row_data, row_idx in zip(buffer_rows, buffer_indices):
                            result = _apply_stateful_transform(row_data, idx, state_manager)
                            if result:
                                yield result
                        next_row_idx = idx + 1
                        buffer_rows, buffer_indices = [], []
                        save_state()
                for row_data, row_idx in zip(buffer_rows, buffer_indices):
                    result = _apply_stateful_transform(row_data, idx, state_manager)
                    if result:
                        yield result

        elif self._format == 'jsonl':
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                for idx, line in enumerate(lines):
                    if idx < next_row_idx:
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        buffer_rows.append(row)
                        buffer_indices.append(idx)
                        if len(buffer_rows) >= self.buffer:
                            for row_data, row_idx in zip(buffer_rows, buffer_indices):
                                result = _apply_stateful_transform(row_data, idx, state_manager)
                                if result:
                                    yield result
                            next_row_idx = idx + 1
                            buffer_rows, buffer_indices = [], []
                            save_state()
                    except json.JSONDecodeError:
                        pass
                for row_data, row_idx in zip(buffer_rows, buffer_indices):
                    result = _apply_stateful_transform(row_data, idx, state_manager)
                    if result:
                        yield result

        elif self._format == 'json':
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if not isinstance(data, list):
                    data = [data]
                for idx, row in enumerate(data):
                    if idx < next_row_idx:
                        continue
                    buffer_rows.append(row)
                    buffer_indices.append(idx)
                    if len(buffer_rows) >= self.buffer:
                        for row_data, row_idx in zip(buffer_rows, buffer_indices):
                            result = _apply_stateful_transform(row_data, idx, state_manager)
                            if result:
                                yield result
                        next_row_idx = idx + 1
                        buffer_rows, buffer_indices = [], []
                        save_state()
                for row_data, row_idx in zip(buffer_rows, buffer_indices):
                    result = _apply_stateful_transform(row_data, idx, state_manager)
                    if result:
                        yield result

        # Final save
        save_state()
