"""Optional NFC reader diagnostic; does not open hardware on import."""
from __future__ import annotations

import argparse
import logging

try:
    from .config import Settings
except ImportError:  # direct-script execution
    from config import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Print NFC UIDs received from a serial reader")
    defaults = Settings.from_env()
    parser.add_argument("--port", default=defaults.serial_port)
    parser.add_argument("--baud-rate", type=int, default=defaults.baud_rate)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        import serial
    except ImportError:
        parser.error("pyserial is not installed; run python -m pip install -r requirements.txt")

    try:
        with serial.Serial(args.port, args.baud_rate, timeout=1) as reader:
            print(f"Listening on {args.port} at {args.baud_rate} baud. Press Ctrl+C to stop.")
            while True:
                line = reader.readline().decode("utf-8", errors="replace").strip()
                if line:
                    print(line)
    except KeyboardInterrupt:
        print("Reader diagnostic stopped.")
    except OSError as exc:
        logging.error("Could not open/read %s: %s", args.port, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
