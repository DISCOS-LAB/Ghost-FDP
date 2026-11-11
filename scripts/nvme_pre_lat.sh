#!/usr/bin/env bash
# NVMe driver prep latency — CSV rows until interrupted.
#
# Times nvme_prep_rq only (setup_cmd + DMA map, wraps SA-FDP fentry).
# Not attached to nvme_setup_cmd, so it does not share that trampoline
# with inference / AutoStream.
#
#   cd test_no_fdp
#   sudo ../../scripts/nvme_setup_lat.sh nvme_lat.csv
#
#   sudo ../../scripts/nvme_setup_lat.sh nvme_lat.csv -i 10

set -euo pipefail

INTERVAL=10
OUTPUT=""

usage() {
  cat <<'EOF'
Usage: sudo nvme_setup_lat.sh OUTPUT.csv [-i SECONDS]

  OUTPUT.csv   CSV path (e.g. nvme_lat.csv)
  -i SECONDS   Sample interval (default 10)

Columns: timestamp, prep_n, prep_avg_ns
  prep = nvme_prep_rq
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -i|--interval)
      [[ $# -ge 2 ]] || usage
      INTERVAL="$2"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    -*)
      echo "unknown option: $1" >&2
      usage
      ;;
    *)
      if [[ -z "$OUTPUT" ]]; then
        OUTPUT="$1"
        shift
      else
        usage
      fi
      ;;
  esac
done

[[ -n "$OUTPUT" ]] || usage
[[ "$INTERVAL" =~ ^[1-9][0-9]*$ ]] || {
  echo "interval must be a positive integer" >&2
  exit 1
}

if [[ "$(id -u)" -ne 0 ]]; then
  echo "need root for bpftrace fentry (sudo $0 ...)" >&2
  exit 1
fi

if ! command -v bpftrace >/dev/null 2>&1; then
  echo "bpftrace not found" >&2
  exit 1
fi

mkdir -p "$(dirname -- "$OUTPUT")"
if [[ ! -s "$OUTPUT" ]]; then
  echo "timestamp,prep_n,prep_avg_ns" >"$OUTPUT"
fi

BPF=$(cat <<EOF
fentry:nvme:nvme_prep_rq { @tp[tid] = nsecs; }
fexit:nvme:nvme_prep_rq /@tp[tid]/ {
  @ps += nsecs - @tp[tid];
  @pn += 1;
  delete(@tp[tid]);
}

interval:s:${INTERVAL} {
  \$pn = @pn;
  \$pa = \$pn > 0 ? @ps / \$pn : 0;
  printf("%llu,%llu\\n", \$pn, \$pa);
  clear(@pn);
  clear(@ps);
}

END {
  clear(@tp);
  clear(@pn);
  clear(@ps);
}
EOF
)

echo "Starting NVMe prep latency logger..."
echo "Output: $OUTPUT"
echo "Interval: ${INTERVAL}s"
echo "Probe: fentry/fexit nvme:nvme_prep_rq"
echo "Duration: until interrupted (Ctrl+C)"
echo "--------------------------------------------------"

stdbuf -oL bpftrace -q -e "$BPF" | while IFS= read -r line; do
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  row="${ts},${line}"
  echo "$row" >>"$OUTPUT"
  IFS=',' read -r pn pa <<<"$line"
  echo "[$ts] prep n=${pn} avg=${pa} ns"
done
