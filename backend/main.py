import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("windsight")

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="WindSight Wind Forecast Analyser")

# Configure CORS origins from environment variable or default local origins
raw_origins = os.getenv("ALLOWED_ORIGINS", "")
if raw_origins.strip() == "*":
    cors_origins = ["*"]
elif raw_origins.strip():
    cors_origins = [o.strip() for o in raw_origins.split(",") if o.strip()]
else:
    cors_origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:80",
        "http://127.0.0.1:80",
        "http://localhost",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True if cors_origins != ["*"] else False,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_URL = "https://data.elexon.co.uk/bmrs/api/v1"

# Day-ahead purchasing decision cutoff hour (UTC).
# In GB day-ahead power markets, contracts are traded on D-1.
# 0 = midnight UTC (i.e. 00:00 UTC at the start of delivery day D / 24:00 on D-1).
# 1..23 = cutoff hour on day D-1 (e.g. 11 = 11:00 UTC Day-Ahead gate closure on D-1).
# Override globally with env var DECISION_CUTOFF_HOUR_UTC (0-23) or per-request via cutoff_hour.
DECISION_CUTOFF_HOUR_UTC: int = int(os.getenv("DECISION_CUTOFF_HOUR_UTC", "0"))


def compute_delivery_cutoff(delivery_ts: pd.Timestamp, cutoff_hour: int) -> pd.Timestamp:
    """
    Compute the day-ahead purchasing decision cutoff timestamp for a delivery interval.

    In GB and European electricity markets (e.g. EPEX SPOT):
    Power contracts for all settlement periods of delivery day D are auctioned
    on the prior day (D-1).

    - If cutoff_hour == 0:
      Represents midnight 00:00:00 UTC of delivery date D (i.e. Day D-1 24:00:00 UTC).
      All forecasts published on Day D-1 are eligible. Forecasts published on Day D itself
      are strictly excluded.
    - If cutoff_hour in 1..23:
      Represents cutoff_hour:00:00 UTC on Day D-1 (e.g. 11:00 UTC day-ahead gate closure).
      Any forecast published after cutoff_hour:00:00 UTC on Day D-1 (or on Day D) is excluded.
    """
    d_midnight = delivery_ts.normalize()
    if cutoff_hour == 0:
        return d_midnight
    return d_midnight - pd.Timedelta(days=1) + pd.Timedelta(hours=cutoff_hour)


# ---------------------------------------------------------------------------
# Elexon API helpers
# ---------------------------------------------------------------------------


async def _get_json(url: str, params: dict) -> Any:
    """HTTP GET to Elexon; raise HTTPException on failure."""
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Elexon returned HTTP {exc.response.status_code} for {url}. "
                f"Body: {exc.response.text[:300]}"
            ),
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Cannot reach Elexon API: {exc}",
        ) from exc


async def fetch_fuelhh_wind(start: date, end: date) -> list[dict]:
    """
    Fetch actual FUELHH wind generation rows.

    KEY FIX: use settlementDateFrom / settlementDateTo to filter by delivery
    date (the physical day electricity was generated).  The generic `from`/`to`
    params filter by publishTime and will silently return records for the
    current date when querying historical periods.
    """
    url = f"{BASE_URL}/datasets/FUELHH/stream"
    params = {
        "settlementDateFrom": start.isoformat(),
        "settlementDateTo": end.isoformat(),
        "format": "json",
    }
    data = await _get_json(url, params)
    if isinstance(data, dict):
        data = data.get("data", data.get("results", []))
    if not isinstance(data, list):
        raise HTTPException(status_code=502, detail="FUELHH: unexpected response shape")

    wind = [r for r in data if str(r.get("fuelType", "")).upper() == "WIND"]
    logger.info("FUELHH: %d total rows, %d WIND for %s to %s", len(data), len(wind), start, end)
    return wind


async def fetch_windfor(start: date, end: date) -> list[dict]:
    """
    Fetch WINDFOR forecast rows.

    KEY FIX: WINDFOR has no settlementDate field; the `from`/`to` params also
    filter by publishTime, not by delivery startTime.  We use
    publishDateTimeFrom / publishDateTimeTo to retrieve all forecast
    publications from D-2 to D+1 and then filter by startTime in Python.

    This is necessary because each WINDFOR publication covers ~36 h of
    future delivery slots; we need D-1 publications to obtain day-ahead
    forecasts for date D.
    """
    pub_from = (
        datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        - timedelta(days=2)
    )
    pub_to = (
        datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)
        + timedelta(days=1)
    )
    url = f"{BASE_URL}/datasets/WINDFOR/stream"
    params = {
        "publishDateTimeFrom": pub_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "publishDateTimeTo": pub_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "format": "json",
    }
    data = await _get_json(url, params)
    if isinstance(data, dict):
        data = data.get("data", data.get("results", []))
    if not isinstance(data, list):
        raise HTTPException(status_code=502, detail="WINDFOR: unexpected response shape")

    logger.info("WINDFOR: %d rows from pub window %s to %s", len(data), pub_from, pub_to)
    return data


