#!/usr/bin/env bash
# One command per drive: back up every partition on it.
#
#   sudo scripts/backup-drive.sh <disk> [--label NAME] [--strip-prefix PREFIX]... [--dry-run]
#
#   sudo scripts/backup-drive.sh /dev/sdd
#   sudo scripts/backup-drive.sh /dev/sdd --strip-prefix "KG 10.23.21 Backup/Documents/" \
#                                         --strip-prefix "KG 10.23.21 Backup/"
#
# For each partition with a filesystem: release any other mount of it (such as the
# desktop's read-write automount), mount it read-only, register it (intake.sh), then
# scan, hash and copy it (`run`). Each partition is its own source, labelled
# <label>-p<N>; a partition already in the catalog keeps the label it was given. When
# all are done it prints `report sources` and unmounts the partitions so the drive can
# be unplugged. Sleep is blocked while it runs. Run it again to resume.
#
# --label defaults to the drive's model and the end of its serial number.
# --strip-prefix applies to every partition. --dry-run shows what each partition would
# copy, writing nothing to the destination.
#
# Environment: DRIVECON_SRV (default /srv), DRIVECON_DEST_DIR (default: from .env), and for
# tests DRIVECON_CMD, DRIVECON_QUERY_CMD, DRIVECON_INTAKE, DRIVECON_NO_INHIBIT.
set -euo pipefail

usage() {
  echo "usage: $0 <disk> [--label NAME] [--strip-prefix PREFIX]... [--dry-run]" >&2
  exit 2
}
die() { echo "backup-drive: $1" >&2; exit "${2:-1}"; }

repo=$(cd "$(dirname "$0")/.." && pwd)
[ $# -ge 1 ] || usage
args=("$@")
device=$1
shift
base="" dry_run=0
strip_args=()
while [ $# -gt 0 ]; do
  case $1 in
    --label) base=${2:?}; shift 2 ;;
    --strip-prefix) strip_args+=(--strip-prefix "${2:?}"); shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    *) usage ;;
  esac
done

if [ "$(id -u)" -ne 0 ] && [ -z "${DRIVECON_ALLOW_NONROOT:-}" ]; then
  die "needs root (mounting, SMART): run it with sudo"
fi

# Keep the machine awake for the whole drive.
if [ -z "${DRIVECON_NO_INHIBIT:-}" ] && [ -z "${DRIVECON_INHIBITED:-}" ] \
  && command -v systemd-inhibit >/dev/null; then
  export DRIVECON_INHIBITED=1
  exec systemd-inhibit --what=sleep:idle --why="drivecon backing up $device" "$0" "${args[@]}"
fi

cd "$repo"   # compose.yaml and .env live here
SRV=${DRIVECON_SRV:-/srv}
DRIVECON_CMD=${DRIVECON_CMD:-docker compose run --rm drivecon}
DRIVECON_QUERY_CMD=${DRIVECON_QUERY_CMD:-docker compose run --rm -T drivecon}
INTAKE=${DRIVECON_INTAKE:-$repo/scripts/intake.sh}

# --- the disk ---------------------------------------------------------------------
lsblk -dn "$device" >/dev/null 2>&1 || die "$device: no such disk" 2
if [ "$(lsblk -dno TYPE "$device")" = part ]; then
  die "$device is a partition; give the whole disk (/dev/$(lsblk -no PKNAME "$device")) and every partition is done" 2
fi

# --- the destination must really be there -------------------------------------------
dest=${DRIVECON_DEST_DIR:-}
if [ -z "$dest" ] && [ -f .env ]; then
  dest=$(sed -n 's/^DRIVECON_DEST_DIR=\([^[:space:]#]*\).*/\1/p' .env | tail -n 1)
fi
dest=${dest:-/mnt/dest}
mountpoint -q "$dest" || die "destination $dest is not mounted; mount the backup drive first" 2
dest_dev=$(findmnt -no SOURCE "$dest")
case $dest_dev in "$device"*) die "$dest is on $device itself; that is the destination, not a source" 2 ;; esac

# --- label --------------------------------------------------------------------------
if [ -z "$base" ]; then
  model=$(lsblk -dno MODEL "$device" | xargs) serial=$(lsblk -dno SERIAL "$device" | xargs)
  base=$(printf '%s-%s' "${model:-disk}" "${serial: -4}" | tr -cs 'A-Za-z0-9._-' '-' | sed 's/^-*//; s/-*$//')
fi
case $base in
  *[!A-Za-z0-9._-]*|[!A-Za-z0-9]*|"") die "label must start with a letter or digit and use only letters, digits, . _ -" 2 ;;
esac

