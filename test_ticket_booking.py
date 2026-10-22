"""预约关键行为测试；所有网络调用使用模拟对象，不创建真实订单。"""

import json
import tempfile
import unittest
import urllib.parse
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from ticket_booking import (
    Attempt, BackendRejected, DirectClient, MonitorError, TZ, TransportFailure,
    find_existing, has_stock, prepare, read_notice, release_time, submit_once, use_date,
    validate_detail,
)


DAY = "2099-09-17"
SLOT = "06:00-07:30"


def detail(stock=1, **changes):
    result = {
        "id": 1593, "name": "健身票6:00-7:30", "shortName": "jsp6:00-7:30",
        "begintime": "06:00:00", "endtime": "07:30:00", "discountPrice": 0,
        "ifPriorReservation": "1", "priorReservationDays": 1,
        "lastNumArray": [{"day": DAY, "lastNumber": stock}],
    }
    result.update(changes)
    return result


def order(status="0", **changes):
    value = {
        "id": 1, "ticketId": 1593, "useDay": DAY, "shopnum": "0001",
        "name": status, "billnum": "TEST_BILL_ONLY",
    }
    value.update(changes)
    return value


def plan():
    return {"ticket_id": 1593, "use_day": DAY, "slot": SLOT}


class ValidationTests(unittest.TestCase):
    def test_no_stock_precheck_skips_slow_notice_and_order_reads(self):
        client = Mock()
        client.club_code, client.shop_num, client.token = "CLUB", "0001", "TOKEN"
        client.request.side_effect = [[], [{"id": 1593, "name": "健身票6:00-7:30",
                                             "begintime": "06:00:00", "endtime": "07:30:00"}]]
        use_day = "2020-01-01"
        client.detail.return_value = detail(stock=0, lastNumArray=[{"day": use_day, "lastNumber": 0}])
        result = prepare(client, SLOT, use_day)
        self.assertTrue(result["fast_only"])
        self.assertEqual(result["current_rows"][0]["remaining"], 0)
        paths = [call.args[0] for call in client.request.call_args_list]
        self.assertEqual(paths, ["configurable/getConfigInfo", "ticket/getOnlineAllTicketInfo"])
        client.detail.assert_called_once_with(1593)

    def test_pre_release_empty_stock_keeps_full_plan_for_scheduling(self):
        client = Mock()
        client.club_code, client.shop_num, client.token = "CLUB", "0001", "TOKEN"
        client.request.side_effect = [
            [],
            [{"id": 1593, "name": "健身票6:00-7:30", "begintime": "06:00:00", "endtime": "07:30:00"}],
            {"info": "<p>须知</p>", "duration": 0},
            [],
        ]
        client.detail.return_value = detail(stock=0)
        result = prepare(client, SLOT, DAY)
        self.assertFalse(result.get("fast_only", False))
        paths = [call.args[0] for call in client.request.call_args_list]
        self.assertIn("ticket/getSaleTicketExplanation", paths)
        self.assertIn("ticket/getTicketOrders", paths)

    def test_release_uses_previous_day_end_time(self):
        self.assertEqual(release_time(DAY, SLOT).isoformat(), "2099-09-16T07:30:00+08:00")

    def test_millisecond_order_date_uses_shanghai_timezone(self):
        millis = datetime(2099, 9, 17, tzinfo=TZ).timestamp() * 1000
        self.assertEqual(use_date(millis), DAY)

    def test_malformed_order_date_fails_closed(self):
        for value in [None, True, "bad", float("inf"), float("nan")]:
            with self.subTest(value=value), self.assertRaises(MonitorError):
                use_date(value)

    def test_no_ticket_for_different_date(self):
        self.assertFalse(has_stock(detail(), SLOT, "2099-09-18"))

    def test_unknown_stock_is_not_permission_to_submit(self):
        with self.assertRaises(MonitorError):
            has_stock(detail(stock=None), SLOT, DAY)

    def test_paid_or_unknown_price_is_rejected(self):
        for price in [1, -1, None, "NaN", "Infinity"]:
            with self.subTest(price=price), self.assertRaises(MonitorError):
                validate_detail(detail(discountPrice=price), SLOT)

    def test_extra_booking_flows_fail_closed(self):
        for changed in [{"ifAffiliate": "存在附属卡"}, {"ifentrance": "1"},
                        {"priorReservationDays": 2}, {"ticketConfig": {"favorabletype": "2"}}]:
            with self.subTest(changed=changed), self.assertRaises(MonitorError):
                validate_detail(detail(**changed), SLOT)

    def test_notice_change_changes_approval_hash(self):
        client = Mock()
        client.request.side_effect = [
            {"info": "<p>须知 A</p>", "duration": "5"},
            {"info": "<p>须知 B</p>", "duration": "5"},
        ]
        a, b = read_notice(client), read_notice(client)
        self.assertNotEqual(a["hash"], b["hash"])
        self.assertEqual(a["seconds"], 5)


