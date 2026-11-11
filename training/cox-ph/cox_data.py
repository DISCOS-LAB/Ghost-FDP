"""CSV schema and Cox PH feature prep for training.py window*.csv.

Each row is one LBA chunk at landmark τ (sv_tau_sec in ../training.conf):

  cidx          chunk index
  sv_t2e_s      duration: τ → first post-τ non-seq write,
                or τ → window end if censored
  sv_ev_ind     event indicator: 1 if a non-seq write occurred after τ
  upd_freq      non-seq write count in [window_start, τ]
  last_int_ms   recency at τ: τ − last_write_ms (integer milliseconds)

Model covariates (same log1p transform as inference):
  upd_freq_log1p
  last_int_s_log1p   where last_int_s = last_int_ms / 1000
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

DURATION_COL = "sv_t2e_s"
EVENT_COL = "sv_ev_ind"
ID_COL = "cidx"

CSV_WINDOW_PREFIX = "window"
RAW_FEATURE_COLS = ["upd_freq", "last_int_ms"]
FEATURE_COLS = ["upd_freq_log1p", "last_int_s_log1p"]

_CSV_COLS = {ID_COL, DURATION_COL, EVENT_COL, *RAW_FEATURE_COLS}


class Tee:
    """Mirror print() to stdout and an in-memory buffer (flush with dump())."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, *args, **kwargs) -> None:
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        msg = sep.join(str(a) for a in args) + end
        self.lines.append(msg)
        sys.stdout.write(msg)
        sys.stdout.flush()

    def dump(self, path: str) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(self.lines)


def csv_usecols(name: str) -> bool:
    """pandas usecols filter: keep landmark-τ columns only."""
    return name in _CSV_COLS


def window_csv_path(data_dir: str, window_idx: int) -> str:
    return os.path.join(data_dir, f"{CSV_WINDOW_PREFIX}{window_idx}.csv")


def build_file_list(data_dir: str, start_idx: int, end_idx: int) -> list[str]:
    """window{start}.csv … window{end}.csv; warn on missing files."""
    files = []
    for i in range(start_idx, end_idx + 1):
        path = window_csv_path(data_dir, i)
        if os.path.exists(path):
            files.append(path)
        else:
            print(f"[WARN] File not found: {path}")
    return files


def load_csvs(
    file_list: list[str],
    *,
    chunksize: int,
    sample_frac: float | None,
    seed: int,
) -> pd.DataFrame:
    """Read window CSVs in chunks; optionally subsample each chunk."""
    if sample_frac is not None and not (0 < sample_frac <= 1):
        raise ValueError("--sample-frac must be in (0, 1]")

    rng = np.random.default_rng(seed)
    dfs = []
    for f in file_list:
        for chunk in pd.read_csv(f, usecols=csv_usecols, chunksize=chunksize):
            if sample_frac is not None:
                sample_n = max(1, int(round(len(chunk) * sample_frac)))
                if sample_n < len(chunk):
                    chunk = chunk.sample(
                        n=sample_n,
                        random_state=int(rng.integers(0, 2**31 - 1)),
                    )
            dfs.append(chunk)
    if not dfs:
        raise ValueError("No rows loaded from CSV files.")
    return pd.concat(dfs, ignore_index=True)


def add_model_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add log1p covariates used by Cox PH / inference."""
    out = df.copy()
    upd_freq = pd.to_numeric(out["upd_freq"], errors="coerce").clip(lower=0)
    last_int_ms = pd.to_numeric(out["last_int_ms"], errors="coerce").clip(lower=0)
    last_int_s = last_int_ms / 1000.0
    out["upd_freq"] = upd_freq
    out["last_int_ms"] = last_int_ms
    out["upd_freq_log1p"] = np.log1p(upd_freq)
    out["last_int_s_log1p"] = np.log1p(last_int_s)
    return out


def prepare_df(df: pd.DataFrame) -> pd.DataFrame:
    """Validate landmark columns, build features, drop invalid rows."""
    required = [ID_COL, DURATION_COL, EVENT_COL, *RAW_FEATURE_COLS]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns: {missing}. Found: {list(df.columns)}")

    out = df[required].copy()
    for col in [DURATION_COL, EVENT_COL, *RAW_FEATURE_COLS]:
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out = add_model_features(out)

    before = len(out)
    out = out.dropna(subset=[DURATION_COL, EVENT_COL, *FEATURE_COLS])
    dropped = before - len(out)
    if dropped:
        print(f"Dropped rows with NA: {dropped:,}")

    out[EVENT_COL] = out[EVENT_COL].astype(int)
    out = out[out[DURATION_COL] > 0].copy()
    # No pre-τ write → upd_freq / last_int undefined (training.py already skips these).
    out = out[out["upd_freq"] > 0].copy()
    return out


def model_frame(
    df: pd.DataFrame,
    *,
    duration_col: str = DURATION_COL,
    event_col: str = EVENT_COL,
    feature_cols: list[str] | None = None,
) -> pd.DataFrame:
    """Duration / event / covariate columns for lifelines APIs."""
    feats = feature_cols or FEATURE_COLS
    return df[[duration_col, event_col, *feats]].copy()


def cox_feature_matrix(
    df: pd.DataFrame,
    feature_cols: list[str] | None = None,
    *,
    scaler=None,
) -> pd.DataFrame:
    """Covariate matrix. scaler is optional (older artifacts only)."""
    feats = feature_cols or FEATURE_COLS
    x = df[list(feats)].astype("float64", copy=False)
    if scaler is None:
        return x
    return pd.DataFrame(
        scaler.transform(x),
        columns=list(feats),
        index=df.index,
    )
