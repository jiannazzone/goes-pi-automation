#!/usr/bin/env bash
# Archive retention -- RUNS ON THE ARCHIVE HOST (media-center), not the Pi.
#
# goes-sync.sh (on the Pi) rsyncs products here with --remove-source-files and
# never deletes on this end, so the archive grows without bound until the drive
# fills. This is the counterweight: a hard age cap. Every file older than
# RETENTION_DAYS is deleted, then the emptied date dirs are swept -- the same
# `find -mindepth 1 -type d -empty -delete` idiom goes-sync uses on the Pi.
#
# All products age out equally -- ABI IMAGES, L2, and the flat EMWIN GIF/text
# files -- keyed on file mtime, which `rsync -a` preserves from the source, so
# "age" tracks capture/reception time, not when the file landed here.
#
# Config from /etc/goes-monitor/retention.env (via systemd EnvironmentFile):
#   RETENTION_ROOT   dir to prune (default /NAS/goes19; must equal PIN below)
#   RETENTION_DAYS   keep this many days (default 7)
#   DRY_RUN=1        report what would go, delete nothing
set -euo pipefail

ROOT="${RETENTION_ROOT:-/NAS/goes19}"
DAYS="${RETENTION_DAYS:-7}"
DRY_RUN="${DRY_RUN:-0}"

# Hard pin: this deployment only ever prunes the GOES-19 archive. Refuses any
# other root -- even a hand-run with a mistyped RETENTION_ROOT. A single trailing
# slash is tolerated. Set PIN="" to reuse the script for a different archive.
PIN="/NAS/goes19"
if [[ -n "$PIN" && "${ROOT%/}" != "$PIN" ]]; then
  echo "goes-retention: pinned to '$PIN' -- refusing RETENTION_ROOT='$ROOT'" >&2
  exit 1
fi

# Backstop against catastrophic roots if PIN is ever cleared.
case "${ROOT%/}" in
  ""|"/"|"/root"|"/home"|"/etc"|"/usr"|"/var"|"/boot"|"/bin"|"/sbin")
    echo "goes-retention: refusing to prune ROOT='$ROOT'" >&2; exit 1 ;;
esac

# If the archive drive isn't mounted the tree is simply absent. No-op rather than
# pruning (or sweeping) an empty mountpoint. The unit also gates on the mount;
# this is the belt to that suspenders.
if [[ ! -d "$ROOT" ]]; then
  echo "goes-retention: ROOT '$ROOT' not present (unmounted?) -- nothing to do"
  exit 0
fi

echo "goes-retention: root=$ROOT age>${DAYS}d dry_run=$DRY_RUN start=$(date -u +%FT%TZ)"

# -xdev keeps us on ROOT's own filesystem (never descend into a nested mount).
# Group + `|| true` so a stray unreadable dir can't abort the count under pipefail.
n="$( { find "$ROOT" -xdev -type f -mtime +"$DAYS" 2>/dev/null || true; } | wc -l | tr -d ' ')"
echo "goes-retention: $n files older than ${DAYS}d"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "goes-retention: DRY_RUN -- would delete these (first 50):"
  find "$ROOT" -xdev -type f -mtime +"$DAYS" 2>/dev/null | sort | head -n 50
  echo "goes-retention: DRY_RUN -- nothing deleted"
  exit 0
fi

# Delete the aged files, then sweep the dirs left empty -- this reclaims the
# emptied scan dirs, session folders, and EMWIN dirs.
find "$ROOT" -xdev -type f -mtime +"$DAYS" -delete
find "$ROOT" -xdev -mindepth 1 -type d -empty -delete

echo "goes-retention: removed $n files, done=$(date -u +%FT%TZ)"
