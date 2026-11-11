#!/usr/bin/env python3
"""FDP inference: eBPF per-I/O PI steering via Cox hazard.

  sudo python3 inference.py --dev /dev/nvme0 --ns 1 --outdir out

Window / chunk / PI count / Cox coefs: inference.conf (next to this script).
PI bounds: uniform equal-width on previous-window [p1, p99].
Outputs: out/inference.log; if dump_pi_counts, out/infer/hazardN.csv
"""

from __future__ import annotations

import argparse
import configparser
import ctypes
import math
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from bcc import BPF

CONFIG_PATH = Path(__file__).resolve().parent / "inference.conf"
TRACE_PIPE = "/sys/kernel/debug/tracing/trace_pipe"

LOG1P_LUT_MAX = 65535
FIXED_POINT_SCALE = 10
BOOTSTRAP_THRESHOLD_X100 = 0xFFFFFFFF  # first window: all PI0
HAZARD_P1 = 0.01
HAZARD_P99 = 0.99
HAZARD_HIST_BINS = 65536


def load_inference_config(path: Path) -> dict:
    """Read window_size, chunk_size, fdp_pi_num, dump_pi_counts, Cox coefs."""
    if not path.is_file():
        raise SystemExit(f"config not found: {path}")
    cp = configparser.ConfigParser()
    if not cp.read(path):
        raise SystemExit(f"failed to read config: {path}")
    if not cp.has_section("inference"):
        raise SystemExit(f"{path} must have an [inference] section")
    try:
        window_sec = cp.getint("inference", "window_size")
        chunk_size = cp.getint("inference", "chunk_size")
        fdp_pi_num = cp.getint("inference", "fdp_pi_num")
        dump_pi_counts = cp.getboolean("inference", "dump_pi_counts")
        coef_upd_freq = cp.getfloat("inference", "coef_upd_freq")
        coef_last_int = cp.getfloat("inference", "coef_last_int_s_log1p")
    except (configparser.NoOptionError, ValueError) as e:
        raise SystemExit(f"{path}: {e}") from e
    if window_sec <= 0 or chunk_size <= 0:
        raise SystemExit(f"{path}: window_size and chunk_size must be > 0")
    if fdp_pi_num < 2:
        raise SystemExit(f"{path}: fdp_pi_num must be >= 2")
    return {
        "window_size": window_sec,
        "chunk_size": chunk_size,
        "fdp_pi_num": fdp_pi_num,
        "pi_threshold_num": fdp_pi_num - 1,
        "dump_pi_counts": dump_pi_counts,
        "coef_upd_freq": coef_upd_freq,
        "coef_last_int_s_log1p": coef_last_int,
    }


def make_global_config_cls(pi_threshold_num: int):
    """ctypes struct matching BPF struct global_config."""

    class GlobalConfig(ctypes.Structure):
        _fields_ = [
            ("window_start_ms", ctypes.c_uint64),
            ("window_end_ms", ctypes.c_uint64),
            ("window_id", ctypes.c_uint32),
            ("pi_thresholds_x100", ctypes.c_uint32 * pi_threshold_num),
        ]

    return GlobalConfig


def coef_x100_pair(coef_upd_freq: float, coef_last_int: float) -> tuple[int, int]:
    return (
        int(round(coef_upd_freq * 100.0)),
        int(round(coef_last_int * 100.0)),
    )


def trunc_div(a, b):
    """Toward-zero integer division (matches BPF)."""
    return int(a / b) if b else 0


def log1p_x10(x):
    return int(round(math.log1p(float(x)) * FIXED_POINT_SCALE))


def log_x10(x):
    return int(round(math.log(float(x)) * FIXED_POINT_SCALE))


def hazard_score_offset_x100(window_sec: int, coef_upd_freq: float, coef_last_int: float) -> int:
    """x100 offset: -min(coef1*(log1p(1)+log(r)-log1p(r)) + coef2*log1p(r))."""
    coef_upd_x100, coef_last_x100 = coef_x100_pair(coef_upd_freq, coef_last_int)
    feat_upd_x10 = log1p_x10(1) + log_x10(window_sec) - log1p_x10(window_sec)
    last_x10 = log1p_x10(window_sec)
    term1_x100 = trunc_div(coef_upd_x100 * feat_upd_x10, 10)
    term2_x100 = trunc_div(coef_last_x100 * last_x10, 10)
    return max(0, -(term1_x100 + term2_x100))


