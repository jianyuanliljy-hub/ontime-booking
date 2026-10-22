#!/usr/bin/env python3
"""定时预约原型：单账号、单场次、单张免费票；默认仅检查，--execute 才会下单。"""

import argparse
import errno
import getpass
import hashlib
import http.client
import json
import os
import random
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from ticket_config import SITE

from ticket_monitor import (
    MonitorError, READ_PATHS, TicketClient, VENUE, TZ,
    normalize_slot, parse_entry_url, ticket_rows,
)


QUERY_PATHS = READ_PATHS | {
    "ticket/getSaleTicketExplanation", "ticket/buyTicketRule", "ticket/getTicketOrders",
    "configurable/getConfigInfo", "common/getHealthCommitment",
}
CREATE_PATH = "ticket/freeBuyTicket"


class BackendRejected(MonitorError):
    pass


class TransportFailure(MonitorError):
    def __init__(self, message, retryable=True, retry_after=0):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class LoginRequired(MonitorError):
    pass


class QueryDeadline(MonitorError):
    pass


class RunCancelled(MonitorError):
    pass


def check_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise RunCancelled("已停止自动预约。")


def report_event(callback, state, **values):
    if callback is not None:
        callback(state, values)


def pause(seconds, cancel_event=None, sleep=None):
    check_cancelled(cancel_event)
    if cancel_event is None:
        (sleep or time.sleep)(seconds)
    else:
        cancel_event.wait(seconds)
        check_cancelled(cancel_event)


