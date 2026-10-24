import { useEffect, useState } from "react";
import {
  LineChart,
  Line,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
  ReferenceLine,
} from "recharts";
import { Wind, Activity, Target, RefreshCw, AlertTriangle, TrendingUp, TrendingDown, Info } from "lucide-react";
import "./App.css";

type Metrics = {
  matchedIntervals: number;
  missingForecast: number;
  missingActual: number;
  maeMW: number;
  rmseMW: number;
  biasMW: number;
  meanActualMW: number;
  mape: number | null;
  overforecastIntervals: number;
  underforecastIntervals: number;
};

type Diagnostics = {
  rawActualCount: number;
  rawForecastCount: number;
  actualInRange?: number;
  forecastInRange?: number;
  actualDropped?: number;
  forecastDropped?: number;
  forecastCandidates?: number;
  matchedIntervals?: number;
  missingForecast?: number;
  missingActual?: number;
};

type DataRow = {
  startTime: string;
  deliveryHour: string;
  forecastPublishTime: string;
  actualMW: number;
  forecastMW: number;
  errorMW: number;
  absErrorMW: number;
};

type Analysis = {
  message?: string;
  decisionCutoffNote?: string;
  diagnostics?: Diagnostics;
  metrics: Metrics | null;
  data: DataRow[];
};

function formatMW(v: number | null | undefined): string {
  if (v == null) return "—";
  return v.toLocaleString(undefined, { maximumFractionDigits: 0 }) + " MW";
}

function formatPct(v: number | null | undefined): string {
  if (v == null) return "—";
  return v.toFixed(1) + "%";
}

const API_BASE = import.meta.env.VITE_API_URL !== undefined ? import.meta.env.VITE_API_URL : "http://127.0.0.1:8000";