def log(log_file: Path, msg: str) -> None:
    ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    lines = msg.splitlines() or [""]
    with log_file.open("a", encoding="utf-8") as f:
        for line in lines:
            out = f"{ts} {line}\n"
            f.write(out)
            sys.stdout.write(out)
    sys.stdout.flush()


def _strict_increasing_x100(thresholds, cap=None):
    out = list(thresholds)
    for i in range(1, len(out)):
        if out[i] <= out[i - 1]:
            out[i] = out[i - 1] + 1
    if cap is not None:
        out = [min(t, cap) for t in out]
        for i in range(1, len(out)):
            if out[i] <= out[i - 1]:
                out[i] = out[i - 1] + 1
    return out


def build_pi_thresholds_uniform_x100(lo_x100, hi_x100, fdp_pi_num: int):
    """Equal-width bounds on [p1, p99]."""
    n_bounds = fdp_pi_num - 1
    min_score = lo_x100 / 100.0
    max_score = hi_x100 / 100.0
    if max_score <= min_score or n_bounds <= 0:
        return []
    span = max_score - min_score
    thresholds = []
    for i in range(n_bounds):
        score = min_score + span * ((i + 1) / fdp_pi_num)
        thresholds.append(int(round(score * 100.0)))
    return _strict_increasing_x100(thresholds, cap=hi_x100 - 1)


def compute_hist_quantile(hist_counts, q):
    """First bin where CDF >= q. Returns (score, total, bin); score None if empty."""
    total = sum(hist_counts)
    if total <= 0:
        return None, 0, 0
    need = total * q
    acc = 0
    q_bin = 0
    for i, c in enumerate(hist_counts):
        acc += c
        if acc >= need:
            q_bin = i
            break
    return q_bin / 100.0, total, q_bin


def hist_support_x100(hist_counts):
    """(p1, p1_bin, p99, total, p99_bin)."""
    p1, total, p1_bin = compute_hist_quantile(hist_counts, HAZARD_P1)
    p99, _, p99_bin = compute_hist_quantile(hist_counts, HAZARD_P99)
    return p1, p1_bin, p99, total, p99_bin


def _hist_count(v):
    return int(v.value) if hasattr(v, "value") else int(v)


def read_and_clear_hazard_hist(hist_map):
    """Read global hazard_hist[0..BINS) then zero non-empty bins."""
    counts = [0] * HAZARD_HIST_BINS
    try:
        items = hist_map.items_lookup_batch()
    except Exception:
        items = None

    if items is None:
        zero = ctypes.c_uint64(0)
        for i in range(HAZARD_HIST_BINS):
            key = ctypes.c_int(i)
            counts[i] = _hist_count(hist_map[key])
            if counts[i]:
                hist_map[key] = zero
        return counts

    keys_to_zero = []
    values_to_zero = []
    for k, v in items:
        idx = int(k.value) if hasattr(k, "value") else int(k)
        if idx < 0 or idx >= HAZARD_HIST_BINS:
            continue
        c = _hist_count(v)
        counts[idx] = c
        if c:
            keys_to_zero.append(k)
            values_to_zero.append(ctypes.c_uint64(0))

    if keys_to_zero:
        try:
            n = len(keys_to_zero)
            hist_map.items_update_batch(
                (hist_map.Key * n)(*keys_to_zero),
                (hist_map.Leaf * n)(*values_to_zero),
            )
        except Exception:
            zero = ctypes.c_uint64(0)
            for k in keys_to_zero:
                hist_map[k] = zero
    return counts


def pi_partition_lines(pi_thresholds_x100, fdp_pi_num: int, p99=None, p1=None):
    lines = [
        "seq: reuse previous stream_id (skip feature update; pi_write_count updated for that stream)"
    ]
    if not pi_thresholds_x100:
        lines.append("PI0: bootstrap default for non-sequential writes")
        return lines

    lower = 0.0
    for pi_idx, t in enumerate(pi_thresholds_x100):
        hi = t / 100.0
        if pi_idx == 0 and p1 is not None:
            lines.append(f"PI{pi_idx}: {lower:.6f} (or p1={p1:.6f}) ~ {hi:.6f}")
        else:
            lines.append(f"PI{pi_idx}: {lower:.6f} ~ {hi:.6f}")
        lower = (t + 1) / 100.0
    last_pi = fdp_pi_num - 1
    if p99 is not None:
        lines.append(f"PI{last_pi}: {lower:.6f} ~ (p99={p99:.4f} or over)")
    else:
        lines.append(f"PI{last_pi}: {lower:.6f} ~ (p99 or over)")
    return lines