class OrderTests(unittest.TestCase):
    def client(self):
        c = Mock()
        c.shop_num, c.token = "0001", "TEST_TOKEN"
        return c

    def test_active_matching_order_blocks_new_booking(self):
        c = self.client()
        c.request.return_value = [order()]
        self.assertEqual(find_existing(c, 1593, DAY)["billnum"], "TEST_BILL_ONLY")

    def test_refunded_order_does_not_count_as_valid_ticket(self):
        c = self.client()
        c.request.return_value = [order("3")]
        self.assertIsNone(find_existing(c, 1593, DAY))

    def test_unknown_matching_status_still_blocks_duplicates(self):
        c = self.client()
        c.request.return_value = [order("unrecognized")]
        self.assertIsNotNone(find_existing(c, 1593, DAY))

    def test_missing_order_fields_prevent_booking(self):
        c = self.client()
        c.request.return_value = [{"id": 1}]
        with self.assertRaises(MonitorError):
            find_existing(c, 1593, DAY)

    def test_second_page_is_checked(self):
        c = self.client()
        first = [order(id=x, ticketId=1571) for x in range(10)]
        c.request.side_effect = [first, [order()]]
        self.assertIsNotNone(find_existing(c, 1593, DAY))
        self.assertEqual(c.request.call_args.kwargs["pageNo"], 1)

    def test_repeated_pagination_fails_closed(self):
        c = self.client()
        c.request.return_value = [order(id=x, ticketId=1571) for x in range(10)]
        with self.assertRaises(MonitorError):
            find_existing(c, 1593, DAY)


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.attempt = Attempt(self.tmp.name, "TEST_USER_ONLY", 1593, DAY)
        self.client = Mock()
        self.client.detail.return_value = detail()
        self.client.request.return_value = {"info": "<p>测试购票须知</p>", "duration": "5"}
        self.notice = read_notice(self.client)
        self.sleep = Mock()

    def run_submit(self):
        return submit_once(self.client, plan(), self.attempt, self.notice["hash"], self.sleep)

    def test_zero_stock_does_not_create_journal_or_order(self):
        self.client.detail.return_value = detail(stock=0)
        self.assertEqual(self.run_submit()["state"], "no_stock")
        self.client.create.assert_not_called()
        self.assertFalse(self.attempt.path.exists())

    def test_rules_rejection_does_not_submit(self):
        self.client.check_rules.side_effect = BackendRejected("规则未通过")
        with self.assertRaises(BackendRejected):
            self.run_submit()
        self.client.create.assert_not_called()

    def test_changed_notice_requires_new_approval(self):
        self.client.request.return_value = {"info": "新须知", "duration": 5}
        with self.assertRaises(MonitorError):
            self.run_submit()
        self.client.create.assert_not_called()

    def test_countdown_then_stock_loss_does_not_submit(self):
        self.client.detail.side_effect = [detail(), detail(stock=0)]
        self.assertEqual(self.run_submit()["state"], "stock_lost")
        self.sleep.assert_called_once_with(5)
        self.client.create.assert_not_called()

    @patch("ticket_booking.find_existing", return_value={"billnum": "TEST", "status": "0"})
    def test_success_requires_verified_order(self, find):
        def assert_intent_recorded(*args):
            self.assertEqual(json.loads(self.attempt.path.read_text())["state"], "submission_started")
        self.client.create.side_effect = assert_intent_recorded
        result = self.run_submit()
        self.assertEqual(result["state"], "confirmed")
        self.sleep.assert_called_once_with(5)
        self.client.create.assert_called_once_with(1593, DAY)

    @patch("ticket_booking.find_existing", return_value=None)
    def test_timeout_is_not_retried_and_is_recorded_unknown(self, find):
        self.client.create.side_effect = TransportFailure("超时")
        result = self.run_submit()
        self.assertEqual(result["state"], "unknown")
        self.client.create.assert_called_once()
        self.assertEqual(find.call_count, 6)
        with self.assertRaises(MonitorError):
            self.run_submit()
        self.client.create.assert_called_once()

    @patch("ticket_booking.find_existing", return_value=None)
    def test_success_response_without_order_is_not_claimed_success(self, find):
        self.assertEqual(self.run_submit()["state"], "unknown")

    @patch("ticket_booking.find_existing", return_value={"billnum": "TEST", "status": "5"})
    def test_non_unused_order_requires_review(self, find):
        self.assertEqual(self.run_submit()["state"], "order_found_needs_review")

    def test_crash_journal_blocks_restart(self):
        self.attempt.begin(plan())
        with self.assertRaises(MonitorError):
            self.run_submit()
        self.client.create.assert_not_called()

    def test_second_process_lock_is_rejected(self):
        with self.attempt:
            with self.assertRaises(MonitorError):
                with Attempt(self.tmp.name, "TEST_USER_ONLY", 1593, DAY):
                    pass

    def test_journal_does_not_store_credentials(self):
        self.attempt.begin(plan())
        self.assertNotIn("TEST_USER_ONLY", self.attempt.path.read_text())
        self.assertNotIn("token", self.attempt.path.read_text())


