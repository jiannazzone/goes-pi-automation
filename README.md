# GOES-19 24/7 SDR automation (sat-pi)

Unattended live-decode of GOES-19 HRIT on a Raspberry Pi 5, with off-Pi archival,
SD-card protection, and Pushover alerting. Four components, all systemd-driven,
Python 3 **stdlib only** for the monitors.

Source tree lives in `~/goes-automation`. Re-run `sudo ./install.sh` after any edit
(idempotent; never overwrites a filled-in `/etc/goes-monitor/*.env`).

## Components

| Unit | Role | Runs as | Schedule |
|---|---|---|---|
| `goes-decode.service` | SatDump `live goes_hrit` decode, products only, HTTP status on 127.0.0.1:8080 | aiannazzone | long-running, `Restart=on-failure` |
| `goes-health.timer` → `goes-health.service` | Poll :8080/api, page on degrade (deframer_lock/SNR) | aiannazzone | every 60 s |
| `goes-diskguard.timer` → `goes-diskguard.service` | Guard the SD-card rootfs; warn @80%, critical + stop decode @95% | **root** | every 15 min |
| `goes-sync.timer` → `goes-sync.service` | rsync products to media-center, delete after transfer | aiannazzone | daily 04:00 |

Config: `/etc/goes-monitor/{decode,pushover,sync}.env` (root:root 0600).
State:  `/var/lib/goes-monitor/*.json`. Scripts: `/opt/goes-monitor/bin/`.

## Archive-host retention (media-center)

`goes-sync` only ever *adds* to the archive (`ARCHIVE_DEST`, e.g. `/NAS/goes19`)
and never deletes there, so without a counterweight the archive drive fills. The
retention job is that counterweight — a **hard age cap** that runs on
media-center, not the Pi.

| Unit | Role | Host | Schedule |
|---|---|---|---|
| `goes-retention.timer` → `goes-retention.service` | Delete archived products older than `RETENTION_DAYS`, then sweep the emptied dirs | **media-center** | daily 05:00 |

All products age out equally — ABI `IMAGES`, `L2`, and the flat `EMWIN/` files —
by file mtime, which `rsync -a` preserves from the Pi, so age tracks capture time
rather than arrival time. Safety: hard-**pinned to `/NAS/goes19`** (`PIN` in the
script — refuses any other root, even a hand-run), `-xdev`-fenced to the archive
filesystem, no-ops if the drive is unmounted, and `DRY_RUN=1` reports without
deleting. Config in `/etc/goes-monitor/retention.env` (`RETENTION_ROOT` /
`RETENTION_DAYS`); it must point at the same dir as the Pi's `sync.env`
`ARCHIVE_DEST`.

Deploy on media-center (it needs this source tree there — clone or pull it):

```bash
git -C ~/goes-automation pull   # or clone it there the first time
cd ~/goes-automation
sudo ./install-archive.sh                                 # script + units + retention.env
sudo DRY_RUN=1 /opt/goes-monitor/bin/goes-retention.sh    # preview — deletes nothing
sudo systemctl enable --now goes-retention.timer          # then arm the daily prune
```


## Confirmed on device (2026-07-06)

- SatDump **v1.2.2**; no `ingestor` subcommand → uses `satdump live`.
- Pipeline **`goes_hrit`** (HRIT @ 1694.1 MHz baked in). Products-only is the
  default (`write_lrit`=false; no CADU/baseband unless flagged). A 40 s live test
  produced **no `.cadu`/frame files**.
- Tuner is an **RTL-SDR Blog V4** (Rafael Micro R828D). Live test confirmed the
  full invocation: `--source rtlsdr --samplerate 2.4e6 --frequency 1694.1e6
  --gain <n> --bias --http_server 127.0.0.1:8080`. Log showed *Set samplerate
  2400000 / freq 1694100000 / Bias 1 / Gain 40.2*. **`--gain` is the correct flag**
  (value ~0–49; AGC auto-off in manual mode).
- **Status JSON is served at `/api`** (root path returns a plain "use /api"
  string). Real keys (this build): `psk_demod.snr` / `.peak_snr` / `.freq`, and
  `ccsds_conv_concat_decoder.deframer_lock` / `.viterbi_lock` / `.viterbi_ber` /
  `.rs_avg`. There is **no `lock_state`** — the lock signal is **`deframer_lock`**.