def retry_after_seconds(value, server_date=None):
    """识别 Retry-After 秒数/HTTP 日期，不擅自缩短服务器要求的等待。"""
    if not isinstance(value, str):
        return 0.0
    if value.strip().isdigit():
        return float(value.strip())
    try:
        until = parsedate_to_datetime(value)
        base = parsedate_to_datetime(server_date) if server_date else datetime.now(until.tzinfo)
        if until.tzinfo is None or base.tzinfo is None:
            return 0.0
        return max(0.0, (until - base).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 0.0


def read_retry(client, operation, deadline=None, cancel_event=None, on_event=None,
               stage="query", attempts=4, sleep=None):
    """仅重试已核实的查询流程；上下文内从传输层禁止 POST。"""
    for index in range(attempts):
        check_cancelled(cancel_event)
        if deadline is not None and time.monotonic() >= deadline:
            raise QueryDeadline("查询时间窗口已结束，本次结果未知。")
        old_deadline = getattr(client, "query_deadline", None)
        old_query_only = getattr(client, "query_only", False)
        client.query_deadline, client.query_only = deadline, True
        try:
            return operation()
        except TransportFailure as exc:
            if not exc.retryable or index + 1 == attempts:
                raise
            delay = max(0.5 * (2 ** index) + random.uniform(0, 0.25), exc.retry_after)
            if deadline is not None and time.monotonic() + delay >= deadline:
                raise QueryDeadline("服务器要求等待或查询过慢，已停止本轮查询；不能判断为无票。") from None
            report_event(on_event, "query_retry", stage=stage, attempt=index + 1,
                         delay_seconds=round(delay, 2))
        finally:
            client.query_deadline, client.query_only = old_deadline, old_query_only
        pause(delay, cancel_event, sleep)


class DirectClient(TicketClient):
    """固定官网域名，复用经证书验证的 HTTPS 连接；不自动重放请求。"""

    def __init__(self, user_id):
        self.user_id, self.token, self.shop_num = user_id, None, None
        self.connection = None
        self.timings = []
        self.club_code = None
        self.query_deadline = None
        self.query_only = False
        self.retry_not_before = 0.0

    def timeout_for(self, path, method):
        if method == "POST":
            return 30.0
        if path == "ticket/getTicketOrders":
            return 10.0
        if path in {"datediscount/getTbTicketInfoById", "ticket/buyTicketRule",
                    "ticket/getSaleTicketExplanation"}:
            return 4.0
        return 10.0

    def close(self):
        if self.connection:
            self.connection.close()
            self.connection = None

    def exchange(self, path, params, method="GET"):
        if (method == "GET" and path not in QUERY_PATHS) or (
            method == "POST" and path != CREATE_PATH
        ) or method not in {"GET", "POST"}:
            raise MonitorError("接口或请求方法不在已核实的流程内。")
        if self.query_only and method != "GET":
            raise MonitorError("重试查询期间禁止创建订单。")
        cooldown = self.retry_not_before - time.monotonic()
        if cooldown > 0:
            raise TransportFailure("官网要求继续等待，尚未发送新请求。", retry_after=cooldown)
        encoded = urllib.parse.urlencode(params)
        target = "/easyserpClient/" + path
        headers = {"Accept": "application/json"}
        body = None
        if method == "GET":
            target += "?" + encoded
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
            body = encoded.encode("utf-8")
        started = time.perf_counter()
        budget = self.timeout_for(path, method)
        if method == "GET" and self.query_deadline is not None:
            budget = min(budget, self.query_deadline - time.monotonic())
        if budget <= 0:
            raise QueryDeadline("查询时间窗口已结束，本次请求未发送。")
        record = {"path": path, "method": method, "http_status": None,
                  "server_date": None, "phase": "connect", "outcome": "unknown",
                  "started_at": datetime.now(TZ).isoformat()}
        timer = None
        expired = threading.Event()
        complete = threading.Event()
        try:
            if self.connection is None:
                self.connection = http.client.HTTPSConnection(
                    urllib.parse.urlsplit(SITE.origin).netloc,
                    timeout=min(3.0, budget), context=ssl.create_default_context())
            if self.connection.sock is None:
                self.connection.connect()
            record["connect_ms"] = round((time.perf_counter() - started) * 1000, 1)
            remaining = budget - (time.perf_counter() - started)
            if remaining <= 0:
                raise TimeoutError()
            sock = self.connection.sock
            sock.settimeout(remaining)

            def interrupt_socket():
                if not complete.is_set():
                    expired.set()
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

            # 已建立连接后的整次收发计时，防止分段慢响应不断续期 socket 超时。
            timer = threading.Timer(remaining, interrupt_socket)
            timer.daemon = True
            timer.start()
            record["phase"] = "send"
            self.connection.request(method, target, body=body, headers=headers)
            record["phase"] = "headers"
            response = self.connection.getresponse()
            record.update(http_status=response.status, server_date=response.getheader("Date"),
                          headers_ms=round((time.perf_counter() - started) * 1000, 1))
            if response.status in {401, 403}:
                raise LoginRequired("登录失效或访问受限，请重新取得本人的入口后核对。")
            if response.status != 200:
                delay = retry_after_seconds(response.getheader("Retry-After"), record["server_date"])
                self.retry_not_before = max(self.retry_not_before, time.monotonic() + delay)
                record["retry_after_seconds"] = delay
                raise TransportFailure(f"官网返回 HTTP {response.status}。",
                                       retryable=response.status in {408, 429, 500, 502, 503, 504},
                                       retry_after=delay)
            record["phase"] = "body"
            raw = response.read(2_000_001)
            if expired.is_set():
                raise TimeoutError()
            record["bytes_received"] = len(raw)
            if response.will_close:
                self.close()
            if len(raw) > 2_000_000:
                raise MonitorError("官网响应过大，停止解析。")
            record["phase"] = "parse"
            payload = json.loads(raw)
            if payload == -1 or payload == "-1":
                raise LoginRequired("官网报告身份信息失效，请重新获取登录入口。")
            if not isinstance(payload, dict) or payload.get("msg") != "success":
                raise BackendRejected("官网未通过本次请求，请在官网查看原因。")
            if "data" not in payload:
                raise MonitorError("接口缺少 data，结果未知。")
            record.update(phase="complete", outcome="success")
            return payload["data"]
        except ssl.SSLCertVerificationError:
            record["outcome"] = "certificate_error"
            self.close()
            raise TransportFailure("官网证书验证失败，停止请求。", retryable=False) from None
        except (OSError, http.client.HTTPException, ValueError) as exc:
            record["outcome"] = ("timeout" if expired.is_set() or isinstance(exc, (TimeoutError, socket.timeout))
                                 else "transport_error")
            self.close()
            message = ("订单请求未取得可靠响应，结果可能未知，不能直接重发。" if method == "POST"
                       else "官网查询连接或响应异常，本次结果未知。")
            raise TransportFailure(message) from None
        except MonitorError as exc:
            record["outcome"] = type(exc).__name__
            self.close()
            raise
        finally:
            complete.set()
            if timer is not None:
                timer.cancel()
                timer.join()
            record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
            self.timings.append(record)

    def request(self, path, **params):
        result = self.exchange(path, params)
        if path == "publicNumber/getClubInfoByClubMemberCode" and isinstance(result, dict):
            self.club_code = result.get("clubmembercode")
        return result

    def detail(self, ticket_id):
        result = self.request("datediscount/getTbTicketInfoById", id=ticket_id,
                              shopNum=self.shop_num, token=self.token)
        if not isinstance(result, dict) or str(result.get("id")) != str(ticket_id):
            raise MonitorError("详情返回了不同的票种编号，停止预约。")
        return result

    def check_rules(self, ticket_id, use_day):
        return self.request("ticket/buyTicketRule", **self.order_params(ticket_id, use_day))

    def order_params(self, ticket_id, use_day):
        return {
            "shopNum": self.shop_num, "token": self.token, "useDay": use_day,
            "ticketInfo": json.dumps([{"ticketId": ticket_id, "price": 0}], separators=(",", ":")),
        }

    def create(self, ticket_id, use_day):
        # 调用方必须先核对价格、日期、库存、规则、声明及本地防重复记录。
        return self.exchange(CREATE_PATH, self.order_params(ticket_id, use_day), method="POST")


class NoticeText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, value):
        self.parts.append(value)

    def handle_endtag(self, tag):
        if tag in {"p", "div"}:
            self.parts.append("\n")


