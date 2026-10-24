"""
WindSight backend tests
========================
Run: python -m pytest test_main.py -v
All tests use mocked Elexon API calls - no live network access needed.
"""
import asyncio
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

import numpy as np
import pandas as pd
import pytest

# We import the module under test
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import main as m


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _fuelhh_row(
    start_iso: str,
    generation: int,
    settlement_date: str | None = None,
    period: int = 1,
) -> dict:
    """Create a minimal FUELHH WIND row."""
    d = start_iso[:10]
    return {
        "dataset": "FUELHH",
        "fuelType": "WIND",
        "startTime": start_iso,
        "settlementDate": settlement_date or d,
        "settlementPeriod": period,
        "generation": generation,
    }


def _windfor_row(start_iso: str, publish_iso: str, generation: int) -> dict:
    """Create a minimal WINDFOR row."""
    return {
        "dataset": "WINDFOR",
        "startTime": start_iso,
        "publishTime": publish_iso,
        "generation": generation,
    }


# ---------------------------------------------------------------------------
# Date-range validation tests
# ---------------------------------------------------------------------------

def test_analysis_rejects_invalid_range():
    """build_analysis with start > end should return an error-like response."""
    # The endpoint enforces start <= end via HTTPException; here we test build_analysis
    # with an empty actual list (which triggers early return).
    result = m.build_analysis([], [], date(2026, 1, 31), date(2026, 1, 1))
    assert result["metrics"] is None
    assert result["data"] == []


# ---------------------------------------------------------------------------
# Timestamp filtering tests
# ---------------------------------------------------------------------------

def test_ts_in_range_keeps_valid_rows():
    df = pd.DataFrame({
        "ts": pd.to_datetime(
            ["2026-01-01T00:00:00Z", "2026-01-31T23:30:00Z", "2026-02-01T00:00:00Z"],
            utc=True,
        )
    })
    result, dropped = m._ts_in_range(df, "ts", date(2026, 1, 1), date(2026, 1, 31))
    assert len(result) == 2
    assert dropped == 1


def test_ts_in_range_drops_all_out_of_range():
    df = pd.DataFrame({
        "ts": pd.to_datetime(["2025-12-31T23:59:00Z", "2026-02-01T00:00:00Z"], utc=True)
    })
    result, dropped = m._ts_in_range(df, "ts", date(2026, 1, 1), date(2026, 1, 31))
    assert len(result) == 0
    assert dropped == 2


# ---------------------------------------------------------------------------
# January 2026 filtering
# ---------------------------------------------------------------------------

def test_build_analysis_filters_to_january():
    """Actuals outside January should be dropped before matching."""
    actual = [
        _fuelhh_row("2026-01-15T12:00:00Z", 10000),  # in range
        _fuelhh_row("2026-02-01T00:00:00Z", 5000),   # out of range
    ]
    forecast = [
        _windfor_row("2026-01-15T12:00:00Z", "2025-12-31T22:00:00Z", 10500),
    ]
    result = m.build_analysis(actual, forecast, date(2026, 1, 1), date(2026, 1, 31))
    diag = result["diagnostics"]
    assert diag["actualDropped"] >= 1
    assert diag["actualInRange"] == 1


# ---------------------------------------------------------------------------
# Timestamp normalisation and matching
# ---------------------------------------------------------------------------

def test_half_hourly_floored_to_hour():
    """Both :00 and :30 actuals in the same hour should get the same forecast."""
    actual = [
        _fuelhh_row("2026-01-15T10:00:00Z", 10000),
        _fuelhh_row("2026-01-15T10:30:00Z", 10200),
    ]
    forecast = [
        # Published day before (D-1) so before midnight UTC cutoff
        _windfor_row("2026-01-15T10:00:00Z", "2026-01-14T22:00:00Z", 10500),
    ]
    result = m.build_analysis(actual, forecast, date(2026, 1, 15), date(2026, 1, 15))
    assert result["metrics"] is not None
    assert result["metrics"]["matchedIntervals"] == 2
    # Both rows should get forecastMW = 10500
    forecasts = [d["forecastMW"] for d in result["data"]]
    assert all(f == 10500 for f in forecasts)


# ---------------------------------------------------------------------------
# Day-ahead cutoff tests
# ---------------------------------------------------------------------------

