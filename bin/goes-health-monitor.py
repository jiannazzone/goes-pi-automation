#!/usr/bin/env python3
"""GOES-19 HRIT decode health monitor.

Polls the SatDump live HTTP status endpoint once per invocation (driven by a
systemd timer, ~60 s cadence) and pages via Pushover when the decode silently
degrades. Catches the failure systemd cannot see: process alive, disk quiet,
nothing actually being received.

Health rule:  locked AND snr >= SNR_FLOOR.
Unhealthy  :  not locked, OR snr below floor, OR endpoint unreachable / keys
              missing (fail-safe -- a crashed/hung SatDump also trips this).

Anti-spam (critical for a remote box):
  * Persistent state file tracks health, consecutive-fail count, alert flag,
    and last-alert time across invocations.
  * Alert only on the healthy->unhealthy transition, and only after DEBOUNCE
    consecutive bad polls (~3 min).
  * One recovery message when health returns.
  * While it stays unhealthy, re-alert every REALERT_SECONDS (6 h), not every poll.

Stdlib only (urllib, json) -- no pip, per operator's minimal-dependency rule.
Config comes from the environment (set by the systemd unit / EnvironmentFile).
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

# --- Config (overridable via environment / systemd) ---------------------------
ENDPOINT = os.environ.get("ENDPOINT", "http://127.0.0.1:8080/api")
SNR_FLOOR = float(os.environ.get("SNR_FLOOR", "2.5"))
DEBOUNCE = int(os.environ.get("DEBOUNCE", "3"))          # consecutive bad polls
REALERT_SECONDS = int(os.environ.get("REALERT_SECONDS", str(6 * 3600)))
STATE_FILE = os.environ.get("STATE_FILE", "/var/lib/goes-monitor/health-state.json")
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "5"))

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")
PUSHOVER_PRIORITY = os.environ.get("PUSHOVER_PRIORITY", "0")
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def log(msg):
    """Print to stdout -> captured by journald."""
    print(msg, flush=True)


def find_field(obj, name):
    """Depth-first search for the first value keyed `name` anywhere in the JSON.

    Module keys (bpsk_demod / xrit_decoder / ...) vary by pipeline and SatDump
    version, so we search by field name rather than assuming the container path.
    """
    if isinstance(obj, dict):
        if name in obj:
            return obj[name]
        for v in obj.values():
            found = find_field(v, name)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_field(v, name)
            if found is not None:
                return found
    return None


def poll():
    """Return (healthy: bool, reason: str, metrics: dict)."""
    try:
        req = urllib.request.Request(ENDPOINT, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
        data = json.loads(raw)
    except Exception as e:  # unreachable, timeout, non-JSON -> fail safe
        return False, "endpoint unreachable ({})".format(e.__class__.__name__), {}

    # Confirmed on device (SatDump v1.2.2, goes_hrit, GET /api):
    #   psk_demod.snr / .peak_snr / .freq
    #   ccsds_conv_concat_decoder.deframer_lock (bool) / .viterbi_lock (0|1)
    #                            / .viterbi_ber / .rs_avg
    # deframer_lock is the definitive "receiving frames" signal (there is no
    # lock_state field in this build).
    lock = find_field(data, "deframer_lock")
    snr = find_field(data, "snr")
    ber = find_field(data, "viterbi_ber")
    rs = find_field(data, "rs_avg")
    vit = find_field(data, "viterbi_lock")
    metrics = {"deframer_lock": lock, "viterbi_lock": vit,
               "snr": snr, "viterbi_ber": ber, "rs_avg": rs}

    # Missing the primary keys -> treat as unhealthy (wrong/renamed schema).
    if lock is None and snr is None:
        return False, "expected keys (deframer_lock/snr) missing from status JSON", metrics

    locked = bool(lock)
    try:
        snr_val = float(snr) if snr is not None else None
    except (TypeError, ValueError):
        snr_val = None

    if not locked:
        return False, "decoder not locked", metrics
    if snr_val is None:
        return False, "snr unreadable", metrics
    if snr_val < SNR_FLOOR:
        return False, "snr {:.1f} dB below floor {:.1f}".format(snr_val, SNR_FLOOR), metrics
    return True, "locked, snr {:.1f} dB".format(snr_val), metrics


def fmt_metrics(m):
    def g(k):
        v = m.get(k)
        return "n/a" if v is None else v
    return "deframer_lock={} viterbi_lock={} snr={} viterbi_ber={} rs_avg={}".format(
        g("deframer_lock"), g("viterbi_lock"), g("snr"), g("viterbi_ber"), g("rs_avg"))


def send_pushover(title, message, priority=None):
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        log("WARNING: Pushover token/user not configured -- would have sent: "
            "[{}] {}".format(title, message))
        return False
    payload = urllib.parse.urlencode({
        "token": PUSHOVER_TOKEN,
        "user": PUSHOVER_USER,
        "title": title,
        "message": message,
        "priority": PUSHOVER_PRIORITY if priority is None else priority,
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
        return {"status": "healthy", "fail_count": 0, "alert_active": False, "last_alert": 0}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log("ERROR: could not persist state: {}".format(e))


def main():
    now = time.time()
    state = load_state()
    healthy, reason, metrics = poll()
    detail = fmt_metrics(metrics)

    if healthy:
        log("HEALTHY: {} | {}".format(reason, detail))
        if state.get("alert_active"):
            send_pushover(
                "GOES-19 decode RECOVERED",
                "Decode healthy again: {}\n{}".format(reason, detail))
        save_state({"status": "healthy", "fail_count": 0,
                    "alert_active": False, "last_alert": 0})
        return

    # Unhealthy path
    fail_count = int(state.get("fail_count", 0)) + 1
    was_alerting = bool(state.get("alert_active", False))
    last_alert = float(state.get("last_alert", 0))
    log("UNHEALTHY ({} consecutive): {} | {}".format(fail_count, reason, detail))

    is_first = (not was_alerting) and fail_count >= DEBOUNCE
    is_realert = was_alerting and (now - last_alert) >= REALERT_SECONDS

    if is_first or is_realert:
        suffix = " (still down, {:.0f}h re-alert)".format((now - last_alert) / 3600.0) \
                 if is_realert else ""
        send_pushover(
            "GOES-19 decode DEGRADED",
            "Reason: {}{}\n{}\nEndpoint: {}".format(reason, suffix, detail, ENDPOINT))
        # Advance the re-alert clock whether or not delivery succeeded, so a
        # Pushover outage can't turn this into a per-poll storm. The 6 h timer
        # retries on its own.
        last_alert = now

    save_state({"status": "unhealthy", "fail_count": fail_count,
                "alert_active": was_alerting or is_first, "last_alert": last_alert})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never let the timer unit hard-fail on a bug
        log("ERROR: monitor crashed: {}: {}".format(e.__class__.__name__, e))
        sys.exit(0)
