#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 XuEimy
"""Foreground iPhone location workflow; all device calls stay on the event loop."""
import argparse
import asyncio
import fcntl
import json
import math
from pathlib import Path
import signal
import sys
import time
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version

DEPENDENCY_VERSION = "11.47.0"


def state_dir():
    base = Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache")
    path = base / "iphone-location-workflow"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def report(root, phase, **fields):
    # No UDID, coordinates, device names, or location history in status files.
    data = {"phase": phase, "updated": time.time(), **fields}
    temp = root / "status.tmp"
    temp.write_text(json.dumps(data))
    temp.replace(root / "status.json")


@contextmanager
def exclusive(root):
    with (root / "session.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A session is active. Run 'stop' before starting or clearing another one.") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def is_running(root):
    try:
        with exclusive(root):
            return False
    except RuntimeError:
        return True


async def clear_and_flush(service, delay=3):
    """clear() is a no-reply message; leave the event loop alive for delivery."""
    await asyncio.wait_for(service.clear(), timeout=15)
    await asyncio.sleep(delay)


async def hold(service, latitude, longitude, stop, interval=3, acknowledged=lambda: None):
    """Always attempt restoration, including on a failed update or cancellation."""
    try:
        while not stop():
            await asyncio.wait_for(service.set(latitude, longitude), timeout=15)
            acknowledged()
            await asyncio.sleep(interval)
    finally:
        await clear_and_flush(service)


def invoke_cli(callback):
    # Typer signals normal completion with SystemExit(0), not a return value.
    try:
        return callback()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)


def run_device(args, root):
    try:
        installed = version("pymobiledevice3")
    except PackageNotFoundError:
        raise RuntimeError("Dependency missing. Run sh setup.sh and use .venv/bin/python.") from None
    if installed != DEPENDENCY_VERSION:
        raise RuntimeError(f"Install requirements.txt; this adapter requires pymobiledevice3=={DEPENDENCY_VERSION}.")
    from pymobiledevice3.cli.cli_common import OSUTILS
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
    from pymobiledevice3.__main__ import main as upstream_main

    original_set = LocationSimulation.set
    original_clear = LocationSimulation.clear
    original_wait = OSUTILS.wait_return
    original_argv = sys.argv

    class Adapter:
        def __init__(self, service):
            self.service = service

        async def set(self, lat, lon):
            await original_set(self.service, lat, lon)

        async def clear(self):
            await original_clear(self.service)

    async def held_set(service, latitude, longitude):
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        updates = 0

        def acknowledged():
            nonlocal updates
            updates += 1
            report(root, "holding", updates=updates)
            if updates == 1:
                print("Coordinates acknowledged. Verify the blue dot in Maps; keep this terminal open.", flush=True)

        try:
            await hold(Adapter(service), latitude, longitude,
                       lambda: stop_event.is_set() or (root / "stop").exists(),
                       interval=args.interval, acknowledged=acknowledged)
        except BaseException:
            report(root, "error", message="Session failed. Run clear and verify the real position in Maps.")
            raise
        else:
            report(root, "clearing")
            print("Updates stopped; initial clear sent. Reconnecting for final clear.", flush=True)
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)

    async def flushed_clear(service):
        await clear_and_flush(Adapter(service))
        report(root, "cleared")
        print("Clear command sent. Reopen Maps and verify your real position.", flush=True)

    # Process-local compatibility adapter; installed upstream files are never edited.
    LocationSimulation.set = held_set
    LocationSimulation.clear = flushed_clear
    OSUTILS.wait_return = lambda: None
    operation = "set" if args.command == "start" else "clear"
    sys.argv = ["pymobiledevice3", "developer", "dvt", "simulate-location", operation,
                "--" + args.transport, "--udid", args.udid]
    if operation == "set":
        sys.argv += ["--", str(args.latitude), str(args.longitude)]
    try:
        return invoke_cli(upstream_main)
    finally:
        LocationSimulation.set = original_set
        LocationSimulation.clear = original_clear
        OSUTILS.wait_return = original_wait
        sys.argv = original_argv


def run_start_and_restore(args, root):
    """Clear again on a fresh connection AFTER the holding connection closes.

    The caller keeps the session lock throughout both calls. Even a successful
    first clear needs the independent pass observed to work on the test device.
    run_device restores its adapter and exits the upstream connection contexts
    before returning, so this invocation creates a new connection to the SAME
    explicitly selected device. No second simulation command is issued.
    """
    try:
        result = run_device(args, root)
    finally:
        report(root, "clearing")
        clear_args = argparse.Namespace(command="clear", udid=args.udid, transport=args.transport)
        try:
            clear_result = run_device(clear_args, root)
        except BaseException:
            report(root, "error", message="Final clear failed. Reconnect the phone and run clear explicitly.")
            raise
        if clear_result:
            report(root, "error", message="Final clear failed. Reconnect the phone and run clear explicitly.")
            raise RuntimeError("Final clear failed. Keep the phone connected and run clear --udid DEVICE_UDID.")
    return result


def stop_session(root, timeout=120):
    if not is_running(root):
        print("No workflow session is running. If Maps is still simulated, use clear --udid DEVICE_UDID.")
        return 1
    (root / "stop").touch()
    deadline = time.monotonic() + timeout
    while is_running(root) and time.monotonic() < deadline:
        time.sleep(0.2)
    if is_running(root):
        raise RuntimeError("Stop timed out. Keep the phone connected; check the active terminal before retrying.")
    status = json.loads((root / "status.json").read_text())
    if status.get("phase") != "cleared":
        raise RuntimeError("The session exited without completing final clear. Run clear --udid DEVICE_UDID.")
    print("Simulation stopped; final clear sent on a new connection. Verify the real position in Maps.")
    return 0


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("start", "clear"):
        command = sub.add_parser(name)
        command.add_argument("--udid", required=True, help="Exact device ID from pymobiledevice3 usbmux list")
        command.add_argument("--transport", choices=("userspace", "native"), default="userspace")
        if name == "start":
            command.add_argument("--latitude", type=float, required=True)
            command.add_argument("--longitude", type=float, required=True)
            command.add_argument("--interval", type=float, default=3)
    sub.add_parser("stop")
    sub.add_parser("status")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.command == "start":
        if not math.isfinite(args.latitude) or not -90 <= args.latitude <= 90:
            p.error("latitude must be finite and between -90 and 90")
        if not math.isfinite(args.longitude) or not -180 <= args.longitude <= 180:
            p.error("longitude must be finite and between -180 and 180")
        if not math.isfinite(args.interval) or not 1 <= args.interval <= 30:
            p.error("interval must be finite and between 1 and 30 seconds")
    root = state_dir()
    if args.command == "status":
        path = root / "status.json"
        print(json.dumps({"running": is_running(root), "last_report": json.loads(path.read_text()) if path.exists() else None}))
        return 0
    if args.command == "stop":
        return stop_session(root)
    with exclusive(root):
        (root / "stop").unlink(missing_ok=True)
        report(root, "connecting")
        try:
            result = run_start_and_restore(args, root) if args.command == "start" else run_device(args, root)
        except BaseException:
            report(root, "error", message="Check connection, then run clear if needed.")
            raise
        if result:
            report(root, "error", message="Device command failed; inspect terminal output.")
        return result


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
