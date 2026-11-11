#!/usr/bin/env python3
"""Schoenfeld residual / PH-assumption diagnostics for a trained Cox PH model.

Computes proportional_hazard_test (rank transform) and plots Schoenfeld
residuals vs event time with LOWESS. --cache-in replots without recomputing.

  python3 check_cox_residuals.py \\
    --model cox_model.pkl \\
    --data-dir ../out/train \\
    --train-range 0 5 \\
    --out-prefix cox_residuals

  python3 check_cox_residuals.py \\
    --cache-in cox_residuals_residual_cache.pkl \\
    --out-prefix cox_residuals
"""

from __future__ import annotations

import argparse
import os
import pickle

import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
from lifelines import CoxPHFitter
from lifelines.statistics import proportional_hazard_test
from statsmodels.nonparametric.smoothers_lowess import lowess

from cox_data import (
    build_file_list,
    cox_feature_matrix,
    load_csvs,
    model_frame,
    prepare_df,
)


def parse_args():
    p = argparse.ArgumentParser(
        description="Schoenfeld residual diagnostics for a trained Cox PH model."
    )
    p.add_argument("--model", help="Trained artifact (.pkl); required unless --cache-in")
    p.add_argument("--data-dir", help="Directory of window*.csv")
    p.add_argument(
        "--train-range",
        nargs=2,
        type=int,
        metavar=("START", "END"),
        help="CSV range: windowSTART.csv … windowEND.csv",
    )
    p.add_argument(
        "--chunksize",
        type=int,
        default=500_000,
        help="Rows per read_csv chunk (default: 500000)",
    )
    p.add_argument(
        "--sample-frac",
        type=float,
        default=None,
        help="If set (0,1], keep this fraction of rows per chunk",
    )
    p.add_argument("--seed", type=int, default=0, help="RNG seed for sampling / plot subsample")
    p.add_argument(
        "--max-plot-points",
        type=int,
        default=30000,
        help="Max scatter points per covariate (default: 30000). <=0 = all.",
    )
    p.add_argument(
        "--out-prefix",
        default="cox_residuals",
        help="Prefix for pdf / csv / cache (default: cox_residuals)",
    )
    p.add_argument(
        "--cache-out",
        default=None,
        help="Residual cache .pkl. Default: <out-prefix>_residual_cache.pkl",
    )
    p.add_argument(
        "--cache-in",
        default=None,
        help="Skip model/data; redraw plot from a residual cache .pkl",
    )
    args = p.parse_args()
    if not args.cache_in:
        missing = [
            flag
            for flag, val in (
                ("--model", args.model),
                ("--data-dir", args.data_dir),
                ("--train-range", args.train_range),
            )
            if not val
        ]
        if missing:
            p.error(
                "required when --cache-in is not set: " + ", ".join(missing)
            )
    return args


def draw_schoenfeld_plot(feature_cols, scho, event_times, ph_summary, pdf_out, max_plot_points, seed):
    """One panel per covariate: residual vs time, LOWESS, ±1σ band, PH p-value."""
    n = len(feature_cols)
    fig, axes = plt.subplots(n, 1, figsize=(10, 3.6 * n), constrained_layout=True)
    if n == 1:
        axes = [axes]

    sns.set_style("whitegrid")
    rng = np.random.default_rng(seed)

    for i, cov in enumerate(feature_cols):
        ax = axes[i]
        y = scho[cov].values
        x = event_times

        if max_plot_points and max_plot_points > 0 and len(x) > max_plot_points:
            idx = rng.choice(len(x), size=max_plot_points, replace=False)
            x = x[idx]
            y = y[idx]

        order = np.argsort(x)
        x_sorted = x[order]
        y_sorted = y[order]
        smooth = lowess(y_sorted, x_sorted, frac=0.3, return_sorted=True)

        ax.scatter(
            x,
            y,
            s=2,
            facecolors="black",
            edgecolors="black",
            linewidths=0.7,
            alpha=0.3,
            zorder=3,
        )

        yhat_interp = np.interp(x_sorted, smooth[:, 0], smooth[:, 1])
        resid_std = float(np.std(y_sorted - yhat_interp))
        ax.fill_between(
            smooth[:, 0],
            smooth[:, 1] - resid_std,
            smooth[:, 1] + resid_std,
            facecolor="#C8E6C9",
            edgecolor="#66BB6A",
            linewidth=1.5,
            alpha=0.7,
            zorder=1,
            label="~1 sigma band",
        )

        ax.plot(smooth[:, 0], smooth[:, 1], color="red", linewidth=2.0, zorder=4, label="LOWESS")
        ax.axhline(0.0, linestyle="--", color="black", linewidth=1)
        ax.set_ylim(-5, 5)

        pval = np.nan
        if ph_summary is not None and cov in ph_summary.index and "p" in ph_summary.columns:
            pval = ph_summary.loc[cov, "p"]
        ax.set_title(f"Schoenfeld test p={pval:.3g} ({cov})", fontsize=11)
        ax.set_xlabel("Time", fontsize=10)
        ax.set_ylabel("Residual", fontsize=10)
        ax.legend(loc="best", fontsize=8)

    plt.savefig(pdf_out, dpi=150)
    plt.close(fig)
    print(f"[done] Schoenfeld residual plot: {pdf_out}")


