import argparse
import csv
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import speedtest

# Directory of this script, so it works regardless of the current working dir.
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "internet_speed_log.csv"
LOCK_FILE = LOG_DIR / "speedtest.lock"
SCRIPT_PATH = Path(__file__).resolve()

# Name of the Windows Scheduled Task created by --install.
TASK_NAME = "InternetSpeedTestLogger"

# When the script runs via pythonw.exe (no console), launching a console child
# process like PowerShell makes Windows pop up a new console window. This flag
# suppresses that window so unattended runs don't flash anything on screen.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# A lock older than this is treated as abandoned.
# Kept under the default 15-min interval so a stale lock never blocks for long.
LOCK_STALE_SECONDS = 14 * 60

CSV_FIELDS = [
    "timestamp",
    "status",
    "ping_ms",
    "download_mbps",
    "upload_mbps",
    "duration_seconds",
    "server_id",
    "server_sponsor",
    "server_name",
    "server_country",
    "adapter_name",
    "adapter_link_speed",
    "computer_name",
    "error",
]

class AlreadyRunning(Exception):
    """Raised when another instance already holds the lock."""

def get_adapter_info():

    blank = {"name": "", "link_speed": ""}
    if sys.platform != "win32":
        return blank

    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$idx=(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
        "Sort-Object RouteMetric | Select-Object -First 1).IfIndex;"
        "$a=if($idx){Get-NetAdapter -InterfaceIndex $idx} else "
        "{Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | "
        "Select-Object -First 1};"
        "[pscustomobject]@{name=$a.Name; link_speed=$a.LinkSpeed} | "
        "ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30,
            creationflags=_NO_WINDOW,
        )
        data = json.loads(result.stdout.strip())
        return {
            "name": data.get("name") or "",
            "link_speed": data.get("link_speed") or "",
        }
    except Exception: 
        return blank

def acquire_lock():

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if LOCK_FILE.exists():
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age > LOCK_STALE_SECONDS:
            LOCK_FILE.unlink(missing_ok=True)

    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise AlreadyRunning
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)

def release_lock():
    LOCK_FILE.unlink(missing_ok=True)

