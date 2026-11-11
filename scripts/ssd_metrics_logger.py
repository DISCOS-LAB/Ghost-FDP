#!/usr/bin/env python3
"""SSD Metrics Logger — periodically logs WAF/Power/Temp/Util to CSV until interrupted."""

from __future__ import annotations

import argparse
import csv
import datetime
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

REPORT_INTERVAL = 600.0  # seconds between CSV rows / WAF samples
METRIC_SAMPLE_INTERVAL = 10.0  # Power/Temp/Util sample period

# NVMe vendor power log
LOG_ID = "0x00"
LOG_LEN = 256
OFF_CUR = 0x00


class SSDMetricsLogger:
    def __init__(self, device: str, output_file: str):
        self.device = device
        self.output_file = Path(output_file)
        self.running = True

        # WAF baselines
        self.host0 = None
        self.phys0 = None
        self.host_prev = None
        self.phys_prev = None
        self.last_waf_time = None
        self.smart_log_result = None

        self.power_samples: list[float] = []
        self.temp_samples: list[float] = []
        self.util_samples: list[float] = []

        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        signal.signal(signal.SIGINT, self._stop)
        signal.signal(signal.SIGTERM, self._stop)

    def _stop(self, signum, frame):
        print("\nReceived interrupt signal. Stopping logger...")
        self.running = False

    def _get_host_bytes_written(self):
        """Host bytes written from cached smart-log (DUW * 512000)."""
        try:
            if self.smart_log_result is None:
                return None
            lines = [
                line
                for line in self.smart_log_result.split("\n")
                if "Data Units Written" in line
            ]
            if not lines:
                return None
            duw = int(lines[0].split(":")[1].strip().split()[0])
            # NVMe spec: Data Units Written are in units of 1000 * 512B
            return duw * 512_000
        except (ValueError, IndexError) as e:
            print(f"Warning: Failed to parse host bytes written: {e}", file=sys.stderr)
            return None

    def _get_physical_bytes_written(self):
        """
        Physical media bytes written from OCP smart-add-log.
        Parsed as a 128-bit value: (hi << 64) + lo.
        """
        try:
            result = subprocess.run(
                ["sudo", "nvme", "ocp", "smart-add-log", self.device],
                capture_output=True,
                text=True,
                check=True,
            )
            lines = [
                line
                for line in result.stdout.split("\n")
                if "Physical media units written" in line
            ]
            if not lines:
                return None
            parts = lines[0].split()
            # 128-bit value as hi << 64 + lo
            return (int(parts[-2]) << 64) + int(parts[-1])
        except (subprocess.CalledProcessError, ValueError, IndexError) as e:
            print(f"Warning: Failed to get physical bytes written: {e}", file=sys.stderr)
            return None

    def _calculate_waf(self, current_time: float):
        """
        Calculate interval and total WAF when REPORT_INTERVAL has elapsed.
        waf_i = (phys1 - phys_prev) / (host1 - host_prev)
        waf_t = (phys1 - phys0) / (host1 - host0)
        Returns (waf_i, waf_t), or ('N/A', 'N/A') on the first sample / errors.
        """
        if self.host0 is None or self.phys0 is None:
            host_bytes = self._get_host_bytes_written()
            phys_bytes = self._get_physical_bytes_written()
            self.host0 = host_bytes
            self.phys0 = phys_bytes
            self.host_prev = host_bytes
            self.phys_prev = phys_bytes
            self.last_waf_time = current_time
            return "N/A", "N/A"

        if current_time - self.last_waf_time < REPORT_INTERVAL:
            return "N/A", "N/A"

        host1 = self._get_host_bytes_written()
        phys1 = self._get_physical_bytes_written()
        self.last_waf_time = current_time
        if host1 is None or phys1 is None:
            return "N/A", "N/A"

        waf_t = "N/A"
        waf_i = "N/A"
        host_diff_total = host1 - self.host0
        phys_diff_total = phys1 - self.phys0
        host_diff_interval = host1 - self.host_prev
        phys_diff_interval = phys1 - self.phys_prev

        if host_diff_total != 0 and phys_diff_total != 0:
            waf_t = round(phys_diff_total / host_diff_total, 2)
        if host_diff_interval != 0 and phys_diff_interval != 0:
            waf_i = round(phys_diff_interval / host_diff_interval, 2)

        self.host_prev = host1
        self.phys_prev = phys1
        return waf_i, waf_t

    @staticmethod
    def _u16_le(buf: bytes, off: int) -> int:
        """Read a 16-bit little-endian unsigned integer from buffer."""
        if off + 2 > len(buf):
            raise ValueError(f"buffer too short: need {off + 2} bytes, got {len(buf)}")
        return int.from_bytes(buf[off : off + 2], byteorder="little", signed=False)

    def _get_power(self):
        try:
            raw = subprocess.check_output(
                [
                    "nvme",
                    "get-log",
                    self.device,
                    f"--log-id={LOG_ID}",
                    f"--log-len={LOG_LEN}",
                    "--raw-binary",
                ],
                stderr=subprocess.DEVNULL,
            )
            return self._u16_le(raw, OFF_CUR) / 100.0
        except (subprocess.CalledProcessError, ValueError, IndexError) as e:
            print(f"Warning: Failed to get power: {e}", file=sys.stderr)
            return None

    def _get_temp(self):
        """Temperature in Kelvin; also caches smart-log for WAF."""
        try:
            result = subprocess.run(
                ["nvme", "smart-log", self.device],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=True,
            )
            self.smart_log_result = result.stdout
            for line in result.stdout.split("\n"):
                if "temperature" not in line.lower():
                    continue
                match = re.search(r"\((\d+)\s*K\)", line)
                if match:
                    return float(match.group(1))
            return None
        except (subprocess.CalledProcessError, ValueError, AttributeError) as e:
            print(f"Warning: Failed to get temperature: {e}", file=sys.stderr)
            self.smart_log_result = None
            return None

    def _get_util(self):
        """
        Get current utilization from iostat.
        Uses the second device data line; last field is %util.
        """
        try:
            device_name = self.device.rsplit("/", 1)[-1]
            result = subprocess.run(
                ["iostat", "-x", "-d", "-y", "1", "2", device_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=True,
            )
            data_lines = [
                line
                for line in result.stdout.strip().split("\n")
                if line.strip()
                and not line.startswith("Linux")
                and not line.startswith("Device")
                and line.startswith(device_name)
            ]
            if len(data_lines) >= 2:
                return float(data_lines[1].split()[-1])
            return None
        except (subprocess.CalledProcessError, ValueError, IndexError) as e:
            print(f"Warning: Failed to get utilization: {e}", file=sys.stderr)
            return None

    def _collect_metrics_sample(self):
        power = self._get_power()
        temp = self._get_temp()
        util = self._get_util()
        return (
            power if power is not None else "N/A",
            temp if temp is not None else "N/A",
            util if util is not None else "N/A",
        )

    def _append_metrics_row(self, timestamp, waf_i, waf_t, power, temp, util):
        file_exists = self.output_file.exists()
        with open(self.output_file, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Timestamp", "waf_i", "waf_t", "Power", "Temp", "Util"])
            writer.writerow([timestamp, waf_i, waf_t, power, temp, util])

    @staticmethod
    def _average_or_na(samples):
        if not samples:
            return "N/A"
        return round(sum(samples) / len(samples), 2)

    def run(self):
        print("Starting SSD metrics logger...")
        print(f"Device: {self.device}")
        print(f"Output: {self.output_file}")
        print(f"Report interval: {REPORT_INTERVAL}s")
        print(f"Metric sample interval: {METRIC_SAMPLE_INTERVAL}s")
        print("Duration: until interrupted (Ctrl+C)")
        print("-" * 50)

        last_report_time = time.time()
        while self.running:
            current_time = time.time()
            power, temp, util = self._collect_metrics_sample()
            if power != "N/A":
                self.power_samples.append(power)
            if temp != "N/A":
                self.temp_samples.append(temp)
            if util != "N/A":
                self.util_samples.append(util)

            if current_time - last_report_time >= REPORT_INTERVAL:
                waf_i, waf_t = self._calculate_waf(current_time)
                avg_power = self._average_or_na(self.power_samples)
                avg_temp = self._average_or_na(self.temp_samples)
                avg_util = self._average_or_na(self.util_samples)
                now = datetime.datetime.now()
                self._append_metrics_row(
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    waf_i,
                    waf_t,
                    avg_power,
                    avg_temp,
                    avg_util,
                )
                print(
                    f"[{now.strftime('%H:%M:%S')}] "
                    f"avg -> Power:{avg_power}, Temp:{avg_temp}, Util:{avg_util}, "
                    f"WAF_i:{waf_i}, WAF_t:{waf_t}"
                )
                self.power_samples.clear()
                self.temp_samples.clear()
                self.util_samples.clear()
                last_report_time = current_time

            time.sleep(METRIC_SAMPLE_INTERVAL)

        print("Logger stopped.")


def main():
    parser = argparse.ArgumentParser(
        description="SSD Metrics Logger — log WAF/Power/Temp/Util to CSV until interrupted"
    )
    parser.add_argument(
        "device",
        help="SSD device path (e.g. /dev/nvme0n1)",
    )
    parser.add_argument(
        "output",
        help="Output CSV file path",
    )
    args = parser.parse_args()

    logger = SSDMetricsLogger(device=args.device, output_file=args.output)
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
