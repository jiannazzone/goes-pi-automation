#!/usr/bin/env bash
# Installer for the GOES-19 24/7 automation. Idempotent; never clobbers an
# already-filled /etc/goes-monitor/*.env. Run with: sudo ./install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-aiannazzone}"
BIN=/opt/goes-monitor/bin
ETC=/etc/goes-monitor
STATE=/var/lib/goes-monitor
UNITS=/etc/systemd/system

echo "== Installing scripts to $BIN =="
install -d -m 0755 "$BIN"
install -m 0755 "$SRC/bin/goes-health-monitor.py" "$BIN/"
install -m 0755 "$SRC/bin/goes-disk-guard.py"    "$BIN/"
install -m 0755 "$SRC/bin/goes-sync.sh"          "$BIN/"

echo "== Config dir $ETC (secrets root:root 0600) =="
install -d -m 0750 "$ETC"
# Install env files only if absent, so re-running never overwrites real secrets.
for f in pushover sync decode; do
  if [[ -f "$ETC/$f.env" ]]; then
    echo "  keep existing $ETC/$f.env"
  else
    install -m 0600 "$SRC/config/$f.env.example" "$ETC/$f.env"
    echo "  created $ETC/$f.env (PLACEHOLDER -- edit before enabling)"
  fi
done

echo "== RTL-SDR: blacklist DVB kernel drivers (else they grab the tuner at boot) =="
install -m 0644 "$SRC/config/blacklist-rtlsdr.conf" /etc/modprobe.d/blacklist-rtlsdr.conf
# Take effect now without a reboot (ignore errors if already unloaded / in use).
for m in dvb_usb_rtl28xxu dvb_usb_v2 rtl2832_sdr rtl2832; do modprobe -r "$m" 2>/dev/null || true; done

echo "== State dir $STATE (owner $RUN_USER, group-writable for root diskguard) =="
install -d -m 0775 -o "$RUN_USER" -g "$RUN_USER" "$STATE"

echo "== systemd units -> $UNITS =="
install -m 0644 "$SRC"/systemd/*.service "$SRC"/systemd/*.timer "$UNITS/"
systemctl daemon-reload

echo "== Enabling the disk guard now (safe: protective, decode-independent) =="
systemctl enable --now goes-diskguard.timer

echo "== Pinning CPU governor to performance now (safe: real-time SDR stability) =="
systemctl enable --now cpu-performance.service

cat <<EOF

Installed. Enabled now: goes-diskguard.timer, cpu-performance.service
NOT yet enabled (need config / hardware -- see README "Fill-in checklist"):
  goes-decode.service   (set GAIN in $ETC/decode.env, connect dongle)
  goes-health.timer     (enable together with the decode)
  goes-sync.timer       (fill $ETC/sync.env + install the SSH key)
EOF
