#!/usr/bin/env python3
"""Host CPU/memory logger — CSV rows until interrupted.

Run from the test directory so cpu_mem.csv lands next to that run:

  cd test_no_fdp
  python3 ../../scripts/mon_cpu_mem.py cpu_mem.csv

  cd test_safdp
  python3 ../../scripts/mon_cpu_mem.py cpu_mem.csv

CPU columns match sar -u (host-wide 0–100, not per-core sum).
mem_used_kb matches sar -r kbmemused on this kernel:
  MemTotal - MemFree - Buffers - Cached - Slab
(excludes page cache; includes vmalloc BPF maps). mem_avail_kb is MemAvailable.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import signal
import sys
import time
from pathlib import Path

DEFAULT_INTERVAL = 10.0

CPU_FIELDS = (
    "cpu_user",
    "cpu_nice",
    "cpu_sys",
    "cpu_iowait",
    "cpu_steal",
    "cpu_idle",
    "cpu_used",
)

MEM_FIELDS = (
    "mem_total_kb",
    "mem_used_kb",
    "mem_used_pct",
    "mem_avail_kb",
    "mem_cached_kb",
    "mem_slab_kb",
    "mem_vmalloc_kb",
)

HEADER = ("timestamp",) + CPU_FIELDS + MEM_FIELDS


def _read_proc_stat() -> tuple[int, ...]:
    """Return (user, nice, system, idle, iowait, irq, softirq, steal)."""
    with open("/proc/stat", encoding="utf-8") as f:
        parts = f.readline().split()
    if parts[0] != "cpu":
        raise RuntimeError("unexpected /proc/stat: first line is not aggregate cpu")
    vals = [int(x) for x in parts[1:9]]
    while len(vals) < 8:
        vals.append(0)
    return tuple(vals)


def _cpu_pct(prev: tuple[int, ...], cur: tuple[int, ...]) -> dict[str, float]:
    d = [c - p for c, p in zip(cur, prev)]
    total = sum(d)
    if total <= 0:
        z = {k: 0.0 for k in CPU_FIELDS}
        z["cpu_idle"] = 100.0
        return z

    def pct(n: int) -> float:
        return round(100.0 * n / total, 2)

    user, nice, system, idle, iowait, irq, softirq, steal = d
    idle_pct = pct(idle)
    return {
        "cpu_user": pct(user),
        "cpu_nice": pct(nice),
        "cpu_sys": pct(system + irq + softirq),
        "cpu_iowait": pct(iowait),
        "cpu_steal": pct(steal),
        "cpu_idle": idle_pct,
        "cpu_used": round(100.0 - idle_pct, 2),
    }


def _read_meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                out[parts[0].rstrip(":")] = int(parts[1])
    return out


def _mem_row(info: dict[str, int]) -> dict[str, float]:
    total = info["MemTotal"]
    free = info.get("MemFree", 0)
    buffers = info.get("Buffers", 0)
    cached = info.get("Cached", 0)
    slab = info.get("Slab", 0)
    avail = info.get("MemAvailable", free)
    vmalloc = info.get("VmallocUsed", 0)
    used = total - free - buffers - cached - slab
    if used < 0:
        used = 0
    used_pct = round(100.0 * used / total, 2) if total else 0.0
    return {
        "mem_total_kb": total,
        "mem_used_kb": used,
        "mem_used_pct": used_pct,
        "mem_avail_kb": avail,
        "mem_cached_kb": cached,
        "mem_slab_kb": slab,
        "mem_vmalloc_kb": vmalloc,
    }


class CpuMemLogger:
    def __init__(self, output_file: str, interval: float):
        self.output_file = Path(output_file)
        self.interval = interval
        self.running = True
        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        signal.signal(signal.SIGINT, self._stop)
        signal.signal(signal.SIGTERM, self._stop)

    def _stop(self, signum, frame):
        print("\nReceived interrupt signal. Stopping logger...")
        self.running = False

    def _ensure_header(self) -> None:
        if self.output_file.exists() and self.output_file.stat().st_size > 0:
            return
        with open(self.output_file, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(HEADER)

    def _append(self, row: dict) -> None:
        with open(self.output_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([row[k] for k in HEADER])
            f.flush()

    def run(self) -> None:
        print("Starting CPU/memory logger...")
        print(f"Output: {self.output_file}")
        print(f"Interval: {self.interval}s")
        print("Duration: until interrupted (Ctrl+C)")
        print("-" * 50)

        self._ensure_header()
        prev = _read_proc_stat()
        next_tick = time.monotonic() + self.interval

        while self.running:
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                time.sleep(min(sleep_for, 0.5))
                continue
            if not self.running:
                break

            cur = _read_proc_stat()
            cpu = _cpu_pct(prev, cur)
            mem = _mem_row(_read_meminfo())
            prev = cur
            now = datetime.datetime.now()
            row = {"timestamp": now.strftime("%Y-%m-%d %H:%M:%S"), **cpu, **mem}
            self._append(row)
            print(
                f"[{row['timestamp']}] "
                f"cpu user:{cpu['cpu_user']} sys:{cpu['cpu_sys']} "
                f"iowait:{cpu['cpu_iowait']} used:{cpu['cpu_used']} | "
                f"mem used:{mem['mem_used_kb']} kb ({mem['mem_used_pct']}%) "
                f"avail:{mem['mem_avail_kb']} kb"
            )
            next_tick += self.interval
            # If we fell behind (sleep/pause), skip catch-up bursts.
            if time.monotonic() - next_tick > self.interval:
                next_tick = time.monotonic() + self.interval

        print("Logger stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Log host CPU and memory usage to CSV until interrupted"
    )
    parser.add_argument(
        "output",
        help="Output CSV path (e.g. cpu_mem.csv)",
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"Seconds between samples (default {DEFAULT_INTERVAL:g})",
    )
    args = parser.parse_args()
    if args.interval <= 0:
        raise SystemExit("interval must be > 0")

    logger = CpuMemLogger(output_file=args.output, interval=args.interval)
    try:
        logger.run()
    except KeyboardInterrupt:
        print("\nKeyboard interrupt received.")
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

