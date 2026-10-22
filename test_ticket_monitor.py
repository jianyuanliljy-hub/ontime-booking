"""验证会影响监票判断的边界情况；测试不访问网站。"""

import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest.mock import Mock, patch

from ticket_config import SITE

from ticket_monitor import (
    MonitorError, NoRedirect, TicketClient, normalize_slot,
    parse_entry_url, parse_remaining, signature, ticket_rows,
)


def detail(**changes):
    value = {
        "id": 1593, "name": "健身票6:00-7:30", "shortName": "jsp6:00-7:30",
        "begintime": "06:00:00", "endtime": "07:30:00", "discountPrice": 0,
        "lastNumArray": [{"day": "2026-09-15", "lastNumber": 0}],
    }
    value.update(changes)
    return value


class StockTests(unittest.TestCase):
    def test_zero_is_distinct_from_unknown(self):
        self.assertEqual(parse_remaining(0), 0)
        self.assertEqual(parse_remaining("0"), 0)
        self.assertEqual(parse_remaining("12"), 12)
        for value in [None, "", "未知", -1, "-1", True, 0.5, {}, []]:
            with self.subTest(value=value):
                self.assertIsNone(parse_remaining(value))

    def test_missing_stock_does_not_become_sold_out(self):
        rows = ticket_rows(detail(lastNumArray=[{"day": "2026-09-15"}]))
        self.assertIsNone(rows[0]["remaining"])

    def test_missing_date_cannot_report_available(self):
        self.assertIsNone(ticket_rows(detail(lastNumArray=[{"lastNumber": 10}]))[0]["remaining"])

    def test_empty_calendar_is_unknown(self):
        row = ticket_rows(detail(lastNumArray=[]))[0]
        self.assertIsNone(row["remaining"])
        self.assertIsNone(row["date"])
        self.assertTrue(row["warnings"])

    def test_multiple_days_are_preserved(self):
        days = [{"day": "2026-09-15", "lastNumber": 0}, {"day": "2026-09-16", "lastNumber": 2}]
        rows = ticket_rows(detail(lastNumArray=days))
        self.assertEqual([(r["date"], r["remaining"]) for r in rows],
                         [("2026-09-15", 0), ("2026-09-16", 2)])

    def test_conflicting_ticket_name_uses_actual_time_and_warns(self):
        row = ticket_rows(detail(name="健身票13:00-15:20", begintime="13:00:00", endtime="15:30:00"))[0]
        self.assertEqual(row["slot"], "13:00-15:30")
        self.assertTrue(row["warnings"])

    def test_bad_date_is_an_error(self):
        with self.assertRaises(MonitorError):
            ticket_rows(detail(lastNumArray=[{"day": "2026-02-30", "lastNumber": 2}]))

    def test_time_filter_is_normalized_and_validated(self):
        self.assertEqual(normalize_slot("6:00--7:30"), "06:00-07:30")
        for value in ["24:00-25:00", "6:70-7:30", "7:30-6:00", "6-7"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_slot(value)

    def test_changes_include_date_but_ignore_check_timestamp(self):
        first = {"checked_at": "a", "tickets": ticket_rows(detail())}
        second = {**first, "checked_at": "b"}
        self.assertEqual(signature(first), signature(second))
        second = {"checked_at": "b", "tickets": ticket_rows(detail(lastNumArray=[{"day": "2026-09-16", "lastNumber": 0}]))}
        self.assertNotEqual(signature(first), signature(second))


class ClientTests(unittest.TestCase):
    def test_entry_only_accepts_expected_site(self):
        host = urllib.parse.urlsplit(SITE.origin).netloc
        query = f"#/branch?clubId={SITE.club_id}&userId=TEST_USER_ONLY"
        self.assertEqual(parse_entry_url(SITE.origin + "/easyserpApp/index.html" + query), "TEST_USER_ONLY")
        for root in [f"http://{host}", "https://evil.example", f"https://{host}.evil.example", f"https://user@{host}"]:
            with self.subTest(root=root), self.assertRaises(MonitorError):
                parse_entry_url(root + "/easyserpApp/index.html" + query)

    def test_non_query_route_is_rejected_before_network(self):
        client = TicketClient("TEST_USER_ONLY")
        client.opener = Mock()
        with self.assertRaises(MonitorError):
            client.request("unapproved/submit")
        client.opener.open.assert_not_called()

    def test_redirect_does_not_forward_credentials(self):
        with self.assertRaises(MonitorError):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com")

    def test_network_error_does_not_expose_url_credentials(self):
        client = TicketClient("TEST_USER_ONLY")
        client.opener = Mock()
        client.opener.open.side_effect = urllib.error.URLError("https://example.com/?token=DO_NOT_PRINT")
        with self.assertRaises(MonitorError) as caught:
            client.request("ticket/getOnlineAllTicketInfo", token="DO_NOT_PRINT")
        self.assertNotIn("DO_NOT_PRINT", str(caught.exception))

    def test_login_error_is_not_a_zero_stock_result(self):
        client = TicketClient("TEST_USER_ONLY")
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(json.dumps({"msg": "fail", "data": "secret"}).encode())
        with self.assertRaises(MonitorError):
            client.request("ticket/getOnlineAllTicketInfo")

    @patch("ticket_monitor.time.sleep")
    def test_date_filter_does_not_confuse_today_and_tomorrow(self, sleep):
        client = TicketClient("TEST_USER_ONLY")
        client.token, client.shop_num = "test", "0001"
        client.request = Mock(side_effect=[[{"id": 1593, "name": "健身票6:00-7:30"}], detail()])
        snapshot = client.snapshot(["06:00-07:30"], "2026-09-17")
        self.assertEqual(snapshot["tickets"], [])
        self.assertEqual(snapshot["returned_dates"], ["2026-09-15"])
        self.assertTrue(snapshot["notes"])

    def test_empty_ticket_list_is_not_sold_out(self):
        client = TicketClient("TEST_USER_ONLY")
        client.token = "test"
        client.request = Mock(return_value=[])
        with self.assertRaises(MonitorError):
            client.snapshot()


if __name__ == "__main__":
    unittest.main()
