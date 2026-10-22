"""本地页面服务测试，官网请求与下单均替换为测试对象。"""

import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from ticket_config import SITE

from ticket_booking import LoginRequired, RunCancelled, TZ
from ticket_web import BookingService, LocalServer, UIError, default_overview_date, running_app_url
from test_ticket_booking import DAY, SLOT, detail


ENTRY = SITE.origin + f"/easyserpApp/index.html#/branch?clubId={SITE.club_id}&userId=" + "A" * 32


def checked_plan():
    return {"venue": "目标体育馆", "ticket_id": 1593, "slot": SLOT, "use_day": DAY,
            "quantity": 1, "price": 0, "existing_order": None,
            "notice": {"hash": "TEST_NOTICE", "text": "本地测试须知", "seconds": 5},
            "current_rows": [{"date": DAY, "slot": SLOT, "remaining": 0}],
            "release_at": "2099-09-16T07:30:00+08:00"}


def client_factory(user_id):
    client = Mock(user_id=user_id, token="TEST_PRIVATE_TOKEN", shop_num="0001", timings=[])
    client.request.return_value = [detail()]
    client.detail.return_value = detail()
    return client


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = BookingService(self.tmp.name, client_factory)
        self.addCleanup(self.service.close)

    def wait(self):
        self.service.worker.join(2)
        self.assertFalse(self.service.worker.is_alive())

    def connect(self):
        self.service.connect({"entry": ENTRY})
        self.wait()
        self.assertTrue(self.service.snapshot()["connected"])

    def test_default_overview_moves_to_tomorrow_after_last_slot(self):
        before = datetime(2026, 9, 16, 21, 29, tzinfo=TZ)
        after = datetime(2026, 9, 16, 21, 30, tzinfo=TZ)
        self.assertEqual(default_overview_date(["06:00-07:30", "19:30-21:30"], before).isoformat(), "2026-09-16")
        self.assertEqual(default_overview_date(["06:00-07:30", "19:30-21:30"], after).isoformat(), "2026-09-17")

    def test_connect_publishes_read_only_overview(self):
        self.connect()
        overview = self.service.snapshot()["overview"]
        self.assertEqual(overview["use_day"], self.service.snapshot()["default_date"])
        self.assertEqual(overview["slots"][0]["slot"], SLOT)
        self.assertIsNone(overview["slots"][0]["remaining"])
        self.service.client.create.assert_not_called()

    def test_manual_overview_refresh_never_calls_create(self):
        self.connect()
        self.service.overview({"use_day": DAY})
        self.wait()
        self.assertEqual(self.service.snapshot()["overview"]["use_day"], DAY)
        self.service.client.create.assert_not_called()

    def ready(self):
        self.connect()
        with patch("ticket_web.prepare", return_value=checked_plan()):
            self.service.check({"use_day": DAY, "slot": SLOT})
            self.wait()
        return {"plan_id": self.service.snapshot()["plan_id"], "notice_hash": "TEST_NOTICE", "accept": True}

    def test_connect_does_not_expose_credentials_or_create(self):
        self.connect()
        snapshot = json.dumps(self.service.snapshot())
        self.assertNotIn("A" * 32, snapshot)
        self.assertNotIn("TEST_PRIVATE_TOKEN", snapshot)
        self.service.client.create.assert_not_called()
        self.assertEqual(self.service.snapshot()["slots"], [SLOT])

    def test_invalid_entry_fails_without_constructing_client(self):
        factory = Mock()
        self.service.client_factory = factory
        with self.assertRaises(UIError):
            self.service.connect({"entry": "https://example.com/not-an-entry"})
        factory.assert_not_called()

    def test_expired_login_returns_actionable_state(self):
        client = client_factory("A" * 32)
        client.login.side_effect = LoginRequired("不要输出私密错误信息")
        self.service.client_factory = lambda _: client
        self.service.connect({"entry": ENTRY})
        self.wait()
        state = self.service.snapshot()
        self.assertFalse(state["connected"])
        self.assertEqual(state["job"]["phase"], "login_required")
        self.assertNotIn("不要输出", state["job"]["message"])

    def test_precheck_is_read_only_and_state_survives_page_refresh(self):
        self.ready()
        first = self.service.snapshot()
        second = self.service.snapshot()
        self.assertEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(second["plan"]["notice"]["text"], "本地测试须知")
        self.service.client.create.assert_not_called()

    def test_snapshot_cannot_mutate_live_plan(self):
        self.ready()
        data = self.service.snapshot()
        data["plan"]["price"] = 999
        self.assertEqual(self.service.snapshot()["plan"]["price"], 0)

    def test_schedule_requires_exact_plan_notice_and_explicit_acceptance(self):
        request = self.ready()
        for change in ({"accept": False}, {"accept": "true"}, {"notice_hash": "old"}, {"plan_id": "old"}):
            with self.subTest(change=change), self.assertRaises(UIError):
                self.service.schedule({**request, **change})
        self.service.client.create.assert_not_called()

    def test_stale_precheck_requires_check_again(self):
        request = self.ready()
        self.service.state["checked_at"] = (datetime.now(TZ) - timedelta(minutes=11)).isoformat()
        with self.assertRaisesRegex(UIError, "10 分钟"):
            self.service.schedule(request)

    def test_past_target_cannot_be_scheduled(self):
        self.connect()
        with self.assertRaisesRegex(UIError, "已经开始"):
            self.service.check({"use_day": "2020-01-01", "slot": SLOT})

    def test_waiting_can_cancel_and_cannot_start_duplicate(self):
        request = self.ready()
        started = threading.Event()

        def executor(*args, **kwargs):
            kwargs["on_event"]("waiting", {"release_at": checked_plan()["release_at"]})
            started.set()
            kwargs["cancel_event"].wait(1)
            if kwargs["cancel_event"].is_set():
                raise RunCancelled()
            return {"state": "unknown"}

        with patch("ticket_web.execute", side_effect=executor) as execute:
            self.service.schedule(request)
            self.assertTrue(started.wait(1))
            with self.assertRaises(UIError):
                self.service.schedule(request)
            with self.assertRaises(UIError):
                self.service.connect({"entry": ENTRY})
            self.service.cancel({"job_id": self.service.snapshot()["job"]["id"]})
            self.wait()
            execute.assert_called_once()
        self.assertEqual(self.service.snapshot()["job"]["phase"], "cancelled")

    def test_post_boundary_disables_cancel_before_submission(self):
        request = self.ready()
        crossed = threading.Event()
        finish = threading.Event()

        def executor(*args, **kwargs):
            kwargs["before_submit"]()
            crossed.set()
            finish.wait(1)
            return {"state": "confirmed"}

        with patch("ticket_web.execute", side_effect=executor):
            self.service.schedule(request)
            self.assertTrue(crossed.wait(1))
            try:
                with self.assertRaisesRegex(UIError, "订单核对"):
                    self.service.cancel({"job_id": self.service.snapshot()["job"]["id"]})
                self.assertFalse(self.service.cancel_event.is_set())
            finally:
                finish.set()
                self.wait()

    def test_reconcile_past_date_never_calls_execute(self):
        self.connect()
        with patch("ticket_web.reconcile_saved", return_value={"state": "unknown"}) as reconcile, patch("ticket_web.execute") as execute:
            self.service.reconcile({"use_day": "2020-01-01", "slot": SLOT})
            self.wait()
            reconcile.assert_called_once()
            execute.assert_not_called()
        self.service.client.create.assert_not_called()

    def test_disconnect_forgets_credentials_and_plan(self):
        self.ready()
        self.service.disconnect({})
        self.assertIsNone(self.service.client)
        self.assertFalse(self.service.snapshot()["connected"])
        self.assertIsNone(self.service.snapshot()["plan"])

    def test_reconciliation_result_keeps_its_own_target(self):
        self.ready()
        with patch("ticket_web.reconcile_saved", return_value={"state": "confirmed"}):
            self.service.reconcile({"use_day": "2020-01-01", "slot": SLOT})
            self.wait()
        state = self.service.snapshot()
        self.assertEqual(state["job"]["target"], {"use_day": "2020-01-01", "slot": SLOT})
        self.assertEqual(state["plan"]["use_day"], DAY)

    def test_old_cancel_cannot_stop_new_job(self):
        request = self.ready()
        started, finish = threading.Event(), threading.Event()

        def executor(*args, **kwargs):
            started.set()
            finish.wait(1)
            return {"state": "cancelled"}

        with patch("ticket_web.execute", side_effect=executor):
            self.service.schedule(request)
            self.assertTrue(started.wait(1))
            try:
                with self.assertRaises(UIError):
                    self.service.cancel({"job_id": "previous-job"})
                self.assertFalse(self.service.cancel_event.is_set())
            finally:
                finish.set()
                self.wait()

    def test_diagnostic_failure_does_not_leave_job_running(self):
        with patch("ticket_web.save_diagnostics", side_effect=ValueError("private detail")):
            self.connect()
        state = self.service.snapshot()
        self.assertFalse(state["job"]["active"])
        self.assertEqual(state["job"]["phase"], "connected")
        self.assertNotIn("private detail", json.dumps(state))

    def test_recovery_scan_failure_does_not_leave_job_running(self):
        with patch.object(self.service, "recoveries", side_effect=OSError("private path")):
            self.connect()
        self.assertFalse(self.service.snapshot()["job"]["active"])

    def test_login_loss_while_reconciling_clears_stale_connection(self):
        self.ready()
        with patch("ticket_web.reconcile_saved", return_value={
                "state": "unknown", "requires_login": True, "message": "请重新登录后核对"}):
            self.service.reconcile({"use_day": DAY, "slot": SLOT})
            self.wait()
        state = self.service.snapshot()
        self.assertFalse(state["connected"])
        self.assertIsNone(state["plan_id"])
        self.assertIsNone(state["checked_at"])
        self.assertEqual(state["job"]["phase"], "unknown")

    def test_login_exception_clears_all_plan_metadata(self):
        request = self.ready()
        with patch("ticket_web.execute", side_effect=LoginRequired()):
            self.service.schedule(request)
            self.wait()
        state = self.service.snapshot()
        self.assertIsNone(state["plan_id"])
        self.assertIsNone(state["checked_at"])

    def test_thread_start_failure_does_not_leave_job_running(self):
        self.ready()
        with patch("ticket_web.threading.Thread.start", side_effect=RuntimeError("private error")):
            with self.assertRaises(UIError):
                self.service.check({"use_day": DAY, "slot": SLOT})
        self.assertFalse(self.service.snapshot()["job"]["active"])

    def test_cancellation_before_check_completes_does_not_publish_plan(self):
        self.connect()
        read_started, finish_read = threading.Event(), threading.Event()

        def prepare(*args, **kwargs):
            read_started.set()
            finish_read.wait(1)
            return checked_plan()

        with patch("ticket_web.prepare", side_effect=prepare):
            self.service.check({"use_day": DAY, "slot": SLOT})
            self.assertTrue(read_started.wait(1))
            try:
                self.service.cancel({"job_id": self.service.snapshot()["job"]["id"]})
                self.service.progress("query_retry", {"attempt": 2})
                self.assertEqual(self.service.snapshot()["job"]["phase"], "stopping")
            finally:
                finish_read.set()
                self.wait()
        self.assertIsNone(self.service.snapshot()["plan"])
        self.assertEqual(self.service.snapshot()["job"]["phase"], "cancelled")

    def test_cannot_cancel_a_check_already_published_during_cleanup(self):
        self.connect()
        cleanup_started, finish_cleanup = threading.Event(), threading.Event()

        def save(*args):
            cleanup_started.set()
            finish_cleanup.wait(1)

        with patch("ticket_web.prepare", return_value=checked_plan()), patch("ticket_web.save_diagnostics", side_effect=save):
            self.service.check({"use_day": DAY, "slot": SLOT})
            self.assertTrue(cleanup_started.wait(1))
            try:
                with self.assertRaises(UIError):
                    self.service.cancel({"job_id": self.service.snapshot()["job"]["id"]})
                self.assertIsNotNone(self.service.snapshot()["plan"])
                self.assertFalse(self.service.cancel_event.is_set())
            finally:
                finish_cleanup.set()
                self.wait()
        self.assertEqual(self.service.snapshot()["job"]["phase"], "ready")


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.service = BookingService(cls.tmp.name, client_factory)
        cls.server = LocalServer(("127.0.0.1", 0), cls.service)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(1)
        cls.service.close()
        cls.tmp.cleanup()

    def request(self, path, data=None, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        request_headers = {"Origin": self.origin, "Content-Type": "application/json", "X-CSRF-Token": self.service.csrf}
        request_headers.update(headers or {})
        method = "POST" if data is not None or raw is not None else "GET"
        connection.request(method, path, body=raw if raw is not None else json.dumps(data) if data is not None else None, headers=request_headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_page_assets_load_with_local_only_csp_and_no_cache(self):
        for path in ("/", "/app.js", "/app.css"):
            status, headers, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertGreater(len(body), 100)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertEqual(headers["Cache-Control"], "no-store")

    def test_state_does_not_contain_entry_or_token(self):
        status, _, body = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertNotIn(b"userId", body)
        self.assertNotIn(b"TEST_PRIVATE_TOKEN", body)

    def test_launcher_reuses_existing_local_service(self):
        self.assertEqual(running_app_url(self.server.server_port), self.origin)

    def test_foreign_host_and_origin_cannot_mutate(self):
        for headers in ({"Host": "attacker.example"}, {"Origin": "https://attacker.example"},
                        {"X-CSRF-Token": "wrong"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/connect", {"entry": ENTRY}, headers)[0], 403)

    def test_other_files_are_not_served(self):
        for path in ("/ticket_booking.py", "/../BOOKING_NOTICE.md", "/booking-state", "/api/state?token=x"):
            self.assertEqual(self.request(path)[0], 404)

    def test_invalid_payload_and_content_type_rejected(self):
        self.assertEqual(self.request("/api/connect", raw="not-json")[0], 400)
        self.assertEqual(self.request("/api/connect", raw="[]")[0], 400)
        self.assertEqual(self.request("/api/connect", {}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("/api/connect", raw="x" * 8193)[0], 400)

    def test_non_ascii_csrf_is_rejected_without_crashing_handler(self):
        self.assertEqual(self.request("/api/connect", {}, {"X-CSRF-Token": "é"})[0], 403)

    def test_old_page_cannot_apply_action_to_a_new_connection(self):
        revision = self.service.snapshot().get("revision", 0)
        self.assertEqual(self.request("/api/disconnect", {"revision": revision})[0], 202)
        self.assertEqual(self.request("/api/connect", {"entry": ENTRY, "revision": revision})[0], 409)
        self.assertIsNone(self.service.client)

    def test_dispatch_failure_returns_safe_json(self):
        with patch.object(self.service, "disconnect", side_effect=OSError("TEST_PRIVATE_PATH")):
            status, _, body = self.request("/api/disconnect", {"revision": self.service.snapshot().get("revision", 0)})
        self.assertEqual(status, 500)
        self.assertNotIn(b"TEST_PRIVATE_PATH", body)
        self.assertIn("error", json.loads(body))


if __name__ == "__main__":
    unittest.main()
