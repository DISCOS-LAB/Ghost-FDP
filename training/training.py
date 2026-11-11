#!/usr/bin/env python3
"""FDP training data collection via eBPF fentry on nvme_setup_cmd.

  sudo python3 training.py --dev /dev/nvme0 --ns 1 --outdir out

Window / τ / chunk size: training.conf (next to this script).
Outputs: out/training.log, out/train/windowN.csv
"""

from __future__ import annotations

import argparse
import configparser
import ctypes
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from bcc import BPF

CONFIG_PATH = Path(__file__).resolve().parent / "training.conf"


def load_training_config(path: Path) -> dict[str, int]:
    """Read sv_window_size, sv_tau_sec, chunk_size from training.conf."""
    if not path.is_file():
        raise SystemExit(f"config not found: {path}")
    cp = configparser.ConfigParser()
    read = cp.read(path)
    if not read:
        raise SystemExit(f"failed to read config: {path}")
    if not cp.has_section("training"):
        raise SystemExit(f"{path} must have a [training] section")
    try:
        window_sec = cp.getint("training", "sv_window_size")
        tau_sec = cp.getint("training", "sv_tau_sec")
        chunk_size = cp.getint("training", "chunk_size")
    except (configparser.NoOptionError, ValueError) as e:
        raise SystemExit(f"{path}: {e}") from e
    if window_sec <= 0 or tau_sec <= 0 or chunk_size <= 0:
        raise SystemExit(
            f"{path}: sv_window_size, sv_tau_sec, chunk_size must be > 0"
        )
    tau_ms = tau_sec * 1000
    window_ms = window_sec * 1000
    if tau_ms >= window_ms:
        raise SystemExit(
            f"{path}: sv_tau_sec={tau_sec} must be < sv_window_size={window_sec}"
        )
    return {
        "sv_window_size": window_sec,
        "sv_tau_sec": tau_sec,
        "chunk_size": chunk_size,
        "sv_tau_ms": tau_ms,
        "window_ms": window_ms,
    }


def log(log_file: Path, msg: str) -> None:
    """Append timestamped line(s) to log file and stdout."""
    ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    lines = msg.splitlines() or [""]
    with log_file.open("a", encoding="utf-8") as f:
        for line in lines:
            out = f"{ts} {line}\n"
            f.write(out)
            sys.stdout.write(out)
    sys.stdout.flush()


class GlobalConfig(ctypes.Structure):
    """Must match struct global_config in training.bpf.c."""

    _fields_ = [
        ("window_start_ms", ctypes.c_uint64),
        ("window_end_ms", ctypes.c_uint64),
        ("window_id", ctypes.c_uint32),
    ]


def namespace_block_path(controller_path, nsid):
    """Build /dev/nvme0n1 from /dev/nvme0 and NSID."""
    m = re.match(r"^/dev/(nvme\d+)$", controller_path)
    if not m:
        raise ValueError(
            f"device must be NVMe controller path (e.g. /dev/nvme0), got: {controller_path!r}"
        )
    return f"/dev/{m.group(1)}n{int(nsid)}"


def get_namespace_size_bytes(controller_path, nsid):
    """Namespace size in bytes via blockdev on /dev/nvme{c}n{nsid}."""
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
        raise RuntimeError(
            f"Failed to get namespace size for {ns_path} "
            f"(controller={controller_path}, ns={nsid}): {e}"
        ) from e


def get_logical_block_size_bytes(ns_path):
    """Logical sector size in bytes (512 or 4096 typical) via blockdev --getss."""
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
    """Match kernel: slba from sector, chunk index from chunk-aligned logical extents."""
    if logical_b == 512:
        return 9, chunk_size // 512
    if logical_b == 4096:
        return 12, chunk_size // 4096
    raise ValueError(
        f"Unsupported logical block size {logical_b} (expected 512 or 4096)"
    )


def load_bpf_code(max_chunks, ns_path, lba_shift, lbas_per_chunk, sv_tau_ms):
    """Load training.bpf.c and inject compile-time constants."""
    bpf_text = (Path(__file__).parent / "training.bpf.c").read_text()
    st = os.stat(ns_path)
    bpf_text = bpf_text.replace("#define __MAX_CHUNKS__ 0", f"#define __MAX_CHUNKS__ {max_chunks}")
    bpf_text = bpf_text.replace(
        "#define __TRACK_BD_MAJOR__ 0", f"#define __TRACK_BD_MAJOR__ {os.major(st.st_rdev)}"
    )
    bpf_text = bpf_text.replace(
        "#define __TRACK_BD_MINOR__ 0", f"#define __TRACK_BD_MINOR__ {os.minor(st.st_rdev)}"
    )
    bpf_text = bpf_text.replace("#define __LBA_SHIFT__ 0", f"#define __LBA_SHIFT__ {lba_shift}")
    bpf_text = bpf_text.replace(
        "#define __LBAS_PER_CHUNK__ 0", f"#define __LBAS_PER_CHUNK__ {lbas_per_chunk}"
    )
    bpf_text = bpf_text.replace(
        "#define __SV_TAU_MS__ 0", f"#define __SV_TAU_MS__ {sv_tau_ms}"
    )
    return bpf_text


