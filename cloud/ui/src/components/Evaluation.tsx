import { useEffect, useState } from "react";

import { CopilotApiError, getEvaluation } from "../api";
import type { Comparison, EvaluationReport, StrategyScore } from "../types";

/**
 * The measured numbers, and the uncertainty around them.
 *
 * Both charts are dot-and-interval plots rather than bars. A bar encodes
 * magnitude from zero, and every NDCG here sits between 0.856 and 0.946 — a
 * bar chart would either start at zero and flatten every difference, or start
 * at 0.8 and be a lying bar chart. A dot carries no from-zero implication, so
 * a clipped axis is honest, and the confidence interval is the point anyway.
 */

/** Single series, so the only colour requirement is contrast on the surface. */
const AXIS_MIN = 0.75;
const AXIS_MAX = 1.0;

export default function Evaluation() {
  const [report, setReport] = useState<EvaluationReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getEvaluation()
      .then((r) => !cancelled && setReport(r.report))
      .catch((err) => {
        if (!cancelled) {
          setError(
            err instanceof CopilotApiError ? err.message : "Could not load the evaluation.",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) {
    return (
      <div className="page">
        <div className="notice notice--danger">
          <span className="notice__icon">✕</span>
          <span>{error}</span>
        </div>
      </div>
    );
  }

  if (!report) {
    return (
      <div className="page">
        <div className="thinking">
          <span className="spinner" />
          <span>Loading the evaluation…</span>
        </div>
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page__head">
        <h1 className="page__title">Evaluation</h1>
        <p className="page__sub">
          {report.cases} held-out questions at k={report.k}, {report.bootstrap_resamples.toLocaleString()}{" "}
          bootstrap resamples, Holm-Bonferroni corrected across six pairwise tests. The questions
          were generated from uniformly sampled passages, not written by the author, and were never
          tuned against.
        </p>
      </header>

      {!report.current && (
        <div className="notice notice--danger">
          <span className="notice__icon">!</span>
          <span>
            Historical baseline only — do not treat these scores as the deployed model's quality.
            {" "}{report.stale_reasons.join("; ")}. Rebuild the index and rerun the held-out
            evaluation before release.
          </span>
        </div>
      )}

      <div className="notice notice--info">
        <span className="notice__icon">i</span>
        <span>
          Baseline {report.baseline_id}: {report.evaluated_embedding_provider}/
          {report.evaluated_embedding_model}, index {report.evaluated_index_version}, generated{" "}
          {new Date(report.generated_at_utc).toLocaleString()}.
        </span>
      </div>

      <div className="notice notice--info">
        <span className="notice__icon">i</span>
        <span>{report.headline}</span>
      </div>

      <section className="card">
        <header className="card__head">
          <strong style={{ fontSize: 13 }}>NDCG@{report.k} by strategy</strong>
          <span className="card__spacer" />
          <span className="badge badge--neutral">95% CI</span>
        </header>
        <div className="pane">
          <ScoreChart strategies={report.strategies} />
          <ScoreTable strategies={report.strategies} />
        </div>
      </section>

      <section className="card">
        <header className="card__head">
          <strong style={{ fontSize: 13 }}>Pairwise differences</strong>
          <span className="card__spacer" />
          <span className="badge badge--neutral">interval crossing 0 = not significant</span>
        </header>
        <div className="pane">
          <ForestChart comparisons={report.comparisons} />
          <p className="field__hint" style={{ marginTop: 12 }}>
            A filled marker is significant after correction; a hollow one is not. Significance is
            never shown by colour alone — the marker shape and the label both carry it.
          </p>
        </div>
      </section>
    </div>
  );
}

// ---------------------------------------------------------------------------
// NDCG dot-and-interval plot
// ---------------------------------------------------------------------------

function ScoreChart({ strategies }: { strategies: StrategyScore[] }) {
  const rowHeight = 38;
  const padLeft = 86;
  const padRight = 56;
  const padTop = 10;
  const axisHeight = 26;
  const width = 640;
  const plotWidth = width - padLeft - padRight;
  const height = padTop + strategies.length * rowHeight + axisHeight;

  const x = (value: number) =>
    padLeft + ((value - AXIS_MIN) / (AXIS_MAX - AXIS_MIN)) * plotWidth;

  const ticks = [0.75, 0.8, 0.85, 0.9, 0.95, 1.0];

  return (
    <div className="viz">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        role="img"
        aria-label="NDCG at 8 by retrieval strategy, with 95 percent confidence intervals"
      >
        {ticks.map((tick) => (
          <g key={tick}>
            <line
              x1={x(tick)}
              y1={padTop}
              x2={x(tick)}
              y2={padTop + strategies.length * rowHeight}
              className="viz-grid"
            />
            <text x={x(tick)} y={height - 8} className="viz-axis-label" textAnchor="middle">
              {tick.toFixed(2)}
            </text>
          </g>
        ))}

        {strategies.map((strategy, i) => {
          const y = padTop + i * rowHeight + rowHeight / 2;
          return (
            <g key={strategy.name}>
              <text x={padLeft - 12} y={y + 4} className="viz-row-label" textAnchor="end">
                {strategy.name}
              </text>
              <line
                x1={x(strategy.ci[0])}
                y1={y}
                x2={x(strategy.ci[1])}
                y2={y}
                className="viz-interval"
              />
              {/* Interval caps make the endpoints readable at a glance. */}
              <line x1={x(strategy.ci[0])} y1={y - 5} x2={x(strategy.ci[0])} y2={y + 5} className="viz-interval" />
              <line x1={x(strategy.ci[1])} y1={y - 5} x2={x(strategy.ci[1])} y2={y + 5} className="viz-interval" />
              <circle cx={x(strategy.ndcg)} cy={y} r={5.5} className="viz-dot" />
              <text x={width - padRight + 10} y={y + 4} className="viz-value">
                {strategy.ndcg.toFixed(3)}
              </text>
              <title>
                {`${strategy.name}: NDCG ${strategy.ndcg.toFixed(3)} (95% CI ${strategy.ci[0].toFixed(3)}–${strategy.ci[1].toFixed(3)}), median ${strategy.median_ms} ms`}
              </title>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function ScoreTable({ strategies }: { strategies: StrategyScore[] }) {
  return (
    <div className="table-wrap" style={{ marginTop: 14 }}>
      <table className="data">
        <thead>
          <tr>
            <th>strategy</th>
            <th>NDCG@8</th>
            <th>95% CI</th>
            <th>MRR</th>
            <th>recall</th>
            <th>median</th>
          </tr>
        </thead>
        <tbody>
          {strategies.map((s) => (
            <tr key={s.name}>
              <td>{s.name}</td>
              <td className="num">{s.ndcg.toFixed(3)}</td>
              <td className="num">
                {s.ci[0].toFixed(3)} – {s.ci[1].toFixed(3)}
              </td>
              <td className="num">{s.mrr.toFixed(3)}</td>
              <td className="num">{s.recall.toFixed(3)}</td>
              <td className="num">{s.median_ms.toLocaleString()} ms</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Forest plot of paired differences
// ---------------------------------------------------------------------------

function ForestChart({ comparisons }: { comparisons: Comparison[] }) {
  const rowHeight = 34;
  const padLeft = 150;
  const padRight = 64;
  const padTop = 10;
  const axisHeight = 26;
  const width = 640;
  const plotWidth = width - padLeft - padRight;
  const height = padTop + comparisons.length * rowHeight + axisHeight;

  const lows = comparisons.map((c) => c.ci[0]);
  const highs = comparisons.map((c) => c.ci[1]);
  const min = Math.min(-0.02, ...lows);
  const max = Math.max(...highs);
  const pad = (max - min) * 0.08;

  const x = (value: number) =>
    padLeft + ((value - (min - pad)) / (max + pad - (min - pad))) * plotWidth;

  const ticks = [-0.05, 0, 0.05, 0.1, 0.15].filter((t) => t >= min - pad && t <= max + pad);

  return (
    <div className="viz">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        role="img"
        aria-label="Paired NDCG differences with 95 percent confidence intervals"
      >
        {ticks.map((tick) => (
          <g key={tick}>
            <line
              x1={x(tick)}
              y1={padTop}
              x2={x(tick)}
              y2={padTop + comparisons.length * rowHeight}
              className={tick === 0 ? "viz-zero" : "viz-grid"}
            />
            <text x={x(tick)} y={height - 8} className="viz-axis-label" textAnchor="middle">
              {tick === 0 ? "0" : tick.toFixed(2)}
            </text>
          </g>
        ))}

        {comparisons.map((comparison, i) => {
          const y = padTop + i * rowHeight + rowHeight / 2;
          return (
            <g key={comparison.pair}>
              <text x={padLeft - 12} y={y + 4} className="viz-row-label" textAnchor="end">
                {comparison.pair}
              </text>
              <line
                x1={x(comparison.ci[0])}
                y1={y}
                x2={x(comparison.ci[1])}
                y2={y}
                className="viz-interval"
              />
              <line x1={x(comparison.ci[0])} y1={y - 5} x2={x(comparison.ci[0])} y2={y + 5} className="viz-interval" />
              <line x1={x(comparison.ci[1])} y1={y - 5} x2={x(comparison.ci[1])} y2={y + 5} className="viz-interval" />
              <circle
                cx={x(comparison.delta)}
                cy={y}
                r={5.5}
                className={comparison.significant ? "viz-dot" : "viz-dot viz-dot--hollow"}
              />
              <text x={width - padRight + 10} y={y + 4} className="viz-value">
                {comparison.delta > 0 ? "+" : ""}
                {comparison.delta.toFixed(3)}
              </text>
              <title>
                {`${comparison.pair}: ${comparison.delta > 0 ? "+" : ""}${comparison.delta.toFixed(3)} NDCG, 95% CI ${comparison.ci[0].toFixed(3)} to ${comparison.ci[1].toFixed(3)}, p = ${comparison.p}, ${comparison.significant ? "significant" : "not significant"} after correction`}
              </title>
            </g>
          );
        })}
      </svg>

      <div className="table-wrap" style={{ marginTop: 14 }}>
        <table className="data">
          <thead>
            <tr>
              <th>comparison</th>
              <th>Δ NDCG</th>
              <th>95% CI</th>
              <th>p</th>
              <th>after correction</th>
            </tr>
          </thead>
          <tbody>
            {comparisons.map((c) => (
              <tr key={c.pair}>
                <td>{c.pair}</td>
                <td className="num">
                  {c.delta > 0 ? "+" : ""}
                  {c.delta.toFixed(3)}
                </td>
                <td className="num">
                  {c.ci[0].toFixed(3)} – {c.ci[1].toFixed(3)}
                </td>
                <td className="num">{c.p < 0.0001 ? "<0.0001" : c.p.toFixed(4)}</td>
                <td>
                  <span className={`badge ${c.significant ? "badge--ok" : "badge--neutral"}`}>
                    {c.significant ? "significant" : "not significant"}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