- DVB kernel drivers (`dvb_usb_rtl28xxu` etc.) grab the tuner at boot; a
  `/etc/modprobe.d/blacklist-rtlsdr.conf` (installed) keeps them off so librtlsdr
  can claim it.
- Output dir **`/home/aiannazzone/SatDump`**, on `/dev/mmcblk0p2` (ext4) — the
  **same fs as root**, so the disk guard is load-bearing.
- Run-user `aiannazzone` is in `plugdev`+`dialout`; udev `60-librtlsdr0.rules`
  present → USB access without root.
- Tailscale up; **media-center (100.74.1.50) reachable on :22**.

## Divergences from the original brief (please note)

1. **Card partition is 58 GB, not 512 GB** (`mmcblk0p2`, 14% used). Still fine for
   ~1–3 GB/day with daily sync, but ~10× less headroom than assumed.
2. **Samplerate 2.4e6**, not the pipeline's 6e6 default (an Airspy value the
   RTL-SDR can't reach).
3. **No on-device record of the working gain** (the only logged SatDump session
   errored "Samplerate not set!"). You must supply the gain — see checklist.
4. **Decode + health are installed but NOT enabled** — they wait on the dongle
   and the gain value. Only the disk guard is live today.

## Fill-in checklist (what only you can provide)

1. **Tuner gain** → edit `/etc/goes-monitor/decode.env`, set `GAIN=` to the value
   that gave your working ~6 dB (RTL-SDR ~0–49; the `--gain` flag and the rest of
   the invocation are already device-verified). The decode service refuses to
   start until this is a real number.
2. **Pushover** → edit `/etc/goes-monitor/pushover.env`, set `PUSHOVER_TOKEN` and
   `PUSHOVER_USER`.
3. **Archive** → edit `/etc/goes-monitor/sync.env`, set `ARCHIVE_USER` and
   `ARCHIVE_DEST` (dir must exist on media-center). Then authorize the sync key:
   ```
   ssh-copy-id -i ~/.ssh/goes_archive_ed25519.pub <ARCHIVE_USER>@media-center
   # or append ~/.ssh/goes_archive_ed25519.pub to that account's authorized_keys
   ```

## Bring-up sequence (after the checklist)

```bash
# 1. Connect the RTL-SDR + dish, then verify the tuner enumerates:
lsusb | grep -i realtek

# 2. Start the decode and confirm the status endpoint + products:
sudo systemctl enable --now goes-decode.service
curl -s 127.0.0.1:8080/api | python3 -m json.tool     # keys confirmed; values live under real signal
ls -R ~/SatDump                                       # products appear, no .cadu/frame files

# 3. Once decode is locked, start the health monitor:
sudo systemctl enable --now goes-health.timer

# 4. After the SSH key is authorized, dry-run then enable the sync:
sudo systemctl start goes-sync.service && journalctl -u goes-sync.service -n 20 --no-cat
sudo systemctl enable --now goes-sync.timer
```

