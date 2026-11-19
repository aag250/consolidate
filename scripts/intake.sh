#!/usr/bin/env bash
# Host-side intake helper. Needs root and the raw device, which the tool container
# does not have. Collects lsblk/blkid/smartctl JSON, mounts the source read-only,
# then runs the container's `intake` command.
#
#   intake.sh <device> <label> [--partition /dev/sdb1] [--smart-json FILE]
#
# Environment: DRIVECON_SRV (default /srv) holds sources/<label> mount points and
# intake/<label>.json; DRIVECON_CMD is the container command prefix.
set -euo pipefail

usage() { echo "usage: $0 <device> <label> [--partition DEV] [--smart-json FILE]" >&2; exit 2; }
[ $# -ge 2 ] || usage
device=$1 label=$2
shift 2
partition="" smart_file=""
while [ $# -gt 0 ]; do
  case $1 in
    --partition) partition=${2:?}; shift 2 ;;
    --smart-json) smart_file=${2:?}; shift 2 ;;
    *) usage ;;
  esac
done
case $label in *[!A-Za-z0-9._-]*|"") echo "label may only use letters, digits, . _ -" >&2; exit 2 ;; esac

if [ "$(id -u)" -ne 0 ] && [ -z "${DRIVECON_ALLOW_NONROOT:-}" ]; then
  echo "intake.sh needs root (device access, mounting, SMART)" >&2; exit 1
fi

SRV=${DRIVECON_SRV:-/srv}
DRIVECON_CMD=${DRIVECON_CMD:-docker compose run --rm drivecon}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$SRV/intake" "$SRV/sources/$label"

lsblk --json -b -o NAME,MODEL,SERIAL,SIZE,TRAN,FSTYPE,UUID,LABEL "$device" > "$work/lsblk.json"

if [ -z "$partition" ]; then
  partition=$(python3 - "$work/lsblk.json" "$device" <<'PY'
import json, sys
disk = json.load(open(sys.argv[1]))["blockdevices"][0]
if disk.get("fstype"):
    print(sys.argv[2])
else:
    for c in disk.get("children", []):
        if c.get("fstype"):
            print("/dev/" + c["name"]); break
PY
)
fi
[ -n "$partition" ] || { echo "no partition with a filesystem found on $device" >&2; exit 1; }

blkid -o export "$partition" > "$work/blkid.env" || true

if [ -n "$smart_file" ]; then
  cp "$smart_file" "$work/smart.json"
else
  smartctl --json -a "$device" > "$work/smart.json" 2>/dev/null || true
  if ! grep -q '"smart_status"' "$work/smart.json"; then
    smartctl --json -a -d sat "$device" > "$work/smart2.json" 2>/dev/null || true
    if grep -q '"smart_status"' "$work/smart2.json"; then mv "$work/smart2.json" "$work/smart.json"; fi
  fi
fi

mnt="$SRV/sources/$label"
if ! findmnt -n "$mnt" >/dev/null 2>&1; then
  opts=ro
  case $(sed -n 's/^TYPE=//p' "$work/blkid.env") in ext2|ext3|ext4) opts=ro,noload ;; esac
  mount -o "$opts" "$partition" "$mnt"
fi
if ! findmnt -no OPTIONS "$mnt" | tr ',' '\n' | grep -qx ro; then
  echo "refusing to continue: $mnt is not mounted read-only" >&2; exit 1
fi

used=$(df -B1 --output=used "$mnt" | tail -n 1 | tr -d ' ')

python3 - "$work" "$device" "$partition" "$used" "$SRV/intake/$label.json" <<'PY'
import json, os, sys
work, device, partition, used, out = sys.argv[1:6]
blk = dict(l.split("=", 1) for l in open(f"{work}/blkid.env").read().splitlines() if "=" in l)
try:
    smart = json.load(open(f"{work}/smart.json"))
except (ValueError, OSError):
    smart = None
doc = {"device": device, "partition": partition, "used_bytes": int(used),
       "lsblk": json.load(open(f"{work}/lsblk.json")), "blkid": blk, "smart": smart}
json.dump(doc, open(out, "w"), indent=1)
PY

# shellcheck disable=SC2086
exec $DRIVECON_CMD intake --label "$label" --device-info "/intake/$label.json"
