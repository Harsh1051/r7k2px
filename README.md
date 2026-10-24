# WindSight — UK Wind Generation Forecasting & Electricity Purchasing Analysis

WindSight is a web application designed for electricity buyers and energy trading desks in the Great Britain (GB) electricity market. It tracks actual wind generation against day-ahead forecasts to quantify forecast accuracy, identify systematic forecast bias, and support data-backed day-ahead procurement decisions.

---

## 1. Summary of Root Cause & Fixes

When the project was initially inspected, querying for January 1–31, 2026 returned October 2026 records or failed to match intervals:

1. **Incorrect Elexon BMRS Query Parameter for Actuals (`FUELHH`)**:
   - The original code called `https://data.elexon.co.uk/bmrs/api/v1/datasets/FUELHH/stream` using generic `from` and `to` query parameters.
   - Per Elexon BMRS API specifications, `from`/`to` filters by `publishTime` (ingestion time into BMRS) rather than physical delivery/settlement time. Because the API only retains a short rolling window for live ingestion timestamps, historical settlement date queries using `from`/`to` silently defaulted to recent records (October 2026).
   - **Fix**: Replaced `from`/`to` with `settlementDateFrom=YYYY-MM-DD` and `settlementDateTo=YYYY-MM-DD`. This guarantees that records strictly correspond to the requested physical delivery dates.

2. **WINDFOR Forecast Endpoint Querying & Horizon Semantics**:
   - The original code attempted to filter WINDFOR by settlement date, but WINDFOR is a generation forecast dataset without a `settlementDate` field.
   - WINDFOR forecasts are published periodically with each publication containing a 36–48 hour forward curve. For day-ahead purchasing of delivery on day $D$, the forecast must have been published prior to the day-ahead gate closure (e.g. before midnight or 11:00 UTC on day $D-1$).
   - **Fix**: Query WINDFOR using `publishDateTimeFrom` and `publishDateTimeTo` covering the window from $D-2$ to $D+1$. Then filter by `startTime` (delivery time) and `publishTime <= deliveryCutoff` in Python.

3. **Time Granularity Alignment (30-min vs 60-min)**:
   - `FUELHH` actuals are reported at half-hourly resolution (48 settlement periods per day, `:00` and `:30`).
   - `WINDFOR` forecasts are issued at hourly resolution (`:00`).
   - **Fix**: Floor `FUELHH.startTime` to the delivery hour (`dt.floor("h")`) and map the hourly forecast to both 30-minute settlement periods within that hour.

4. **Day-Ahead Purchasing Cutoff Logic**:
   - In day-ahead power markets, energy for delivery date $D$ is purchased on day $D-1$.
   - Any forecast published after the decision cutoff must be excluded to prevent look-ahead bias or using intraday updates.
   - **Fix**: Implemented `compute_delivery_cutoff(delivery_ts, cutoff_hour)`. If `cutoff_hour == 0`, cutoff is midnight UTC at the start of delivery day $D$ (24:00 on $D-1$). If `cutoff_hour` is 1–23 (e.g. 11 for the 11:00 UTC Day-Ahead gate closure), cutoff is $D-1$ at `cutoff_hour:00:00 UTC`. Forecasts published after the cutoff are strictly excluded.

---

## 2. Architecture & Repository Structure

### Repository Structure

```
wind-forecast-analyzer/
├── .dockerignore
├── .env.example              # Environment variables template
├── docker-compose.yml        # Multi-container orchestration (backend + frontend)
├── README.md                 # Project documentation & verification report
├── backend/
│   ├── .dockerignore
│   ├── Dockerfile            # Python 3.11-slim FastAPI container specification
│   ├── main.py               # FastAPI application, Elexon client, matching engine & metrics
│   ├── requirements.txt      # Pinned Python dependencies
│   └── test_main.py          # 27 automated unit & integration tests
└── frontend/
    ├── .dockerignore
    ├── Dockerfile            # Multi-stage build (Node 20 build -> Nginx Alpine)
    ├── nginx.conf            # Nginx server configuration with backend reverse proxy
    ├── package.json
    ├── src/
    │   ├── App.tsx           # React dashboard with KPI cards, Recharts, diagnostics & table
    │   ├── App.css           # Modern styling & responsive layout
    │   └── main.tsx          # React DOM entry
    └── vite.config.ts
```

### Key Components

- **`backend/main.py`**:
  - `fetch_fuelhh_wind`: Retrieves actual wind generation by settlement date.
  - `fetch_windfor`: Retrieves WINDFOR forecast curves with publication window expansion.
  - `compute_delivery_cutoff`: Computes exact day-ahead cutoff timestamps for $D-1$ or midnight UTC.
  - `build_analysis`: Flooring, joining, cutoff exclusion, and metric calculations (MAE, RMSE, MAPE, Bias, Over/Underforecast counts).
  - Dynamic CORS middleware configurable via `ALLOWED_ORIGINS`.
  - `/health`, `/diagnostics`, `/api/wind/analysis`, `/api/wind/actual`, `/api/wind/forecast` endpoints.
