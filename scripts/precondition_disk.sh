#!/usr/bin/env bash
set -euo pipefail

MOUNT_POINT="/mnt/nvme"
FS_TYPE="xfs"
NVME_SECURE_ERASE=1  # nvme format -s: 1 = user data erase

usage() {
  cat <<'EOF'
Usage:
  precondition_disk.sh --mode erase-only --dev /dev/nvme0 --ns 1
  precondition_disk.sh --mode full --dev /dev/nvme0 --ns 1

  --mode   erase-only: unmount + nvme format
           full:       erase + XFS mkfs + mount + fstrim
  --dev    NVMe controller (e.g. /dev/nvme0)
  --ns     Namespace ID (e.g. 1 for /dev/nvme0n1)
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ "${EUID}" -ne 0 ]]; then
  exec sudo -E -- "$0" "$@"
fi

# Whole-disk name (nvme0n1) for a /dev node; empty on failure.
base_disk_of() {
  local node="$1" pk
  pk="$(lsblk -no PKNAME "$node" 2>/dev/null | head -n1 || true)"
  if [[ -n "$pk" ]]; then
    echo "$pk"
    return 0
  fi
  lsblk -no NAME "$node" 2>/dev/null | head -n1 || true
}

# True if target is the OS/root or swap disk.
is_os_disk() {
  local target_dev="$1" target_base root_src root_base swap_src swap_base
  command -v findmnt >/dev/null 2>&1 || return 1

  target_base="$(base_disk_of "$target_dev")"
  [[ -z "$target_base" ]] && return 1

  root_src="$(findmnt -n -o SOURCE / 2>/dev/null || true)"
  if [[ "$root_src" == /dev/* ]]; then
    root_base="$(base_disk_of "$root_src")"
    [[ -n "$root_base" && "$root_base" == "$target_base" ]] && return 0
  fi

  if command -v swapon >/dev/null 2>&1; then
    while IFS= read -r swap_src; do
      [[ "$swap_src" == /dev/* ]] || continue
      swap_base="$(base_disk_of "$swap_src")"
      [[ -n "$swap_base" && "$swap_base" == "$target_base" ]] && return 0
    done < <(swapon --noheadings --raw --output=NAME 2>/dev/null || true)
  fi
  return 1
}

dev="" ns="" mode=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) mode="${2:-}"; shift 2 ;;
    --dev)  dev="${2:-}";  shift 2 ;;
    --ns)   ns="${2:-}";   shift 2 ;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$mode" || -z "$dev" || -z "$ns" ]]; then
  echo "Required: --mode (erase-only|full) --dev /dev/nvmeN --ns NSID" >&2
  usage
  exit 2
fi
case "$mode" in
  erase-only|full) ;;
  *)
    echo "Invalid --mode: $mode (expected erase-only or full)." >&2
    usage
    exit 2
    ;;
esac
if [[ ! "$dev" =~ ^/dev/nvme[0-9]+$ ]]; then
  echo "Expected NVMe controller path like /dev/nvme0, got: $dev" >&2
  exit 2
fi
if ! [[ "$ns" =~ ^[0-9]+$ ]]; then
  echo "NS must be an integer, got: $ns" >&2
  exit 2
fi

device="${dev}n${ns}"
if [[ ! -b "$device" ]]; then
  echo "Not a block device: $device" >&2
  exit 2
fi
if is_os_disk "$device"; then
  echo "Refusing to operate: $device appears to be the OS/root disk." >&2
  exit 3
fi
command -v nvme >/dev/null 2>&1 || {
  echo "nvme not found. Install nvme-cli and retry." >&2
  exit 2
}
if [[ "$mode" == "full" ]] && ! command -v mkfs.xfs >/dev/null 2>&1; then
  echo "mkfs.xfs not found. Install xfsprogs and retry." >&2
  exit 2
fi

echo "[+] Target device: $device  mode: $mode"

mounted_at="$(lsblk -no MOUNTPOINT "$device" | head -n1 || true)"
if [[ -n "$mounted_at" ]]; then
  echo "[+] Unmounting $device from $mounted_at"
  umount "$device" || umount "$mounted_at"
fi

echo "[+] Erasing namespace: nvme format -n $ns -s $NVME_SECURE_ERASE -f $dev"
nvme format "$dev" -n "$ns" -s "$NVME_SECURE_ERASE" -f

if [[ "$mode" == "erase-only" ]]; then
  sync
  echo 3 > /proc/sys/vm/drop_caches || true
  echo "[+] Done (erase-only). $device is erased; no filesystem created or mounted."
  exit 0
fi

echo "[+] Formatting $device as $FS_TYPE"
mkfs.xfs -f "$device" >/dev/null

echo "[+] Mounting to $MOUNT_POINT"
mkdir -p "$MOUNT_POINT"
mount -t "$FS_TYPE" -o nodiscard "$device" "$MOUNT_POINT"

sync
echo 3 > /proc/sys/vm/drop_caches || true
echo "[+] Done. Mounted at $MOUNT_POINT"

fstrim -v "$MOUNT_POINT"
echo "[+] fstrim done"