def read_notice(client):
    data = client.request("ticket/getSaleTicketExplanation", token=client.token)
    if data is None or data == "":
        return {"hash": "none", "seconds": 0, "text": ""}
    if not isinstance(data, dict) or not isinstance(data.get("info"), str):
        raise MonitorError("购票须知格式改变，需先在官网检查。")
    parser = NoticeText()
    parser.feed(data["info"])
    try:
        seconds = int(data.get("duration") or 0)
        if not 0 <= seconds <= 120:
            raise ValueError()
    except (ValueError, TypeError):
        raise MonitorError("购票须知倒计时无效。") from None
    fingerprint = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"hash": fingerprint, "seconds": seconds,
            "text": "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())}


def validate_detail(detail, slot):
    rows = ticket_rows(detail)
    if any(row["slot"] != slot for row in rows):
        raise MonitorError("实际使用时段与目标不一致。")
    try:
        price = Decimal(str(detail.get("discountPrice")))
        if not price.is_finite() or price != 0:
            raise ValueError()
    except (InvalidOperation, ValueError):
        raise MonitorError("当前票价并非明确的 0 元，不执行预约。") from None
    if detail.get("ifAffiliate") not in (None, "", "0", 0):
        raise MonitorError("此票涉及附属卡，需在官网选择使用人。")
    if detail.get("ifentrance") not in (None, "", "0", 0):
        raise MonitorError("此票需要另行预约入场时间，当前原型不自动处理。")
    if detail.get("tbPrivatecourseDiscount"):
        raise MonitorError("此票包含尚未核实的优惠计算，停止预约。")
    if (detail.get("ticketConfig") or {}).get("favorabletype") not in (None, "", "0", 0):
        raise MonitorError("此票的活动规则改变，停止自动预约。")
    if str(detail.get("ifPriorReservation")) != "1":
        raise MonitorError("日期预约模式改变，停止预约。")
    if str(detail.get("priorReservationDays")) != "1":
        raise MonitorError("提前预约天数改变，原有放票时间推算不再适用。")
    return rows


def has_stock(detail, slot, use_day):
    rows = validate_detail(detail, slot)
    matching = [row for row in rows if row["date"] == use_day]
    if len(matching) > 1:
        raise MonitorError("同一日期出现多条库存记录，停止预约。")
    if not matching:
        return False
    if matching[0]["remaining"] is None:
        raise MonitorError("目标日期余量未知，停止预约。")
    return matching[0]["remaining"] >= 1


def use_date(value):
    if isinstance(value, bool):
        raise MonitorError("订单日期格式异常。")
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, TZ).date().isoformat()
        except (OSError, OverflowError, ValueError):
            raise MonitorError("订单时间戳无效，不能排除重复订单。") from None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    raise MonitorError("无法识别订单使用日期，不能排除重复订单。")


