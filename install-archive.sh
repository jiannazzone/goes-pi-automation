#!/usr/bin/env bash
# Archive-host installer -- run this on media-center (the archive), NOT the Pi.
# Installs only the retention job that ages products out of the GOES-19 archive;
# the Pi-side install.sh (RTL-SDR, decode, sync, disk guard) does not belong here.
# Idempotent; never clobbers an already-filled /etc/goes-monitor/retention.env.
# Run with: sudo ./install-archive.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN=/opt/goes-monitor/bin
ETC=/etc/goes-monitor
UNITS=/etc/systemd/system

echo "== Installing retention script to $BIN =="
install -d -m 0755 "$BIN"
install -m 0755 "$SRC/bin/goes-retention.sh" "$BIN/"

echo "== Config $ETC/retention.env (0600; kept if already filled) =="
install -d -m 0750 "$ETC"
if [[ -f "$ETC/retention.env" ]]; then
  echo "  keep existing $ETC/retention.env"
else
  install -m 0600 "$SRC/config/retention.env.example" "$ETC/retention.env"
  echo "  created $ETC/retention.env (defaults: /NAS/goes19, 7 days -- edit if needed)"
fi

echo "== systemd units -> $UNITS =="
install -m 0644 "$SRC/systemd/goes-retention.service" "$SRC/systemd/goes-retention.timer" "$UNITS/"
systemctl daemon-reload

cat <<EOF

Installed on the archive host. The timer is NOT enabled yet -- this job deletes
data, so arm it deliberately after a dry run:

  1. Preview first:   sudo DRY_RUN=1 $BIN/goes-retention.sh
  2. Arm daily 05:00: sudo systemctl enable --now goes-retention.timer
  3. Run once now:    sudo systemctl start goes-retention.service
     Inspect it:      journalctl -u goes-retention.service -e
     Next fire:       systemctl list-timers goes-retention.timer

Tune the window in $ETC/retention.env (RETENTION_DAYS); it must point at the same
dir as the Pi's sync.env ARCHIVE_DEST.
EOF
