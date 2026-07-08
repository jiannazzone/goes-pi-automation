#!/usr/bin/env python3
# One-shot: read /api, print "snr peak lock ber" for the aiming meter.
import sys, json, urllib.request
endpoint = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080/api"
try:
    with urllib.request.urlopen(endpoint, timeout=2) as r:
        d = json.load(r)
    p = d.get("psk_demod", {}); c = d.get("ccsds_conv_concat_decoder", {})
    print("{:.2f} {:.2f} {} {:.4f}".format(
        p.get("snr", 0) or 0, p.get("peak_snr", 0) or 0,
        int(bool(c.get("deframer_lock"))), c.get("viterbi_ber", 0) or 0))
except Exception:
    print("0 0 0 0")