def find_existing(client, ticket_id, use_day):
    # 按官网原有分页参数逐页读取；达到边界仍未读完则停止，避免漏检。
    seen_pages = set()
    for page in range(20):
        records = client.request("ticket/getTicketOrders", shopNum=client.shop_num,
                                 token=client.token, pageNo=page, pageSize=10)
        if not isinstance(records, list) or any(not isinstance(x, dict) for x in records):
            raise MonitorError("本人订单列表格式异常。")
        if any(not {"id", "ticketId", "useDay", "name", "shopnum", "billnum"} <= record.keys() for record in records):
            raise MonitorError("本人订单缺少核对所需字段，停止预约。")
        page_key = json.dumps([str(x.get("id")) for x in records])
        if records and page_key in seen_pages:
            raise MonitorError("订单分页未前进，无法可靠排除重复订单。")
        seen_pages.add(page_key)
        for record in records:
            if str(record.get("ticketId")) != str(ticket_id):
                continue
            if use_date(record.get("useDay")) != use_day:
                continue
            if str(record.get("shopnum")) != str(client.shop_num):
                raise MonitorError("订单场馆字段与所选场馆不一致。")
            if str(record.get("name")) in {"3", "-1"}:
                continue  # 官网标记为已退款或已过期。
            if not record.get("billnum"):
                raise MonitorError("找到同场订单但缺少订单号，请在官网检查。")
            return {"billnum": str(record["billnum"]), "status": str(record.get("name"))}
        if len(records) < 10:
            return None
    raise MonitorError("订单超过 200 条且尚未查完；停止自动下单，需人工核对。")


def prepare(client, slot, use_day, on_event=None):
    report_event(on_event, "preflight", step="login")
    client.login()
    report_event(on_event, "preflight", step="config")
    config = client.request("configurable/getConfigInfo", clubMemberCode=client.club_code)
    if not isinstance(config, list):
        raise MonitorError("场馆流程配置格式异常。")
    if any(isinstance(item, dict) and str(item.get("limitid")) == "63" for item in config):
        report_event(on_event, "preflight", step="health_commitment")
        commitment = client.request("common/getHealthCommitment", token=client.token, shopNum=client.shop_num)
        if commitment:
            raise MonitorError("官网要求额外健康承诺，请先在官网处理，当前原型停止。")
    report_event(on_event, "preflight", step="catalog")
    items = client.request("ticket/getOnlineAllTicketInfo", shopNum=client.shop_num,
                           token=client.token, ticketName="健身票")
    if not isinstance(items, list):
        raise MonitorError("健身票列表格式异常。")
    candidates = []
    for item in items:
        if not isinstance(item, dict) or not str(item.get("name", "")).startswith("健身票"):
            continue
        try:
            item_slot = normalize_slot(item["begintime"][:5] + "-" + item["endtime"][:5])
        except (KeyError, TypeError, ValueError):
            raise MonitorError("列表缺少可靠使用时间，停止自动匹配。") from None
        if item_slot == slot:
            candidates.append(item)
    if len(candidates) != 1:
        raise MonitorError("无法唯一匹配所选健身时段。")
    ticket_id = int(candidates[0]["id"])
    report_event(on_event, "preflight", step="detail")
    detail = client.detail(ticket_id)
    rows = validate_detail(detail, slot)
    matching = [row for row in rows if row["date"] == use_day]
    if len(matching) > 1:
        raise MonitorError("同一日期出现多条库存记录，停止预约。")
    remaining = matching[0]["remaining"] if matching else None
    release_at = release_time(use_day, slot)
    before_release = datetime.now(TZ) < release_at
    if (remaining is None or remaining < 1) and not before_release:
        # 无票或余量未知时，不再读取慢速购票须知和本人订单接口。
        # 预约提交仍要求完整 prepare；只有确认有票才会走后续流程。
        report_event(on_event, "preflight", step="stock_short_circuit", remaining=remaining)
        return {"venue": VENUE, "ticket_id": ticket_id, "slot": slot, "use_day": use_day,
                "quantity": 1, "price": 0, "notice": {"hash": "fast-only", "seconds": 0, "text": ""},
                "current_rows": rows, "existing_order": None, "fast_only": True,
                "release_at": release_at.isoformat()}
    report_event(on_event, "preflight", step="notice")
    notice = read_notice(client)
    report_event(on_event, "preflight", step="orders")
    existing = find_existing(client, ticket_id, use_day)
    return {"venue": VENUE, "ticket_id": ticket_id, "slot": slot, "use_day": use_day,
            "quantity": 1, "price": 0, "notice": notice, "current_rows": rows,
            "existing_order": existing, "release_at": release_at.isoformat()}