def refresh_pi_thresholds(window_idx, hist_counts, run_log_path, fdp_pi_num: int):
    """Recompute uniform PI cuts from previous-window write-weighted histogram."""
    if not hist_counts:
        return None
    p1, p1_bin, p99, _, p99_bin = hist_support_x100(hist_counts)
    if p1 is None or p99 is None:
        return None
    new_thresholds = build_pi_thresholds_uniform_x100(p1_bin, p99_bin, fdp_pi_num)
    if len(new_thresholds) != fdp_pi_num - 1:
        return None
    p1_s = f"{p1:.4f}"
    p99_s = f"{p99:.4f}"
    log(
        run_log_path,
        f"[window={window_idx}] Updated PI thresholds (uniform p1={p1_s} p99={p99_s}): "
        + ", ".join(f"{t / 100.0:.2f}" for t in new_thresholds),
    )
    log(run_log_path, "PI ranges:")
    for line in pi_partition_lines(new_thresholds, fdp_pi_num, p99, p1):
        log(run_log_path, f"  {line}")
    return new_thresholds


def namespace_block_path(controller_path, nsid):
    m = re.match(r"^/dev/(nvme\d+)$", controller_path)
    if not m:
        raise ValueError(
            f"device must be NVMe controller path (e.g. /dev/nvme0), got: {controller_path!r}"
        )
    return f"/dev/{m.group(1)}n{int(nsid)}"


def get_namespace_size_bytes(controller_path, nsid):
    ns_path = namespace_block_path(controller_path, nsid)
    try:
        result = subprocess.run(
            ["sudo", "blockdev", "--getsize64", ns_path],
            capture_output=True,
            text=True,
            check=True,
        )
        return int(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError, OSError) as e:
        raise RuntimeError(f"Failed to get namespace size for {ns_path}: {e}") from e


def get_logical_block_size_bytes(ns_path):
    try:
        result = subprocess.run(
            ["sudo", "blockdev", "--getss", ns_path],
            capture_output=True,
            text=True,
            check=True,
        )
        return int(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError, OSError) as e:
        raise RuntimeError(f"Failed to get logical block size for {ns_path}: {e}") from e


def lba_shift_and_lbas_per_chunk(logical_b, chunk_size):
    if logical_b == 512:
        return 9, chunk_size // 512
    if logical_b == 4096:
        return 12, chunk_size // 4096
    raise ValueError(
        f"Unsupported logical block size {logical_b} (expected 512 or 4096)"
    )


def load_bpf_code(
    max_chunks,
    ns_path,
    lba_shift,
    lbas_per_chunk,
    *,
    window_sec,
    fdp_pi_num,
    dump_pi_counts,
    coef_upd_freq,
    coef_last_int,
):
    """Load inference.bpf.c and inject compile-time constants."""
    bpf_text = (Path(__file__).parent / "inference.bpf.c").read_text()
    st = os.stat(ns_path)
    coef_upd_x100, coef_last_x100 = coef_x100_pair(coef_upd_freq, coef_last_int)
    offset_x100 = hazard_score_offset_x100(window_sec, coef_upd_freq, coef_last_int)
    replacements = {
        "#define __MAX_CHUNKS__ 0": f"#define __MAX_CHUNKS__ {max_chunks}",
        "#define __TRACK_BD_MAJOR__ 0": f"#define __TRACK_BD_MAJOR__ {os.major(st.st_rdev)}",
        "#define __TRACK_BD_MINOR__ 0": f"#define __TRACK_BD_MINOR__ {os.minor(st.st_rdev)}",
        "#define __LOG1P_LUT_MAX__ 0": f"#define __LOG1P_LUT_MAX__ {LOG1P_LUT_MAX}",
        "#define __WINDOW_SEC__ 0": f"#define __WINDOW_SEC__ {window_sec}",
        "#define __LOG_WINDOW_X10__ 0": f"#define __LOG_WINDOW_X10__ {log_x10(window_sec)}",
        "#define __LBA_SHIFT__ 0": f"#define __LBA_SHIFT__ {lba_shift}",
        "#define __LBAS_PER_CHUNK__ 0": f"#define __LBAS_PER_CHUNK__ {lbas_per_chunk}",
        "#define FDP_PI_NUM 0": f"#define FDP_PI_NUM {fdp_pi_num}",
        "#define __COEF_UPD_FREQ_X100__ 0": f"#define __COEF_UPD_FREQ_X100__ {coef_upd_x100}",
        "#define __COEF_LAST_INT_S_LOG1P_X100__ 0": (
            f"#define __COEF_LAST_INT_S_LOG1P_X100__ {coef_last_x100}"
        ),
        "#define __HAZARD_SCORE_OFFSET_X100__ 0": (
            f"#define __HAZARD_SCORE_OFFSET_X100__ {offset_x100}"
        ),
        "#define __DUMP_PI_COUNTS__ 0": f"#define __DUMP_PI_COUNTS__ {1 if dump_pi_counts else 0}",
        "#define __HAZARD_HIST_BINS__ 0": f"#define __HAZARD_HIST_BINS__ {HAZARD_HIST_BINS}",
    }
    for old, new in replacements.items():
        bpf_text = bpf_text.replace(old, new)
    return bpf_text


