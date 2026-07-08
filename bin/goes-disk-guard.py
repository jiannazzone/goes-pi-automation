#!/usr/bin/env python3
"""GOES output disk guard.

The SatDump output dir lives on the SD card that also holds the OS/rootfs, so a
full filesystem can corrupt or wedge this headless Pi. This is load-bearing.

  * SOFT threshold  -> Pushover warning (usually a stalled sync, or frame-saving
                       accidentally left on).
  * HARD threshold  -> Pushover critical AND stop the SatDump decode service.
                       Losing capture beats wedging the Pi.

Runs from a systemd timer (~15 min) as root (needs `systemctl stop`). Stdlib only.
Light anti-spam: alert on entering a band, then re-alert every REALERT_SECONDS.

Thresholds are the two constants below -- easy to edit.
"""

import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

# --- Thresholds (percent of filesystem used) ---------------------------------
SOFT_PCT = 80
HARD_PCT = 95
# -----------------------------------------------------------------------------

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/home/aiannazzone/SatDump")
DECODE_SERVICE = os.environ.get("DECODE_SERVICE", "goes-decode.service")
STATE_FILE = os.environ.get("STATE_FILE", "/var/lib/goes-monitor/diskguard-state.json")
REALERT_SECONDS = int(os.environ.get("REALERT_SECONDS", str(6 * 3600)))

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def log(msg):
    print(msg, flush=True)


def used_pct(path):
    """df-style Use% for the filesystem containing `path` (reserved-block aware)."""
    st = os.statvfs(path)
    used = st.f_blocks - st.f_bfree
    avail = st.f_bavail
    denom = used + avail
    if denom <= 0:
        return 0.0, 0
    pct = used / denom * 100.0
    free_gb = (st.f_bavail * st.f_frsize) / (1024 ** 3)
    return pct, free_gb


def send_pushover(title, message, priority="0"):
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        log("WARNING: Pushover not configured -- would have sent: [{}] {}".format(title, message))
        return False
    payload = urllib.parse.urlencode({
        "token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
        "title": title, "message": message, "priority": priority,
    }).encode("utf-8")
    try:
        with urllib.request.urlopen(PUSHOVER_URL, data=payload, timeout=10) as resp:
            resp.read()
        log("Pushover sent: [{}] {}".format(title, message))
        return True
    except Exception as e:
        log("ERROR: Pushover send failed ({}): {}".format(e.__class__.__name__, e))
        return False


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"band": "ok", "last_alert": 0}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log("ERROR: could not persist state: {}".format(e))


def stop_decode():
    try:
        r = subprocess.run(["systemctl", "stop", DECODE_SERVICE],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            log("Stopped {} to protect the rootfs.".format(DECODE_SERVICE))
        else:
            log("WARNING: `systemctl stop {}` rc={} err={}".format(
                DECODE_SERVICE, r.returncode, r.stderr.strip()))
    except Exception as e:
        log("ERROR: could not stop {}: {}".format(DECODE_SERVICE, e))


def main():
    now = time.time()
    if not os.path.isdir(OUTPUT_DIR):
        log("NOTE: output dir {} does not exist yet; checking its parent fs.".format(OUTPUT_DIR))
    check_path = OUTPUT_DIR if os.path.exists(OUTPUT_DIR) else os.path.dirname(OUTPUT_DIR) or "/"

    pct, free_gb = used_pct(check_path)
    log("Disk: {} at {:.1f}% used, {:.1f} GB free (soft={}%, hard={}%)".format(
        check_path, pct, free_gb, SOFT_PCT, HARD_PCT))

    band = "hard" if pct >= HARD_PCT else ("soft" if pct >= SOFT_PCT else "ok")
    state = load_state()
    prev_band = state.get("band", "ok")
    last_alert = float(state.get("last_alert", 0))

    if band == "hard":
        stop_decode()  # idempotent -- safe if already stopped
        new_alert = (prev_band != "hard") or (now - last_alert >= REALERT_SECONDS)
        if new_alert:
            send_pushover(
                "GOES disk CRITICAL",
                "{} at {:.1f}% used ({:.1f} GB free). Decode service STOPPED to "
                "protect the rootfs. Free space / run the sync, then restart "
                "{}.".format(check_path, pct, free_gb, DECODE_SERVICE),
                priority="1")
            last_alert = now
    elif band == "soft":
        new_alert = (prev_band == "ok") or (now - last_alert >= REALERT_SECONDS)
        if new_alert:
            send_pushover(
                "GOES disk warning",
                "{} at {:.1f}% used ({:.1f} GB free). Likely a stalled sync or "
                "frame-saving left on. Decode still running.".format(check_path, pct, free_gb))
            last_alert = now
    else:
        if prev_band != "ok":
            send_pushover(
                "GOES disk recovered",
                "{} back to {:.1f}% used ({:.1f} GB free).".format(check_path, pct, free_gb))
        last_alert = 0

    save_state({"band": band, "last_alert": last_alert})


if __name__ == "__main__":
    main()