def release_time(use_day, slot):
    day = date.fromisoformat(use_day) - timedelta(days=1)
    return datetime.fromisoformat(day.isoformat() + "T" + slot.split("-")[1]).replace(tzinfo=TZ)


def start_time(plan):
    return datetime.fromisoformat(plan["use_day"] + "T" + plan["slot"].split("-")[0]).replace(tzinfo=TZ)


def wait_until(moment, cancel_event=None):
    while True:
        check_cancelled(cancel_event)
        remaining = (moment - datetime.now(TZ)).total_seconds()
        if remaining <= 0:
            return
        if cancel_event is None:
            time.sleep(min(remaining, 30))
        else:
            cancel_event.wait(min(remaining, 30))


class Attempt:
    """本机互斥锁与提交日志；未知结果跨进程、跨重启也不重复提交。"""

    def __init__(self, directory, user_id, ticket_id, use_day):
        key = hashlib.sha256(f"{user_id}|{ticket_id}|{use_day}".encode()).hexdigest()[:24]
        self.directory = Path(directory)
        self.path = self.directory / (key + ".json")
        self.lock_path = self.directory / (key + ".lock")
        self.lock = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        self.lock = os.fdopen(fd, "r+b")
        try:
            if os.name == "nt":
                # Windows 允许锁住文件末尾之外的首字节，无需写入或改动旧锁文件。
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lock.close()
            self.lock = None
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise MonitorError("本机已有同账号、同场次预约程序运行。") from None
            raise
        return self

    def __exit__(self, *args):
        try:
            if os.name == "nt":
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.lock.close()
            self.lock = None

    def begin(self, plan):
        # 先持久化提交意图再发送 POST。此文件存在时不再创建订单。
        data = {"state": "submission_started", "ticket_id": plan["ticket_id"],
                "use_day": plan["use_day"], "slot": plan["slot"],
                "created_at": datetime.now(TZ).isoformat()}
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise MonitorError("此场次已有提交记录；请先核对官网订单，程序不会重复提交。") from None
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())

    def finish(self, state, order=None):
        # 初始文件即使写坏仍会阻止再次提交；更新仅作结果记录。
        record = json.loads(self.path.read_text(encoding="utf-8"))
        record.update(state=state, order=order, updated_at=datetime.now(TZ).isoformat())
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)


def reconcile_order(client, plan, attempt, response_state="unknown", sleep=None, on_event=None):
    """只查订单，给延迟可见的出票记录留出时间，绝不再次调用 create。"""
    deadline = time.monotonic() + 90
    delays = (0, 2, 5, 10, 20, 30)
    reason = "订单暂未查到"
    requires_login = False
    for index, delay in enumerate(delays):
        if time.monotonic() + delay >= deadline:
            break
        if delay:
            (sleep or time.sleep)(delay)
        report_event(on_event, "confirming", check=index + 1, response_state=response_state)
        try:
            existing = read_retry(client, lambda: find_existing(client, plan["ticket_id"], plan["use_day"]),
                                  deadline=deadline, attempts=2, sleep=sleep,
                                  on_event=on_event, stage="orders")
        except LoginRequired:
            requires_login = True
            reason = "核对订单时登录失效，需要重新登录后继续核对"
            break
        except TransportFailure as exc:
            reason = "订单查询仍不可用"
            if not exc.retryable:
                reason = "订单查询遇到不能自动重试的错误"
                break
            continue
        except QueryDeadline:
            reason = "订单查询预算不足或服务器要求更久的等待"
            break
        except MonitorError:
            reason = "订单返回结果无法可靠核对"
            break
        if existing:
            state = "confirmed" if existing["status"] == "0" else "order_found_needs_review"
            attempt.finish(state, existing)
            return {"state": state, "order": existing}
    state = "rejected_unconfirmed" if response_state == "rejected" else "unknown"
    attempt.finish(state)
    return {"state": state, "requires_login": requires_login,
            "message": reason + "；请先核对订单，程序不会自动重发。"}


