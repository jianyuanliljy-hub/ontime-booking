"""拥堵与丢响应测试。仅用模拟对象及本机 socketpair，不访问官网。"""

import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ticket_booking import (
    Attempt, BackendRejected, DirectClient, LoginRequired, MonitorError,
    QueryDeadline, RunCancelled, execute, main, read_notice, read_retry,
    reconcile_order, reconcile_saved, retry_after_seconds, save_diagnostics, submit_once,
)
from ticket_booking import TransportFailure, TZ
from test_ticket_booking import DAY, SLOT, detail, plan


PATH = "datediscount/getTbTicketInfoById"


def connection_for(status=200, payload=None, extra_headers=None):
    response = Mock(status=status, will_close=False)
    response.read.return_value = json.dumps(payload if payload is not None else {
        "msg": "success", "data": detail(),
    }).encode()
    headers = {"Date": "Mon, 14 Sep 2026 03:11:00 GMT", **(extra_headers or {})}
    response.getheader.side_effect = headers.get
    connection = Mock()
    connection.getresponse.return_value = response
    return connection


class RetryTests(unittest.TestCase):
    def test_retry_after_seconds_and_server_relative_http_date(self):
        self.assertEqual(retry_after_seconds("12"), 12)
        self.assertEqual(retry_after_seconds("Mon, 14 Sep 2026 03:11:12 GMT",
                                             "Mon, 14 Sep 2026 03:11:00 GMT"), 12)
        for value in (None, "-1", "garbage"):
            self.assertEqual(retry_after_seconds(value), 0)

    def test_transient_read_retries_and_restores_context(self):
        client = SimpleNamespace(query_deadline=None, query_only=False)
        operation = Mock(side_effect=[TransportFailure("临时故障"), {"ok": True}])
        sleep = Mock()
        self.assertEqual(read_retry(client, operation, sleep=sleep), {"ok": True})
        self.assertEqual(operation.call_count, 2)
        self.assertGreaterEqual(sleep.call_args.args[0], 0.5)
        self.assertIsNone(client.query_deadline)
        self.assertFalse(client.query_only)

    def test_retry_after_is_never_shortened(self):
        operation = Mock(side_effect=[TransportFailure("忙", retry_after=12), 1])
        sleep = Mock()
        self.assertEqual(read_retry(SimpleNamespace(), operation, sleep=sleep), 1)
        sleep.assert_called_once_with(12)

    def test_retry_after_beyond_window_stops_without_second_request(self):
        operation = Mock(side_effect=TransportFailure("忙", retry_after=120))
        sleep = Mock()
        with self.assertRaises(QueryDeadline):
            read_retry(SimpleNamespace(), operation, deadline=time.monotonic() + 1, sleep=sleep)
        operation.assert_called_once()
        sleep.assert_not_called()

    def test_business_auth_and_permanent_errors_are_not_retried(self):
        for failure in (BackendRejected("拒绝"), LoginRequired("失效"),
                        TransportFailure("证书", retryable=False)):
            with self.subTest(failure=type(failure).__name__):
                operation = Mock(side_effect=failure)
                with self.assertRaises(type(failure)):
                    read_retry(SimpleNamespace(), operation, sleep=Mock())
                operation.assert_called_once()

    def test_query_context_cannot_post(self):
        client = DirectClient("TEST_USER")
        with self.assertRaisesRegex(MonitorError, "禁止创建"):
            read_retry(client, lambda: client.create(1593, DAY))
        self.assertIsNone(client.connection)
        self.assertEqual(client.timings, [])

    def test_expired_deadline_sends_nothing(self):
        client = DirectClient("TEST_USER")
        client.query_deadline = time.monotonic() - 1
        with self.assertRaises(QueryDeadline):
            client.detail(1593)
        self.assertIsNone(client.connection)

    def test_cancel_during_backoff_stops_before_retry(self):
        cancel = Mock()
        cancel.is_set.side_effect = [False, False, True]
        operation = Mock(side_effect=TransportFailure("超时"))
        with self.assertRaises(RunCancelled):
            read_retry(SimpleNamespace(), operation, cancel_event=cancel)
        operation.assert_called_once()
        cancel.wait.assert_called_once()


