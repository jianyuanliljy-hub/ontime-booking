#!/usr/bin/env python3
"""仅用于界面验收的离线服务。不会连接官网；测试状态保存在临时目录。"""

import argparse
import tempfile
from pathlib import Path
from unittest.mock import patch

from ticket_config import DEMO_SITE

from ticket_booking import Attempt, LoginRequired, RunCancelled, release_time
from ticket_web import BookingService, DEFAULT_SLOTS, Handler, LocalServer, ROOT


class FakeClient:
    def __init__(self, user_id):
        self.user_id = user_id
        self.token = "LOCAL_TEST_TOKEN"
        self.shop_num = "0001"
        self.timings = []

    def close(self):
        pass

    def login(self):
        if self.user_id != "A" * 32:
            raise LoginRequired("本机模拟的入口失效")

    def request(self, path, **params):
        if path != "ticket/getOnlineAllTicketInfo":
            raise AssertionError("离线界面验收不允许额外接口")
        return [{"id": 1593 + index, "name": "健身票", "begintime": s.split("-")[0] + ":00",
                 "endtime": s.split("-")[1] + ":00"} for index, s in enumerate(DEFAULT_SLOTS)]

    def detail(self, ticket_id):
        index = int(ticket_id) - 1593
        slot = DEFAULT_SLOTS[index] if 0 <= index < len(DEFAULT_SLOTS) else DEFAULT_SLOTS[0]
        return {"id": ticket_id, "name": "健身票" + slot,
                "begintime": slot.split("-")[0] + ":00", "endtime": slot.split("-")[1] + ":00",
                "discountPrice": 0, "ifPriorReservation": "1", "priorReservationDays": "1",
                "lastNumArray": [{"day": "2099-09-17", "lastNumber": "0"}]}


def fake_prepare(client, slot, day, on_event=None):
    if on_event:
        on_event("preflight", {"step": "login"})
    client.login()
    return {"venue": "目标体育馆", "ticket_id": 1593, "slot": slot, "use_day": day,
            "quantity": 1, "price": 0, "existing_order": None,
            "notice": {"hash": "LOCAL_TEST_NOTICE", "seconds": 5,
                       "text": "本机演练说明\n这是一份用于验证页面操作的模拟须知。\n确认后只会运行本机测试流程，不会访问官网，不会创建真实订单。"},
            "current_rows": [{"date": day, "slot": slot, "remaining": 0}],
            "release_at": release_time(day, slot).isoformat()}


def fake_execute(client, plan, notice, directory, **kwargs):
    cancel, event = kwargs["cancel_event"], kwargs["on_event"]
    event("waiting", {"release_at": plan["release_at"]})
    if cancel.wait(3 if plan["slot"] == "19:30-21:30" else 120):
        raise RunCancelled()
    kwargs["before_submit"]()
    event("submitting", {})
    event("confirming", {"check": 1})
    return {"state": "confirmed", "order": {"billnum": "LOCAL_TEST_ONLY", "status": "0"}}


def fake_reconcile(client, directory, slot, day, **kwargs):
    # 只处理临时目录中的模拟记录，绝不调用真实核对接口。
    attempt = Attempt(directory, client.user_id, 1593, day)
    with attempt:
        order = {"billnum": "LOCAL_TEST_RECOVERY", "status": "0"}
        attempt.finish("confirmed", order)
        return {"state": "confirmed", "order": order}


class FixtureHandler(Handler):
    def do_GET(self):
        if self.path == "/":
            self.local_origin()
            html = (ROOT / "web/index.html").read_text()
            html = html.replace("<main>", '<main><div class="page-alert">本机演练 · 全部为模拟数据，不连接官网、不创建真实订单。</div>')
            return self.send(200, html.encode(), "text/html; charset=utf-8")
        return super().do_GET()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--recovery", action="store_true", help="生成一笔旧日期的模拟待核对记录")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ontime-ui-test-") as directory:
        if args.recovery:
            plan = fake_prepare(FakeClient("A" * 32), "06:00-07:30", "2020-01-01")
            with Attempt(Path(directory) / "booking-state", "A" * 32, 1593, plan["use_day"]) as attempt:
                attempt.begin(plan)
                attempt.finish("unknown")
        service = BookingService(directory, FakeClient)
        server = LocalServer(("127.0.0.1", args.port), service)
        server.RequestHandlerClass = FixtureHandler
        with patch("ticket_monitor.SITE", DEMO_SITE), patch("ticket_web.prepare", fake_prepare), patch("ticket_web.execute", fake_execute), patch("ticket_web.reconcile_saved", fake_reconcile):
            print(f"离线界面验收：http://127.0.0.1:{args.port}；仅 A 重复 32 次的模拟入口可登录。", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                service.close()
                server.server_close()
