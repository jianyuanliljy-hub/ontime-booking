#!/usr/bin/env python3
"""准点：只在本机开放的单页预约界面，复用已验证的预约后端。"""

import argparse
import copy
import json
import secrets
import threading
import time
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ticket_booking import (
    Attempt, DirectClient, LoginRequired, MonitorError, RunCancelled, TZ,
    check_cancelled, execute, normalize_slot, parse_entry_url, prepare, report_event,
    read_retry, reconcile_saved, save_diagnostics, start_time, ticket_rows,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_SLOTS = ["06:00-07:30", "07:30-09:30", "10:00-12:00", "12:00-13:00",
                 "13:00-15:30", "15:30-17:30", "17:30-19:30", "19:30-21:30"]


class UIError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def target(data, allow_past=False):
    try:
        day = date.fromisoformat(data["use_day"]).isoformat()
        slot = normalize_slot(data["slot"])
    except (KeyError, TypeError, ValueError, MonitorError):
        raise UIError("请选择有效的使用日期和时段。") from None
    if not allow_past and datetime.now(TZ) >= start_time({"use_day": day, "slot": slot}):
        raise UIError("这个场次已经开始，请选择之后的日期或时段。")
    return day, slot


def default_overview_date(slots, now=None):
    """Return today until the last slot ends, then move the default to tomorrow."""
    moment = now or datetime.now(TZ)
    day = moment.date()
    end_times = []
    for value in slots or ():
        try:
            end = normalize_slot(value).split("-")[1]
            hour, minute = (int(part) for part in end.split(":"))
            end_times.append(datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ))
        except (TypeError, ValueError, MonitorError):
            continue
    if end_times and moment >= max(end_times):
        return day + timedelta(days=1)
    return day


def scan_overview(client, slots, use_day, items=None, cancel_event=None, on_event=None, automatic=False):
    """Read each fitness-ticket detail and return a safe, read-only stock overview."""
    deadline = time.monotonic() + 60
    if items is None:
        items = read_retry(
            client,
            lambda: client.request("ticket/getOnlineAllTicketInfo", shopNum=client.shop_num,
                                   token=client.token, ticketName="健身票"),
            deadline=deadline, cancel_event=cancel_event, on_event=on_event, stage="catalog",
        )
    if not isinstance(items, list):
        raise MonitorError("官网没有返回可识别的健身票列表。")

    ticket_ids = {}
    for item in items:
        if not isinstance(item, dict) or not str(item.get("name", "")).startswith("健身票"):
            continue
        try:
            current_slot = normalize_slot(item["begintime"][:5] + "-" + item["endtime"][:5])
            current_id = int(item.get("id", item.get("ticketId")))
        except (KeyError, TypeError, ValueError, MonitorError):
            continue
        if current_slot in ticket_ids and ticket_ids[current_slot] != current_id:
            ticket_ids[current_slot] = None
        else:
            ticket_ids[current_slot] = current_id

    result = []
    total = len(slots)
    for index, current_slot in enumerate(slots, start=1):
        check_cancelled(cancel_event)
        report_event(on_event, "scanning", use_day=use_day, slot=current_slot,
                     index=index, total=total)
        ticket_id = ticket_ids.get(current_slot)
        row = {"slot": current_slot, "ticket_id": ticket_id, "remaining": None, "status": "unknown"}
        if ticket_id is None:
            result.append(row)
            continue
        try:
            detail = read_retry(
                client, lambda ticket_id=ticket_id: client.detail(ticket_id), deadline=deadline,
                cancel_event=cancel_event, on_event=on_event, stage="overview_detail",
            )
            matches = [item for item in ticket_rows(detail) if item["date"] == use_day]
            if len(matches) == 1 and matches[0]["remaining"] is not None:
                row["remaining"] = matches[0]["remaining"]
                row["status"] = "available" if row["remaining"] > 0 else "sold"
        except LoginRequired:
            raise
        except RunCancelled:
            raise
        except MonitorError:
            # One malformed or slow slot must not hide the other readable slots.
            pass
        result.append(row)
    return {"use_day": use_day, "checked_at": datetime.now(TZ).isoformat(),
            "automatic": bool(automatic), "slots": result}


