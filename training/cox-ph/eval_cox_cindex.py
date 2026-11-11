#!/usr/bin/env python3
"""Evaluate C-index of a trained Cox PH model on window*.csv.

  python3 eval_cox_cindex.py \\
    --model cox_model.pkl \\
    --data-dir ../out/train \\
    --eval-range 6 10 \\
    --sample-frac 0.1
"""

from __future__ import annotations

import argparse
import os
import pickle
from datetime import datetime, timezone

from lifelines.utils import concordance_index

from cox_data import (
    FEATURE_COLS,
    ID_COL,
    Tee,
    build_file_list,
    cox_feature_matrix,
    load_csvs,
    model_frame,
    prepare_df,
)


def parse_args():
    p = argparse.ArgumentParser(
        description="Compute C-index of a trained Cox model on window*.csv."
    )
    p.add_argument("--model", required=True, help="Trained artifact (.pkl)")
    p.add_argument(
        "--data-dir",
        required=True,
        help="Directory of window*.csv (e.g. ../out/train)",
    )
    p.add_argument(
        "--eval-range",
        nargs=2,
        type=int,
        metavar=("START", "END"),
        required=True,
        help="Eval files: windowSTART.csv … windowEND.csv",
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
    p.add_argument(
        "--result-out",
        default=None,
        help="Text log path. Default: <model stem>_eval_cindex.txt",
    )
    return p.parse_args()


def default_result_out(model_path: str) -> str:
    root, _ = os.path.splitext(model_path)
    return f"{root}_eval_cindex.txt"


def main():
    args = parse_args()
    model_path = os.path.abspath(os.path.expanduser(args.model))
    data_dir = os.path.abspath(os.path.expanduser(args.data_dir))
    start_idx, end_idx = args.eval_range
    result_out = os.path.abspath(
        os.path.expanduser(args.result_out or default_result_out(model_path))
    )
    if start_idx > end_idx:
        raise ValueError("eval-range START must be <= END")

    log = Tee()
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    log.print(f"[{ts}] eval_cox_cindex.py")
    log.print(f"model={model_path}")
    log.print(f"data_dir={data_dir}")
    log.print(f"eval_range={start_idx}..{end_idx}")
    log.print(f"sample_frac={args.sample_frac} seed={args.seed}")
    log.print(f"result_out={result_out}")
    log.print("")

    with open(model_path, "rb") as f:
        artifact = pickle.load(f)

    cph = artifact["cph"]
    scaler = artifact.get("scaler")  # older artifacts only
    duration_col = artifact.get("duration_col", "sv_t2e_s")
    event_col = artifact.get("event_col", "sv_ev_ind")
    feature_cols = list(artifact.get("feature_cols", FEATURE_COLS))
    train_cindex = artifact.get("train_cindex")

    log.print(f"artifact features={feature_cols}")
    if train_cindex is not None:
        log.print(f"artifact train_cindex={float(train_cindex):.4f}")
    log.print("")

    files = build_file_list(data_dir, start_idx, end_idx)
    if not files:
        raise SystemExit("No eval CSV files found in the requested range.")

    log.print(f"Eval files: {len(files)} ({start_idx}..{end_idx})")
    df = load_csvs(
        files,
        chunksize=args.chunksize,
        sample_frac=args.sample_frac,
        seed=args.seed,
    )
    log.print(f"Eval rows (raw): {len(df):,}")

    df = prepare_df(df)
    log.print(f"Eval rows (processed): {len(df):,}")
    log.print(f"Eval event rate: {df[event_col].mean():.4f}")
    log.print(f"Eval unique chunks: {df[ID_COL].nunique():,}")

    model_df = model_frame(
        df,
        duration_col=duration_col,
        event_col=event_col,
        feature_cols=feature_cols,
    )
    model_df[feature_cols] = cox_feature_matrix(df, feature_cols, scaler=scaler)

    risk = cph.predict_partial_hazard(model_df[feature_cols])
    cindex = concordance_index(
        model_df[duration_col],
        -risk.values.ravel(),
        model_df[event_col],
    )
    log.print("")
    log.print(f"Eval C-index: {cindex:.4f}")
    if train_cindex is not None:
        log.print(f"Train C-index (from artifact): {float(train_cindex):.4f}")
        log.print(f"Delta (eval - train): {cindex - float(train_cindex):+.4f}")

    log.print("")
    log.print(f"Saved eval result: {result_out}")
    log.dump(result_out)
    print(f"[done] eval C-index={cindex:.4f}")


if __name__ == "__main__":
    main()