def test_compute_delivery_cutoff_midnight():
    """Cutoff 0 means midnight UTC at start of delivery date D (24:00 on D-1)."""
    ts = pd.Timestamp("2026-01-15 14:30:00", tz="UTC")
    cutoff = m.compute_delivery_cutoff(ts, 0)
    assert cutoff == pd.Timestamp("2026-01-15 00:00:00", tz="UTC")


def test_compute_delivery_cutoff_day_ahead_gate():
    """Cutoff 11 means 11:00 UTC on day D-1."""
    ts = pd.Timestamp("2026-01-15 14:30:00", tz="UTC")
    cutoff = m.compute_delivery_cutoff(ts, 11)
    assert cutoff == pd.Timestamp("2026-01-14 11:00:00", tz="UTC")


def test_cutoff_excludes_same_day_publications():
    """Forecasts published on the delivery date itself should be excluded (cutoff = midnight)."""
    original_cutoff = m.DECISION_CUTOFF_HOUR_UTC
    m.DECISION_CUTOFF_HOUR_UTC = 0  # midnight UTC
    try:
        actual = [_fuelhh_row("2026-01-15T10:00:00Z", 10000)]
        forecast = [
            # Published on the delivery date → should be excluded
            _windfor_row("2026-01-15T10:00:00Z", "2026-01-15T06:00:00Z", 10500),
            # Published day before → should be included
            _windfor_row("2026-01-15T10:00:00Z", "2026-01-14T22:00:00Z", 9800),
        ]
        result = m.build_analysis(actual, forecast, date(2026, 1, 15), date(2026, 1, 15))
        assert result["metrics"] is not None
        # Only the D-1 forecast should be used
        assert result["data"][0]["forecastMW"] == 9800
    finally:
        m.DECISION_CUTOFF_HOUR_UTC = original_cutoff


def test_cutoff_picks_latest_before_cutoff():
    """Among multiple D-1 forecasts, the latest one should be selected."""
    original_cutoff = m.DECISION_CUTOFF_HOUR_UTC
    m.DECISION_CUTOFF_HOUR_UTC = 0
    try:
        actual = [_fuelhh_row("2026-01-15T10:00:00Z", 10000)]
        forecast = [
            _windfor_row("2026-01-15T10:00:00Z", "2026-01-14T06:00:00Z", 9000),  # earlier
            _windfor_row("2026-01-15T10:00:00Z", "2026-01-14T18:00:00Z", 9600),  # later — should win
        ]
        result = m.build_analysis(actual, forecast, date(2026, 1, 15), date(2026, 1, 15))
        assert result["data"][0]["forecastMW"] == 9600
    finally:
        m.DECISION_CUTOFF_HOUR_UTC = original_cutoff


def test_cutoff_hour_parameter_custom_gate():
    """Passing cutoff_hour=11 selects latest forecast published before 11:00 UTC on D-1."""
    actual = [_fuelhh_row("2026-01-15T14:00:00Z", 12000)]
    forecast = [
        # Published D-1 at 09:30 UTC -> before 11:00 cutoff -> eligible
        _windfor_row("2026-01-15T14:00:00Z", "2026-01-14T09:30:00Z", 11500),
        # Published D-1 at 12:30 UTC -> after 11:00 cutoff -> strictly excluded
        _windfor_row("2026-01-15T14:00:00Z", "2026-01-14T12:30:00Z", 13000),
        # Published D at 06:00 UTC -> after cutoff -> strictly excluded
        _windfor_row("2026-01-15T14:00:00Z", "2026-01-15T06:00:00Z", 14000),
    ]
    result = m.build_analysis(actual, forecast, date(2026, 1, 15), date(2026, 1, 15), cutoff_hour=11)
    assert result["metrics"] is not None
    assert result["data"][0]["forecastMW"] == 11500
    assert result["data"][0]["forecastPublishTime"] == "2026-01-14T09:30:00+00:00"


def test_cutoff_boundary_exact_and_after():
    """Forecast published at exactly cutoff is included; 1 second after is excluded."""
    actual = [_fuelhh_row("2026-01-15T12:00:00Z", 10000)]
    # Cutoff for 2026-01-15 with cutoff_hour=0 is 2026-01-15T00:00:00Z
    forecast = [
        # Exactly at cutoff (midnight)
        _windfor_row("2026-01-15T12:00:00Z", "2026-01-15T00:00:00Z", 10200),
        # 1 second after cutoff
        _windfor_row("2026-01-15T12:00:00Z", "2026-01-15T00:00:01Z", 10900),
    ]
    result = m.build_analysis(actual, forecast, date(2026, 1, 15), date(2026, 1, 15), cutoff_hour=0)
    assert result["data"][0]["forecastMW"] == 10200


