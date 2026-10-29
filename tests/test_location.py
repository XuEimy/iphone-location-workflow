import asyncio
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


if __name__ == "__main__":
    unittest.main()