def reconcile_saved(client, directory, slot, use_day, on_event=None):
    """恢复中断后的只读核对；兼容已有本地提交记录，不要求场次尚未开始。"""
    candidates = []
    for path in Path(directory).glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or record.get("slot") != slot or record.get("use_day") != use_day:
                continue
            attempt = Attempt(directory, client.user_id, int(record["ticket_id"]), use_day)
            if attempt.path == path:
                candidates.append((attempt, record))
        except (ValueError, TypeError, KeyError, OSError):
            continue
    if len(candidates) != 1:
        raise MonitorError("无法唯一匹配此入口、日期和时段的提交记录，请先在官网核对；不会重新下单。")
    attempt, record = candidates[0]
    with attempt:
        read_retry(client, client.login, deadline=time.monotonic() + 20, stage="login", on_event=on_event)
        return reconcile_order(client, record, attempt, on_event=on_event)


def submit_once(client, plan, attempt, accepted_notice, sleep=time.sleep, observed_detail=None,
                cancel_event=None, on_event=None, before_submit=None):
    check_cancelled(cancel_event)
    if datetime.now(TZ) >= start_time(plan):
        raise MonitorError("目标场次已经开始，不再下单。")
    if attempt.path.exists():
        raise MonitorError("已有本场次提交记录，需要先核对官网，禁止再次提交。")
    # 一旦发现有票，验证与须知有独立时间预算，不沿用即将结束的轮询窗口。
    validation_deadline = time.monotonic() + 25
    def read(operation, stage):
        return read_retry(client, operation, deadline=validation_deadline, cancel_event=cancel_event,
                          on_event=on_event, stage=stage, sleep=sleep)
    detail = read(lambda: client.detail(plan["ticket_id"]), "stock") if observed_detail is None else observed_detail
    if not has_stock(detail, plan["slot"], plan["use_day"]):
        return {"state": "no_stock"}
    read(lambda: client.check_rules(plan["ticket_id"], plan["use_day"]), "rules")
    current_notice = read(lambda: read_notice(client), "notice")
    if current_notice["hash"] != accepted_notice:
        raise MonitorError("购票须知未确认或已变化，请先阅读并确认当前版本。")
    if time.monotonic() + current_notice["seconds"] >= validation_deadline:
        raise QueryDeadline("验证剩余时间不足以完成须知倒计时，未发送订单。")
    # 与网页相同：规则通过并展示须知后才开始倒计时，不提前绕过。
    report_event(on_event, "reading_notice", seconds=current_notice["seconds"])
    print(f"规则已通过，遵守官网 {current_notice['seconds']} 秒须知倒计时。", flush=True)
    if cancel_event is None:
        sleep(current_notice["seconds"])
    else:
        cancel_event.wait(current_notice["seconds"])
        check_cancelled(cancel_event)
    if datetime.now(TZ) >= start_time(plan):
        raise MonitorError("倒计时后目标场次已经开始，不再下单。")
    if not has_stock(read(lambda: client.detail(plan["ticket_id"]), "stock"), plan["slot"], plan["use_day"]):
        return {"state": "stock_lost"}
    check_cancelled(cancel_event)
    if before_submit is not None:
        before_submit()
    check_cancelled(cancel_event)
    if time.monotonic() >= validation_deadline:
        raise MonitorError("下单前的验证等待过久，未发送订单；请重新检查。")
    if datetime.now(TZ) >= start_time(plan):
        raise MonitorError("最后一次余量检查后目标场次已经开始，不再下单。")
    report_event(on_event, "submitting")
    attempt.begin(plan)
    response_state = "response_success"
    try:
        client.create(plan["ticket_id"], plan["use_day"])
    except BackendRejected:
        response_state = "rejected"
    except Exception:
        # 请求一旦开始，未分类的响应异常也不能作为再次提交的依据。
        response_state = "unknown"
    # 创建请求只发一次，包括超时或被拒绝的情况；其后仅作只读核对。
    return reconcile_order(client, plan, attempt, response_state, sleep=sleep, on_event=on_event)