class BookingService:
    """同一时刻只运行一个操作。凭据留在进程内，不返回浏览器或写盘。"""

    def __init__(self, directory=ROOT, client_factory=DirectClient):
        self.directory = Path(directory)
        self.client_factory = client_factory
        self.client = None
        self.lock = threading.RLock()
        self.worker = None
        self.cancel_event = threading.Event()
        self.csrf = secrets.token_urlsafe(32)
        self.state = {"revision": 0, "connected": False, "verified_at": None, "slots": DEFAULT_SLOTS,
                      "overview": None, "plan": None, "plan_id": None, "checked_at": None, "recoveries": [],
                      "job": {"id": None, "target": None, "active": False, "phase": "idle", "kind": None,
                              "can_cancel": False, "message": "", "result": None}}

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.state)
        result.update(app="ontime", csrf=self.csrf, server_now=datetime.now(TZ).isoformat(),
                      default_date=default_overview_date(result.get("slots")).isoformat())
        return result

    def ensure_idle(self):
        if self.state["job"]["active"]:
            raise UIError("当前操作还在进行，请先等待完成或停止预约。", 409)

    def ensure_connected(self):
        if not self.client or not self.state["connected"]:
            raise UIError("请先粘贴官方预约平台的入口链接，连接账号。", 409)

    def progress(self, phase, values=None):
        with self.lock:
            if self.state["job"]["phase"] != "stopping":
                self.state["job"].update(phase=phase, values=values or {})

    def invalidate_login(self):
        self.state.update(connected=False, verified_at=None, overview=None, plan=None,
                          plan_id=None, checked_at=None)

    def recoveries(self):
        if not self.client:
            return []
        found = []
        for path in (self.directory / "booking-state").glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("state") not in {"unknown", "submission_started", "rejected_unconfirmed"}:
                    continue
                attempt = Attempt(self.directory / "booking-state", self.client.user_id,
                                  int(record["ticket_id"]), record["use_day"])
                if attempt.path == path:
                    found.append({"use_day": record["use_day"], "slot": record["slot"],
                                  "state": record["state"]})
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
        return sorted(found, key=lambda item: (item["use_day"], item["slot"]), reverse=True)

    def launch(self, kind, operation, cancellable=True, booking_target=None):
        self.ensure_idle()
        self.cancel_event = threading.Event()
        self.state["revision"] += 1
        self.state["job"] = {"id": secrets.token_urlsafe(16), "target": booking_target,
                             "active": True, "phase": kind, "kind": kind,
                             "can_cancel": cancellable, "message": "", "result": None,
                             "started_at": datetime.now(TZ).isoformat(), "values": {}}

        def run():
            result = None
            state = "error"
            message = ""
            diagnostic_start = 0
            try:
                diagnostic_start = len(self.client.timings) if self.client else 0
                result = operation()
                state = result["state"]
                message = result.get("message", "")
                if result.get("requires_login"):
                    with self.lock:
                        self.invalidate_login()
            except RunCancelled:
                state, message = "cancelled", "已停止，未发送新的订单请求。"
            except LoginRequired:
                state, message = "login_required", "登录失效或访问受限。请从官方预约平台重新复制本人入口。"
                with self.lock:
                    self.invalidate_login()
            except (MonitorError, UIError) as exc:
                message = str(exc)
            except Exception:
                # 不把可能包含接口参数的异常堆栈或响应内容返回页面。
                message = "操作未能完成。如已提交，请先在官方预约平台核对订单，再继续操作。"
            finally:
                if self.client:
                    try:
                        diagnostic_client = copy.copy(self.client)
                        diagnostic_client.timings = self.client.timings[diagnostic_start:]
                        save_diagnostics(diagnostic_client, self.directory / "booking-diagnostics", state)
                    except Exception:
                        message = message or "操作已结束，但诊断记录未能保存。"
                with self.lock:
                    try:
                        self.state["recoveries"] = self.recoveries()
                    except Exception:
                        message = message or "操作已结束，但历史提交记录暂时无法读取，请在官方预约平台核对订单。"
                    self.state["job"].update(active=False, phase=state, can_cancel=False,
                                             result=result, message=message,
                                             finished_at=datetime.now(TZ).isoformat())

        try:
            self.worker = threading.Thread(target=run, daemon=True, name="booking-" + kind)
            self.worker.start()
        except Exception:
            message = "本地任务未能启动，请稍后重试。"
            self.state["job"].update(active=False, phase="error", can_cancel=False, message=message)
            raise UIError(message, 503) from None

    def connect(self, data):
        try:
            entry = data["entry"]
            if not isinstance(entry, str) or len(entry) > 4096:
                raise ValueError()
            user_id = parse_entry_url(entry.strip())
        except (KeyError, ValueError, MonitorError):
            raise UIError("请粘贴从官方预约平台「体育场馆预约」复制的完整本人入口链接。") from None
        with self.lock:
            self.ensure_idle()
            if self.client:
                self.client.close()
            self.client = self.client_factory(user_id)
            self.state.update(connected=False, verified_at=None, overview=None, plan=None,
                              plan_id=None, checked_at=None, recoveries=[])

            def operation():
                deadline = time.monotonic() + 40
                read_retry(self.client, self.client.login, deadline=deadline,
                           cancel_event=self.cancel_event, on_event=self.progress, stage="login")
                items = read_retry(self.client, lambda: self.client.request(
                    "ticket/getOnlineAllTicketInfo", shopNum=self.client.shop_num,
                    token=self.client.token, ticketName="健身票"), deadline=deadline,
                    cancel_event=self.cancel_event, on_event=self.progress, stage="catalog")
                if not isinstance(items, list):
                    raise MonitorError("官网没有返回可识别的健身票列表。")
                slots = set()
                for item in items:
                    if isinstance(item, dict) and str(item.get("name", "")).startswith("健身票"):
                        try:
                            slots.add(normalize_slot(item["begintime"][:5] + "-" + item["endtime"][:5]))
                        except (KeyError, TypeError, ValueError):
                            raise MonitorError("官网票种时间格式变化，请先在官方预约平台查看。") from None
                if not slots:
                    raise MonitorError("官网目前没有返回健身票，请稍后重新连接。")
                sorted_slots = sorted(slots)
                overview_day = default_overview_date(sorted_slots).isoformat()
                with self.lock:
                    check_cancelled(self.cancel_event)
                    self.state.update(connected=True, verified_at=datetime.now(TZ).isoformat(),
                                      slots=sorted_slots)
                overview = scan_overview(self.client, sorted_slots, overview_day, items=items,
                                         cancel_event=self.cancel_event, on_event=self.progress,
                                         automatic=True)
                with self.lock:
                    check_cancelled(self.cancel_event)
                    self.state.update(overview=overview, verified_at=datetime.now(TZ).isoformat())
                    self.state["job"]["can_cancel"] = False
                return {"state": "connected", "overview": overview}

            self.launch("connecting", operation)

    def check(self, data):
        day, slot = target(data)
        with self.lock:
            self.ensure_idle()
            self.ensure_connected()
            if slot not in self.state["slots"]:
                raise UIError("官网当前没有这个时段，请重新选择。")
            self.state.update(plan=None, plan_id=None, checked_at=None)

            def operation():
                plan = read_retry(self.client, lambda: prepare(self.client, slot, day, on_event=self.progress),
                                  deadline=time.monotonic() + 60, cancel_event=self.cancel_event,
                                  on_event=self.progress, stage="preflight")
                with self.lock:
                    check_cancelled(self.cancel_event)
                    self.state.update(plan=plan, plan_id=secrets.token_urlsafe(20),
                                      checked_at=datetime.now(TZ).isoformat(),
                                      verified_at=datetime.now(TZ).isoformat())
                    self.state["job"]["can_cancel"] = False
                return {"state": "ready"}

            self.launch("checking_plan", operation, booking_target={"use_day": day, "slot": slot})

    def overview(self, data):
        with self.lock:
            self.ensure_idle()
            self.ensure_connected()
            raw_day = data.get("use_day")
            automatic = data.get("automatic") is True
            if raw_day:
                try:
                    use_day = date.fromisoformat(raw_day).isoformat()
                except (TypeError, ValueError):
                    raise UIError("请选择有效的使用日期。") from None
            else:
                use_day = default_overview_date(self.state["slots"]).isoformat()
            slots = list(self.state["slots"])

            def operation():
                overview = scan_overview(self.client, slots, use_day,
                                         cancel_event=self.cancel_event, on_event=self.progress,
                                         automatic=automatic)
                with self.lock:
                    check_cancelled(self.cancel_event)
                    self.state.update(overview=overview, verified_at=datetime.now(TZ).isoformat())
                    self.state["job"]["can_cancel"] = False
                return {"state": "overview_ready", "overview": overview}

            self.launch("scanning", operation, booking_target={"use_day": use_day, "slot": ""})

    def schedule(self, data):
        with self.lock:
            self.ensure_idle()
            self.ensure_connected()
            plan = self.state["plan"]
            if not plan or data.get("plan_id") != self.state["plan_id"]:
                raise UIError("日期或时段已变化，请先重新检查余票。", 409)
            if plan.get("fast_only"):
                raise UIError("当前检查只确认了余票状态；确认有票后才能进入预约。", 409)
            if data.get("accept") is not True or data.get("notice_hash") != plan["notice"]["hash"]:
                raise UIError("请阅读当前购票须知，并勾选同意后再预约。")
            if (datetime.now(TZ) - datetime.fromisoformat(self.state["checked_at"])).total_seconds() > 600:
                raise UIError("上次检查已超过 10 分钟，请重新检查后预约。", 409)
            target(plan)

            def before_submit():
                with self.lock:
                    check_cancelled(self.cancel_event)
                    self.state["job"].update(can_cancel=False, phase="submitting")

            def operation():
                return execute(self.client, plan, data["notice_hash"], self.directory / "booking-state",
                               cancel_event=self.cancel_event, on_event=self.progress,
                               before_submit=before_submit)

            self.launch("scheduling", operation, booking_target={"use_day": plan["use_day"], "slot": plan["slot"]})

    def cancel(self, data):
        with self.lock:
            job = self.state["job"]
            if not job["active"]:
                raise UIError("当前没有进行中的操作。", 409)
            if data.get("job_id") != job["id"]:
                raise UIError("当前任务已变化，请刷新状态后再决定是否停止。", 409)
            if not job["can_cancel"]:
                if job["kind"] in {"connecting", "checking_plan"}:
                    raise UIError("检查结果已返回，正在完成收尾，请稍候。", 409)
                raise UIError("已经进入提交或订单核对阶段。请等待结果，停止查询不会取消官网订单。", 409)
            self.cancel_event.set()
            job.update(phase="stopping", can_cancel=False)

    def reconcile(self, data):
        day, slot = target(data, allow_past=True)
        with self.lock:
            self.ensure_idle()
            self.ensure_connected()
            self.launch("reconciling", lambda: reconcile_saved(
                self.client, self.directory / "booking-state", slot, day, on_event=self.progress),
                cancellable=False, booking_target={"use_day": day, "slot": slot})

    def disconnect(self, data):
        with self.lock:
            self.ensure_idle()
            if self.client:
                self.client.close()
            self.client = None
            self.state.update(connected=False, verified_at=None, overview=None, plan=None,
                              plan_id=None, checked_at=None, recoveries=[])
            self.state["revision"] += 1
            self.state["job"] = {"id": None, "target": None, "active": False, "phase": "idle", "kind": None,
                                 "can_cancel": False, "message": "", "result": None}

    def close(self):
        with self.lock:
            if self.state["job"]["can_cancel"]:
                self.cancel_event.set()
            if self.client and not self.state["job"]["active"]:
                self.client.close()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service):
        self.service = service
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body, content_type="application/json; charset=utf-8"):
        raw = json.dumps(body, ensure_ascii=False).encode() if isinstance(body, dict) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self' data:; font-src 'self'; object-src 'none'; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def local_origin(self):
        port = self.server.server_port
        host = self.headers.get("Host", "")
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            raise UIError("此页面仅供本机访问。", 403)
        return "http://" + host

    def do_GET(self):
        try:
            self.local_origin()
            if self.path == "/api/state":
                return self.send(200, self.server.service.snapshot())
            static = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.css": ("app.css", "text/css; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/favicon.ico": (None, "image/x-icon")}
            if self.path not in static:
                raise UIError("页面不存在。", 404)
            file, kind = static[self.path]
            self.send(200, (ROOT / "web" / file).read_bytes() if file else b"", kind)
        except UIError as exc:
            self.send(exc.status, {"error": str(exc)})
        except Exception:
            self.send(500, {"error": "本地页面或状态暂时无法读取，请检查启动窗口。"})

    def do_POST(self):
        try:
            origin = self.local_origin()
            csrf = self.headers.get("X-CSRF-Token", "")
            if self.headers.get("Origin") != origin or not csrf.isascii() or not secrets.compare_digest(
                    csrf, self.server.service.csrf):
                raise UIError("页面连接已更新，请刷新后再试。", 403)
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise UIError("请求格式错误。", 415)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError()
                self.connection.settimeout(5)
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError()
            except (ValueError, OSError):
                raise UIError("请求内容无效或过长。") from None
            routes = {"/api/connect": "connect", "/api/check": "check", "/api/overview": "overview",
                      "/api/schedule": "schedule",
                      "/api/cancel": "cancel", "/api/reconcile": "reconcile", "/api/disconnect": "disconnect"}
            if self.path not in routes:
                raise UIError("操作不存在。", 404)
            service = self.server.service
            # 页面版本与操作同时检查，防止旧页面或迟到请求影响新的账号、任务。
            with service.lock:
                if type(data.get("revision")) is not int or data["revision"] != service.state["revision"]:
                    raise UIError("账号或任务状态已更新，请刷新后再操作。", 409)
                getattr(service, routes[self.path])(data)
            self.send(202, {"ok": True})
        except UIError as exc:
            self.send(exc.status, {"error": str(exc)})
        except Exception:
            self.send(500, {"error": "本地操作出现异常，请先刷新确认任务状态；已提交的订单请在官方预约平台核对。"})


