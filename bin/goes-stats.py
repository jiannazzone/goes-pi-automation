#!/usr/bin/env python3
"""GOES-19 decode stats monitor -- manual, run-on-demand.

Polls the SatDump live HTTP status endpoint and shows SNR / peak SNR / BER /
RS errors / lock state, with running min/avg/max and lock-% over the session.
Unlike goes-health-monitor.py (the unattended pager), this is an interactive
dashboard you start yourself when you want to watch the link.

Two ways to use it:

  Live dashboard (foreground):
      goes-stats.py                 # refresh every 2 s
      goes-stats.py -i 5            # every 5 s

  Background logging (survives your terminal; review later):
      nohup goes-stats.py -q -l ~/goes-stats.csv -i 10 >/dev/null 2>&1 &
      # ... later:
      tail -f ~/goes-stats.csv

  One-shot snapshot:
      goes-stats.py --once

Options:
  -e, --endpoint URL   status endpoint (default http://127.0.0.1:8080/api)
  -i, --interval SEC   seconds between polls (default 2)
  -l, --log FILE       append timestamped CSV rows (created with a header)
  -q, --quiet          no live dashboard -- pair with -l for background runs
      --once           print a single snapshot and exit
      --no-color       disable ANSI colour

Stdlib only. Ctrl-C stops it and prints a session summary.
"""

import argparse
import json
import os
import signal
import sys
import time
import urllib.request

CSV_HEADER = ("iso_time,epoch,snr,peak_snr,freq_offset,"
              "deframer_lock,viterbi_lock,viterbi_ber,rs_avg")


def find_field(obj, name):
    """First value keyed `name` anywhere in the JSON (schema-tolerant)."""
    if isinstance(obj, dict):
        if name in obj:
            return obj[name]
        for v in obj.values():
            r = find_field(v, name)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_field(v, name)
            if r is not None:
                return r
    return None


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def poll(endpoint, timeout):
    """Return a dict of metrics; `reachable` False if the endpoint is down."""
    try:
        req = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return {"reachable": False}
    return {
        "reachable": True,
        "snr": fnum(find_field(data, "snr")),
        "peak_snr": fnum(find_field(data, "peak_snr")),
        "freq": fnum(find_field(data, "freq")),
        "deframer_lock": bool(find_field(data, "deframer_lock")),
        "viterbi_lock": find_field(data, "viterbi_lock"),
        "ber": fnum(find_field(data, "viterbi_ber")),
        "rs_avg": fnum(find_field(data, "rs_avg")),
    }


class Stat:
    """Running min / max / sum for one metric (ignores None samples)."""
    def __init__(self):
        self.n = 0
        self.min = None
        self.max = None
        self.sum = 0.0

    def add(self, v):
        if v is None:
            return
        self.n += 1
        self.sum += v
        self.min = v if self.min is None else min(self.min, v)
        self.max = v if self.max is None else max(self.max, v)

    @property
    def avg(self):
        return self.sum / self.n if self.n else None


def color(s, code, enable):
    return "\033[{}m{}\033[0m".format(code, s) if enable else s


def fmt_ber(v):
    if v is None:
        return "n/a"
    if v == 0:
        return "0"
    return "{:.2e}".format(v)


def fmt(v, spec="{:.2f}"):
    return "n/a" if v is None else spec.format(v)