def execute(client, plan, accepted_notice, directory, window=30, poll=0.5,
            cancel_event=None, on_event=None, before_submit=None):
    check_cancelled(cancel_event)
    if plan["existing_order"]:
        state = "already_exists" if plan["existing_order"]["status"] == "0" else "order_found_needs_review"
        return {"state": state, "order": plan["existing_order"]}
    if accepted_notice != plan["notice"]["hash"]:
        raise MonitorError("需要先阅读购票须知，并用 --accept-notice 指定当前版本摘要。")
    with Attempt(directory, client.user_id, plan["ticket_id"], plan["use_day"]) as attempt:
        if attempt.path.exists():
            raise MonitorError("已有提交记录，不自动再次下单。")
        release = datetime.fromisoformat(plan["release_at"])
        report_event(on_event, "waiting", release_at=plan["release_at"])
        wait_until(release - timedelta(seconds=45), cancel_event)
        # 临近放票重新登录和核对订单；这些步骤可能延迟，不假定一定能赶上。
        report_event(on_event, "preparing")
        read_retry(client, client.login, deadline=time.monotonic() + 15,
                   cancel_event=cancel_event, on_event=on_event, stage="login")
        check_cancelled(cancel_event)
        existing = read_retry(client, lambda: find_existing(client, plan["ticket_id"], plan["use_day"]),
                              deadline=time.monotonic() + 30, cancel_event=cancel_event,
                              on_event=on_event, stage="orders")
        if existing:
            state = "already_exists" if existing["status"] == "0" else "order_found_needs_review"
            return {"state": state, "order": existing}
        report_event(on_event, "waiting", release_at=plan["release_at"])
        wait_until(release - timedelta(seconds=2), cancel_event)
        warmup_budget = min(2, (release - datetime.now(TZ)).total_seconds())
        if warmup_budget > 0:
            try:
                read_retry(client, lambda: client.detail(plan["ticket_id"]),
                           deadline=time.monotonic() + warmup_budget, attempts=1,
                           cancel_event=cancel_event, on_event=on_event, stage="warmup")
            except TransportFailure as exc:
                if not exc.retryable:
                    raise
                report_event(on_event, "warmup_unavailable")
            except QueryDeadline:
                report_event(on_event, "warmup_unavailable")
        wait_until(release, cancel_event)
        deadline = time.monotonic() + window
        latest_query_ok = False
        while time.monotonic() < deadline:
            check_cancelled(cancel_event)
            started = time.monotonic()
            if datetime.now(TZ) >= start_time(plan):
                raise MonitorError("目标场次已经开始，停止预约。")
            try:
                latest_query_ok = False
                detail = read_retry(client, lambda: client.detail(plan["ticket_id"]), deadline=deadline,
                                    cancel_event=cancel_event, on_event=on_event, stage="stock")
                latest_query_ok = True
            except QueryDeadline:
                break
            matching = [row for row in ticket_rows(detail) if row["date"] == plan["use_day"]]
            report_event(on_event, "checking", remaining=matching[0]["remaining"] if matching else None)
            if has_stock(detail, plan["slot"], plan["use_day"]):
                result = submit_once(client, plan, attempt, accepted_notice, observed_detail=detail,
                                     cancel_event=cancel_event, on_event=on_event, before_submit=before_submit)
                if result["state"] not in {"no_stock", "stock_lost"}:
                    return result
            pause(min(max(0, poll - (time.monotonic() - started)), max(0, deadline - time.monotonic())), cancel_event)
        state = "window_ended" if latest_query_ok else "query_unavailable"
        return {"state": state, "message": "查询窗口结束，未提交订单。" if latest_query_ok else "查询未能完成，票量未知，未提交订单。"}


class ConsoleProgress:
    def __init__(self):
        self.last_stock = object()

    def __call__(self, state, values):
        labels = {"login": "登录", "orders": "订单核对", "stock": "余量查询",
                  "rules": "购票规则", "notice": "购票须知", "preflight": "预检查"}
        if state == "query_retry":
            message = (f"{labels.get(values['stage'], '查询')}暂时不可用，第 {values['attempt']} 次重试前"
                       f"等待 {values['delay_seconds']} 秒。")
        elif state == "checking":
            if values["remaining"] == self.last_stock:
                return
            self.last_stock = values["remaining"]
            remaining = "未知或目标日期尚未返回" if self.last_stock is None else f"{self.last_stock} 张"
            message = f"目标余量：{remaining}；正在限时检查。"
        elif state == "confirming":
            message = f"正在核对本人订单（第 {values.get('check', 1)} 轮）；此阶段不会再次下单。"
        else:
            message = {
                "preflight": "正在预检查登录、票种、须知及已有订单。",
                "waiting": f"等待计划放票时间：{values.get('release_at', '')}",
                "preparing": "正在刷新登录并排除已有订单。",
                "warmup_unavailable": "连接预热未完成，将在查询窗口内重新检查。",
                "submitting": "正在发送一次订单请求，最多等待 30 秒，随后查询订单确认。",
            }.get(state)
        if message:
            print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] {message}", flush=True)