def trace_pipe_reader(stop_event, run_log_path):
    """Read bpf_trace_printk() (write-helper failures) from trace_pipe."""
    try:
        with open(TRACE_PIPE, "r") as f:
            while not stop_event.is_set():
                line = f.readline()
                if line:
                    log(run_log_path, f"[trace] {line.rstrip()}")
    except FileNotFoundError:
        pass
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(
        description="FDP inference PI steering. Window/chunk/coefs from inference.conf."
    )
    parser.add_argument("--dev", required=True, help="NVMe controller (e.g. /dev/nvme0)")
    parser.add_argument("--ns", type=int, required=True, help="Namespace ID (e.g. 1)")
    parser.add_argument("--outdir", required=True, help="Output directory (e.g. out)")
    args = parser.parse_args()

    cfg = load_inference_config(CONFIG_PATH)
    window_sec = cfg["window_size"]
    chunk_size = cfg["chunk_size"]
    fdp_pi_num = cfg["fdp_pi_num"]
    pi_threshold_num = cfg["pi_threshold_num"]
    dump_pi_counts = cfg["dump_pi_counts"]
    coef_upd_freq = cfg["coef_upd_freq"]
    coef_last_int = cfg["coef_last_int_s_log1p"]
    interval_ms = window_sec * 1000
    offset_x100 = hazard_score_offset_x100(window_sec, coef_upd_freq, coef_last_int)
    GlobalConfig = make_global_config_cls(pi_threshold_num)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    infer_dir = outdir / "infer"
    if dump_pi_counts:
        infer_dir.mkdir(parents=True, exist_ok=True)
    run_log_path = outdir / "inference.log"

    ns_path = namespace_block_path(args.dev, args.ns)
    device_size = get_namespace_size_bytes(args.dev, args.ns)
    logical_b = get_logical_block_size_bytes(ns_path)
    lba_shift, lbas_per_chunk = lba_shift_and_lbas_per_chunk(logical_b, chunk_size)
    max_chunks = device_size // chunk_size

    log(
        run_log_path,
        f"Config {CONFIG_PATH}: window={window_sec}s chunk={chunk_size} "
        f"fdp_pi_num={fdp_pi_num} dump_pi_counts={dump_pi_counts} "
        f"coef_upd_freq={coef_upd_freq} coef_last_int_s_log1p={coef_last_int} "
        f"tracking {ns_path} {device_size / (1024**3):.2f} GB max_chunks={max_chunks:,}",
    )
    log(
        run_log_path,
        f"hazard = {coef_upd_freq}*(log1p(uc)+log({window_sec})-log1p(elapsed)) "
        f"+ {coef_last_int}*log1p(last_int_s) + {offset_x100 / 100.0:.2f}  "
        f"PI split=uniform [p1, p99]",
    )

    if not BPF.support_kfunc():
        raise SystemExit(
            "fentry/kfunc not supported on this kernel (need BTF + bpf trampoline)."
        )
    if not Path("/sys/module/nvme_core").exists():
        raise SystemExit("nvme_core is not loaded.")

    bpf_code = load_bpf_code(
        max_chunks,
        ns_path,
        lba_shift,
        lbas_per_chunk,
        window_sec=window_sec,
        fdp_pi_num=fdp_pi_num,
        dump_pi_counts=dump_pi_counts,
        coef_upd_freq=coef_upd_freq,
        coef_last_int=coef_last_int,
    )
    b = BPF(text=bpf_code, cflags=["-Wno-duplicate-decl-specifier"])
    chunk_array = b.get_table("chunk_array")
    g_cfg_map = b.get_table("g_cfg_map")
    log1p_lut_x10_map = b.get_table("log1p_lut_x10")
    hazard_hist = b.get_table("hazard_hist")
    wkey = ctypes.c_int(0)

    key0 = ctypes.c_int(0)
    info0 = chunk_array[key0]
    info0.last_accessed_lba = ctypes.c_uint64(-1).value
    chunk_array[key0] = info0

    for i in range(LOG1P_LUT_MAX + 1):
        log1p_lut_x10_map[ctypes.c_int(i)] = ctypes.c_ushort(log1p_x10(i))

    stop_trace = threading.Event()
    threading.Thread(
        target=trace_pipe_reader, args=(stop_trace, run_log_path), daemon=True
    ).start()

    pi_count_headers = ",".join(f"pi{pi}_write_count" for pi in range(fdp_pi_num))
    header = f"cidx,hazard_score,{pi_count_headers}\n"
    pi_thresholds_x100 = []
    prev_hist_counts = None
    window_idx = 0

    try:
        while True:
            window_start_ms = time.monotonic_ns() // 1_000_000
            window_end_ms = window_start_ms + interval_ms

            new_thresholds = refresh_pi_thresholds(
                window_idx, prev_hist_counts, run_log_path, fdp_pi_num
            )
            if new_thresholds is not None:
                pi_thresholds_x100 = new_thresholds

            cfg_now = GlobalConfig()
            cfg_now.window_start_ms = window_start_ms
            cfg_now.window_end_ms = window_end_ms
            cfg_now.window_id = window_idx
            values = (
                pi_thresholds_x100
                if len(pi_thresholds_x100) == pi_threshold_num
                else [BOOTSTRAP_THRESHOLD_X100] * pi_threshold_num
            )
            for i, val in enumerate(values):
                cfg_now.pi_thresholds_x100[i] = val
            g_cfg_map[wkey] = cfg_now

            time.sleep(window_sec)

            hist_counts = read_and_clear_hazard_hist(hazard_hist)
            prev_hist_counts = hist_counts
            p1, p1_bin, p99, hist_total, p99_bin = hist_support_x100(hist_counts)

            dumped = 0
            window_out = None
            if dump_pi_counts:
                window_out = infer_dir / f"hazard{window_idx}.csv"
                try:
                    items = chunk_array.items_lookup_batch()
                except Exception as e:
                    log(run_log_path, f"Batch lookup failed: {e}")
                    items = []

                keys_to_update = []
                values_to_update = []
                with open(window_out, "w") as out_f:
                    out_f.write(header)
                    for k, v in items:
                        if v.update_count <= 0:
                            continue
                        dumped += 1
                        hazard_raw = int(v.hazard_score_x100) / 100.0
                        pi_counts_csv = ",".join(
                            str(int(v.pi_write_count[pi])) for pi in range(fdp_pi_num)
                        )
                        out_f.write(f"{k.value},{hazard_raw:.6f},{pi_counts_csv}\n")
                        for pi in range(fdp_pi_num):
                            v.pi_write_count[pi] = 0
                        keys_to_update.append(k)
                        values_to_update.append(v)

                if keys_to_update:
                    try:
                        n = len(keys_to_update)
                        chunk_array.items_update_batch(
                            (chunk_array.Key * n)(*keys_to_update),
                            (chunk_array.Leaf * n)(*values_to_update),
                        )
                    except AttributeError:
                        for k, v in zip(keys_to_update, values_to_update):
                            chunk_array[k] = v

            p1_s = f"{p1:.4f}" if p1 is not None else "none"
            p99_s = f"{p99:.4f}" if p99 is not None else "none"
            extra = (
                f", wrote {window_out}, dumped_chunks={dumped}" if dump_pi_counts else ""
            )
            log(
                run_log_path,
                f"[window={window_idx}] hist_total={hist_total} "
                f"p1_bin={p1_bin} p1={p1_s} "
                f"p99_bin={p99_bin} p99={p99_s}{extra}",
            )
            window_idx += 1

    except KeyboardInterrupt:
        log(run_log_path, "Tracing stopped.")
    finally:
        stop_trace.set()


if __name__ == "__main__":
    main()