class TransportTests(unittest.TestCase):
    def test_create_uses_original_form_protocol_one_zero_price_ticket(self):
        client = DirectClient("TEST_USER_ONLY")
        client.shop_num, client.token = "0001", "TEST_TOKEN"
        connection = Mock()
        response = Mock(status=200, will_close=False)
        response.read.return_value = b'{"msg":"success","data":{}}'
        response.getheader.return_value = "Mon, 14 Sep 2026 03:11:00 GMT"
        connection.getresponse.return_value = response
        client.connection = connection
        client.create(1593, DAY)
        args = connection.request.call_args
        self.assertEqual(args.args[:2], ("POST", "/easyserpClient/ticket/freeBuyTicket"))
        body = urllib.parse.parse_qs(args.kwargs["body"].decode())
        self.assertEqual(json.loads(body["ticketInfo"][0]), [{"ticketId": 1593, "price": 0}])
        self.assertEqual(body["useDay"], [DAY])

    def test_no_arbitrary_endpoint_or_wrong_method(self):
        client = DirectClient("TEST_USER_ONLY")
        for path, method in [("ticket/freeBuyTicket", "GET"), ("unknown", "POST"), ("unknown", "GET")]:
            with self.subTest(path=path, method=method), self.assertRaises(MonitorError):
                client.exchange(path, {}, method)
        self.assertIsNone(client.connection)

    def test_write_transport_failure_is_not_replayed(self):
        client = DirectClient("TEST_USER_ONLY")
        connection = Mock()
        connection.getresponse.side_effect = TimeoutError("TEST_SECRET")
        client.connection = connection
        with self.assertRaises(TransportFailure) as caught:
            client.create(1593, DAY)
        connection.request.assert_called_once()
        self.assertNotIn("TEST_SECRET", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