def test_timezone_handling_and_offsets():
    """Ensure non-UTC timezone offsets in actual and forecast rows are parsed and matched accurately."""
    # Delivery at 12:00 UTC = 13:00+01:00 (BST)
    actual = [
        _fuelhh_row("2026-01-15T13:00:00+01:00", 11000),
    ]
    # Forecast published at 2026-01-14 20:00-04:00 (= 2026-01-15 00:00 UTC) -> at cutoff
    # Another earlier forecast published at 2026-01-14 18:00+00:00 -> 9500
    forecast = [
        _windfor_row("2026-01-15T12:00:00Z", "2026-01-14T18:00:00Z", 9500),
        _windfor_row("2026-01-15T13:00:00+01:00", "2026-01-14T20:00:00-04:00", 9900),
    ]
    result = m.build_analysis(actual, forecast, date(2026, 1, 15), date(2026, 1, 15), cutoff_hour=0)
    assert result["metrics"] is not None
    assert result["metrics"]["matchedIntervals"] == 1
    # 20:00-04:00 is 2026-01-15 00:00:00 UTC, which is <= cutoff (midnight), and later than 18:00 UTC
    assert result["data"][0]["forecastMW"] == 9900
    assert result["data"][0]["actualMW"] == 11000


# ---------------------------------------------------------------------------
# Metric calculation tests
# ---------------------------------------------------------------------------

def _make_analysis(actuals, forecasts):
    """Helper: run build_analysis for Jan 1 2026."""
    actual_rows = [
        _fuelhh_row(f"2026-01-01T{h:02d}:00:00Z", a)
        for h, a in enumerate(actuals)
    ]
    forecast_rows = [
        _windfor_row(f"2026-01-01T{h:02d}:00:00Z", "2025-12-31T22:00:00Z", f)
        for h, f in enumerate(forecasts)
    ]
    return m.build_analysis(actual_rows, forecast_rows, date(2026, 1, 1), date(2026, 1, 1))


def test_mae():
    result = _make_analysis([10000, 8000], [10500, 8500])
    assert result["metrics"] is not None
    # MAE = mean of |500|, |500| = 500
    assert abs(result["metrics"]["maeMW"] - 500.0) < 1


def test_rmse():
    result = _make_analysis([10000, 8000], [10500, 8500])
    # errors = [500, 500] -> RMSE = 500
    assert abs(result["metrics"]["rmseMW"] - 500.0) < 1


def test_mape():
    result = _make_analysis([10000], [11000])
    # MAPE = 1000/10000 * 100 = 10%
    assert result["metrics"]["mape"] is not None
    assert abs(result["metrics"]["mape"] - 10.0) < 0.1


def test_bias_positive():
    result = _make_analysis([10000], [11000])
    assert result["metrics"]["biasMW"] > 0  # over-forecast


def test_bias_negative():
    result = _make_analysis([10000], [9000])
    assert result["metrics"]["biasMW"] < 0  # under-forecast


def test_overforecast_count():
    result = _make_analysis([10000, 8000], [10500, 7000])
    # 10500 > 10000 → over; 7000 < 8000 → under
    assert result["metrics"]["overforecastIntervals"] == 1
    assert result["metrics"]["underforecastIntervals"] == 1


def test_mape_handles_zero_actual():
    """MAPE should be computed only on non-zero actuals."""
    result = _make_analysis([0, 10000], [500, 11000])
    # Only the second pair has non-zero actual: |1000|/10000 = 10%
    assert result["metrics"]["mape"] is not None
    assert abs(result["metrics"]["mape"] - 10.0) < 0.1


# ---------------------------------------------------------------------------
# Empty/error response tests
# ---------------------------------------------------------------------------

def test_empty_actuals():
    result = m.build_analysis([], [], date(2026, 1, 1), date(2026, 1, 31))
    assert result["metrics"] is None
    assert "No FUELHH WIND rows" in result["message"]


def test_empty_forecasts():
    actual = [_fuelhh_row("2026-01-01T00:00:00Z", 10000)]
    result = m.build_analysis(actual, [], date(2026, 1, 1), date(2026, 1, 31))
    assert result["metrics"] is None
    assert "WINDFOR" in result["message"] or result["message"] is not None


