"""Host-side serial bridge: reads the Arduino RFID UID and forwards it to the Docker web app.

This process deliberately stays on the host because Docker Desktop on Windows cannot
reliably access a USB serial port as a normal Linux container device.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

UID_LABEL = re.compile(r"\bUID\b\s*[:=]\s*(.*)$", re.IGNORECASE)
HEX_UID = re.compile(r"^[0-9A-F]+$")


def parse_uid_line(raw: str | bytes) -> str | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    value = str(raw).strip()
    if not value or value.casefold() in {"nfc reader ready", "reader ready", "ready"}:
        return None
    match = UID_LABEL.search(value)
    if match:
        value = match.group(1).strip()
    value = re.sub(r"[\s:-]+", "", value).upper()
    return value if HEX_UID.fullmatch(value) else None


def send_scan(url: str, token: str, uid: str, timeout: float = 5.0) -> dict:
    payload = json.dumps({"uid": uid}).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/api/hardware/scan", data=payload, method="POST",
        headers={"Content-Type": "application/json", "X-Bridge-Token": token},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def load_simple_env(path: Path) -> None:
    """Read only the bridge's simple KEY=VALUE settings without a dotenv dependency."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('\"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def main() -> int:
    load_simple_env(Path(__file__).resolve().parent / ".env")
    parser = argparse.ArgumentParser(description="Forward Arduino RC522 scans to the NFC Attendance web app")
    parser.add_argument("--port", default=os.getenv("NFC_SERIAL_PORT", "COM7"), help="Arduino serial port (default: COM7)")
    parser.add_argument("--baud-rate", type=int, default=int(os.getenv("NFC_BAUD_RATE", "9600")))
    parser.add_argument("--url", default=os.getenv("ATTENDANCE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.getenv("BRIDGE_API_TOKEN", ""))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not args.token:
        parser.error("Set BRIDGE_API_TOKEN in your environment or pass --token (must match the web app .env).")
    try:
        import serial
    except ImportError:
        logging.error("pyserial is missing. Run: python -m pip install -r requirements-bridge.txt")
        return 2
    logging.info("Connecting to Arduino on %s at %s baud", args.port, args.baud_rate)
    logging.info("Forwarding scans to %s; keep the Arduino Serial Monitor closed", args.url)
    last_uid, last_at = "", 0.0
    try:
        with serial.Serial(args.port, args.baud_rate, timeout=1) as device:
            time.sleep(2.0)  # Arduino boards often reset when serial is opened.
            while True:
                line = device.readline()
                if not line:
                    continue
                uid = parse_uid_line(line)
                if not uid:
                    logging.debug("Ignoring serial message: %r", line)
                    continue
                now = time.monotonic()
                if uid == last_uid and now - last_at < 2.0:
                    continue
                last_uid, last_at = uid, now
                try:
                    result = send_scan(args.url, args.token, uid)
                    logging.info("Tag %s forwarded: %s", uid, result.get("status", "accepted"))
                except urllib.error.HTTPError as exc:
                    detail = exc.read().decode("utf-8", errors="replace")
                    logging.error("Web app returned HTTP %s: %s", exc.code, detail)
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                    logging.error("Could not reach the web app: %s", exc)
    except KeyboardInterrupt:
        logging.info("Bridge stopped.")
        return 0
    except serial.SerialException as exc:
        logging.error("Could not use serial port %s: %s. Close Arduino Serial Monitor and verify the COM port.", args.port, exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