def running_app_url(port):
    address = f"http://127.0.0.1:{port}"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(address + "/api/state", timeout=1) as response:
            state = json.loads(response.read(100000))
        return address if isinstance(state, dict) and state.get("app") == "ontime" else None
    except (OSError, ValueError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="启动后在默认浏览器打开页面")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口应为 1–65535")
    if args.open:
        existing = running_app_url(args.port)
        if existing:
            webbrowser.open(existing)
            print("准点已经在运行，已打开原页面。", flush=True)
            return
    service = BookingService()
    try:
        server = LocalServer(("127.0.0.1", args.port), service)
    except PermissionError:
        raise SystemExit("当前运行环境不允许开启本地服务。请在本机终端运行启动文件，或允许本地回环端口访问。")
    except OSError:
        raise SystemExit("本地端口无法使用。已有页面时请直接打开，或用 --port 8766 指定其他端口。")
    address = f"http://127.0.0.1:{server.server_port}"
    print(f"准点已启动：{address}\n预约期间请保持此进程运行、电脑唤醒。按 Ctrl+C 关闭。", flush=True)
    if args.open:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n本地服务已停止；如果已提交，请在官方预约平台核对订单。", flush=True)
    finally:
        service.close()
        server.server_close()


if __name__ == "__main__":
    main()
