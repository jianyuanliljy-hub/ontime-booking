"""站点配置和入口限制的离线回归测试，不连接任何站点。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ticket_config import DEMO_SITE, SiteConfig, load_site
from ticket_monitor import MonitorError, parse_entry_url


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "site.local.json"

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_missing_config_uses_demo(self):
        self.assertEqual(load_site(self.path), DEMO_SITE)

    def test_custom_site_keeps_host_venue_and_club(self):
        self.write({"origin": "https://booking.example.com/", "venue": "测试场馆", "club_id": "7"})
        self.assertEqual(load_site(self.path), SiteConfig("https://booking.example.com", "测试场馆", "7"))

    def test_insecure_or_ambiguous_origin_is_rejected(self):
        for origin in ["http://booking.example.com", "https://user:secret@booking.example.com",
                       "https://booking.example.com/path", "https://booking.example.com?q=x",
                       "https://booking.example.com#entry", "https://booking.example.com:8765",
                       "https://", "not-a-url"]:
            with self.subTest(origin=origin):
                self.write({"origin": origin, "venue": "测试场馆", "club_id": "7"})
                with self.assertRaises(ValueError) as caught:
                    load_site(self.path)
                self.assertNotIn(origin, str(caught.exception))

    def test_invalid_venue_or_club_stops_instead_of_using_defaults(self):
        for changes in [{"venue": " "}, {"club_id": 7}, {"club_id": ""}, {"club_id": "a"}]:
            with self.subTest(changes=changes):
                self.write({"origin": "https://booking.example.com", "venue": "测试场馆",
                            "club_id": "7", **changes})
                with self.assertRaises(ValueError):
                    load_site(self.path)

    def test_malformed_config_does_not_expose_content(self):
        self.path.write_text("PRIVATE_INVALID_CONTENT", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            load_site(self.path)
        self.assertNotIn("PRIVATE_INVALID_CONTENT", str(caught.exception))

    def test_custom_entry_requires_exact_host_and_club(self):
        site = SiteConfig("https://booking.example.com", "测试场馆", "7")
        entry = "/easyserpApp/index.html#/branch?clubId=7&userId=TEST_USER_ONLY"
        with patch("ticket_monitor.SITE", site):
            self.assertEqual(parse_entry_url(site.origin + entry), "TEST_USER_ONLY")
            for origin in ["http://booking.example.com", "https://booking.example.com.evil.example",
                           "https://user@booking.example.com", "https://other.example.com"]:
                with self.subTest(origin=origin), self.assertRaises(MonitorError):
                    parse_entry_url(origin + entry)
            with self.assertRaises(MonitorError):
                parse_entry_url(site.origin + entry.replace("clubId=7", "clubId=26"))
