#!/usr/bin/env bash
# Daily product sync to the archive host. THIS SYNC IS THE BACKUP: once products
# land on the archive, a dead SD card costs only the rebuildable OS/config.
#
# rsync --remove-source-files deletes each source file only AFTER it is confirmed
# transferred, so an interrupted run is safe -- unsent files stay put for next time.
# Empty date dirs that rsync leaves behind are then pruned (top dir protected).
#
# SETTLE WINDOW: the decoder writes product PNGs in-place (no temp+rename), so a
# file caught mid-write could be transferred truncated and then deleted by
# --remove-source-files -- a permanent loss, since this sync IS the backup. We
# therefore only transfer files untouched for >SETTLE_MIN minutes, guaranteeing
# rsync never grabs a product still being written. A product being written at run
# time simply waits for the next run.
#
# Config comes from /etc/goes-monitor/sync.env (via systemd EnvironmentFile):
#   OUTPUT_DIR, ARCHIVE_USER, ARCHIVE_HOST, ARCHIVE_DEST, SSH_KEY (optional),
#   SETTLE_MIN (optional, default 5)
set -euo pipefail

: "${OUTPUT_DIR:?OUTPUT_DIR not set}"
: "${ARCHIVE_USER:?ARCHIVE_USER not set}"
: "${ARCHIVE_HOST:?ARCHIVE_HOST not set}"
: "${ARCHIVE_DEST:?ARCHIVE_DEST not set}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/goes_archive_ed25519}"
SETTLE_MIN="${SETTLE_MIN:-5}"

# Refuse to run against unfilled placeholders (installed defaults).
case "${ARCHIVE_USER}:${ARCHIVE_DEST}" in
  *REPLACE_ME*|*CHANGE_ME*)
    echo "sync not configured yet (ARCHIVE_USER/ARCHIVE_DEST still placeholder); skipping." >&2
    exit 0 ;;
esac

if [[ ! -d "$OUTPUT_DIR" ]]; then
  echo "OUTPUT_DIR $OUTPUT_DIR does not exist yet; nothing to sync." >&2
  exit 0
fi

SSH_CMD="ssh -o BatchMode=yes -o ConnectTimeout=15"
[[ -f "$SSH_KEY" ]] && SSH_CMD="$SSH_CMD -i $SSH_KEY"

echo "Syncing $OUTPUT_DIR/ -> ${ARCHIVE_USER}@${ARCHIVE_HOST}:${ARCHIVE_DEST}/ (settled >${SETTLE_MIN}m)"

# Build the transfer list from files untouched for >SETTLE_MIN minutes, so we
# never read a product the decoder is still writing. Null-delimited to survive
# any spaces in product dir names (e.g. "Full Disk"). Paths are relative to
# OUTPUT_DIR so rsync recreates the tree under ARCHIVE_DEST.
#
# SESSION-FOLDER GUARD: the live service writes products flat (EMWIN/, IMAGES/,
# L2/, "Admin Messages"/...), but a manual/offline SatDump run (e.g. decoding a
# recording during dish realignment) left pointing at OUTPUT_DIR auto-creates a
# top-level session folder named "<date>_<time>_<pipeline>_<freq>" with its own
# nested product tree. Since we transfer with --remove-source-files, such a
# folder would be MOVED to the archive (polluting it a level too deep) and
# deleted from the Pi. Prune any top-level date-named session dir so those
# scratch runs stay put. The pattern's trailing "_??-??_" (minutes, no seconds,
# then more text) matches session roots but never product timestamp folders,
# which are "<date>_<HH-MM-SS>" and live several levels down, not at depth 1.
cd "$OUTPUT_DIR"
LIST="$(mktemp)"
trap 'rm -f "$LIST"' EXIT
find . -type d -path './????-??-??_??-??_*' -prune -o \
  -type f -mmin +"$SETTLE_MIN" -print0 > "$LIST"

if [[ ! -s "$LIST" ]]; then
  echo "Nothing settled to sync (all files newer than ${SETTLE_MIN}m); done."
  exit 0
fi

# --partial keeps interrupted transfers resumable; --remove-source-files frees
# space only after a file is safely on the archive.
rsync -a --partial --remove-source-files --info=stats1,progress2 \
  --from0 --files-from="$LIST" \
  -e "$SSH_CMD" \
  ./ "${ARCHIVE_USER}@${ARCHIVE_HOST}:${ARCHIVE_DEST}"/

# Prune the now-empty date dirs rsync leaves. -mindepth 1 protects OUTPUT_DIR itself.
find "$OUTPUT_DIR" -mindepth 1 -type d -empty -delete
echo "Sync complete."
