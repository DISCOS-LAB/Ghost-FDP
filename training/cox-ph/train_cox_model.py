#!/usr/bin/env python3
"""Fit Cox PH on window*.csv (landmark-τ rows) and save a pickle artifact.

CSV columns (from training.py):
  cidx, sv_t2e_s, sv_ev_ind, upd_freq, last_int_ms

Covariates: upd_freq_log1p, last_int_s_log1p  (no StandardScaler)

  python3 train_cox_model.py \\
    --data-dir ../out/train \\
    --train-range 0 5 \\
    --model-out cox_model.pkl \\
    --penalizer 0.1
"""

from __future__ import annotations

import argparse
import os
import pickle
from datetime import datetime, timezone

from lifelines import CoxPHFitter
from lifelines.utils import concordance_index

from cox_data import (
    DURATION_COL,
    EVENT_COL,
    FEATURE_COLS,
    ID_COL,
    RAW_FEATURE_COLS,
    Tee,
    build_file_list,
    load_csvs,
    prepare_df,
)


def parse_args():
    p = argparse.ArgumentParser(
        description="Train Cox PH from window{start}..window{end} and save a .pkl artifact."
    )
    p.add_argument(
        "--data-dir",
        required=True,
        help="Directory of window*.csv (e.g. ../out/train)",
    )
    p.add_argument(
        "--train-range",
        nargs=2,
        type=int,
        metavar=("START", "END"),
        required=True,
        help="Train files: windowSTART.csv … windowEND.csv",
    )
    p.add_argument("--model-out", required=True, help="Output artifact path (.pkl)")
    p.add_argument(
        "--result-out",
        default=None,
        help="Text log path. Default: <model-out stem>_result.txt",
    )
    p.add_argument(
        "--penalizer",
        type=float,
        default=0.1,
        help="L2 penalizer for CoxPHFitter (default: 0.1)",
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
    p.add_argument("--seed", type=int, default=0, help="RNG seed for sampling")
    return p.parse_args()


def default_result_out(model_out: str) -> str:
    root, _ = os.path.splitext(model_out)
    return f"{root}_result.txt"


def main():
    args = parse_args()
    data_dir = os.path.abspath(os.path.expanduser(args.data_dir))
    train_start, train_end = args.train_range
    model_out = os.path.abspath(os.path.expanduser(args.model_out))
    result_out = os.path.abspath(
        os.path.expanduser(args.result_out or default_result_out(model_out))
    )
    if train_start > train_end:
        raise ValueError("train-range START must be <= END")

    log = Tee()
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    log.print(f"[{ts}] train_cox_model.py")
    log.print(f"data_dir={data_dir}")
    log.print(f"train_range={train_start}..{train_end}")
    log.print(f"model_out={model_out}")
    log.print(f"result_out={result_out}")
    log.print(f"penalizer={args.penalizer}")
    log.print(f"sample_frac={args.sample_frac} seed={args.seed}")
    log.print(f"features={FEATURE_COLS}")
    log.print("")

    files = build_file_list(data_dir, train_start, train_end)
    if not files:
        raise SystemExit("No train files found.")

    log.print(f"Train files: {len(files)} ({train_start}..{train_end})")
    train_df = load_csvs(
        files,
        chunksize=args.chunksize,
        sample_frac=args.sample_frac,
        seed=args.seed,
    )
    log.print(f"Train rows (raw): {len(train_df):,}")

    train_df = prepare_df(train_df)
    log.print(f"Train rows (processed): {len(train_df):,}")
    log.print(f"Train event rate: {train_df[EVENT_COL].mean():.4f}")
    log.print(f"Train unique chunks: {train_df[ID_COL].nunique():,}")

    train_df = train_df.astype({c: "float64" for c in FEATURE_COLS}, copy=False)

    cph = CoxPHFitter(penalizer=args.penalizer)
    cph.fit(
        train_df[[DURATION_COL, EVENT_COL] + FEATURE_COLS],
        duration_col=DURATION_COL,
        event_col=EVENT_COL,
    )

    # Higher partial hazard = shorter expected time-to-event → negate for C-index.
    train_risk = cph.predict_partial_hazard(train_df[FEATURE_COLS])
    train_cindex = concordance_index(
        train_df[DURATION_COL], -train_risk.values.ravel(), train_df[EVENT_COL]
    )
    log.print(f"Train C-index: {train_cindex:.4f}")
    log.print("")
    log.print("=== Cox summary ===")
    log.print(cph.summary[["coef", "exp(coef)", "se(coef)", "p"]].to_string())

    artifact = {
        "cph": cph,
        "duration_col": DURATION_COL,
        "event_col": EVENT_COL,
        "id_col": ID_COL,
        "feature_cols": FEATURE_COLS,
        "raw_feature_cols": RAW_FEATURE_COLS,
        "penalizer": args.penalizer,
        "train_range": [train_start, train_end],
        "train_cindex": float(train_cindex),
        "train_rows": len(train_df),
        "result_out": result_out,
    }
    os.makedirs(os.path.dirname(model_out) or ".", exist_ok=True)
    with open(model_out, "wb") as f:
        pickle.dump(artifact, f)
    log.print("")
    log.print(f"Saved model artifact: {model_out}")
    log.print(f"Saved train result: {result_out}")
    log.dump(result_out)


if __name__ == "__main__":
    main()