class NetworkTests(unittest.TestCase):
    def test_reuses_connection_but_refreshes_socket_timeout(self):
        client = DirectClient("TEST_USER")
        connection = connection_for()
        client.connection = connection
        client.detail(1593)
        client.query_deadline = time.monotonic() + 0.5
        client.detail(1593)
        values = [call.args[0] for call in connection.sock.settimeout.call_args_list]
        self.assertGreater(values[0], 3)
        self.assertLessEqual(values[1], 0.5)
        self.assertEqual(connection.request.call_count, 2)
        connection.connect.assert_not_called()

    def test_failure_discards_connection_then_read_recovers(self):
        for failure in (TimeoutError("SECRET"), socket.timeout("SECRET"), ConnectionResetError("SECRET"),
                        http.client.RemoteDisconnected("SECRET")):
            with self.subTest(failure=type(failure).__name__):
                client = DirectClient("TEST_USER")
                broken = connection_for()
                broken.getresponse.side_effect = failure
                client.connection = broken
                good = connection_for()
                with patch("ticket_booking.http.client.HTTPSConnection", return_value=good):
                    self.assertEqual(read_retry(client, lambda: client.detail(1593), sleep=Mock())["id"], 1593)
                broken.close.assert_called_once()
                self.assertEqual(len(client.timings), 2)
                self.assertEqual(client.timings[0]["phase"], "headers")
                expected = "timeout" if isinstance(failure, (TimeoutError, socket.timeout)) else "transport_error"
                self.assertEqual(client.timings[0]["outcome"], expected)
                self.assertNotIn("SECRET", json.dumps(client.timings))
                client.close()

    def test_busy_http_status_retries_only_reads(self):
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                client = DirectClient("TEST_USER")
                client.connection = connection_for(status)
                with patch("ticket_booking.http.client.HTTPSConnection", return_value=connection_for()):
                    self.assertEqual(read_retry(client, lambda: client.detail(1593), sleep=Mock())["id"], 1593)
                self.assertEqual([r["http_status"] for r in client.timings], [status, 200])
                client.close()

    def test_login_markers_stop_without_retry(self):
        for status, payload in ((401, None), (403, None), (200, -1), (200, "-1")):
            with self.subTest(status=status, payload=payload):
                client = DirectClient("TEST_USER")
                connection = connection_for(status, payload)
                client.connection = connection
                with self.assertRaises(LoginRequired):
                    read_retry(client, lambda: client.detail(1593), sleep=Mock())
                connection.request.assert_called_once()

    def test_redirect_and_other_4xx_are_not_retried(self):
        for status in (302, 400, 404):
            client = DirectClient("TEST_USER")
            connection = connection_for(status)
            client.connection = connection
            with self.assertRaises(TransportFailure) as caught:
                read_retry(client, lambda: client.detail(1593), sleep=Mock())
            self.assertFalse(caught.exception.retryable)
            connection.request.assert_called_once()

    def test_retry_after_survives_change_of_query_stage(self):
        client = DirectClient("TEST_USER")
        connection = connection_for(503, extra_headers={"Retry-After": "20"})
        client.connection = connection
        with self.assertRaises(TransportFailure):
            client.detail(1593)
        connection.getresponse.return_value.read.assert_not_called()
        with patch("ticket_booking.http.client.HTTPSConnection") as factory:
            with self.assertRaises(QueryDeadline):
                read_retry(client, lambda: client.request("ticket/getTicketOrders"),
                           deadline=time.monotonic() + 1)
            with self.assertRaises(TransportFailure):
                client.create(1593, DAY)
            factory.assert_not_called()
        self.assertEqual(client.timings[0]["retry_after_seconds"], 20)

    def test_diagnostics_preserve_failures_without_credentials(self):
        client = DirectClient("SECRET_USER")
        client.token = "SECRET_TOKEN"
        connection = connection_for()
        connection.getresponse.side_effect = TimeoutError("SECRET_TOKEN")
        client.connection = connection
        with self.assertRaises(TransportFailure):
            client.detail(1593)
        with tempfile.TemporaryDirectory() as directory:
            path = save_diagnostics(client, directory, "error")
            data = path.read_text()
            self.assertNotIn("SECRET", data)
            self.assertNotIn("userId", data)
            self.assertEqual(json.loads(data)["timings"][0]["outcome"], "timeout")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(path, save_diagnostics(client, directory, "error"))

    def run_slow_socket(self, script):
        local, remote = socket.socketpair()
        connection = http.client.HTTPConnection("local-test-only")
        connection.sock = local
        client = DirectClient("TEST_USER")
        client.connection = connection
        client.timeout_for = lambda path, method: 0.18

        def respond():
            try:
                remote.recv(65536)
                script(remote)
            except OSError:
                pass
            finally:
                remote.close()

        worker = threading.Thread(target=respond, daemon=True)
        worker.start()
        try:
            started = time.monotonic()
            with self.assertRaises(TransportFailure):
                client.detail(1593)
            self.assertLess(time.monotonic() - started, 0.8)
            self.assertIsNone(client.connection)
            return client.timings[0]
        finally:
            client.close()
            worker.join(timeout=1)

    def test_hanging_response_headers_are_interrupted(self):
        record = self.run_slow_socket(lambda remote: time.sleep(0.4))
        self.assertEqual(record["phase"], "headers")
        self.assertEqual(record["outcome"], "timeout")

    def test_slow_chunked_response_cannot_extend_total_budget(self):
        def drip(remote):
            remote.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            for _ in range(12):
                remote.sendall(b"1\r\nx\r\n")
                time.sleep(0.045)
        record = self.run_slow_socket(drip)
        self.assertEqual(record["phase"], "body")
        self.assertEqual(record["outcome"], "timeout")


class SubmissionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = Mock(user_id="TEST_USER", token="TEST_TOKEN")
        self.client.detail.return_value = detail()
        self.client.request.return_value = {"info": "测试须知", "duration": "5"}
        self.notice = read_notice(self.client)
        self.attempt = Attempt(self.tmp.name, "TEST_USER", 1593, DAY)

    @patch("ticket_booking.find_existing")
    def test_order_becomes_visible_after_lost_create_response(self, find):
        self.client.create.side_effect = TransportFailure("服务器已经执行，但响应丢失")
        find.side_effect = [None, TransportFailure("订单查询拥堵"), None,
                            {"billnum": "TEST_BILL", "status": "0"}]
        result = submit_once(self.client, plan(), self.attempt, self.notice["hash"], sleep=Mock())
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(find.call_count, 4)
        self.client.create.assert_called_once()
        self.assertEqual(json.loads(self.attempt.path.read_text())["state"], "confirmed")

    def test_cancel_during_notice_prevents_create_and_journal(self):
        cancel = threading.Event()
        cancel.wait = lambda seconds: cancel.set()
        with self.assertRaises(RunCancelled):
            submit_once(self.client, plan(), self.attempt, self.notice["hash"],
                        sleep=Mock(), cancel_event=cancel)
        self.client.create.assert_not_called()
        self.assertFalse(self.attempt.path.exists())

    @patch("ticket_booking.find_existing", return_value={"billnum": "TEST_BILL", "status": "0"})
    def test_cancel_after_create_still_checks_result(self, find):
        cancel = threading.Event()
        self.client.request.return_value = {"info": "测试须知", "duration": "0"}
        notice = read_notice(self.client)
        self.client.create.side_effect = lambda *args: cancel.set()
        result = submit_once(self.client, plan(), self.attempt, notice["hash"],
                             sleep=Mock(), cancel_event=cancel)
        self.assertEqual(result["state"], "confirmed")
        self.client.create.assert_called_once()

    @patch("ticket_booking.find_existing", side_effect=LoginRequired("失效"))
    def test_login_loss_during_confirmation_preserves_unknown(self, find):
        self.attempt.begin(plan())
        result = reconcile_order(self.client, plan(), self.attempt, sleep=Mock())
        self.assertEqual(result["state"], "unknown")
        self.assertIn("登录失效", result["message"])
        self.assertTrue(result["requires_login"])
        find.assert_called_once()
        self.client.create.assert_not_called()

    @patch("ticket_booking.find_existing", return_value={"billnum": "TEST_BILL", "status": "0"})
    def test_unexpected_create_error_still_reconciles_once(self, find):
        self.client.create.side_effect = RuntimeError("TEST_PRIVATE_RESPONSE")
        result = submit_once(self.client, plan(), self.attempt, self.notice["hash"], sleep=Mock())
        self.assertEqual(result["state"], "confirmed")
        self.client.create.assert_called_once()
        self.assertNotIn("TEST_PRIVATE_RESPONSE", json.dumps(result))

    def test_last_stock_request_crossing_start_time_never_submits(self):
        before = datetime(2099, 9, 17, 5, 59, 59, tzinfo=TZ)
        after = before + timedelta(seconds=2)
        with patch("ticket_booking.datetime") as clock:
            clock.fromisoformat.side_effect = datetime.fromisoformat
            clock.now.side_effect = [before, before, after, after, after]
            with self.assertRaisesRegex(MonitorError, "场次已经开始"):
                submit_once(self.client, plan(), self.attempt, self.notice["hash"], sleep=Mock())
        self.client.create.assert_not_called()
        self.assertFalse(self.attempt.path.exists())

    @patch("ticket_booking.find_existing", side_effect=TransportFailure("证书错误", retryable=False))
    def test_permanent_confirmation_failure_stops_all_rounds(self, find):
        self.attempt.begin(plan())
        sleep = Mock()
        result = reconcile_order(self.client, plan(), self.attempt, sleep=sleep)
        self.assertEqual(result["state"], "unknown")
        find.assert_called_once()
        sleep.assert_not_called()
        self.client.create.assert_not_called()

    @patch("ticket_booking.find_existing", side_effect=TransportFailure("等待", retry_after=120))
    def test_retry_after_beyond_confirmation_budget_ends_recovery(self, find):
        self.attempt.begin(plan())
        sleep = Mock()
        result = reconcile_order(self.client, plan(), self.attempt, sleep=sleep)
        self.assertEqual(result["state"], "unknown")
        self.assertIn("更久的等待", result["message"])
        find.assert_called_once()
        sleep.assert_not_called()
        self.client.create.assert_not_called()

    @patch("ticket_booking.find_existing", return_value={"billnum": "TEST_BILL", "status": "0"})
    def test_restart_can_only_reconcile_saved_attempt(self, find):
        self.attempt.begin(plan())
        self.attempt.finish("unknown")
        result = reconcile_saved(self.client, self.tmp.name, SLOT, DAY)
        self.assertEqual(result["state"], "confirmed")
        self.client.create.assert_not_called()
        self.client.login.assert_called_once()

    def test_changed_entry_or_missing_journal_never_creates(self):
        self.attempt.begin(plan())
        self.client.user_id = "DIFFERENT_USER"
        with self.assertRaises(MonitorError):
            reconcile_saved(self.client, self.tmp.name, SLOT, DAY)
        self.client.create.assert_not_called()
        self.client.login.assert_not_called()

    @patch("ticket_booking.wait_until")
    @patch("ticket_booking.find_existing", return_value=None)
    def test_warmup_failure_is_not_mistaken_for_sold_out(self, find, wait):
        self.client.detail.side_effect = [TransportFailure("预热超时"), QueryDeadline("窗口结束")]
        prepared = {**plan(), "existing_order": None, "notice": self.notice,
                    "release_at": "2099-09-16T07:30:00+08:00"}
        result = execute(self.client, prepared, self.notice["hash"], self.tmp.name)
        self.assertEqual(result["state"], "query_unavailable")
        self.client.create.assert_not_called()

    @patch("ticket_booking.reconcile_saved", return_value={"state": "confirmed"})
    @patch("ticket_booking.prepare")
    @patch("ticket_booking.DirectClient")
    @patch("ticket_booking.parse_entry_url", return_value="TEST_USER")
    @patch("ticket_booking.getpass.getpass", return_value="TEST_ENTRY")
    def test_cli_reconcile_accepts_past_date_without_preparing_or_creating(self, getpass, parse, factory, prepare, recover):
        factory.return_value.timings = []
        args = ["booking", "--date", "2020-01-01", "--reconcile-only",
                "--state-dir", self.tmp.name, "--diagnostics-dir", self.tmp.name]
        with patch("sys.argv", args):
            self.assertEqual(main(), 0)
        prepare.assert_not_called()
        factory.return_value.create.assert_not_called()
        recover.assert_called_once()


if __name__ == "__main__":
    unittest.main()