def run_speed_test(server_id=None):
    """Run one speed test and return a result dict matching CSV_FIELDS.

    Never raises: failures are captured into the 'status'/'error' fields so the
    logging loop can keep running.
    """
    row = {field: "" for field in CSV_FIELDS}
    row["timestamp"] = datetime.now().isoformat(timespec="seconds")
    row["computer_name"] = platform.node()

    adapter = get_adapter_info()
    row["adapter_name"] = adapter["name"]
    row["adapter_link_speed"] = adapter["link_speed"]

    start = time.monotonic()
    try:

        st = speedtest.Speedtest()
        if server_id:
            st.get_servers([int(server_id)])
        st.get_best_server()
        download_bps = st.download()
        upload_bps = st.upload(pre_allocate=False)

        results = st.results.dict()
        server = results.get("server", {})

        row["status"] = "ok"
        row["ping_ms"] = round(results.get("ping", 0), 2)
        row["download_mbps"] = round(download_bps / 1_000_000, 2)
        row["upload_mbps"] = round(upload_bps / 1_000_000, 2)
        row["server_id"] = server.get("id", "")
        row["server_sponsor"] = server.get("sponsor", "")
        row["server_name"] = server.get("name", "")
        row["server_country"] = server.get("country", "")
    except Exception as exc: 
        row["status"] = "error"
        row["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        row["duration_seconds"] = round(time.monotonic() - start, 2)

    return row

def _archive_incompatible_log():
    """If the existing CSV has a different header, rename it so the active log
    keeps a consistent set of columns (old evidence is preserved, not lost)."""
    if not (LOG_FILE.exists() and LOG_FILE.stat().st_size > 0):
        return
    with LOG_FILE.open("r", encoding="utf-8") as f:
        first_line = f.readline().strip()
    if first_line == ",".join(CSV_FIELDS):
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = LOG_FILE.with_name(f"{LOG_FILE.stem}_pre-{stamp}.csv")
    LOG_FILE.rename(backup)
    print(f"Archived old-format log to {backup.name} (columns changed).")

def log_result(row):
    """Append a result row to the CSV file, writing the header if new."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _archive_incompatible_log()
    file_exists = LOG_FILE.exists() and LOG_FILE.stat().st_size > 0

    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

def format_summary(row):
    if row["status"] == "ok":
        return (
            f"{row['timestamp']}  "
            f"down={row['download_mbps']} Mbps  "
            f"up={row['upload_mbps']} Mbps  "
            f"ping={row['ping_ms']} ms  "
            f"link={row['adapter_link_speed']}  "
            f"{row['duration_seconds']}s  "
            f"({row['server_sponsor']})"
        )
    if row["status"] == "skipped":
        return f"{row['timestamp']}  SKIPPED  {row['error']}"
    return f"{row['timestamp']}  ERROR  {row['error']}"

def run_and_log(server_id=None):
    """Acquire the lock, run one test, log it. Logs a 'skipped' row instead if
    another test is already running (so gaps in the data are explained)."""
    try:
        acquire_lock()
    except AlreadyRunning:
        skipped = {field: "" for field in CSV_FIELDS}
        skipped["timestamp"] = datetime.now().isoformat(timespec="seconds")
        skipped["status"] = "skipped"
        skipped["error"] = "another speed test is already running"
        skipped["computer_name"] = platform.node()
        log_result(skipped)
        print(format_summary(skipped))
        return

    try:
        row = run_speed_test(server_id)
        log_result(row)
        print(format_summary(row))
    finally:
        release_lock()

def list_servers(limit=15):
    try:
        st = speedtest.Speedtest()
        servers = st.get_servers()
    except Exception as exc:
        print(f"Could not retrieve server list: {exc}")
        return 1

    flat = [s for group in servers.values() for s in group]
    flat.sort(key=lambda s: s.get("d", 0))

    print(f"{'ID':>7}  {'Dist':>8}  Sponsor / Location")
    print("-" * 60)
    for s in flat[:limit]:
        print(
            f"{s['id']:>7}  {s.get('d', 0):>6.1f}km  "
            f"{s['sponsor']} - {s['name']}, {s['country']}"
        )
    print("\nPin a fixed server with:  --server <ID>")
    return 0


def _pythonw_executable():
    """Return pythonw.exe (no console window) if available, else python.exe."""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else exe)


def _harden_task():
    """
    Sets wake-to-run, start-when-available, run-on-battery, a 10-minute
    time limit (kills a hung test), and IgnoreNew so the scheduler itself
    won't start an overlapping instance. Returns True on success.
    """
    ps = (
        "$ErrorActionPreference='Stop';"
        "$s=New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit (New-TimeSpan -Minutes 10) "
        "-MultipleInstances IgnoreNew;"
        f"Set-ScheduledTask -TaskName '{TASK_NAME}' -Settings $s | Out-Null"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, text=True,
        creationflags=_NO_WINDOW,
    )
    return result.returncode == 0


def install_task(interval_minutes, server_id=None):
    """Register a Windows Scheduled Task that runs one test every interval."""
    if sys.platform != "win32":
        print("--install is only supported on Windows (uses schtasks).")
        return 1

    minutes = max(1, int(interval_minutes))
    extra = f" --server {int(server_id)}" if server_id else ""
    run_command = f'"{_pythonw_executable()}" "{SCRIPT_PATH}" --once{extra}'

    cmd = [
        "schtasks", "/Create",
        "/TN", TASK_NAME,
        "/SC", "MINUTE",
        "/MO", str(minutes),
        "/TR", run_command,
        "/F",  # overwrite if the task already exists
    ]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            creationflags=_NO_WINDOW)
    if result.returncode != 0:
        print("Failed to create scheduled task:")
        print((result.stderr or result.stdout).strip())
        return result.returncode

    hardened = _harden_task()

    print(f"Installed scheduled task '{TASK_NAME}' (every {minutes} min).")
    print(f"Logging to {LOG_FILE}")
    if hardened:
        print("Applied: wake-to-run, start-when-available, run-on-battery,")
        print("         10-min time limit, no overlapping instances.")
    else:
        print("Note: could not apply wake/overlap settings. Run from an")
        print("      elevated (Administrator) shell to enable them.")
    print()
    print("Reliability caveats:")
    print("  - By default this task runs only while you are LOGGED IN. To run")
    print("    while logged out, edit the task and choose 'Run whether user is")
    print("    logged on or not' (requires your account password).")
    print("  - Wake-to-run only works if your power plan allows wake timers")
    print("    (Power Options > Sleep > Allow wake timers).")
    print(f'Remove it later with: python "{SCRIPT_PATH}" --uninstall')
    return 0

def uninstall_task():
    """Remove the Windows Scheduled Task created by --install."""
    if sys.platform != "win32":
        print("--uninstall is only supported on Windows (uses schtasks).")
        return 1

    cmd = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            creationflags=_NO_WINDOW)
    if result.returncode == 0:
        print(f"Removed scheduled task '{TASK_NAME}'.")
    else:
        print("Failed to remove scheduled task (it may not exist):")
        print((result.stderr or result.stdout).strip())
    return result.returncode

def main():
    parser = argparse.ArgumentParser(description="Log internet speed to CSV.")
    parser.add_argument(
        "--interval",
        type=float,
        default=15,
        help="Minutes between tests (default: 15).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single test and exit.",
    )
    parser.add_argument(
        "--server",
        type=int,
        default=None,
        metavar="ID",
        help="Pin tests to a fixed server ID (see --list-servers). Default: "
             "auto-select the best server each run.",
    )
    parser.add_argument(
        "--list-servers",
        action="store_true",
        help="List nearby servers with their IDs and exit.",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Register a Windows Scheduled Task that runs every --interval "
             "minutes (carries over --server if set).",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the scheduled task created by --install.",
    )
    args = parser.parse_args()

    if args.list_servers:
        sys.exit(list_servers())

    if args.uninstall:
        sys.exit(uninstall_task())

    if args.install:
        sys.exit(install_task(args.interval, args.server))

    if args.once:
        run_and_log(args.server)
        return

    interval_seconds = args.interval * 60
    mode = f"server {args.server}" if args.server else "best server"
    print(f"Logging internet speed every {args.interval} min ({mode}) to {LOG_FILE}")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            run_and_log(args.server)
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("\nStopped.")

if __name__ == "__main__":
    main()