# ---------------------------------------------------------------------------
# Analysis logic
# ---------------------------------------------------------------------------


def _ts_in_range(df: pd.DataFrame, col: str, start: date, end: date):
    """Return (filtered_df, n_dropped) keeping rows where col is in [start, end+1 day)."""
    lo = pd.Timestamp(start, tz="UTC")
    hi = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
    mask = (df[col] >= lo) & (df[col] < hi)
    return df[mask].copy(), int((~mask).sum())


def build_analysis(
    actual_rows: list[dict],
    forecast_rows: list[dict],
    start: date,
    end: date,
    cutoff_hour: int | None = None,
) -> dict:
    """
    Match FUELHH half-hourly actuals to WINDFOR hourly day-ahead forecasts.

    FUELHH granularity: 30-min intervals (startTime at :00 and :30).
    WINDFOR granularity: 60-min intervals (startTime at :00 only).

    Matching: floor each FUELHH startTime to the hour ('deliveryHour'),
    then join on that key. Both the :00 and :30 slots of each hour get the
    same hourly forecast value.

    Day-ahead cutoff: for delivery date D, the relevant forecast is the
    latest WINDFOR row published at or before the decision cutoff.
    - If cutoff_hour == 0 (default): midnight 00:00 UTC entering day D (24:00 on D-1).
    - If cutoff_hour in 1..23: cutoff_hour:00 UTC on day D-1 (e.g. 11 = 11:00 UTC Day-Ahead gate).
    Forecasts published after the cutoff are strictly excluded.
    """
    effective_cutoff = DECISION_CUTOFF_HOUR_UTC if cutoff_hour is None else cutoff_hour
    diag: dict[str, Any] = {
        "rawActualCount": len(actual_rows),
        "rawForecastCount": len(forecast_rows),
        "cutoffHourUTC": effective_cutoff,
    }

    # --- Actuals ---
    actual = pd.DataFrame(
        [
            {
                "startTime": r.get("startTime"),
                "settlementDate": r.get("settlementDate"),
                "settlementPeriod": r.get("settlementPeriod"),
                "actualMW": r.get("generation"),
            }
            for r in actual_rows
        ]
    )
    if actual.empty:
        return {
            "message": "No FUELHH WIND rows returned. The API may not hold data for this period.",
            "diagnostics": diag, "metrics": None, "data": [],
        }

    actual["startTime"] = pd.to_datetime(actual["startTime"], utc=True, errors="coerce")
    actual["actualMW"] = pd.to_numeric(actual["actualMW"], errors="coerce")
    actual = actual.dropna(subset=["startTime", "actualMW"])
    actual, actual_dropped = _ts_in_range(actual, "startTime", start, end)
    diag["actualInRange"] = len(actual)
    diag["actualDropped"] = actual_dropped

    if actual.empty:
        return {
            "message": (
                f"FUELHH returned {diag['rawActualCount']} rows but none have "
                f"a startTime in {start} to {end}. "
                "Check API parameter correctness."
            ),
            "diagnostics": diag, "metrics": None, "data": [],
        }

    # Floor to hour for matching with hourly WINDFOR
    actual["deliveryHour"] = actual["startTime"].dt.floor("h")

    # --- Forecasts ---
    forecast = pd.DataFrame(
        [
            {
                "startTime": r.get("startTime"),
                "publishTime": r.get("publishTime"),
                "forecastMW": r.get("generation"),
            }
            for r in forecast_rows
        ]
    )
    if forecast.empty:
        return {
            "message": "No WINDFOR rows returned.",
            "diagnostics": diag, "metrics": None, "data": [],
        }

    forecast["startTime"] = pd.to_datetime(forecast["startTime"], utc=True, errors="coerce")
    forecast["publishTime"] = pd.to_datetime(forecast["publishTime"], utc=True, errors="coerce")
    forecast["forecastMW"] = pd.to_numeric(forecast["forecastMW"], errors="coerce")
    forecast = forecast.dropna(subset=["startTime", "publishTime", "forecastMW"])

    # Filter: keep only WINDFOR rows whose startTime is within our delivery window
    forecast, forecast_dropped = _ts_in_range(forecast, "startTime", start, end)
    diag["forecastInRange"] = len(forecast)
    diag["forecastDropped"] = forecast_dropped

    if forecast.empty:
        return {
            "message": (
                f"WINDFOR returned {diag['rawForecastCount']} rows but none have "
                f"a startTime in {start} to {end}. "
                "Extend the publish window or check WINDFOR archive availability."
            ),
            "diagnostics": diag, "metrics": None, "data": [],
        }

    # --- Day-ahead cutoff ---
    forecast["deliveryCutoff"] = forecast["startTime"].apply(
        lambda ts: compute_delivery_cutoff(ts, effective_cutoff)
    )
    # Exclude forecasts published after the cutoff
    before_cutoff = forecast[forecast["publishTime"] <= forecast["deliveryCutoff"]]
    diag["forecastCandidates"] = len(before_cutoff)

    if before_cutoff.empty:
        cutoff_desc = (
            "midnight 00:00 UTC of delivery date (24:00 on D-1)"
            if effective_cutoff == 0
            else f"{effective_cutoff:02d}:00 UTC on day D-1"
        )
        return {
            "message": (
                f"No WINDFOR forecasts published before the day-ahead cutoff "
                f"({cutoff_desc}). "
                "Try adjusting cutoff_hour or check WINDFOR archive."
            ),
            "diagnostics": diag, "metrics": None, "data": [],
        }

    # For each delivery hour, pick the latest forecast before the cutoff
 
    before_cutoff = before_cutoff.copy()
    before_cutoff["deliveryHour"] = before_cutoff["startTime"].dt.floor("h")

    best = (
        before_cutoff
        .sort_values(["deliveryHour", "publishTime"])
        .drop_duplicates(subset=["deliveryHour"], keep="last")
    )

    # --- Merge ---
    merged = actual.merge(
        best[["deliveryHour", "forecastMW", "publishTime"]],
        on="deliveryHour",
        how="left",
    )
    matched = merged.dropna(subset=["forecastMW"]).copy()
    no_forecast = merged[merged["forecastMW"].isna()]

    diag["matchedIntervals"] = len(matched)
    diag["missingForecast"] = len(no_forecast)
    diag["missingActual"] = len(
        set(best["deliveryHour"].unique()) - set(actual["deliveryHour"].unique())
    )

    if matched.empty:
        return {
            "message": (
                "Actuals and forecasts fetched but no intervals could be matched. "
                "This may indicate a WINDFOR archive gap for this delivery period. "
                "See /diagnostics for raw counts."
            ),
            "diagnostics": diag, "metrics": None, "data": [],
        }

    # --- Metrics ---
    matched["errorMW"] = matched["forecastMW"] - matched["actualMW"]
    matched["absErrorMW"] = matched["errorMW"].abs()

    mae = float(matched["absErrorMW"].mean())
    rmse = float(np.sqrt((matched["errorMW"] ** 2).mean()))
    bias = float(matched["errorMW"].mean())
    mean_actual = float(matched["actualMW"].mean())
    non_zero = matched[matched["actualMW"] != 0]
    mape = (
        float((non_zero["absErrorMW"] / non_zero["actualMW"].abs()).mean() * 100)
        if len(non_zero) > 0 else None
    )
    overforecast = int((matched["errorMW"] > 0).sum())
    underforecast = int((matched["errorMW"] < 0).sum())

    records = [
        {
            "startTime": row.startTime.isoformat(),
            "deliveryHour": row.deliveryHour.isoformat(),
            "forecastPublishTime": row.publishTime.isoformat(),
            "actualMW": round(float(row.actualMW), 1),
            "forecastMW": round(float(row.forecastMW), 1),
            "errorMW": round(float(row.errorMW), 1),
            "absErrorMW": round(float(row.absErrorMW), 1),
        }
        for row in matched.sort_values("startTime").itertuples()
    ]

    return {
        "dataset": {"actual": "FUELHH", "forecast": "WINDFOR"},
        "deliveryPeriod": {"start": start.isoformat(), "end": end.isoformat()},
        "decisionCutoffNote": (
            f"Day-ahead decision cutoff: "
            + (
                "midnight UTC (00:00 UTC on delivery date D, end of D-1)"
                if effective_cutoff == 0
                else f"{effective_cutoff:02d}:00 UTC on day D-1"
            )
            + f" (cutoff_hour={effective_cutoff}). Forecasts published after this cutoff are excluded."
        ),
        "diagnostics": diag,
        "metrics": {
            "matchedIntervals": diag["matchedIntervals"],
            "missingForecast": diag["missingForecast"],
            "missingActual": diag["missingActual"],
            "maeMW": round(mae, 2),
            "rmseMW": round(rmse, 2),
            "biasMW": round(bias, 2),
            "meanActualMW": round(mean_actual, 2),
            "mape": round(mape, 2) if mape is not None else None,
            "overforecastIntervals": overforecast,
            "underforecastIntervals": underforecast,
        },
        "data": records,
    }