def export_survival_row(v, interval_ms, window_start_ms, sv_tau_ms):
    """CSV fields at landmark τ, or None to skip."""
    ev = int(v.sv_event_indicator)
    if ev:
        last_int_ms = int(v.last_int_ms)
        sv_t2e_s = int(v.sv_time2event_ms) / 1000.0
    else:
        last_write_ms = int(v.last_write_ms)
        if last_write_ms == 0:
            return None
        delta_ms = int(window_start_ms) + sv_tau_ms - last_write_ms
        if delta_ms <= 0:
            return None
        last_int_ms = delta_ms
        sv_t2e_s = (interval_ms - sv_tau_ms) / 1000.0

    if sv_t2e_s <= 0.0:
        return None
    upd_cnt = int(v.update_count)
    if upd_cnt == 0:
        return None
    return sv_t2e_s, ev, upd_cnt, last_int_ms


def main():
    parser = argparse.ArgumentParser(
        description="FDP training data collection. Window/τ/chunk from training.conf."
    )
    parser.add_argument("--dev", required=True, help="NVMe controller (e.g. /dev/nvme0)")
    parser.add_argument("--ns", type=int, required=True, help="Namespace ID (e.g. 1)")
    parser.add_argument("--outdir", required=True, help="Output directory (e.g. out)")
    args = parser.parse_args()

    cfg = load_training_config(CONFIG_PATH)
    window_sec = cfg["sv_window_size"]
    tau_sec = cfg["sv_tau_sec"]
    chunk_size = cfg["chunk_size"]
    sv_tau_ms = cfg["sv_tau_ms"]
    interval_ms = cfg["window_ms"]

    outdir = Path(args.outdir)
    train_dir = outdir / "train"
    train_dir.mkdir(parents=True, exist_ok=True)
    run_log_path = outdir / "training.log"

    ns_path = namespace_block_path(args.dev, args.ns)
    device_size = get_namespace_size_bytes(args.dev, args.ns)
    logical_b = get_logical_block_size_bytes(ns_path)
    lba_shift, lbas_per_chunk = lba_shift_and_lbas_per_chunk(logical_b, chunk_size)
    max_chunks = device_size // chunk_size

    log(
        run_log_path,
        f"Config {CONFIG_PATH}: window={window_sec}s τ={tau_sec}s "
        f"chunk={chunk_size}  tracking {ns_path}  "
        f"{device_size / (1024**3):.2f} GB  max_chunks={max_chunks:,}",
    )

    if not BPF.support_kfunc():
        raise SystemExit(
            "fentry/kfunc not supported on this kernel (need BTF + bpf trampoline)."
        )
    if not Path("/sys/module/nvme_core").exists():
        raise SystemExit("nvme_core is not loaded.")

    bpf_code = load_bpf_code(max_chunks, ns_path, lba_shift, lbas_per_chunk, sv_tau_ms)
    b = BPF(text=bpf_code, cflags=["-Wno-duplicate-decl-specifier"])
    chunk_array = b.get_table("chunk_array")
    g_cfg_map = b.get_table("g_cfg_map")
    wkey = ctypes.c_int(0)

    # Zero-init last_accessed_lba==0 looks sequential for slba=0.
    key0 = ctypes.c_int(0)
    info0 = chunk_array[key0]
    info0.last_accessed_lba = ctypes.c_uint64(-1).value
    chunk_array[key0] = info0

    header = "cidx,sv_t2e_s,sv_ev_ind,upd_freq,last_int_ms\n"
    window_idx = 0

    try:
        while True:
            window_start_ms = time.monotonic_ns() // 1_000_000
            window_end_ms = window_start_ms + interval_ms
            g_cfg_map[wkey] = GlobalConfig(window_start_ms, window_end_ms, window_idx)
            time.sleep(window_sec)

            window_out = train_dir / f"window{window_idx}.csv"
            window_rows = 0

            try:
                items = chunk_array.items_lookup_batch()
            except Exception as e:
                log(run_log_path, f"Batch lookup failed: {e}")
                items = []

            with open(window_out, "w") as out_f:
                out_f.write(header)
                for k, v in items:
                    if int(v.window_id) != window_idx:
                        continue
                    row = export_survival_row(v, interval_ms, window_start_ms, sv_tau_ms)
                    if row is None:
                        continue
                    sv_t2e_s, ev, upd_freq, last_int_ms = row
                    out_f.write(
                        f"{k.value},{sv_t2e_s:.3f},{ev},{upd_freq},{last_int_ms}\n"
                    )
                    window_rows += 1

            log(
                run_log_path,
                f"[window={window_idx}] Wrote {window_out}, rows={window_rows}",
            )
            window_idx += 1

    except KeyboardInterrupt:
        log(run_log_path, "Tracing stopped.")


if __name__ == "__main__":
    main()
