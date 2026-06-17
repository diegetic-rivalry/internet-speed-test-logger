# Internet Speed Test Logger

Logs download/upload/ping speed every 15 minutes to a CSV also logs the server sponsor and location. Designed to be used on Windows.

## Setup

```powershell
pip install speedtest-cli
```

## Usage

```powershell
# Run continuously, every 15 minutes (Ctrl+C to stop)
python internet-speed-test-logger.py

# One test and exit
python internet-speed-test-logger.py --once

# Custom interval (minutes)
python internet-speed-test-logger.py --interval 5

# List nearby servers and their IDs
python internet-speed-test-logger.py --list-servers

# Pin to one fixed nearby server (cleaner, consistent test path)
python internet-speed-test-logger.py --server 22683

# Install / remove an unattended Windows Scheduled Task
python internet-speed-test-logger.py --install
python internet-speed-test-logger.py --install --server 22683 --interval 15
python internet-speed-test-logger.py --uninstall
```

Results are written to `logs/internet_speed_log.csv`.

## What each row records

| Column | Meaning |
|---|---|
| `timestamp` | Local time the test started (ISO 8601) |
| `status` | `ok`, `error`, or `skipped` (overlapping run) |
| `ping_ms` | Latency to the test server |
| `download_mbps` / `upload_mbps` | Measured throughput |
| `duration_seconds` | How long the test took (rises under congestion) |
| `server_id` / `server_sponsor` / `server_name` / `server_country` | Test server |
| `adapter_name` | Active network adapter (e.g. `Ethernet`) |
| `adapter_link_speed` | **Negotiated** link rate (e.g. `1 Gbps`) |
| `computer_name` | Machine the test ran on |
| `error` | Failure detail, if any |

## Design notes

- **No overlapping tests.** A lock file (`logs/speedtest.lock`) plus the
  scheduler's `IgnoreNew` setting ensure only one test runs at a time, so a
  slow test never competes with the next one and skews both. Skipped runs are
  logged as `skipped` rows so gaps are explained, not silent.
- **Test duration is recorded**, so slowdowns during peak hours show up as
  evidence of congestion.
- **Schema changes are safe.** If the CSV columns change between versions, the
  old file is archived (`internet_speed_log_pre-<timestamp>.csv`) rather than
  mixed with new rows.

## Scheduled task reliability

`--install` applies these settings (best-effort; run from an Administrator
shell if they don't apply): wake-to-run, start-when-available, run-on-battery,
a 10-minute time limit, and no overlapping instances. Still verify in Task
Scheduler:

- **Logged out:** by default the task runs only while you are **logged in**.
  To run while logged out, edit the task and choose *"Run whether user is
  logged on or not"* (requires your account password).
- **Sleep:** wake-to-run only fires if your power plan allows wake timers
  (*Power Options > Sleep > Allow wake timers*). If the PC is fully powered
  off, it will not run.