# ---------------------------------------------------------------------------
# FastAPI endpoints
# ---------------------------------------------------------------------------


@app.get("/")
def home():
    return {
        "service": "WindSight API",
        "description": "UK Wind Generation Forecasting & Electricity Purchasing Analysis",
        "endpoints": ["/api/wind/actual", "/api/wind/forecast", "/api/wind/analysis", "/diagnostics", "/health"],
        "defaultPeriod": "January 2026",
        "defaultCutoffHourUTC": DECISION_CUTOFF_HOUR_UTC,
        "docs": "/docs",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "WindSight API",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "defaultCutoffHourUTC": DECISION_CUTOFF_HOUR_UTC,
    }


@app.get("/api/wind/actual")
async def actual_wind(
    start: date = Query(default=date(2026, 1, 1)),
    end: date = Query(default=date(2026, 1, 31)),
):
    """FUELHH WIND rows for the given settlement date range."""
    if start > end:
        raise HTTPException(status_code=400, detail="start must be <= end")
    rows = await fetch_fuelhh_wind(start, end)
    return {"dataset": "FUELHH", "start": start, "end": end, "count": len(rows), "data": rows}


@app.get("/api/wind/forecast")
async def wind_forecast(
    start: date = Query(default=date(2026, 1, 1)),
    end: date = Query(default=date(2026, 1, 31)),
):
    """WINDFOR rows whose startTime falls within [start, end]. All published revisions included."""
    if start > end:
        raise HTTPException(status_code=400, detail="start must be <= end")
    rows = await fetch_windfor(start, end)
    lo = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    hi = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)
    filtered = []
    for r in rows:
        try:
            st = datetime.fromisoformat(str(r.get("startTime", "")).replace("Z", "+00:00"))
            if lo <= st <= hi:
                filtered.append(r)
        except (ValueError, AttributeError):
            pass
    return {
        "dataset": "WINDFOR",
        "note": "Filtered server-side by publishDateTime; startTime filtered client-side",
        "start": start,
        "end": end,
        "count": len(filtered),
        "data": filtered,
    }