- **`backend/test_main.py`**:
  - 27 unit and integration tests covering cutoff selection, boundary conditions, timezone offsets, metric math, API parameters, and 502 handling.
- **`frontend/src/App.tsx`**:
  - React dashboard consuming `/api/wind/analysis` via configurable `VITE_API_URL` or relative reverse proxy.
  - Clean ESLint compliance and strict typing.

---

## 3. Verified Live Elexon API Data Status (January 1–31, 2026)

Live data retrieval from the official Elexon BMRS API (`https://data.elexon.co.uk/bmrs/api/v1`) was executed and verified for the full month of **January 1–31, 2026**:

| Metric                                                | Verified Live Result                                                   |
| ----------------------------------------------------- | ---------------------------------------------------------------------- |
| **FUELHH Wind Records Returned**                      | **1,488 rows** (31 days × 48 settlement periods = 1,488 expected)      |
| **Out-of-range / Dropped Actuals**                    | **0** (no October 2026 or out-of-range rows returned)                  |
| **WINDFOR Forecast Records Ingested**                 | **19,856 rows** (from publication window `2025-12-30` to `2026-02-01`) |
| **WINDFOR Forecasts Before Cutoff**                   | **11,408 candidate rows**                                              |
| **Matched Delivery Intervals**                        | **1,488 intervals (100% complete coverage)**                           |
| **Missing Forecast Intervals**                        | **0**                                                                  |
| **Missing Actual Intervals**                          | **0**                                                                  |
| **Mean Actual Generation**                            | **11,580.74 MW**                                                       |
| **MAE (Mean Absolute Error)**                         | **1,940.82 MW**                                                        |
| **RMSE (Root Mean Squared Error)**                    | **2,410.57 MW**                                                        |
| **Forecast Bias ($\text{Forecast} - \text{Actual}$)** | **+1,713.72 MW (systematic over-forecasting)**                         |
| **MAPE (Mean Absolute % Error)**                      | **18.21%**                                                             |
| **Over-forecast Intervals**                           | **1,304 intervals (87.6%)**                                            |
| **Under-forecast Intervals**                          | **183 intervals (12.3%)**                                              |

Sample live matched interval:

- **Delivery Interval**: `2026-01-01T00:00:00+00:00`
- **Forecast Publish Time**: `2025-12-31T22:30:00+00:00` (published on $D-1$ before midnight cutoff)
- **Actual Generation**: 13,756.0 MW
- **Day-Ahead Forecast**: 20,032.0 MW
- **Error**: +6,276.0 MW

---

## 4. How to Run Locally (Windows PowerShell)

### Prerequisites

- Python 3.10+ (tested with Python 3.14)
- Node.js 18+ and npm

### Step 1: Start the Backend

Open a PowerShell terminal:

```powershell
cd c:\wind-forecast-analyzer\backend

# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Run FastAPI server
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Interactive Swagger docs: **http://127.0.0.1:8000/docs**
Healthcheck: **http://127.0.0.1:8000/health**

### Step 2: Start the Frontend

Open a second PowerShell terminal:

```powershell
cd c:\wind-forecast-analyzer\frontend

# Install dependencies
npm.cmd install