# --- partitions ---------------------------------------------------------------------
mapfile -t parts < <(lsblk -lnpo NAME,TYPE "$device" | awk '$2 == "part" {print $1}')
whole_disk=0
if [ ${#parts[@]} -eq 0 ]; then parts=("$device"); whole_disk=1; fi   # filesystem on the bare disk

unmount_elsewhere() {  # release every mount of $1 except a read-only one at $2; 1 if one is busy
  local part=$1 keep=$2 target opts
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    target=$(printf '%b' "${line% *}") opts=${line##* }
    if [ "$target" = "$keep" ] && tr ',' '\n' <<<"$opts" | grep -qx ro; then continue; fi
    echo "  unmounting $part from $target"
    umount "$target" 2>/dev/null && continue
    sleep "${DRIVECON_UMOUNT_RETRY_SECS:-3}"   # a file manager or indexer often lets go in a moment
    umount "$target" 2>/dev/null && continue
    echo "  $target is in use, so it cannot be mounted read-only. Holding it open:" >&2
    if command -v fuser >/dev/null; then
      fuser -vm "$target" 2>&1 | sed 's/^/    /' >&2 || true
    fi
    echo "  Close any file manager window, terminal or program using it, then run again." >&2
    return 1
  done < <(findmnt -rno TARGET,OPTIONS --source "$part" || true)
}

attempted=()   # labels whose mount point this run may have mounted
cleanup() {    # unmount what this run mounted, however it ends
  local label mnt
  for label in ${attempted[@]+"${attempted[@]}"}; do
    mnt="$SRV/sources/$label"
    if findmnt -n "$mnt" >/dev/null 2>&1; then umount "$mnt" || true; fi
  done
  attempted=()
}
trap cleanup EXIT

mount_ro() {  # dry runs mount themselves; intake.sh does it for real runs
  local part=$1 mnt=$2 fstype=$3 opts=ro
  mkdir -p "$mnt"
  findmnt -n "$mnt" >/dev/null 2>&1 && return 0
  case $fstype in ext2|ext3|ext4) opts=ro,noload ;; esac
  mount -o "$opts" "$part" "$mnt"
}

done_labels=() skipped=() failed=()
for part in "${parts[@]}"; do
  fstype=$(blkid -p -o value -s TYPE "$part" 2>/dev/null || true)
  case $fstype in
    "") skipped+=("$part (no filesystem)"); continue ;;
    swap) skipped+=("$part (swap)"); continue ;;
    crypto_LUKS|BitLocker|LVM2_member|linux_raid_member|zfs_member)
      skipped+=("$part ($fstype: unlock or assemble it first, then use intake.sh on the result)")
      continue ;;
  esac
  uuid=$(blkid -p -o value -s UUID "$part" 2>/dev/null || true)
  label=$base
  [ "$whole_disk" = 1 ] || label="$base-p${part##*[!0-9]}"
  if [ -n "$uuid" ]; then
    known=$($DRIVECON_QUERY_CMD source-label --uuid "$uuid") || die "could not read the catalog"
    [ -z "$known" ] || label=$known
  fi
  mnt="$SRV/sources/$label"
  echo
  echo "=== $part ($fstype) -> $label"
  if ! unmount_elsewhere "$part" "$mnt"; then
    failed+=("$label ($part is in use elsewhere; nothing copied from it)")
    continue
  fi
  attempted+=("$label")

  if [ "$dry_run" = 1 ]; then
    mount_ro "$part" "$mnt" "$fstype"
    case $fstype in ntfs|vfat|exfat|msdos) os=windows ;; *) os=linux ;; esac
    $DRIVECON_CMD dry-run "$label" --os "$os" || failed+=("$label (dry-run exit $?)")
    done_labels+=("$label")
    continue
  fi

  code=0
  "$INTAKE" "$device" "$label" --partition "$part" ${strip_args[@]+"${strip_args[@]}"} || code=$?
  if [ "$code" -ne 0 ]; then
    failed+=("$label (intake exit $code)")
    [ "$code" -eq 3 ] && continue   # declined past a SMART warning
    break
  fi
  $DRIVECON_CMD run "$label" || code=$?
  done_labels+=("$label")
  case $code in
    0) ;;
    4) failed+=("$label (destination full)"); break ;;
    130) failed+=("$label (interrupted)"); break ;;
    *) failed+=("$label (run exit $code)") ;;
  esac
done

echo
if [ "$dry_run" != 1 ]; then
  $DRIVECON_CMD report sources || true
  for label in ${done_labels[@]+"${done_labels[@]}"}; do   # what was skipped as unknown, per partition
    out=$($DRIVECON_QUERY_CMD report unknown "$label" 2>/dev/null || true)
    case $out in ""|"No unknown"*) ;; *) echo; echo "Unknown file types skipped on $label:"; echo "$out" ;; esac
  done
fi

cleanup

echo
for s in ${skipped[@]+"${skipped[@]}"}; do echo "skipped: $s"; done
if [ ${#failed[@]} -gt 0 ]; then
  for f in "${failed[@]}"; do echo "PROBLEM: $f" >&2; done
  echo "Fix the problem and run the same command again; finished partitions are not redone." >&2
  exit 1
fi
if [ "$dry_run" = 1 ]; then
  echo "Dry run done for: ${done_labels[*]:-nothing}"
else
  echo "Done: ${done_labels[*]:-nothing}. The partitions are unmounted; the drive can be unplugged."
fi
