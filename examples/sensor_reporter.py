"""Sends this machine's real temperature and load to the brain, so agents can reason about them.

    python3.15 -m examples.sensor_reporter --interval 30
"""

import argparse
import os
import socket
import time
from pathlib import Path

from brain.sdk import BrainClient


def cpu_temperature() -> float | None:
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        try:
            return int((zone / "temp").read_text()) / 1000
        except OSError, ValueError:
            continue
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8484")
    parser.add_argument("--device", default=socket.gethostname())
    parser.add_argument("--interval", type=float, default=30.0, help="Seconds between reports.")
    parser.add_argument("--once", action="store_true", help="Send one report, then stop.")
    args = parser.parse_args()

    brain = BrainClient(args.url)
    while True:
        metrics: dict = {"load": round(os.getloadavg()[0], 2)}
        if (temperature := cpu_temperature()) is not None:
            metrics["cpu_temperature"] = (temperature, "°C")
        device = brain.report(args.device, name=args.device, kind="computer", **metrics)
        print(f"Reported {metrics} as {device['name']}")
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
