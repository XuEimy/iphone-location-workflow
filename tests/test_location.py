import asyncio
import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import location


class HoldTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_delivers_updates_and_clears_once(self):
        service = type("Service", (), {"set": AsyncMock(), "clear": AsyncMock()})()
        count = 0

        def ack():
            nonlocal count
            count += 1

        with patch("location.asyncio.sleep", new_callable=AsyncMock):
            await location.hold(service, 1, 2, lambda: count == 3, acknowledged=ack)
        self.assertEqual(service.set.await_count, 3)
        service.clear.assert_awaited_once()

    async def test_failed_update_still_clears(self):
        service = type("Service", (), {"set": AsyncMock(side_effect=OSError("disconnected")), "clear": AsyncMock()})()
        with patch("location.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(OSError):
                await location.hold(service, 1, 2, lambda: False)
        service.clear.assert_awaited_once()

    async def test_cancel_still_clears(self):
        service = type("Service", (), {"set": AsyncMock(side_effect=asyncio.CancelledError), "clear": AsyncMock()})()
        with patch("location.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(asyncio.CancelledError):
                await location.hold(service, 1, 2, lambda: False)
        service.clear.assert_awaited_once()

    async def test_clear_failure_is_not_success(self):
        service = type("Service", (), {"clear": AsyncMock(side_effect=OSError("disconnected"))})()
        with self.assertRaises(OSError):
            await location.clear_and_flush(service)

    async def test_clear_keeps_event_loop_alive_for_delivery(self):
        events = []
        service = type("Service", (), {"clear": AsyncMock(side_effect=lambda: events.append("clear"))})()

        async def sleep(delay):
            self.assertEqual(delay, 3)
            events.append("flush")

        with patch("location.asyncio.sleep", sleep):
            await location.clear_and_flush(service)
        self.assertEqual(events, ["clear", "flush"])


class SessionTests(unittest.TestCase):
    def test_upstream_success_exit_is_not_recorded_as_failure(self):
        def upstream():
            raise SystemExit(0)
        self.assertEqual(location.invoke_cli(upstream), 0)

    def test_second_session_cannot_start(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with location.exclusive(root):
                self.assertTrue(location.is_running(root))
                with self.assertRaises(RuntimeError):
                    with location.exclusive(root):
                        self.fail("lock should reject a second session")
            self.assertFalse(location.is_running(root))

    def test_invalid_coordinates_never_access_a_device(self):
        for latitude, longitude in [("nan", "0"), ("0", "inf"), ("91", "0"), ("0", "181")]:
            with self.subTest(latitude=latitude, longitude=longitude), patch("location.run_device") as run:
                with self.assertRaises(SystemExit):
                    location.main(["start", "--udid", "TEST", "--latitude", latitude, "--longitude", longitude])
                run.assert_not_called()

    def test_inactive_stop_does_not_claim_restoration(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(location.stop_session(Path(folder)), 1)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = argparse.Namespace(command="start", udid="TEST-DEVICE", transport="userspace")

    def test_successful_first_clear_still_gets_independent_clear_under_lock(self):
        calls = []

        def device(args, root):
            self.assertTrue(location.is_running(root))
            calls.append(args.command)
            self.assertEqual(args.udid, "TEST-DEVICE")
            self.assertEqual(args.transport, "userspace")
            if args.command == "clear":
                # The hold invocation has returned before the next one begins.
                self.assertEqual(calls, ["start", "clear"])
                self.assertEqual(json.loads((root / "status.json").read_text())["phase"], "clearing")
                location.report(root, "cleared")
            return 0

        with location.exclusive(self.root), patch("location.run_device", side_effect=device):
            self.assertEqual(location.run_start_and_restore(self.args, self.root), 0)
        self.assertEqual(calls, ["start", "clear"])

    def test_failed_hold_still_attempts_final_clear_and_preserves_failure(self):
        with patch("location.run_device", side_effect=[1, 0]) as device:
            self.assertEqual(location.run_start_and_restore(self.args, self.root), 1)
        self.assertEqual([call.args[0].command for call in device.call_args_list], ["start", "clear"])

    def test_hold_exception_still_attempts_final_clear(self):
        with patch("location.run_device", side_effect=[OSError("lost transport"), 0]) as device:
            with self.assertRaisesRegex(OSError, "lost transport"):
                location.run_start_and_restore(self.args, self.root)
        self.assertEqual(device.call_args_list[1].args[0].command, "clear")

    def test_final_clear_failure_cannot_report_success(self):
        with patch("location.run_device", side_effect=[0, 1]):
            with self.assertRaisesRegex(RuntimeError, "Final clear failed"):
                location.run_start_and_restore(self.args, self.root)
        self.assertEqual(json.loads((self.root / "status.json").read_text())["phase"], "error")

    def test_final_clear_exception_cannot_report_success(self):
        with patch("location.run_device", side_effect=[0, OSError("phone absent")]):
            with self.assertRaises(OSError):
                location.run_start_and_restore(self.args, self.root)
        self.assertEqual(json.loads((self.root / "status.json").read_text())["phase"], "error")

    def test_main_routes_start_through_final_clear(self):
        with patch("location.state_dir", return_value=self.root), patch("location.run_device", return_value=0) as device:
            self.assertEqual(location.main(["start", "--udid", "TEST-DEVICE", "--latitude", "1", "--longitude", "2"]), 0)
        self.assertEqual([call.args[0].command for call in device.call_args_list], ["start", "clear"])

    def test_standalone_clear_does_not_start_simulation_or_recurse(self):
        with patch("location.state_dir", return_value=self.root), patch("location.run_device", return_value=0) as device:
            self.assertEqual(location.main(["clear", "--udid", "TEST-DEVICE"]), 0)
        self.assertEqual([call.args[0].command for call in device.call_args_list], ["clear"])

    def test_stop_waits_through_final_clear_before_returning(self):
        location.report(self.root, "cleared")
        # Session lock remains held during both cleanup stages.
        with patch("location.is_running", side_effect=[True, True, True, False, False]) as running, patch("location.time.sleep") as sleep:
            self.assertEqual(location.stop_session(self.root), 0)
        self.assertEqual(running.call_count, 5)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue((self.root / "stop").exists())

    def test_stop_rejects_incomplete_final_clear(self):
        location.report(self.root, "error")
        with patch("location.is_running", side_effect=[True, False, False]):
            with self.assertRaisesRegex(RuntimeError, "without completing final clear"):
                location.stop_session(self.root)


if __name__ == "__main__":
    unittest.main()