def hms(seconds):
    seconds = int(seconds)
    return "{:02d}:{:02d}:{:02d}".format(seconds // 3600, (seconds % 3600) // 60, seconds % 60)


def render(m, snr_s, peak_s, ber_s, rs_s, locked_count, samples, start, endpoint, use_color):
    up = hms(time.time() - start)
    now = time.strftime("%H:%M:%S")
    lines = []
    lines.append("GOES-19 decode stats  ·  {}  ·  {}  ·  samples {} (uptime {})".format(
        endpoint, now, samples, up))
    lines.append("")

    if not m["reachable"]:
        lines.append("  " + color("ENDPOINT DOWN", "1;31", use_color) +
                     "  (decode stopped or not started?)")
    else:
        dfl = m["deframer_lock"]
        lock_txt = color("● LOCKED", "1;32", use_color) if dfl else color("○ no lock", "1;31", use_color)
        lines.append("  LOCK    deframer {}     viterbi {}".format(lock_txt, m["viterbi_lock"]))
        lines.append("  SNR     {:>7} dB  (peak {})   min {}  avg {}  max {}".format(
            fmt(m["snr"]), fmt(m["peak_snr"]),
            fmt(snr_s.min), fmt(snr_s.avg), fmt(snr_s.max)))
        lines.append("  BER     {:>7}     min {}  avg {}  max {}".format(
            fmt_ber(m["ber"]), fmt_ber(ber_s.min), fmt_ber(ber_s.avg), fmt_ber(ber_s.max)))
        lines.append("  RS err  {:>7}     max {}".format(
            fmt(m["rs_avg"], "{:.0f}"), fmt(rs_s.max, "{:.0f}")))
        lines.append("  FREQ    {:>7} Hz offset".format(fmt(m["freq"], "{:.0f}")))

    lockpct = (100.0 * locked_count / samples) if samples else 0.0
    lines.append("  LOCKED  {:.1f}% of {} samples".format(lockpct, samples))
    lines.append("")
    lines.append("  [Ctrl-C to stop]")
    return "\n".join(lines)


def summary(snr_s, ber_s, locked_count, samples, start):
    print("\n— session summary —")
    print("  duration     {}".format(hms(time.time() - start)))
    print("  samples      {}".format(samples))
    print("  locked       {:.1f}%".format(100.0 * locked_count / samples if samples else 0.0))
    print("  SNR min/avg/max   {} / {} / {} dB".format(
        fmt(snr_s.min), fmt(snr_s.avg), fmt(snr_s.max)))
    print("  BER min/avg/max   {} / {} / {}".format(
        fmt_ber(ber_s.min), fmt_ber(ber_s.avg), fmt_ber(ber_s.max)))


def main():
    ap = argparse.ArgumentParser(add_help=True, description="GOES-19 decode stats monitor")
    ap.add_argument("-e", "--endpoint", default=os.environ.get("ENDPOINT", "http://127.0.0.1:8080/api"))
    ap.add_argument("-i", "--interval", type=float, default=2.0)
    ap.add_argument("-l", "--log")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--timeout", type=float, default=3.0)
    args = ap.parse_args()

    if args.quiet and not args.log and not args.once:
        ap.error("--quiet with no output: add -l FILE (background logging) or --once")

    use_color = (not args.no_color) and sys.stdout.isatty()

    logf = None
    if args.log:
        new = not os.path.exists(args.log) or os.path.getsize(args.log) == 0
        logf = open(args.log, "a")
        if new:
            logf.write(CSV_HEADER + "\n")
            logf.flush()

    snr_s, peak_s, ber_s, rs_s = Stat(), Stat(), Stat(), Stat()
    locked_count = 0
    samples = 0
    start = time.time()
    first_draw = [True]

    def draw(block):
        # Redraw in place: home cursor + clear-below, so the dashboard stays put.
        if first_draw[0]:
            sys.stdout.write("\033[2J")
            first_draw[0] = False
        sys.stdout.write("\033[H\033[J" + block + "\n")
        sys.stdout.flush()

    def stop(*_):
        if not args.quiet:
            summary(snr_s, ber_s, locked_count, samples, start)
        if logf:
            logf.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    while True:
        m = poll(args.endpoint, args.timeout)
        samples += 1
        if m.get("reachable"):
            snr_s.add(m["snr"]); peak_s.add(m["peak_snr"])
            ber_s.add(m["ber"]); rs_s.add(m["rs_avg"])
            if m["deframer_lock"]:
                locked_count += 1

        if logf:
            iso = time.strftime("%Y-%m-%dT%H:%M:%S")
            if m.get("reachable"):
                row = "{},{:.0f},{},{},{},{},{},{},{}".format(
                    iso, time.time(),
                    fmt(m["snr"]), fmt(m["peak_snr"]), fmt(m["freq"], "{:.0f}"),
                    1 if m["deframer_lock"] else 0,
                    m["viterbi_lock"] if m["viterbi_lock"] is not None else "",
                    fmt_ber(m["ber"]), fmt(m["rs_avg"], "{:.0f}"))
            else:
                row = "{},{:.0f},,,,,,,".format(iso, time.time())
            logf.write(row + "\n")
            logf.flush()

        if not args.quiet:
            block = render(m, snr_s, peak_s, ber_s, rs_s, locked_count, samples,
                           start, args.endpoint, use_color)
            if args.once:
                print(block)            # plain snapshot, no screen-clear
            else:
                draw(block)             # live dashboard, redraw in place

        if args.once:
            if logf:
                logf.close()
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