@app.get("/api/wind/analysis")
async def wind_analysis(
    start: date = Query(default=date(2026, 1, 1)),
    end: date = Query(default=date(2026, 1, 31)),
    cutoff_hour: int | None = Query(
        default=None,
        ge=0,
        le=23,
        description="Day-ahead purchasing decision cutoff hour in UTC (0 = midnight D 00:00, 1-23 = hour on D-1)",
    ),
):
    """
    Match FUELHH actual wind against WINDFOR day-ahead forecasts and return
    error metrics (MAE, RMSE, MAPE, bias) and per-interval data.
    """
    if start > end:
        raise HTTPException(status_code=400, detail="start must be <= end")
    actual_rows, forecast_rows = await asyncio.gather(
        fetch_fuelhh_wind(start, end),
        fetch_windfor(start, end),
    )
    return build_analysis(actual_rows, forecast_rows, start, end, cutoff_hour=cutoff_hour)


@app.get("/diagnostics")
async def diagnostics(
    start: date = Query(default=date(2026, 1, 1)),
    end: date = Query(default=date(2026, 1, 31)),
    cutoff_hour: int | None = Query(
        default=None,
        ge=0,
        le=23,
        description="Day-ahead purchasing decision cutoff hour in UTC (0 = midnight D 00:00, 1-23 = hour on D-1)",
    ),
):
    """Debug endpoint: raw counts and sample records from each dataset."""
    if start > end:
        raise HTTPException(status_code=400, detail="start must be <= end")
    effective_cutoff = DECISION_CUTOFF_HOUR_UTC if cutoff_hour is None else cutoff_hour
    actual_rows, forecast_rows = await asyncio.gather(
        fetch_fuelhh_wind(start, end),
        fetch_windfor(start, end),
    )
    lo = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    hi = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)
    windfor_in_range = []
    for r in forecast_rows:
        try:
            st = datetime.fromisoformat(str(r.get("startTime", "")).replace("Z", "+00:00"))
            if lo <= st <= hi:
                windfor_in_range.append(r)
        except (ValueError, AttributeError):
            pass
    return {
        "requestedPeriod": {"start": start.isoformat(), "end": end.isoformat()},
        "decisionCutoffHourUTC": effective_cutoff,
        "fuelhh": {
            "windRowsTotal": len(actual_rows),
            "sample": actual_rows[:3],
        },
        "windfor": {
            "rowsFromPublishWindow": len(forecast_rows),
            "rowsWithStartTimeInRange": len(windfor_in_range),
            "samplePublished": forecast_rows[:3],
            "sampleInRange": windfor_in_range[:3],
        },
    }
