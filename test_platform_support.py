"""跨平台文件锁、北京时间与 Windows 启动入口的离线验证。"""

import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from ticket_booking import Attempt, release_time, use_date
from ticket_monitor import booking_timezone


ROOT = Path(__file__).resolve().parent
LOCK_CHILD = """
import os, sys
from ticket_booking import Attempt, MonitorError
try:
    with Attempt(sys.argv[1], "TEST_LOCK_USER", 1, "2099-09-17"):
        if len(sys.argv) > 2:
            os._exit(0)
except MonitorError:
    sys.exit(23)
"""


class PlatformTests(unittest.TestCase):
    def test_missing_timezone_database_keeps_beijing_date_and_release_time(self):
        with patch("ticket_monitor.ZoneInfo", side_effect=ZoneInfoNotFoundError("test")):
            zone = booking_timezone()
        utc = datetime(2099, 9, 16, 17, tzinfo=timezone.utc)
        self.assertEqual(utc.astimezone(zone).isoformat(), "2099-09-17T01:00:00+08:00")
        self.assertEqual(zone.utcoffset(None), timedelta(hours=8))
        with patch("ticket_booking.TZ", zone):
            self.assertEqual(use_date(utc.timestamp() * 1000), "2099-09-17")
            self.assertEqual(release_time("2099-09-17", "06:00-07:30").isoformat(),
                             "2099-09-16T07:30:00+08:00")

    def child(self, directory, crash=False):
        return subprocess.run(
            [sys.executable, "-B", "-S", "-X", "utf8", "-c", LOCK_CHILD, directory]
            + (["crash"] if crash else []),
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", timeout=20,
        )

    def test_other_process_is_blocked_then_can_acquire_released_lock(self):
        with tempfile.TemporaryDirectory(prefix="ontime lock 中文 ") as directory:
            with Attempt(directory, "TEST_LOCK_USER", 1, "2099-09-17"):
                result = self.child(directory)
                self.assertEqual(result.returncode, 23, result.stderr)
            result = self.child(directory)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_process_exit_releases_lock_without_removing_lock_file(self):
        with tempfile.TemporaryDirectory(prefix="ontime lock 中文 ") as directory:
            result = self.child(directory, crash=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with Attempt(directory, "TEST_LOCK_USER", 1, "2099-09-17") as attempt:
                self.assertTrue(attempt.lock_path.exists())

    def test_context_exception_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "test"):
                with Attempt(directory, "TEST_LOCK_USER", 1, "2099-09-17"):
                    raise RuntimeError("test")
            result = self.child(directory)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_lock_io_error_closes_handle_and_does_not_claim_another_booking(self):
        api = "msvcrt.locking" if os.name == "nt" else "fcntl.flock"
        with tempfile.TemporaryDirectory() as directory:
            attempt = Attempt(directory, "TEST_LOCK_USER", 1, "2099-09-17")
            with patch("ticket_booking." + api, side_effect=OSError(errno.EIO, "test")):
                with self.assertRaises(OSError) as caught:
                    attempt.__enter__()
            self.assertEqual(caught.exception.errno, errno.EIO)
            self.assertIsNone(attempt.lock)

    def test_chinese_order_record_survives_atomic_update(self):
        with tempfile.TemporaryDirectory(prefix="ontime journal 中文 ") as directory:
            with Attempt(directory, "TEST_LOCK_USER", 1, "2099-09-17") as attempt:
                attempt.begin({"ticket_id": 1, "use_day": "2099-09-17", "slot": "06:00-07:30"})
                attempt.finish("confirmed", {"billnum": "TEST_ORDER", "name": "模拟凭证"})
                record = json.loads(attempt.path.read_text(encoding="utf-8"))
                self.assertEqual(record["order"]["name"], "模拟凭证")
                self.assertFalse(attempt.path.with_suffix(".tmp").exists())


@unittest.skipUnless(os.name == "nt", "需要原生 Windows 的 cmd.exe")
class WindowsLauncherTests(unittest.TestCase):
    def test_launcher_handles_chinese_space_path_and_help_from_another_directory(self):
        with tempfile.TemporaryDirectory(prefix="ontime 中文 path ") as directory:
            destination = Path(directory)
            for path in [ROOT / "start-ontime.cmd", *ROOT.glob("ticket_*.py")]:
                shutil.copy2(path, destination / path.name)
            result = subprocess.run(
                [os.environ["COMSPEC"], "/d", "/c", "call",
                 str(destination / "start-ontime.cmd"), "--help"],
                input="\n", stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", cwd=ROOT, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("--port", result.stdout)

    def test_missing_python_shows_actionable_message_and_fails(self):
        result = subprocess.run(
            [os.environ["COMSPEC"], "/d", "/c", "call", str(ROOT / "start-ontime.cmd")],
            input="\n", stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", env={**os.environ, "PATH": ""}, timeout=30,
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("Python 3.9 or newer was not found", result.stdout)
        self.assertIn("python.org", result.stdout)
