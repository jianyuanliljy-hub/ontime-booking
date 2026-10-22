#!/usr/bin/env python3
"""目标体育馆健身票余量查询，只使用 Python 标准库，不包含下单接口。"""

import argparse
import getpass
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ticket_config import SITE


BASE_URL = SITE.origin + "/easyserpClient/"
VENUE = SITE.venue
READ_PATHS = frozenset({
    "publicNumber/getClubInfoByClubMemberCode",
    "publicNumber/getShopsByClub",
    "memberLogin/loginedJDApp",
    "ticket/getOnlineAllTicketInfo",
    "datediscount/getTbTicketInfoById",
})


class MonitorError(Exception):
    """可直接展示的错误，不携带登录凭据或完整请求地址。"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MonitorError("接口发生重定向，已停止请求，请在官网检查登录状态。")


def parse_entry_url(value):
    url = urllib.parse.urlsplit(value.strip().replace("\\&", "&"))
    if (url.scheme != "https" or url.netloc != urllib.parse.urlsplit(SITE.origin).netloc
            or url.path != "/easyserpApp/index.html"):
        raise MonitorError("请使用本机配置站点的原始购票入口链接。")
    route, separator, query = url.fragment.partition("?")
    params = urllib.parse.parse_qs(query)
    if route != "/branch" or not separator or params.get("clubId") != [SITE.club_id]:
        raise MonitorError("入口应包含正确的场馆标识和本人的 userId。")
    users = params.get("userId", [])
    if len(users) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", users[0]):
        raise MonitorError("入口中的 userId 缺失或格式不正确。")
    return users[0]


def normalize_slot(value):
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-{1,2}\s*(\d{1,2}):(\d{2})\s*", value)
    if not match:
        raise ValueError("时段格式应为 06:00-07:30")
    h1, m1, h2, m2 = map(int, match.groups())
    if max(h1, h2) > 23 or max(m1, m2) > 59 or (h1, m1) >= (h2, m2):
        raise ValueError("时段的起止时间无效")
    return f"{h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}"


def parse_remaining(value):
    # 缺失值、负数和异常字符串都保留为未知，避免误报为 0 或有票。
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"\d+", value):
        return int(value)
    return None


def ticket_rows(detail):
    if not isinstance(detail, dict):
        raise MonitorError("票务详情格式异常。")
    try:
        ticket_id = int(detail["id"])
        name = detail["name"]
        slot = normalize_slot(detail["begintime"][:5] + "-" + detail["endtime"][:5])
        if not isinstance(name, str) or not name.startswith("健身票"):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise MonitorError("票务详情缺少有效的名称或使用时间。") from None

    warnings = []
    try:
        if normalize_slot(name.removeprefix("健身票")) != slot:
            warnings.append("票名与使用时间不一致，时段筛选采用详情的使用时间")
    except ValueError:
        pass
    days = detail.get("lastNumArray")
    if not isinstance(days, list) or not days:
        days = [{}]
        warnings.append("接口未返回日期余量，库存未知")
    detail_url = SITE.origin + "/easyserpApp/index.html#/orderDetails?" + urllib.parse.urlencode({
        "id": ticket_id, "shortName": detail.get("shortName", ""), "type": "ticket",
    })
    rows = []
    for entry in days:
        if not isinstance(entry, dict):
            raise MonitorError("日期余量格式异常。")
        day = entry.get("day")
        if day is not None:
            try:
                day = date.fromisoformat(day).isoformat()
            except (TypeError, ValueError):
                raise MonitorError("接口返回了无效的使用日期。") from None
        remaining = parse_remaining(entry.get("lastNumber")) if day else None
        rows.append({
            "ticket_id": ticket_id, "name": name, "date": day, "slot": slot,
            "remaining": remaining, "price": detail.get("discountPrice"),
            "detail_url": detail_url, "warnings": warnings,
        })
    return rows


class TicketClient:
    def __init__(self, user_id):
        self.user_id = user_id
        self.shop_num = None
        self.token = None
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, **params):
        if path not in READ_PATHS:
            raise MonitorError("请求路径不在本程序的查询范围内。")
        url = BASE_URL + path + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise MonitorError("接口响应过大，停止解析。")
            payload = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise MonitorError(f"官网接口返回 HTTP {exc.code}，本次余量未知。") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise MonitorError("网络、证书或接口响应异常，本次余量未知。") from None
        if not isinstance(payload, dict) or payload.get("msg") != "success":
            raise MonitorError("官网未返回成功结果，请检查入口是否过期或账号是否可用。")
        if "data" not in payload:
            raise MonitorError("接口缺少 data 字段，本次余量未知。")
        return payload["data"]

    def login(self):
        club = self.request("publicNumber/getClubInfoByClubMemberCode", clubId=SITE.club_id)
        try:
            code = club["clubmembercode"]
            shops = self.request("publicNumber/getShopsByClub", clubMemberCode=code, clubId=SITE.club_id)
            matches = [s for s in shops["tbShopsList"] if s["name"] == VENUE]
            if len(matches) != 1:
                raise MonitorError("无法唯一识别目标体育馆，停止查询。")
            self.shop_num = matches[0]["shopNum"]
            auth = self.request("memberLogin/loginedJDApp", userId=self.user_id,
                                clubMemberCode=code, shopNum=self.shop_num)
            self.token = auth["infa"]["openid"]
            if not isinstance(self.token, str) or not self.token:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise MonitorError("官网登录数据格式异常，请重新获取本人的入口链接。") from None

    def snapshot(self, slots=(), target_date=None):
        if not self.token:
            self.login()
        items = self.request("ticket/getOnlineAllTicketInfo", shopNum=self.shop_num,
                             token=self.token, ticketName="")
        if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
            raise MonitorError("票务列表格式异常。")
        items = [x for x in items if isinstance(x.get("name"), str) and x["name"].startswith("健身票")]
        if not items:
            raise MonitorError("官网没有返回健身票列表，不能据此判断售罄。")
        rows = []
        for item in items:
            if "id" not in item:
                raise MonitorError("票务列表缺少票种编号。")
            # 每次重新读取详情，避免把列表缓存或昨天的日期当成当前余量。
            time.sleep(1)
            detail = self.request("datediscount/getTbTicketInfoById", id=item["id"],
                                  shopNum=self.shop_num, token=self.token)
            rows.extend(ticket_rows(detail))
        returned_slots = sorted({r["slot"] for r in rows})
        missing = set(slots) - set(returned_slots)
        if missing:
            raise MonitorError("官网未返回所选时段：" + "、".join(sorted(missing)))
        rows = [r for r in rows if not slots or r["slot"] in slots]
        returned_dates = sorted({r["date"] for r in rows if r["date"]})
        if target_date:
            rows = [r for r in rows if r["date"] == target_date]
        notes = []
        if target_date and not rows:
            notes.append("官网尚未返回目标日期的余量，不能判断该日无票。")
        return {
            "checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
            "venue": VENUE, "target_date": target_date, "requested_slots": list(slots),
            "returned_dates": returned_dates,
            "queried_ticket_count": len(items), "notes": notes,
            "tickets": sorted(rows, key=lambda r: (r["date"] or "", r["slot"])),
        }


def signature(snapshot):
    # 时间戳变化不触发通知；日期变化必须触发。
    return json.dumps({k: v for k, v in snapshot.items() if k != "checked_at"},
                      ensure_ascii=False, sort_keys=True)


def show(snapshot, as_json=False):
    if as_json:
        print(json.dumps(snapshot, ensure_ascii=False), flush=True)
        return
    print(f"\n{snapshot['checked_at']}  {snapshot['venue']}")
    for row in snapshot["tickets"]:
        remaining = row["remaining"]
        stock = "未知" if remaining is None else f"{remaining} 张"
        suffix = "  ← 有余票，购买资格仍需官网确认" if remaining is not None and remaining > 0 else ""
        print(f"{row['date'] or '日期未知'}  {row['slot']}  剩余 {stock}{suffix}")
        for warning in row["warnings"]:
            print(f"  提示：{row['name']}；{warning}")
    for note in snapshot["notes"]:
        print(note + " 已返回日期：" + "、".join(snapshot["returned_dates"]))
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", action="append", default=[], help="指定使用时段，可重复，例如 06:00-07:30")
    parser.add_argument("--date", help="只关注指定使用日期，例如 2026-09-17")
    parser.add_argument("--watch", action="store_true", help="持续查询，按 Ctrl+C 停止")
    parser.add_argument("--interval", type=int, default=120, help="每轮完成后的等待秒数，至少 60，默认 120")
    parser.add_argument("--json", action="store_true", help="输出 JSON；持续模式每条变化输出一行")
    args = parser.parse_args()
    try:
        slots = [normalize_slot(s) for s in args.slot]
        target_date = date.fromisoformat(args.date).isoformat() if args.date else None
    except ValueError as exc:
        parser.error(str(exc))
    if args.interval < 60:
        parser.error("--interval 至少为 60 秒")
    # 交互输入避免完整入口进入 shell 历史；不写配置文件、不缓存 token。
    user_id = parse_entry_url(getpass.getpass("粘贴本人的完整购票入口链接（输入不回显）："))
    client = TicketClient(user_id)
    previous = None
    failures = 0
    if args.watch:
        print("监控运行中：首次及结果变化时输出；连续失败 3 次停止。Ctrl+C 退出。", file=sys.stderr)
    while True:
        try:
            snapshot = client.snapshot(slots, target_date)
            current = signature(snapshot)
            if current != previous or failures:
                show(snapshot, args.json)
            previous = current
            failures = 0
        except MonitorError as exc:
            failures += 1
            print(f"查询失败（本次余量未知）：{exc}", file=sys.stderr, flush=True)
            if not args.watch or failures >= 3:
                return 1
        if not args.watch:
            return 0
        time.sleep(min(args.interval * (2 ** failures), max(args.interval, 600)))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (MonitorError, EOFError) as exc:
        print(f"已停止：{exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n已停止监控。", file=sys.stderr)
        sys.exit(0)
