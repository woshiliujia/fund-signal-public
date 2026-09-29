#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基金信号管理后台服务
启动: python3 server.py [--port 8787] [--no-scheduler]
API 见 static/index.html 调用。
"""
import argparse
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import datetime
import secrets
import http.cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fund_core
import xalpha_adapter

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PORT = 8787
INTERVAL_SECONDS = 60 * 10  # 定时检查：每10分钟(非交易时段不推送)

# ---------- 会话管理 ----------
SESSIONS = {}          # token -> username
SESSION_TTL = 30 * 24 * 3600   # 30天记住登录
SESSION_LOCK = threading.Lock()

# 访客只可查看脱敏后的总览与公开消息；持仓、历史和估值均须登录。
READONLY_PATHS = {
    "/api/dashboard", "/api/news", "/api/strategies", "/api/auth/options",
}
PUBLIC_PATHS = {"/api/login", "/api/register"}


def _guest_dashboard():
    """Return a fixed, non-personalized dashboard for unauthenticated visitors."""
    return {
        "is_demo": True,
        "funds": [{
            "code": "000001",
            "name": "示例基金 · 科技成长",
            "signal": "持有",
            "reason": "这是访客演示信号。登录后可查看你的真实持仓与策略。",
            "industry": "科技成长",
            "news_counts": {"利好": 3, "利空": 1, "中性": 2},
            "strategies": [
                {"label": "趋势策略", "signal": "持有"},
                {"label": "风险控制", "signal": "持有"},
            ],
            "hold": None,
        }],
        "funds_cfg": [],
        "errors": [],
        "history_count": 0,
        "news_stats": {"利好": 3, "利空": 1, "中性": 2},
        "industry_stats": {},
        "latest_news": fund_core.list_news(limit=4),
    }


def _create_session(username, ttl=None):
    token = secrets.token_hex(24)
    duration = ttl or SESSION_TTL
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=duration)
    with SESSION_LOCK:
        SESSIONS[token] = {"user": username, "exp": expires_at.timestamp()}
    fund_core.save_user_session(token, username, expires_at)
    return token


def _extend_session(token, ttl):
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=ttl)
    extended = fund_core.extend_user_session(token, expires_at)
    with SESSION_LOCK:
        s = SESSIONS.get(token)
        if s:
            s["exp"] = expires_at.timestamp()
            return True
    return extended


def _check_session(handler):
    cookie = handler.headers.get("Cookie") or ""
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("fs_session="):
            token = part[len("fs_session="):]
            # PostgreSQL is persistent across container replacement. Only use
            # the in-memory fallback when the database is unavailable.
            if fund_core.pg_available():
                return fund_core.get_user_session(token)
            with SESSION_LOCK:
                s = SESSIONS.get(token)
                if s and s["exp"] > time.time():
                    return s["user"]
                SESSIONS.pop(token, None)
    return None


def _clear_session(token):
    fund_core.delete_user_session(token)
    with SESSION_LOCK:
        SESSIONS.pop(token, None)


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def _read_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if not length:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise ApiError(400, "请求体不是合法 JSON")


def handle_api(path, method, body, query="", user=None):
    cfg = fund_core.load_config()

    if path == "/api/login" and method == "POST":
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if fund_core.check_login(username, password):
            return {"ok": True, "token": _create_session(username), "username": username}
        raise ApiError(401, "用户名或密码错误")

    if path == "/api/register" and method == "POST":
        try:
            fund_core.register_user(body.get("username"), body.get("password"),
                                    body.get("invite_code"))
        except RuntimeError as e:
            raise ApiError(400, str(e))
        if fund_core.check_login(body.get("username"), body.get("password")):
            return {"ok": True, "token": _create_session(body.get("username")),
                    "username": body.get("username")}
        raise ApiError(500, "注册成功但登录失败，请手动登录")

    if path == "/api/auth/options" and method == "GET":
        return {"public_registration": fund_core.public_registration_enabled()}

    if path == "/api/invites" and method == "POST":
        return {"ok": True, "code": fund_core.create_invite_code()}

    if path == "/api/invites" and method == "GET":
        return {"ok": True, "invites": fund_core.list_invite_codes()}

    if path == "/api/logout" and method == "POST":
        return {"ok": True}

    if path == "/api/me" and method == "GET":
        return {"ok": True, "username": user}

    if path == "/api/password" and method == "PUT":
        if not fund_core.change_password(user, body.get("old_password", ""),
                                         body.get("new_password", "")):
            raise ApiError(400, "原密码错误或新密码无效")
        return {"ok": True}

    if path == "/api/config" and method == "GET":
        return {"notify": cfg["notify"], "funds": cfg["funds"]}

    if path == "/api/config" and method == "PUT":
        if "notify" in body:
            n = body["notify"]
            cfg["notify"] = {
                "channel": n.get("channel", cfg["notify"].get("channel", "file")),
                "serverchan_key": n.get("serverchan_key", ""),
                "wecom_webhook": n.get("wecom_webhook", ""),
                "dingtalk_webhook": n.get("dingtalk_webhook", ""),
                "wxpusher_apptoken": n.get("wxpusher_apptoken", ""),
                "wxpusher_uid": n.get("wxpusher_uid", ""),
            }
        if "funds" in body:
            cfg["funds"] = body["funds"]
        fund_core.save_config(cfg)
        return {"ok": True}

    if path == "/api/funds" and method == "POST":
        code = str(body.get("code", "")).strip()
        if not code or not (code.isdigit() and len(code) == 6):
            raise ApiError(400, "基金代码需为6位数字")
        for f in cfg["funds"]:
            if f["code"] == code:
                raise ApiError(400, "基金 %s 已存在" % code)
        name = body.get("name")
        if not name:
            name = fund_core.fetch_fund_name(code)
        fund = {
            "id": code,
            "code": code,
            "name": name or code,
            "strategy": body.get("strategy") or fund_core.STRATEGY_TARGET,
            "params": body.get("params") or {},
        }
        cfg["funds"].append(fund)
        fund_core.save_config(cfg)
        return {"ok": True, "fund": fund}

    if path.startswith("/api/funds/") and method == "PUT":
        fund_id = path.split("/")[-1]
        fund = next((f for f in cfg["funds"] if f["id"] == fund_id), None)
        if not fund:
            raise ApiError(404, "基金不存在")
        if "name" in body:
            fund["name"] = body["name"]
        if "strategy" in body:
            fund["strategy"] = body["strategy"]
        if "params" in body:
            p = fund.setdefault("params", {})
            p.update(body["params"])
            for k in list(p.keys()):
                if p[k] == "" or p[k] is None:
                    p.pop(k)
            if "index_secid" in p:
                p["index_secid"] = str(p["index_secid"])
            for k in ("cost_price", "target_gain", "stop_loss", "ma_days", "hold_amount"):
                if k in p and p[k] != "":
                    try:
                        p[k] = float(p[k])
                    except Exception:
                        p.pop(k, None)
        if "strategies" in body and isinstance(body["strategies"], list):
            clean = []
            for s in body["strategies"]:
                if isinstance(s, dict) and s.get("strategy"):
                    sp = dict(s.get("params") or {})
                    if "index_secid" in sp:
                        sp["index_secid"] = str(sp["index_secid"])
                    for k in ("cost_price", "target_gain", "stop_loss", "ma_days", "hold_amount"):
                        if k in sp and sp[k] != "":
                            try:
                                sp[k] = float(sp[k])
                            except Exception:
                                sp.pop(k, None)
                    clean.append({"strategy": s["strategy"], "params": sp})
            fund["strategies"] = clean
        if body.get("lookup_name"):
            n = fund_core.fetch_fund_name(fund["code"])
            if n:
                fund["name"] = n
        fund_core.save_config(cfg)
        return {"ok": True, "fund": fund}

    if path.startswith("/api/funds/") and path.endswith("/ai-recommend") and method == "POST":
        fund_id = path.split("/")[-2]
        fund = next((f for f in cfg["funds"] if f["id"] == fund_id), None)
        if not fund:
            raise ApiError(404, "基金不存在")
        try:
            rec = fund_core.ai_recommend_fund(fund)
            return {"ok": True, **rec}
        except Exception as e:
            raise ApiError(400, "AI 推荐失败: %s" % e)

    if path.startswith("/api/funds/") and method == "DELETE":
        fund_id = path.split("/")[-1]
        before = len(cfg["funds"])
        cfg["funds"] = [f for f in cfg["funds"] if f["id"] != fund_id]
        if len(cfg["funds"]) == before:
            raise ApiError(404, "基金不存在")
        fund_core.save_config(cfg)
        return {"ok": True}

    if path == "/api/quote" and method == "GET":
        from urllib.parse import parse_qs
        params = parse_qs(query)
        code = params.get("code", [""])[0].strip()
        if not (code.isdigit() and len(code) == 6):
            raise ApiError(400, "基金代码需为6位数字")
        try:
            days = max(5, min(int(params.get("days", [30])[0]), 90))
        except (TypeError, ValueError):
            days = 30
        configured_fund = next((f for f in cfg["funds"] if f["code"] == code), None)
        name = configured_fund.get("name") if configured_fund else code
        history = fund_core.fetch_fund_history(code, days=days)
        est = fund_core.fetch_fund_estimate(code)
        history_out = []
        for i, (date, nav) in enumerate(history):
            previous = history[i - 1][1] if i else None
            history_out.append({
                "date": date,
                "nav": nav,
                "change_pct": (nav - previous) / previous * 100 if previous else None,
            })
        idx_rt = None
        today_chg = None
        if configured_fund:
            secid = None
            for sc in fund_core.get_fund_strategies(configured_fund):
                s = (sc.get("params") or {}).get("index_secid")
                if s:
                    secid = s
                    break
            if secid:
                idx_rt = fund_core.fetch_index_realtime(secid)
                if idx_rt:
                    today_chg = idx_rt.get("chg_pct")
        return {
            "code": code,
            "name": name or code,
            "history": history_out,
            "estimate": est,
            "today_chg": today_chg,
            "idx_rt": {"name": idx_rt["name"], "price": idx_rt["price"],
                       "chg_pct": idx_rt.get("chg_pct"), "time": idx_rt.get("time")}
            if idx_rt else None,
        }

    if path == "/api/run" and method == "POST":
        force = bool(body.get("force"))
        funds_out, errors, push_results, content = fund_core.run_all(cfg, force=force)
        return {
            "ok": True,
            "funds": funds_out,
            "errors": errors,
            "push": push_results,
            "content": content,
            "notified": bool(push_results),
        }

    if path == "/api/history" and method == "GET":
        return {"history": fund_core.load_history()[-50:]}

    if path == "/api/strategies" and method == "GET":
        return {"strategies": fund_core.STRATEGY_LABELS}

    if path == "/api/xalpha/status" and method == "GET":
        return xalpha_adapter.status()

    if path == "/api/xalpha/portfolio" and method == "POST":
        try:
            result = xalpha_adapter.analyse_portfolio(body.get("transactions"))
        except xalpha_adapter.XalphaIntegrationError as exc:
            raise ApiError(400, str(exc))
        return {"ok": True, **result}

    if path == "/api/news" and method == "GET":
        from urllib.parse import parse_qs
        q = parse_qs(query)
        return {
            "news": fund_core.list_news(
                limit=int(q.get("limit", ["50"])[0]),
                category=q.get("category", [None])[0],
                industry=q.get("industry", [None])[0],
                source=q.get("source", [None])[0],
            ),
            "stats": fund_core.news_stats(),
        }

    if path == "/api/news" and method == "POST":
        title = (body.get("title") or "").strip()
        if not title:
            raise ApiError(400, "标题不能为空")
        fund_core.add_news_manual(title, body.get("content") or "",
                                  body.get("category") or "中性",
                                  body.get("industry") or "综合")
        return {"ok": True}

    if path == "/api/news/fetch" and method == "POST":
        added, total = fund_core.collect_news()
        return {"ok": True, "added": added, "total": total}

    if path == "/api/news/interval" and method == "GET":
        return fund_core.get_news_interval()

    if path == "/api/news/interval" and method == "PUT":
        fund_core.save_news_interval(
            int(body.get("trading") or 1), int(body.get("idle") or 10))
        return {"ok": True}

    if path == "/api/news/analyze" and method == "POST":
        limit = int(body.get("limit") or 30)
        force = bool(body.get("force"))
        r = fund_core.analyze_news(limit=limit, force=force)
        return {"ok": True, **r}

    if path == "/api/ai" and method == "GET":
        c = fund_core.get_ai_config()
        # Never send a stored credential back to the browser.  The UI leaves
        # an empty field unchanged on save, so a published deployment does not
        # accidentally expose the key to a logged-in account.
        return {"base_url": c["base_url"], "configured": bool(c["api_key"]), "model": c["model"]}

    if path == "/api/ai" and method == "PUT":
        fund_core.save_ai_config(body)
        return {"ok": True}

    if path.startswith("/api/news/") and method == "DELETE":
        fund_core.delete_news(path.split("/")[-1])
        return {"ok": True}

    if path == "/api/dashboard" and method == "GET":
        if not user:
            return _guest_dashboard()
        funds_out, errors = fund_core.evaluate_all(cfg)
        hist = fund_core.load_history()
        ind_stats = fund_core.news_stats_by_industry()
        for f in cfg["funds"]:
            ind = fund_core.fund_industry(f)
            f["industry"] = ind
            f["news_counts"] = ind_stats.get(ind, {})
            for o in funds_out:
                if o["code"] == f["code"]:
                    o["industry"] = ind
                    o["news_counts"] = ind_stats.get(ind, {})
        return {
            "funds": funds_out,
            "funds_cfg": cfg["funds"],
            "errors": errors,
            "history_count": len(hist),
            "news_stats": fund_core.news_stats(),
            "industry_stats": ind_stats,
            "latest_news": fund_core.list_news(limit=10),
        }

    if path == "/api/backtest" and method == "POST":
        code = str(body.get("code", "")).strip()
        strategy = body.get("strategy") or "drawdown"
        params = body.get("params") or {}
        days = int(body.get("days") or 250)
        if not (code.isdigit() and len(code) == 6):
            raise ApiError(400, "基金代码需为6位数字")
        if strategy not in fund_core.BACKTEST_STRATEGIES:
            raise ApiError(400, "未知策略: %s" % strategy)
        try:
            result = fund_core.backtest_fund(code, strategy, params, days)
        except Exception as e:
            raise ApiError(500, "回测失败: %s" % e)
        return {"ok": True, "strategies": fund_core.BACKTEST_STRATEGIES, **result}

    if path == "/api/db" and method == "GET":
        c = fund_core.get_db_config()
        return {
            "host": c.get("host", ""), "port": c.get("port", 5432),
            "user": c.get("user", ""),
            "dbname": c.get("dbname", ""),
            "configured": bool(c.get("host") and c.get("dbname")),
            "connected": fund_core.pg_available(),
            "storage": "postgres" if fund_core.pg_available() else "json",
            "mode": "production" if os.environ.get("DB_HOST") else "development",
        }

    raise ApiError(404, "接口不存在: %s %s" % (method, path))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status=200, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path):
        if path in ("/", "/index.html"):
            rel = "index.html"
        else:
            rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        path, _, query = self.path.partition("?")
        if not path.startswith("/api/"):
            self._serve_static(path)
            return
        try:
            body = _read_body(self) if method in ("POST", "PUT") else {}
            if path in PUBLIC_PATHS and method == "POST":
                r = handle_api(path, method, body, query)
                ua = self.headers.get("User-Agent") or ""
                ttl = 90 * 24 * 3600 if "MicroMessenger" in ua else SESSION_TTL
                _extend_session(r["token"], ttl)
                r["expire_days"] = ttl // 86400
                cookie = http.cookies.SimpleCookie()
                cookie["fs_session"] = r.pop("token")
                cookie["fs_session"]["path"] = "/"
                cookie["fs_session"]["httponly"] = True
                cookie["fs_session"]["samesite"] = "Lax"
                cookie["fs_session"]["max-age"] = ttl
                self._send_json(r, extra_headers=[("Set-Cookie", cookie.output(header="").strip())])
                return
            user = _check_session(self)
            is_readonly_get = method == "GET" and path in READONLY_PATHS
            if not user and not is_readonly_get and path not in PUBLIC_PATHS:
                raise ApiError(401, "未登录或会话已过期")
            if path == "/api/logout":
                cookie = self.headers.get("Cookie") or ""
                for part in cookie.split(";"):
                    part = part.strip()
                    if part.startswith("fs_session="):
                        _clear_session(part[len("fs_session="):])
                self._send_json({"ok": True},
                                extra_headers=[("Set-Cookie",
                                                "fs_session=; path=/; max-age=0")])
                return
            self._send_json(handle_api(path, method, body, query, user=user))
        except ApiError as e:
            self._send_json({"ok": False, "error": e.message}, e.status)
        except Exception as e:
            self._send_json({"ok": False, "error": "服务器错误: %s" % e}, 500)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


def scheduler_loop():
    """内置调度：交易日 14:50 信号推送；消息按配置间隔抓取(交易时段/盘后分开)。"""
    last_news_min = -1
    while True:
        try:
            now = datetime.datetime.now()
            if (now.weekday() < 5 and 9 <= now.hour < 15
                    and now.minute == 50 and now.second < 30):
                cfg = fund_core.load_config()
                funds_out, errors, push_results, content = fund_core.run_all(cfg)
                print("[scheduler] %s run, funds=%d errors=%d notified=%s" % (
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    len(funds_out), len(errors), bool(push_results)), flush=True)
                time.sleep(60)
            interval = fund_core.get_news_interval()
            interval_min = (interval.get("trading") if fund_core.is_trading_period(now)
                            else interval.get("idle")) or 10
            cur = now.hour * 60 + now.minute
            if (cur != last_news_min
                    and (last_news_min < 0 or (cur - last_news_min) % 1440 >= interval_min)):
                last_news_min = cur
                try:
                    added, total = fund_core.collect_news()
                    print("[scheduler] %s 消息抓取(%d分钟): 新增%d/%d" % (
                        now.strftime("%H:%M"), interval_min, added, total), flush=True)
                    if fund_core.is_trading_period(now):
                        pushed = fund_core.push_important_news(fund_core.load_config())
                        if pushed:
                            print("[scheduler] 重要消息推送: %d 条" % pushed, flush=True)
                except Exception as e:
                    print("[scheduler] 消息抓取失败: %s" % e, flush=True)
            time.sleep(30)
        except Exception as e:
            print("[scheduler] error: %s" % e, flush=True)
            time.sleep(30)


def main():
    global PORT
    parser = argparse.ArgumentParser(description="基金信号管理后台")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-scheduler", action="store_true", help="禁用内置定时")
    args = parser.parse_args()
    PORT = args.port

    if not args.no_scheduler:
        threading.Thread(target=scheduler_loop, daemon=True).start()
    fund_core.ensure_default_user()
    try:
        added, total = fund_core.collect_news()
        print("[news] 启动抓取: 新增%d/%d" % (added, total), flush=True)
    except Exception as e:
        print("[news] 启动抓取失败: %s" % e, flush=True)

    bind_host = "0.0.0.0" if os.environ.get("BIND_ALL") else "127.0.0.1"
    server = ThreadingHTTPServer((bind_host, PORT), Handler)
    mode = "生产" if os.environ.get("DB_HOST") else "开发"
    print("基金信号管理后台(%s): http://%s:%d" % (mode, bind_host, PORT), flush=True)
    print("数据库: %s" % fund_core.get_db_config(), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
