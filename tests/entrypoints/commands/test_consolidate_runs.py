from __future__ import annotations

import pandas as pd

from slop_code.entrypoints.commands.consolidate_runs import EXPECTED_MASS_COLS
from slop_code.entrypoints.commands.consolidate_runs import TEST_COLS
from slop_code.entrypoints.commands.consolidate_runs import (
    _normalize_solve_rates,
)
from slop_code.entrypoints.commands.consolidate_runs import check_mass_columns


def _build_complete_mass_record() -> dict[str, float]:
    return dict.fromkeys(EXPECTED_MASS_COLS, 1.0)


def test_check_mass_columns_passes_when_all_expected_present():
    record = _build_complete_mass_record()

    assert check_mass_columns(record) is False


def test_check_mass_columns_fails_when_complexity_concentration_missing():
    record = _build_complete_mass_record()
    del record["mass.cc"]

    assert check_mass_columns(record) is True


def test_check_mass_columns_only_requires_cc_mass():
    record = _build_complete_mass_record()
    record["mass.unused"] = 1.0

    assert check_mass_columns(record) is False


def test_test_cols_use_new_pass_rate_names_and_drop_elapsed():
    assert "strict_pass_rate" in TEST_COLS
    assert "isolated_pass_rate" in TEST_COLS
    assert "pass_rate" not in TEST_COLS
    assert "checkpoint_pass_rate" not in TEST_COLS
    assert "elapsed" not in TEST_COLS


def test_normalize_solve_rates_uses_max_num_checkpoints_as_denominator():
    # Crashed run produced only 50 checkpoints; full run produced 100.
    # Raw per-run pct inflates the crashed run's rate (10/50 = 20%) even
    # though 10/100 expected = 10% is the benchmark-relative truth.
    df = pd.DataFrame(
        [
            {
                "run_id": "crashed",
                "num_checkpoints": 50,
                "checkpoints_solved": 10,
                "checkpoints_iso_solved": 20,
                "checkpoints_core_solved": 30,
                "pct_checkpoints_solved": 20.0,
                "pct_checkpoints_iso_solved": 40.0,
                "pct_checkpoints_core_solved": 60.0,
            },
            {
                "run_id": "complete",
                "num_checkpoints": 100,
                "checkpoints_solved": 15,
                "checkpoints_iso_solved": 30,
                "checkpoints_core_solved": 50,
                "pct_checkpoints_solved": 15.0,
                "pct_checkpoints_iso_solved": 30.0,
                "pct_checkpoints_core_solved": 50.0,
            },
        ]
    )

    out = _normalize_solve_rates(df)

    assert list(out["expected_checkpoints"]) == [100, 100]
    assert list(out["pct_checkpoints_solved"]) == [10.0, 15.0]
    assert list(out["pct_checkpoints_iso_solved"]) == [20.0, 30.0]
    assert list(out["pct_checkpoints_core_solved"]) == [30.0, 50.0]
    # Raw counts preserved untouched.
    assert list(out["checkpoints_solved"]) == [10, 15]


def test_normalize_solve_rates_is_noop_without_num_checkpoints():
    df = pd.DataFrame([{"run_id": "x", "checkpoints_solved": 5}])

    out = _normalize_solve_rates(df)

    assert "expected_checkpoints" not in out.columns
    assert list(out["checkpoints_solved"]) == [5]
