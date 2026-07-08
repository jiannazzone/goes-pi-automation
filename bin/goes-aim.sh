#!/usr/bin/env bash
# Live SNR meter for aiming the dish. Ctrl-C to stop.
# Push `snr` as high as it goes; under real lock ber -> ~0 and lock=1.
ENDPOINT="${ENDPOINT:-http://127.0.0.1:8080/api}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
while true; do
  read -r snr peak lock ber < <(python3 "$DIR/goes-aim-read.py" "$ENDPOINT")
  bars=$(python3 -c "print('#'*max(0,min(40,round(float('$snr')*2))))")
  printf '\rsnr=%6s dB  peak=%6s  lock=%s  ber=%-7s |%-40s|' "$snr" "$peak" "$lock" "$ber" "$bars"
  sleep 1
done