> **Status JSON keys — resolved on device (2026-07-06).** The monitor now polls
> `/api` and keys on `deframer_lock` + `snr` (this build has no `lock_state`; its
> modules are `psk_demod` / `ccsds_conv_concat_decoder`). It still searches by
> field name anywhere in the JSON, so a future SatDump upgrade that only renames
> containers won't break it — but if a version renames the *fields*, update the
> `find_field(...)` calls in `/opt/goes-monitor/bin/goes-health-monitor.py`.
> One thing untested without signal: confirm `deframer_lock` actually flips to
> `true` under a real lock (expected — it's the standard xRIT deframer flag).

## Acceptance tests (brief §9)

- **Reboot** → `goes-decode` + timers auto-start; `curl 127.0.0.1:8080` responds; products appear.
- **Kill decode** (`sudo pkill -f 'satdump live'`) → systemd restarts within ~10 s.
- **Degrade** → unplug dongle > ~3 min (past debounce): exactly **one** Pushover
  alert, then a recovery when restored. Validated in mock: debounce=3, single
  alert, 6 h re-alert, one recovery, fail-safe on unreachable/missing-keys/non-JSON.
- **Sync** → products land on media-center, sources removed, empty dirs pruned;
  interrupt-safe via `--remove-source-files` + `--partial`.
- **Disk** → validated on a 20 MB tmpfs: warning @80%, critical @95% + decode
  stopped, recovery when freed.

## Manual tools (run from the source tree, not installed)

These are interactive, run-on-demand helpers — **not** systemd units and **not**
copied to `/opt` by `install.sh`. Run them from `~/goes-automation/bin/` while a
decode is up (they read the same `127.0.0.1:8080/api` endpoint). Stdlib only.

- **`goes-aim.sh`** — live one-line SNR bar meter for **aiming the dish**. Push
  `snr` as high as it goes; under real lock `ber → ~0` and `lock=1`. Ctrl-C to
  stop. (Uses helper `goes-aim-read.py`, which one-shots `/api` → `snr peak lock
  ber`.)
  ```bash
  ~/goes-automation/bin/goes-aim.sh
  ```
- **`goes-stats.py`** — fuller interactive dashboard: SNR / peak / BER / RS
  errors / lock with running min·avg·max and lock-%, plus **Pi host health**
  (CPU temp, SD-card usage, load, memory) so one glance covers link *and* box.
  Can also log CSV in the background for later review.
  ```bash
  goes-stats.py               # live dashboard, refresh every 2 s
  goes-stats.py -i 5          # every 5 s
  goes-stats.py --once        # single snapshot, then exit
  # background CSV log (survives your terminal):
  nohup goes-stats.py -q -l ~/goes-stats.csv -i 10 >/dev/null 2>&1 &
  ```
  `DISK_PATH` overrides which filesystem's usage is shown (default `/`).

## Optional: read-only Samba share (manual host setup)

A read-only SMB share over `~/SatDump` lets you reach in from a desktop and grab a
pending product by hand, as a redundant path alongside the daily archive sync. It
is **manual host config** — not installed by `install.sh`, not a systemd unit of
this repo.

**Why read-only, and why it's safe next to the sync.** `~/SatDump` is a *live
spool*, not storage: `goes-sync.service` moves settled products to media-center and
deletes them from the Pi (`--remove-source-files`). So the share is for *reading a
copy off*, never for parking files on the Pi (anything dropped in gets swept to the
archive and removed). Read-only also means an SMB client's stray `.DS_Store` /
`Thumbs.db` writes just fail on the client — no junk enters the spool or the archive.

It does **not** fight the sync:
- rsync only touches files untouched >`SETTLE_MIN` (5 min); reading over Samba does
  not change mtime, so browsing never perturbs the settle timer either way.
- If rsync deletes a file mid-copy, POSIX `unlink()` only drops the directory entry
  — Samba's open handle keeps the inode alive, so your copy finishes reading clean
  data and the file simply vanishes from the folder afterward. No corruption.
- Samba's SMB locks are advisory *between SMB clients only*; they never block
  rsync's local unlink, so the sync can't stall on a handle you're holding.

```bash
# 1. Install Samba
sudo apt update && sudo apt install -y samba

# 2. Append the share block below to /etc/samba/smb.conf

# 3. Set a Samba password for your account (separate from the Unix password;
#    interactive — run it yourself). smbpasswd -a also enables the user.
sudo smbpasswd -a aiannazzone

# 4. Sanity-check and (re)start
testparm                              # validate smb.conf; Ctrl-D to dismiss
sudo systemctl enable --now smbd
```

Share block for `/etc/samba/smb.conf`:

```ini
[goes]
   comment = GOES-19 products (live spool, read-only)
   path = /home/aiannazzone/SatDump
   browseable = yes
   read only = yes
   guest ok = no
   valid users = aiannazzone
```

Reach it from a client (auth as `aiannazzone` with the Samba password):
`smb://sat-pi/goes` (macOS Finder ⌘K / Linux), `\\sat-pi\goes` (Windows) — or use
the Tailscale name/IP if off-LAN.

Optional hardening — restrict the share to your tailnet by adding to the block:
```ini
   hosts allow = 100.64.0.0/10 127.0.0.1
   hosts deny = 0.0.0.0/0
```

## Handy commands

```bash
systemctl list-timers 'goes-*'                 # see all schedules
journalctl -u goes-health.service -f           # watch health polls
journalctl -u goes-diskguard.service -n 20     # last disk checks
sudo systemctl start goes-diskguard.service    # force a disk check now
```

Thresholds live at the top of `goes-disk-guard.py` (`SOFT_PCT`/`HARD_PCT`).
SNR floor / debounce / re-alert are `Environment=` lines in `goes-health.service`.