# Start Vite dev server
npm.cmd run dev
```

Access the dashboard at: **http://localhost:5173**

---

## 5. Automated Test & Build Verification Results

### 1. Backend Automated Tests (pytest)

All unit tests mock external Elexon API calls to guarantee deterministic execution:

```powershell
cd c:\wind-forecast-analyzer\backend
.\.venv\Scripts\python.exe -m pytest test_main.py -v
```

**Actual Result**: `27 passed in 2.14s` (100% pass rate).

- Date range validation: `test_analysis_rejects_invalid_range`, `test_ts_in_range_keeps_valid_rows`, `test_ts_in_range_drops_all_out_of_range`, `test_build_analysis_filters_to_january`
- Granularity & alignment: `test_half_hourly_floored_to_hour`
- Cutoff selection & exclusions: `test_compute_delivery_cutoff_midnight`, `test_compute_delivery_cutoff_day_ahead_gate`, `test_cutoff_excludes_same_day_publications`, `test_cutoff_picks_latest_before_cutoff`, `test_cutoff_hour_parameter_custom_gate`, `test_cutoff_boundary_exact_and_after`
- Timezone handling: `test_timezone_handling_and_offsets`
- Metric math: `test_mae`, `test_rmse`, `test_mape`, `test_bias_positive`, `test_bias_negative`, `test_overforecast_count`, `test_mape_handles_zero_actual`
- Edge cases & errors: `test_empty_actuals`, `test_empty_forecasts`, `test_malformed_actual_row`, `test_no_forecast_candidates_after_cutoff`, `test_api_failure_raises_502`
- API parameters: `test_fetch_fuelhh_uses_settlement_date_params`, `test_fetch_windfor_uses_publish_datetime_params`, `test_fetch_fuelhh_filters_wind_only`

### 2. Frontend ESLint

```powershell
cd c:\wind-forecast-analyzer\frontend
npm.cmd run lint
```

**Actual Result**: `0 errors, 0 warnings` (clean pass).

### 3. Frontend Production Build

```powershell
cd c:\wind-forecast-analyzer\frontend
npm.cmd run build
```

**Actual Result**: `✓ built in 1.63s` (TypeScript compilation and bundle generation succeeded with zero errors).

---

### 6. Docker Build & Runtime Verification

Docker Desktop was used to build and run the application locally.

**Verified results:**

- Backend Docker image built successfully.
- Frontend Docker image built successfully.
- `windsight-backend` container started and reported `healthy`.
- `windsight-frontend` container started successfully.
- Frontend remained accessible after stopping the locally running frontend and backend development servers.

**Commands used for verification:**

```powershell
docker compose build
docker compose up -d
docker compose ps
```

# Rebuild images after code changes

docker compose up -d --build

# Inspect recent logs

docker compose logs --tail=100

# Check container status

docker compose ps

# Stop containers

docker compose down

**Local URLs:**

- Frontend: http://localhost
- Backend API: http://localhost:8000
- Backend health check: http://localhost:8000/health

The frontend was verified through Docker with the local development servers stopped. Backend API responses and live-data calculations should be verified separately when reporting final runtime results.

## 7. Production Deployment Configuration

- **Environment Template**: `.env.example` provided at project root.
- **CORS Configuration**: Configured in `backend/main.py` via `ALLOWED_ORIGINS` environment variable (accepts comma-separated origin URLs or `*`).
- **Healthcheck**: Implemented at `GET /health` with ISO timestamp, status, and default cutoff hour.
- **Reverse Proxy**: Nginx configuration (`frontend/nginx.conf`) forwards `/api/` and `/diagnostics` directly to the backend service.
- **Deployment Status Note**: No public URL is claimed as deployed. Public deployment to cloud platforms (such as Render, Fly.io, Railway, AWS, or GCP) requires active cloud accounts and manual authorization by the repository owner.

---

## 8. AI Usage Disclosure

In compliance with academic and professional honesty standards:

- **AI Coding Assistant**: Google Antigravity & Chat GPT.
- **Assisted Tasks**:
  - Diagnosed Elexon BMRS parameter differences (`settlementDateFrom`/`settlementDateTo` vs generic `from`/`to`).
  - Drafted day-ahead forecast cutoff logic and market gate closure formulas.
  - Implemented unit tests for cutoff selection, timezone offsets, and boundary handling.
  - Created Dockerfiles, `docker-compose.yml`, and `nginx.conf` reverse proxy setup.
  - Resolved React 19 ESLint warnings and typed tooltip formatters.
- **Human Verification**: All code, queries, API schemas, mathematical metrics, and test outputs were reviewed and verified against live Elexon BMRS endpoint responses.

---

## 9. Submission Checklist

### A. Anonymous Public GitHub Repository Checklist

- [ ] Create a new public GitHub repository under an anonymous or neutral GitHub account.
- [ ] Ensure no personal names, email addresses, or institutional identifiers are in git commit history (`git log --format="%an %ae"`).
- [ ] Push code and `.env.example` (ensure `.env` files with secret tokens are ignored).
- [ ] Confirm repository visibility is set to **Public**.

### B. Unlisted YouTube Demo Video Checklist

- [ ] Record a screencast (recommended duration: 3–5 minutes).
- [ ] Demonstrate:
  1. Overview of the WindSight dashboard showing January 1–31, 2026 live data.
  2. KPI cards (MAE, RMSE, MAPE, Forecast Bias, Over/Under-forecasting counts).
  3. Interactive time series charts (Actual vs Day-Ahead Forecast and Forecast Error distribution).
  4. Day-ahead cutoff logic explanation and purchasing insights.
  5. API endpoints demonstration via browser or Swagger docs (`/api/wind/analysis`, `/health`, `/diagnostics`).
  6. Demonstration of automated tests passing (`pytest` with 27 passed tests and `npm run build`).
- [ ] Upload video to YouTube with visibility set strictly to **Unlisted**.
- [ ] Verify audio and video playback quality before submitting the unlisted link.
