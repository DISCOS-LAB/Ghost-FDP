#!/usr/bin/env python3
"""AutoStream SFR (Algorithm 2) via fentry eBPF.

  sudo python3 autoStream.py --dev /dev/nvme0 --ns 1 --outdir out

Chunk / PI count / decay / dump interval: autoStream.conf (next to this script).
Outputs: out/inference.log; if dump_pi_counts, out/infer/piN.csv
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

CONFIG_PATH = Path(__file__).resolve().parent / "autoStream.conf"
TRACE_PIPE = "/sys/kernel/debug/tracing/trace_pipe"
LOG_LUT_MAX = 1_000_000  # floor(log10) LUT keys 1..LOG_LUT_MAX


def load_autostream_config(path: Path) -> dict:
    """Read window_size, chunk_size, fdp_pi_num, dump_pi_counts, decay_period."""
    if not path.is_file():
        raise SystemExit(f"config not found: {path}")
    cp = configparser.ConfigParser()
    if not cp.read(path):
        raise SystemExit(f"failed to read config: {path}")
    if not cp.has_section("autostream"):
        raise SystemExit(f"{path} must have an [autostream] section")
    try:
        window_sec = cp.getint("autostream", "window_size")
        chunk_size = cp.getint("autostream", "chunk_size")
        fdp_pi_num = cp.getint("autostream", "fdp_pi_num")
        dump_pi_counts = cp.getboolean("autostream", "dump_pi_counts")
        decay_period = cp.getint("autostream", "decay_period")
    except (configparser.NoOptionError, ValueError) as e:
        raise SystemExit(f"{path}: {e}") from e
    if window_sec <= 0 or chunk_size <= 0 or decay_period <= 0:
        raise SystemExit(
            f"{path}: window_size, chunk_size, decay_period must be > 0"
        )
    if fdp_pi_num < 2:
        raise SystemExit(f"{path}: fdp_pi_num must be >= 2")
    return {
        "window_size": window_sec,
        "chunk_size": chunk_size,
        "fdp_pi_num": fdp_pi_num,
        "dump_pi_counts": dump_pi_counts,
        "decay_period": decay_period,
    }


def log(log_file: Path, msg: str) -> None:
    ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    lines = msg.splitlines() or [""]
    with log_file.open("a", encoding="utf-8") as f:
        for line in lines:
            out = f"{ts} {line}\n"
            f.write(out)
            sys.stdout.write(out)
    sys.stdout.flush()


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


def build_log_lut_value(access_cnt: int, fdp_pi_num: int) -> int:
    """floor(log10(access_cnt)) capped to [0, fdp_pi_num-1]."""
    if access_cnt <= 0:
        return 0
    sid = int(math.floor(math.log10(float(access_cnt))))
    if sid < 0:
        return 0
    if sid >= fdp_pi_num:
        return fdp_pi_num - 1
    return sid


def load_bpf_code(
    max_chunks,
    ns_path,
    lba_shift,
    lbas_per_chunk,
    *,
    decay_period_sec,
    fdp_pi_num,
    dump_pi_counts,
):
    """Load autoStream.bpf.c and inject compile-time constants."""
    bpf_text = (Path(__file__).parent / "autoStream.bpf.c").read_text()
    st = os.stat(ns_path)
    replacements = {
        "#define __MAX_CHUNKS__ 0": f"#define __MAX_CHUNKS__ {max_chunks}",
        "#define __TRACK_BD_MAJOR__ 0": f"#define __TRACK_BD_MAJOR__ {os.major(st.st_rdev)}",
        "#define __TRACK_BD_MINOR__ 0": f"#define __TRACK_BD_MINOR__ {os.minor(st.st_rdev)}",
        "#define __LOG_LUT_MAX__ 0": f"#define __LOG_LUT_MAX__ {LOG_LUT_MAX}",
        "#define __DECAY_PERIOD_SEC__ 0": f"#define __DECAY_PERIOD_SEC__ {decay_period_sec}",
        "#define __LBA_SHIFT__ 0": f"#define __LBA_SHIFT__ {lba_shift}",
        "#define __LBAS_PER_CHUNK__ 0": f"#define __LBAS_PER_CHUNK__ {lbas_per_chunk}",
        "#define FDP_PI_NUM 0": f"#define FDP_PI_NUM {fdp_pi_num}",
        "#define __DUMP_PI_COUNTS__ 0": f"#define __DUMP_PI_COUNTS__ {1 if dump_pi_counts else 0}",
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


def dump_pi_counts_window(chunk_array, window_out, run_log_path, window_idx, fdp_pi_num):
    """Dump PI write counts; leave SFR state (access_cnt / last_access_ts) intact."""
    pi_count_headers = ",".join(f"pi{pi}_write_count" for pi in range(fdp_pi_num))
    header = f"cidx,access_cnt,{pi_count_headers}\n"

    try:
        items = chunk_array.items_lookup_batch()
    except Exception as e:
        log(run_log_path, f"Batch lookup failed: {e}")
        items = []

    keys_to_update = []
    values_to_update = []
    sampled = 0

    with open(window_out, "w", encoding="utf-8") as out_f:
        out_f.write(header)
        for k, v in items:
            ac = int(v.access_cnt)
            if ac == 0 and int(v.stream_id) == 0:
                continue
            sampled += 1
            pi_counts_csv = ",".join(
                str(int(v.pi_write_count[pi])) for pi in range(fdp_pi_num)
            )
            out_f.write(f"{k.value},{ac},{pi_counts_csv}\n")
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

    log(
        run_log_path,
        f"[window={window_idx}] Wrote {window_out}, sampled_chunks={sampled}",
    )


def main():
    parser = argparse.ArgumentParser(
        description="AutoStream SFR PI steering. Chunk/decay/PI from autoStream.conf."
    )
    parser.add_argument("--dev", required=True, help="NVMe controller (e.g. /dev/nvme0)")
    parser.add_argument("--ns", type=int, required=True, help="Namespace ID (e.g. 1)")
    parser.add_argument("--outdir", required=True, help="Output directory (e.g. out)")
    args = parser.parse_args()

    cfg = load_autostream_config(CONFIG_PATH)
    window_sec = cfg["window_size"]
    chunk_size = cfg["chunk_size"]
    fdp_pi_num = cfg["fdp_pi_num"]
    dump_pi_counts = cfg["dump_pi_counts"]
    decay_period = cfg["decay_period"]

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
        f"decay_period={decay_period}s  tracking {ns_path} "
        f"{device_size / (1024**3):.2f} GB max_chunks={max_chunks:,}",
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
        decay_period_sec=decay_period,
        fdp_pi_num=fdp_pi_num,
        dump_pi_counts=dump_pi_counts,
    )
    b = BPF(text=bpf_code, cflags=["-Wno-duplicate-decl-specifier"])
    chunk_array = b.get_table("chunk_array")
    log_lut_map = b.get_table("log_lut")

    log_lut_map[ctypes.c_int(0)] = ctypes.c_ubyte(0)
    for i in range(1, LOG_LUT_MAX + 1):
        log_lut_map[ctypes.c_int(i)] = ctypes.c_ubyte(
            build_log_lut_value(i, fdp_pi_num)
        )

    stop_trace = threading.Event()
    threading.Thread(
        target=trace_pipe_reader, args=(stop_trace, run_log_path), daemon=True
    ).start()

    window_idx = 0
    try:
        while True:
            time.sleep(window_sec)
            if dump_pi_counts:
                dump_pi_counts_window(
                    chunk_array,
                    infer_dir / f"pi{window_idx}.csv",
                    run_log_path,
                    window_idx,
                    fdp_pi_num,
                )
            else:
                log(
                    run_log_path,
                    f"[window={window_idx}] heartbeat (interval={window_sec}s, "
                    f"decay={decay_period}s)",
                )
            window_idx += 1
    except KeyboardInterrupt:
        log(run_log_path, "Tracing stopped.")
    finally:
        stop_trace.set()


if __name__ == "__main__":
    main()