function App() {
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  async function loadAnalysis() {
    setLoading(true);
    setError("");
    try {
      const resp = await fetch(`${API_BASE}/api/wind/analysis`);
      if (!resp.ok) throw new Error(`API ${resp.status}: ${resp.statusText}`);
      const result: Analysis = await resp.json();
      setAnalysis(result);
    } catch {
      setError(
        `Could not load data. Ensure the backend is running at ${API_BASE} and the Elexon API is reachable.`
      );
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    let ignore = false;
    async function init() {
      try {
        const resp = await fetch(`${API_BASE}/api/wind/analysis`);
        if (!resp.ok) throw new Error(`API ${resp.status}: ${resp.statusText}`);
        const result: Analysis = await resp.json();
        if (!ignore) {
          setAnalysis(result);
        }
      } catch {
        if (!ignore) {
          setError(
            `Could not load data. Ensure the backend is running at ${API_BASE} and the Elexon API is reachable.`
          );
        }
      } finally {
        if (!ignore) {
          setLoading(false);
        }
      }
    }
    void init();
    return () => {
      ignore = true;
    };
  }, []);

  const m = analysis?.metrics;
  const diag = analysis?.diagnostics;

  // Sample every 4th point for the main chart to keep it readable over 31 days
  const chartData = (analysis?.data ?? [])
    .filter((_, i) => i % 2 === 0)
    .map((row) => ({
      ...row,
      time: new Date(row.startTime).toLocaleDateString("en-GB", {
        day: "2-digit", month: "short",
        timeZone: "Europe/London",
      }),
      timeLabel: new Date(row.startTime).toLocaleString("en-GB", {
        day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
        timeZone: "Europe/London",
      }),
    }));

  const errorData = (analysis?.data ?? [])
    .filter((_, i) => i % 2 === 0)
    .map((row) => ({
      time: new Date(row.startTime).toLocaleDateString("en-GB", {
        day: "2-digit", month: "short", timeZone: "Europe/London",
      }),
      errorMW: row.errorMW,
    }));

  return (
    <main className="dashboard">
      <header className="topbar">
        <div className="brand">
          <div className="brand-icon"><Wind size={23} /></div>
          <div>
            <h1>WindSight</h1>
            <p>Renewable Energy Intelligence</p>
          </div>
        </div>
        <button className="refresh" id="refresh-btn" onClick={loadAnalysis} disabled={loading}>
          <RefreshCw size={16} className={loading ? "spin" : ""} />
          Refresh data
        </button>
      </header>

      <section className="intro">
        <div>
          <p className="eyebrow">UK ENERGY MARKET · ELEXON BMRS</p>
          <h2>Wind Forecast Analysis</h2>
          <p className="subtitle">
            Actual FUELHH wind generation vs day-ahead WINDFOR forecasts.
            Supports electricity purchasing decisions.
          </p>
        </div>
        <div className="date-chip">01 — 31 January 2026</div>
      </section>

      {error && <div className="error-banner" role="alert">{error}</div>}

      {loading ? (
        <div className="loading">
          <Wind size={32} className="spin" />
          <p>Fetching generation and forecast data from Elexon BMRS…</p>
        </div>
      ) : (
        <>
          {analysis?.message && (
            <div className="notice" role="status">
              <AlertTriangle size={16} style={{ flexShrink: 0 }} />
              <span>{analysis.message}</span>
            </div>
          )}

          {/* Diagnostics strip */}
          {diag && (
            <div className="diag-strip">
              <span><strong>{(diag.rawActualCount ?? 0).toLocaleString()}</strong> actual rows</span>
              <span className="sep">·</span>
              <span><strong>{(diag.actualInRange ?? diag.rawActualCount ?? 0).toLocaleString()}</strong> in range</span>
              <span className="sep">·</span>
              <span><strong>{(diag.rawForecastCount ?? 0).toLocaleString()}</strong> forecast rows</span>
              <span className="sep">·</span>
              <span><strong>{(diag.forecastCandidates ?? 0).toLocaleString()}</strong> before cutoff</span>
              <span className="sep">·</span>
              <span><strong>{(diag.matchedIntervals ?? m?.matchedIntervals ?? 0).toLocaleString()}</strong> matched</span>
            </div>
          )}

          {/* Metrics grid */}
          <section className="metrics-grid" aria-label="Key metrics">
            <Metric
              id="metric-matched"
              icon={<Activity size={19} />}
              label="Matched intervals"
              value={m ? m.matchedIntervals.toLocaleString() : "—"}
              description="Half-hour actual + forecast pairs"
              color="green"
            />
            <Metric
              id="metric-mae"
              icon={<Target size={19} />}
              label="MAE"
              value={m ? formatMW(m.maeMW) : "—"}
              description="Mean absolute forecast error"
              color="blue"
            />
            <Metric
              id="metric-rmse"
              icon={<Activity size={19} />}
              label="RMSE"
              value={m ? formatMW(m.rmseMW) : "—"}
              description="Penalises large errors more"
              color="purple"
            />
            <Metric
              id="metric-mape"
              icon={<Target size={19} />}
              label="MAPE"
              value={m ? formatPct(m.mape) : "—"}
              description="Mean absolute % error"
              color="orange"
            />
            <Metric
              id="metric-bias"
              icon={m && m.biasMW > 0 ? <TrendingUp size={19} /> : <TrendingDown size={19} />}
              label="Forecast bias"
              value={m ? (m.biasMW > 0 ? "+" : "") + formatMW(m.biasMW) : "—"}
              description={m && m.biasMW > 0 ? "Systematic over-forecast" : "Systematic under-forecast"}
              color={m && m.biasMW > 0 ? "amber" : "teal"}
            />
            <Metric
              id="metric-actual"
              icon={<Wind size={19} />}
              label="Mean actual"
              value={m ? formatMW(m.meanActualMW) : "—"}
              description="Average wind generation"
              color="green"
            />
            <Metric
              id="metric-overforecast"
              icon={<TrendingUp size={19} />}
              label="Over-forecast"
              value={m ? m.overforecastIntervals.toLocaleString() + " intervals" : "—"}
              description="Forecast > actual (buy too little)"
              color="amber"
            />
            <Metric
              id="metric-underforecast"
              icon={<TrendingDown size={19} />}
              label="Under-forecast"
              value={m ? m.underforecastIntervals.toLocaleString() + " intervals" : "—"}
              description="Forecast < actual (buy too much)"
              color="teal"
            />
          </section>

          {/* Main line chart */}
          <section className="panel">
            <div className="panel-heading">
              <div>
                <h3>Actual vs forecast generation</h3>
                <p>Half-hourly observations · sampled every 60 min for clarity · UK time</p>
              </div>
              <span className="live-label">ELEXON DATA</span>
            </div>
            {chartData.length > 0 ? (
              <div className="chart">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={chartData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e8edf3" />
                    <XAxis dataKey="time" minTickGap={50} tick={{ fontSize: 10, fill: "#68778a" }} />
                    <YAxis
                      tick={{ fontSize: 10, fill: "#68778a" }}
                      width={70}
                      label={{ value: "Generation (MW)", angle: -90, position: "insideLeft", style: { fill: "#68778a", fontSize: 11 } }}
                    />
                    <Tooltip
                      formatter={(v: unknown) => [typeof v === "number" ? `${v.toLocaleString()} MW` : `${v}`]}
                      labelFormatter={(l) => l}
                    />
                    <Legend />
                    <Line type="monotone" dataKey="actualMW" name="Actual" stroke="#147d64" dot={false} strokeWidth={2} connectNulls />
                    <Line type="monotone" dataKey="forecastMW" name="Forecast" stroke="#5078d8" dot={false} strokeWidth={2} strokeDasharray="5 3" connectNulls />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            ) : (
              <EmptyState message="No matched data. See the diagnostics strip above." />
            )}
          </section>

          {/* Error chart */}
          {errorData.length > 0 && (
            <section className="panel">
              <div className="panel-heading">
                <div>
                  <h3>Forecast error over time</h3>
                  <p>Positive = over-forecast (wind higher than expected) · Negative = under-forecast</p>
                </div>
                <span className="live-label">ERROR = FORECAST − ACTUAL</span>
              </div>
              <div className="chart">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={errorData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e8edf3" />
                    <XAxis dataKey="time" minTickGap={50} tick={{ fontSize: 10, fill: "#68778a" }} />
                    <YAxis tick={{ fontSize: 10, fill: "#68778a" }} width={70}
                      label={{ value: "Error (MW)", angle: -90, position: "insideLeft", style: { fill: "#68778a", fontSize: 11 } }}
                    />
                    <Tooltip formatter={(v: unknown) => [typeof v === "number" ? `${v.toLocaleString()} MW` : `${v}`, "Error"]} />
                    <ReferenceLine y={0} stroke="#444" strokeWidth={1} />
                    <Bar dataKey="errorMW" name="Error" fill="#5078d8" radius={[2, 2, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </section>
          )}

          {/* Purchasing insights */}
          {m && (
            <section className="panel insight-panel">
              <div className="panel-heading">
                <div>
                  <h3>Purchasing decision insights</h3>
                  <p>How forecast error affects residual electricity demand estimates</p>
                </div>
                <span className="live-label disclaimer-label">INDICATIVE ONLY</span>
              </div>
              <div className="insights">
                <div className="insight-card amber">
                  <strong>Over-forecast risk</strong>
                  <p>
                    In <strong>{m.overforecastIntervals.toLocaleString()}</strong> intervals, forecast exceeded actual.
                    A buyer relying on these forecasts would have purchased too little from other sources,
                    potentially facing short-fall on the balancing mechanism.
                  </p>
                </div>
                <div className="insight-card teal">
                  <strong>Under-forecast risk</strong>
                  <p>
                    In <strong>{m.underforecastIntervals.toLocaleString()}</strong> intervals, actual exceeded forecast.
                    The buyer would have purchased more residual supply than needed,
                    leading to potential excess positions.
                  </p>
                </div>
                <div className="insight-card blue">
                  <strong>Systematic bias: {m.biasMW > 0 ? "over-forecast" : "under-forecast"} ({(m.biasMW > 0 ? "+" : "") + m.biasMW.toLocaleString()} MW avg)</strong>
                  <p>
                    A consistent bias of <strong>{Math.abs(m.biasMW).toLocaleString()} MW</strong> suggests
                    a buyer could apply a bias correction to improve purchasing accuracy.
                    Note: actual cost impact requires pricing data not available here.
                  </p>
                </div>
                <div className="insight-card purple">
                  <strong>Forecast uncertainty: RMSE {m.rmseMW.toLocaleString()} MW</strong>
                  <p>
                    The RMSE of {m.rmseMW.toLocaleString()} MW ({m.mape != null ? m.mape.toFixed(1) + "% MAPE" : "MAPE n/a"})
                    indicates the typical magnitude of forecast errors.
                    This uncertainty band should be factored into risk management for balancing positions.
                  </p>
                </div>
              </div>
              <p className="disclaimer">
                ⚠ Assumption: day-ahead purchasing decision made using the latest WINDFOR forecast
                published before midnight UTC on each delivery date.
                No cost or pricing data is used; financial conclusions are not drawn.
              </p>
            </section>
          )}

          {/* Decision cutoff info */}
          {analysis?.decisionCutoffNote && (
            <div className="info-box">
              <Info size={14} />
              <span>{analysis.decisionCutoffNote}</span>
            </div>
          )}

          {/* Table */}
          <section className="panel table-panel">
            <div className="panel-heading">
              <div>
                <h3>Forecast error details</h3>
                <p>Most recent {Math.min(chartData.length, 30)} matched delivery intervals</p>
              </div>
            </div>
            <div className="table-scroll">
              <table id="error-table">
                <thead>
                  <tr>
                    <th>Delivery time (UK)</th>
                    <th>Forecast published</th>
                    <th>Actual (MW)</th>
                    <th>Forecast (MW)</th>
                    <th>Error (MW)</th>
                    <th>Direction</th>
                  </tr>
                </thead>
                <tbody>
                  {(analysis?.data ?? []).slice(-30).reverse().map((row) => (
                    <tr key={row.startTime}>
                      <td>{new Date(row.startTime).toLocaleString("en-GB", { timeZone: "Europe/London", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}</td>
                      <td>{new Date(row.forecastPublishTime).toLocaleString("en-GB", { timeZone: "Europe/London", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}</td>
                      <td>{row.actualMW.toLocaleString()}</td>
                      <td>{row.forecastMW.toLocaleString()}</td>
                      <td className={row.errorMW > 0 ? "positive" : row.errorMW < 0 ? "negative" : ""}>
                        {row.errorMW > 0 ? "+" : ""}{row.errorMW.toLocaleString()}
                      </td>
                      <td>
                        {row.errorMW > 200 ? "⬆ Over" : row.errorMW < -200 ? "⬇ Under" : "≈ Close"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {(analysis?.data ?? []).length === 0 && (
                <p className="table-empty">No intervals to display.</p>
              )}
            </div>
          </section>
        </>
      )}

      <footer>
        <span>WindSight · Energy forecasting intelligence</span>
        <span>Data: Elexon BMRS · FUELHH (actual, half-hourly) / WINDFOR (day-ahead forecast, hourly)</span>
      </footer>
    </main>
  );
}

function Metric({
  id,
  icon,
  label,
  value,
  description,
  color = "green",
}: {
  id: string;
  icon: React.ReactNode;
  label: string;
  value: string;
  description: string;
  color?: string;
}) {
  return (
    <article className={`metric-card metric-${color}`} id={id}>
      <div className="metric-top">
        <span className="metric-icon">{icon}</span>
        <span className="metric-label">{label}</span>
      </div>
      <strong>{value}</strong>
      <p>{description}</p>
    </article>
  );
}

function EmptyState({ message }: { message: string }) {
  return (
    <div className="empty-state">
      <Wind size={30} />
      <h4>No data available</h4>
      <p>{message}</p>
    </div>
  );
}

export default App;