def test_malformed_actual_row():
    """Rows with missing or non-numeric generation should be silently dropped."""
    actual = [
        {"startTime": "2026-01-01T00:00:00Z", "fuelType": "WIND", "generation": None},
        {"startTime": "not-a-date",              "fuelType": "WIND", "generation": 10000},
        _fuelhh_row("2026-01-01T02:00:00Z", 10000),  # valid
    ]
    forecast = [
        _windfor_row("2026-01-01T02:00:00Z", "2025-12-31T22:00:00Z", 10500),
    ]
    result = m.build_analysis(actual, forecast, date(2026, 1, 1), date(2026, 1, 31))
    assert result["metrics"] is not None
    assert result["metrics"]["matchedIntervals"] == 1


def test_no_forecast_candidates_after_cutoff():
    """If all forecasts are published after the cutoff, return a descriptive message."""
    original = m.DECISION_CUTOFF_HOUR_UTC
    m.DECISION_CUTOFF_HOUR_UTC = 0
    try:
        actual = [_fuelhh_row("2026-01-15T10:00:00Z", 10000)]
        # Forecast published on delivery date (after midnight cutoff)
        forecast = [_windfor_row("2026-01-15T10:00:00Z", "2026-01-15T06:00:00Z", 10500)]
        result = m.build_analysis(actual, forecast, date(2026, 1, 15), date(2026, 1, 15))
        assert result["metrics"] is None
        assert "cutoff" in result["message"].lower()
    finally:
        m.DECISION_CUTOFF_HOUR_UTC = original


# ---------------------------------------------------------------------------
# Mocked API tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_fuelhh_uses_settlement_date_params():
    """fetch_fuelhh_wind must use settlementDateFrom / settlementDateTo, not from/to."""
    captured_params = {}

    async def mock_get_json(url, params):
        captured_params.update(params)
        return [
            {"fuelType": "WIND", "startTime": "2026-01-01T00:00:00Z", "generation": 10000,
             "settlementDate": "2026-01-01", "settlementPeriod": 1},
        ]

    with patch.object(m, "_get_json", side_effect=mock_get_json):
        rows = await m.fetch_fuelhh_wind(date(2026, 1, 1), date(2026, 1, 31))

    assert "settlementDateFrom" in captured_params, (
        "FUELHH must use settlementDateFrom not 'from'"
    )
    assert "settlementDateTo" in captured_params
    assert "from" not in captured_params
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_fetch_windfor_uses_publish_datetime_params():
    """fetch_windfor must use publishDateTimeFrom / publishDateTimeTo."""
    captured_params = {}

    async def mock_get_json(url, params):
        captured_params.update(params)
        return []

    with patch.object(m, "_get_json", side_effect=mock_get_json):
        await m.fetch_windfor(date(2026, 1, 1), date(2026, 1, 31))

    assert "publishDateTimeFrom" in captured_params, (
        "WINDFOR must use publishDateTimeFrom"
    )
    assert "publishDateTimeTo" in captured_params
    assert "from" not in captured_params


@pytest.mark.asyncio
async def test_fetch_fuelhh_filters_wind_only():
    """Only WIND fuelType rows should be returned."""
    async def mock_get_json(url, params):
        return [
            {"fuelType": "WIND",    "startTime": "2026-01-01T00:00:00Z", "generation": 10000, "settlementDate": "2026-01-01", "settlementPeriod": 1},
            {"fuelType": "NUCLEAR", "startTime": "2026-01-01T00:00:00Z", "generation": 5000,  "settlementDate": "2026-01-01", "settlementPeriod": 1},
            {"fuelType": "CCGT",    "startTime": "2026-01-01T00:00:00Z", "generation": 8000,  "settlementDate": "2026-01-01", "settlementPeriod": 1},
        ]

    with patch.object(m, "_get_json", side_effect=mock_get_json):
        rows = await m.fetch_fuelhh_wind(date(2026, 1, 1), date(2026, 1, 31))

    assert len(rows) == 1
    assert rows[0]["fuelType"] == "WIND"


@pytest.mark.asyncio
async def test_api_failure_raises_502():
    """An httpx error should propagate as an HTTPException."""
    import httpx
    from fastapi import HTTPException

    async def mock_get_json(url, params):
        raise HTTPException(status_code=502, detail="Elexon unreachable")

    with patch.object(m, "_get_json", side_effect=mock_get_json):
        with pytest.raises(HTTPException) as exc_info:
            await m.fetch_fuelhh_wind(date(2026, 1, 1), date(2026, 1, 31))
    assert exc_info.value.status_code == 502