def main():
    args = parse_args()
    pdf_out = f"{args.out_prefix}_schoenfeld.pdf"

    if args.cache_in:
        with open(args.cache_in, "rb") as f:
            payload = pickle.load(f)
        draw_schoenfeld_plot(
            payload["feature_cols"],
            payload["scho"],
            np.asarray(payload["event_times"]),
            payload.get("ph_summary"),
            pdf_out,
            args.max_plot_points,
            args.seed,
        )
        return

    with open(args.model, "rb") as f:
        artifact = pickle.load(f)

    cph = artifact["cph"]
    scaler = artifact.get("scaler")  # older artifacts only
    duration_col = artifact["duration_col"]
    event_col = artifact["event_col"]
    feature_cols = artifact["feature_cols"]

    start_idx, end_idx = args.train_range
    files = build_file_list(args.data_dir, start_idx, end_idx)
    if not files:
        raise SystemExit("No input CSV files found in the requested range.")

    df = load_csvs(
        files,
        chunksize=args.chunksize,
        sample_frac=args.sample_frac,
        seed=args.seed,
    )
    df = prepare_df(df)
    model_df = model_frame(
        df,
        duration_col=duration_col,
        event_col=event_col,
        feature_cols=feature_cols,
    )
    model_df[feature_cols] = cox_feature_matrix(df, feature_cols, scaler=scaler)

    # Schoenfeld residuals need a model fit on these rows.
    diag_cph = cph
    fitted_n = getattr(cph, "_n_examples", None)
    if fitted_n is not None and int(fitted_n) != len(model_df):
        penalizer = float(artifact.get("penalizer", 0.1))
        print(
            "[info] row count differs from trained model "
            f"(trained={int(fitted_n):,}, current={len(model_df):,}). "
            "Refitting diagnostic Cox model on current data."
        )
        diag_cph = CoxPHFitter(penalizer=penalizer)
        diag_cph.fit(model_df, duration_col=duration_col, event_col=event_col)

    ph_test = proportional_hazard_test(diag_cph, model_df, time_transform="rank")
    ph_summary = ph_test.summary.copy()
    ph_csv = f"{args.out_prefix}_ph_test.csv"
    ph_summary.to_csv(ph_csv)
    print(f"[done] PH test summary: {ph_csv}")

    scho = diag_cph.compute_residuals(model_df, kind="schoenfeld")
    event_times = model_df.loc[scho.index, duration_col].values
    draw_schoenfeld_plot(
        feature_cols, scho, event_times, ph_summary, pdf_out, args.max_plot_points, args.seed
    )

    cache_out = args.cache_out or f"{args.out_prefix}_residual_cache.pkl"
    with open(cache_out, "wb") as f:
        pickle.dump(
            {
                "feature_cols": feature_cols,
                "event_times": event_times,
                "scho": scho,
                "ph_summary": ph_summary,
            },
            f,
        )
    print(f"[done] residual cache saved: {cache_out}")


if __name__ == "__main__":
    main()