def save_diagnostics(client, directory, state):
    """只保留接口名称、阶段及耗时；不写参数、响应正文或个人入口。"""
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (datetime.now(TZ).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8] + ".json")
    data = {"recorded_at": datetime.now(TZ).isoformat(), "state": state,
            "request_attempts": len(client.timings),
            "create_attempts": sum(item["method"] == "POST" for item in client.timings),
            "timings": client.timings}
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="实际使用日期 YYYY-MM-DD，必须明确指定")
    parser.add_argument("--slot", default="06:00-07:30")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="按放票时间等待，并实际预约最多一张 0 元票")
    mode.add_argument("--reconcile-only", action="store_true", help="只核对本机已有提交记录，绝不重新下单")
    parser.add_argument("--accept-notice", help="已阅读并接受的购票须知 SHA256 摘要")
    parser.add_argument("--window", type=float, default=30, help="放票后的查询窗口，1–60 秒")
    parser.add_argument("--poll", type=float, default=0.5, help="查询起点之间至少间隔 0.5–10 秒，单请求串行")
    parser.add_argument("--state-dir", default=str(Path(__file__).resolve().parent / "booking-state"))
    parser.add_argument("--diagnostics-dir", default=str(Path(__file__).resolve().parent / "booking-diagnostics"),
                        help="脱敏诊断日志目录，每次运行生成独立文件")
    args = parser.parse_args()
    try:
        use_day = date.fromisoformat(args.date).isoformat()
        slot = normalize_slot(args.slot)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.window <= 60 or not 0.5 <= args.poll <= 10:
        parser.error("window 应为 1–60 秒，poll 应为 0.5–10 秒")
    if not args.reconcile_only and datetime.now(TZ) >= start_time({"use_day": use_day, "slot": slot}):
        parser.error("请指定尚未开始的场次")
    user_id = parse_entry_url(getpass.getpass("粘贴本人的完整购票入口（不回显、不保存）："))
    client = DirectClient(user_id)
    progress = ConsoleProgress()
    final_state = "interrupted_or_error"
    try:
        if args.reconcile_only:
            result = reconcile_saved(client, args.state_dir, slot, use_day, on_event=progress)
            final_state = result["state"]
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            return 0 if final_state == "confirmed" else 2
        if args.execute:
            preparation = release_time(use_day, slot) - timedelta(seconds=90)
            print(f"预约目标 {use_day} {slot}，1 张、0 元；准备时间 {preparation.isoformat()}。", flush=True)
            wait_until(preparation)
        report_event(progress, "preflight")
        plan = read_retry(client, lambda: prepare(client, slot, use_day),
                          deadline=time.monotonic() + 60, stage="preflight", on_event=progress)
        print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
        if not args.execute:
            final_state = "preflight_only"
            print("检查结束，没有创建订单。--execute 会真实预约；先确认日期和购票须知。")
            return 0
        result = execute(client, plan, args.accept_notice, args.state_dir, args.window, args.poll,
                         on_event=progress)
        final_state = result["state"]
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0 if result["state"] in {"confirmed", "already_exists"} else 2
    finally:
        client.close()
        try:
            path = save_diagnostics(client, args.diagnostics_dir, final_state)
            print(f"脱敏诊断记录：{path}", file=sys.stderr)
        except OSError:
            print("诊断日志写入失败；请保留终端结果并核对订单。", file=sys.stderr)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (MonitorError, OSError, EOFError) as exc:
        print(f"已停止：{exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n已停止；如已发生提交，请核对官网及 booking-state 记录。", file=sys.stderr)
        sys.exit(130)
