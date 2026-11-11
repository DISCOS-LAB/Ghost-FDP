# Ghost-FDP
This repository contains the implementation of **Ghost-FDP**, which accompanies the submitted paper *“GHOST-FDP: Survival Analysis-Driven I/O Steering for Relational Databases on FDP SSDs”* submitted to the **ICDE'27 2st round**.

---

## FDP Commands

Common `nvme-cli` commands for **NVMe FDP configuration/status** and **namespace management**.

### Common sequence (original order)

```bash
# Delete namespace
sudo nvme delete-ns /dev/nvme0 -n 1

# Enable FDP (feature id: 0x1D)
sudo nvme set-feature /dev/nvme0 -f 0x1D -c 1 -s

# Read back FDP feature
sudo nvme get-feature /dev/nvme0 -f 0x1D -H

# Create a 4K namespace with FDP placement IDs
sudo nvme create-ns /dev/nvme0 \
  --nsze=3672113152 --ncap=3672113152 --block-size=4096 \
  -p 0,1,2,3,4,5,6 -n 7

# Attach namespace to controllers (example controllers mask 0x7)
sudo nvme attach-ns /dev/nvme0 --namespace-id=1 --controllers=0x7

# FDP status (run against the namespace block device)
sudo nvme fdp status /dev/nvme0n1
```

---

## Usage

### 1. BPF helper (kernel)

Steering writes `bio->bi_write_stream` from eBPF. That needs a **minimal kernel patch** (~30 LoC, helper ID 212) on Linux 6.17.12:

```bash
cd /path/to/linux
patch -p1 < /path/to/Ghost-FDP/bpf_probe_write_bio_patch/0003-bpf-add-probe_write_kernel-helper.patch
```

Then rebuild and boot that kernel.

### 2. Training

Edit `training/training.conf` (`sv_window_size`, `sv_tau_sec`, `chunk_size`). Collect traces, then generate I/O with fio:

```bash
cd training
sudo python3 training.py --dev /dev/nvme0 --ns 1 --outdir out
# another terminal
sudo fio fio/test_zipf_1.5.fio
```

Fit Cox PH. The summary prints **two coefficients** (`upd_freq_log1p`, `last_int_s_log1p`):

```bash
cd training/cox-ph
python3 train_cox_model.py \
  --data-dir ../out/train \
  --train-range 0 5 \
  --model-out cox_model.pkl
```

### 3. Steering

Put those two coefficients into `steering/inference.conf` (`coef_upd_freq`, `coef_last_int_s_log1p`), then run:

```bash
cd steering
sudo python3 inference.py --dev /dev/nvme0 --ns 1 --outdir out
```

Keep it attached while the workload (MySQL / PostgreSQL / fio) runs on `/mnt/nvme`.

### 4. WAF monitoring

`scripts/ssd_metrics_logger.py` samples SMART every 10 minutes and writes `waf_i` (interval) / `waf_t` (lifetime), plus power, temperature, and utilization:

```bash
cd scripts
sudo python3 ssd_metrics_logger.py /dev/nvme0n1 ssd_metrics.csv
```

Stop with Ctrl+C. Run it alongside the workload (and steering, if any).
