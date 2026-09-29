#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基金信号核心模块：数据获取、策略计算、推送、配置持久化。"""
import json
import html
import os
import re
import time
import datetime
import hashlib
import urllib.request
import urllib.parse
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE_DIR, "data.json")
HISTORY_PATH = os.path.join(BASE_DIR, "history.json")
LOCK = threading.Lock()
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

SIGNAL_BUY = "买入"
SIGNAL_HOLD = "持有"
SIGNAL_SELL = "卖出"

STRATEGY_TARGET = "target"
STRATEGY_MA = "ma"
STRATEGY_INDEX_MA = "index_ma"
STRATEGY_COMBO = "combo"
STRATEGY_DRAWDOWN = "drawdown"
STRATEGY_DAILY_DROP = "daily_drop"
STRATEGY_ADVANCED = "advanced"
STRATEGY_LABELS = {
    STRATEGY_TARGET: "目标收益/止损",
    STRATEGY_MA: "基金净值均线",
    STRATEGY_INDEX_MA: "跟踪指数均线",
    STRATEGY_COMBO: "组合评分",
    STRATEGY_DRAWDOWN: "回撤止损",
    STRATEGY_DAILY_DROP: "当日暴跌",
    STRATEGY_ADVANCED: "高阶多因子",
}

DEFAULT_CONFIG = {
    "notify": {"channel": "file", "serverchan_key": "", "wecom_webhook": "",
               "dingtalk_webhook": "", "wxpusher_apptoken": "", "wxpusher_uid": ""},
    "funds": [],
}

HISTORY_LIMIT = 100

# Disclosure data changes only quarterly. Keep a small in-memory cache so a
# user opening the same fund detail repeatedly does not repeatedly call the
# third-party archive endpoint.
_LOOKTHROUGH_CACHE = {}
_LOOKTHROUGH_CACHE_TTL = 15 * 60


def http_get(url, headers=None, timeout=15, retries=3, encoding="utf-8"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode(encoding, errors="replace")
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


# ---------- 数据获取 ----------

def fetch_fund_estimate(code):
    """盘中实时估值(天天基金移动接口)。非交易时间/休市返回 None。"""
    url = ("https://fundmobapi.eastmoney.com/FundMApi/FundVarietieValuationDetail.ashx"
           "?FCODE={code}&deviceid=Wap&plat=Wap&product=EFund&version=2.0.0&Uid=").format(code=code)
    try:
        data = json.loads(http_get(url))
        ds = data.get("Datas")
        if not ds:
            return None
        return {
            "name": ds.get("SHORTNAME") or ds.get("FNAME") or code,
            "gsz": float(ds.get("GZ") or 0),
            "gszzl": float(ds.get("GSZZL") or 0),
            "time": ds.get("GZTIME") or "",
        }
    except Exception:
        return None


def fetch_fund_name(code):
    """基金名称(天天基金)。"""
    url = "https://fund.eastmoney.com/pingzhongdata/%s.js" % code
    try:
        text = http_get(url)
        m = re.search(r'fS_name\s*=\s*"([^"]+)"', text)
        if m:
            return m.group(1)
    except Exception:
        pass
    est = fetch_fund_estimate(code)
    if est and est["name"]:
        return est["name"]
    return None


def fetch_fund_lookthrough(code):
    """Return the latest disclosed top stock positions for a public fund.

    This is disclosure data, not a live portfolio: the period is always
    returned to make the reporting lag visible in the UI.
    """
    if not (isinstance(code, str) and re.fullmatch(r"\d{6}", code)):
        raise ValueError("基金代码需为 6 位数字")
    cached = _LOOKTHROUGH_CACHE.get(code)
    if cached and time.time() - cached[0] < _LOOKTHROUGH_CACHE_TTL:
        return cached[1]
    url = ("http://fundf10.eastmoney.com/FundArchivesDatas.aspx?"
           "type=jjcc&code=%s&topline=10&year=&month=&rt=%d") % (code, int(time.time()))
    try:
        text = http_get(url, headers={"Referer": "https://fundf10.eastmoney.com/ccmx_%s.html" % code})
    except Exception as exc:
        raise RuntimeError("暂时无法获取基金披露持仓") from exc
    text = html.unescape(text.replace(r'\\"', '"'))
    period = re.search(r"(\d{4}年[1-4]季度)股票投资明细.*?截止至：.*?(\d{4}-\d{2}-\d{2})", text, re.S)
    rows = []
    row_pattern = re.compile(
        r"<tr><td>\d+</td><td><a[^>]*>(?P<code>\d{6})</a></td>"
        r"<td[^>]*><a[^>]*>(?P<name>[^<]+)</a>.*?"
        r"<td class='tor'>(?P<weight>[\d.]+)%</td>", re.S,
    )
    for match in row_pattern.finditer(text):
        rows.append({
            "code": match.group("code"),
            "name": html.unescape(match.group("name")).strip(),
            "weight_pct": float(match.group("weight")),
        })
        if len(rows) == 10:
            break
    if not rows:
        raise RuntimeError("该基金暂无可用的股票持仓披露")
    result = {
        "code": code,
        "period": period.group(1) if period else "最近披露期",
        "as_of": period.group(2) if period else None,
        "holdings": rows,
        "source": "天天基金公开披露持仓",
        "notice": "持仓按定期报告披露，存在滞后；不代表实时持仓，也不构成投资建议。",
    }
    _LOOKTHROUGH_CACHE[code] = (time.time(), result)
    return result


def fetch_fund_history(code, days=60):
    """历史净值序列(单位净值)。返回 [(date, nav), ...] 按日期升序。支持翻页。"""
    out = []
    per_page = 20
    pages = max(1, -(-days // per_page))
    for page in range(1, pages + 1):
        url = ("https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}"
               "&pageIndex={page}&pageSize={ps}&startDate=&endDate=").format(
                   code=code, page=page, ps=per_page)
        try:
            data = json.loads(http_get(url, headers={"Referer": "https://fundf10.eastmoney.com/"}))
            rows = (data or {}).get("Data", {}).get("LSJZList", [])
        except Exception:
            rows = []
        if not rows:
            break
        for r in rows:
            nav = r.get("DWJZ")
            if nav:
                out.append((r["FSRQ"], float(nav)))
        if len(rows) < per_page:
            break
        time.sleep(0.3)
    return list(reversed(out[:days]))


def _sina_symbol(secid):
    """东财 secid(1.512480/0.159995) → 新浪 symbol(sh512480/sz159995)"""
    market, code = secid.split(".")
    return ("sh" if market == "1" else "sz") + code


def fetch_index_realtime(secid):
    """指数实时行情(新浪)。返回 {name, price, prev_close, chg_pct, date} 或 None。"""
    url = "https://hq.sinajs.cn/list=" + _sina_symbol(secid)
    try:
        text = http_get(url, headers={"Referer": "https://finance.sina.com.cn/"}, encoding="gbk")
        m = re.search(r'"(.*)"', text)
        if not m:
            return None
        parts = m.group(1).split(",")
        if len(parts) < 32 or not parts[3]:
            return None
        price = float(parts[3])
        if price <= 0:
            return None
        prev = float(parts[2]) if parts[2] else 0
        chg = (price - prev) / prev if prev else None
        return {
            "name": parts[0], "price": price, "prev_close": prev,
            "chg_pct": chg, "date": parts[30], "time": parts[31],
        }
    except Exception:
        return None


def fetch_index_kline(secid, days=60):
    """指数日K收盘序列(新浪)。返回 [(date, close), ...] 按日期升序。"""
    url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%%20_data="
           "/CN_MarketDataService.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d"
           % (_sina_symbol(secid), days))
    try:
        text = http_get(url)
        m = re.search(r"\((.*)\)\s*;?\s*$", text, re.S)
        if not m:
            return []
        rows = json.loads(m.group(1))
        return [(r["day"], float(r["close"])) for r in rows]
    except Exception:
        return []


# ---------- 策略 ----------

def calc_ma(values, n):
    n = int(n)
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def fmt_pct(x):
    if x is None:
        return "N/A"
    return ("%+.2f%%" % (x * 100))


def strategy_target(f, est_price, est_pct, history):
    """目标收益/止损: 收益率达目标→卖出, 跌破止损→卖出, 否则持有。"""
    p = f.get("params", {})
    cost = p.get("cost_price")
    if not cost:
        return SIGNAL_HOLD, "未配置持仓成本价，无法计算收益率"
    cur = est_price or (history[-1][1] if history else None)
    if not cur:
        return SIGNAL_HOLD, "无行情数据"
    ret = (cur - cost) / cost
    target = p.get("target_gain", 0.10)
    stop = p.get("stop_loss", -0.08)
    if ret >= target:
        return SIGNAL_SELL, "收益率 %s 达到止盈目标 %+.1f%%" % (fmt_pct(ret), target * 100)
    if ret <= stop:
        return SIGNAL_SELL, "收益率 %s 触发止损线 %+.1f%%" % (fmt_pct(ret), stop * 100)
    if ret < -0.02 and est_pct is not None and est_pct < 0:
        return SIGNAL_SELL, "收益率 %s 低于 -2%% 且当日下跌 %s" % (fmt_pct(ret), fmt_pct(est_pct))
    return SIGNAL_BUY if ret < 0 else SIGNAL_HOLD, "收益率 %s，未触及止盈/止损，可继续持有" % fmt_pct(ret)


def strategy_ma(f, est_price, est_pct, history):
    """均线趋势: 价格站上N日均线→持有/买入, 跌破→卖出。"""
    n = f.get("params", {}).get("ma_days", 20)
    closes = [x[1] for x in history]
    ma = calc_ma(closes, n)
    if not closes:
        return SIGNAL_HOLD, "无历史净值数据"
    cur = est_price or closes[-1]
    if ma is None:
        return SIGNAL_HOLD, "历史数据不足 %d 天" % n
    dev = (cur - ma) / ma
    if est_pct is not None and est_pct >= 0.015:
        return SIGNAL_BUY, "当日上涨 %s，突破 %d 日均线(偏离 %s)" % (fmt_pct(est_pct), n, fmt_pct(dev))
    if dev > 0:
        return SIGNAL_BUY if dev > 0.02 else SIGNAL_HOLD, "净值高于 %d 日均线 %s" % (n, fmt_pct(dev))
    if est_pct is not None and est_pct <= -0.015:
        return SIGNAL_SELL, "当日下跌 %s，跌破 %d 日均线(偏离 %s)" % (fmt_pct(est_pct), n, fmt_pct(dev))
    return SIGNAL_SELL if dev < -0.01 else SIGNAL_HOLD, "净值低于 %d 日均线 %s" % (n, fmt_pct(dev))


def strategy_index_ma(f, est_price, est_pct, history, idx_rt, idx_kline):
    """指数趋势: 跟踪指数站上N日均线→买入, 跌破→卖出。"""
    n = f.get("params", {}).get("ma_days", 20)
    if not idx_kline:
        return SIGNAL_HOLD, "无指数K线数据"
    closes = [x[1] for x in idx_kline]
    ma = calc_ma(closes, n)
    cur = idx_rt["price"] if idx_rt else closes[-1]
    if ma is None:
        return SIGNAL_HOLD, "指数数据不足 %d 天" % n
    dev = (cur - ma) / ma
    idx_name = f.get("params", {}).get("index_name") or (idx_rt["name"] if idx_rt else "指数")
    if dev > 0:
        return SIGNAL_BUY if dev > 0.02 else SIGNAL_HOLD, "跟踪指数 %s 高于 %d 日均线 %s" % (idx_name, n, fmt_pct(dev))
    return SIGNAL_SELL if dev < -0.01 else SIGNAL_HOLD, "跟踪指数 %s 低于 %d 日均线 %s" % (idx_name, n, fmt_pct(dev))


def strategy_combo(f, est_price, est_pct, history, idx_rt, idx_kline):
    """组合: 基金均线 + 指数均线 + 当日涨幅 三者打分。"""
    n = f.get("params", {}).get("ma_days", 20)
    score = 0
    reasons = []
    closes = [x[1] for x in history]
    ma = calc_ma(closes, n)
    cur = est_price or (closes[-1] if closes else None)
    if ma and cur:
        dev = (cur - ma) / ma
        score += 1 if dev > 0 else -1
        reasons.append("基金净值%s%d日均线(%s)" % ("高于" if dev > 0 else "低于", n, fmt_pct(dev)))
    if idx_kline:
        icloses = [x[1] for x in idx_kline]
        ima = calc_ma(icloses, n)
        icur = idx_rt["price"] if idx_rt else icloses[-1]
        if ima and icur:
            idev = (icur - ima) / ima
            score += 1 if idev > 0 else -1
            reasons.append("指数%s%d日均线(%s)" % ("高于" if idev > 0 else "低于", n, fmt_pct(idev)))
    if est_pct is not None:
        if est_pct >= 0.015:
            score += 1
            reasons.append("当日涨幅较大(%s)" % fmt_pct(est_pct))
        elif est_pct <= -0.015:
            score -= 1
            reasons.append("当日跌幅较大(%s)" % fmt_pct(est_pct))
    if score >= 2:
        return SIGNAL_BUY, "；".join(reasons) + "，综合信号偏多"
    if score <= -2:
        return SIGNAL_SELL, "；".join(reasons) + "，综合信号偏空"
    return SIGNAL_HOLD, "；".join(reasons) + "，多空交织，建议观望"


def get_fund_strategies(f):
    """返回基金的策略列表 [{strategy, params, label}]，兼容旧的单策略配置。"""
    strategies = f.get("strategies") or []
    out = []
    for s in strategies:
        if isinstance(s, dict) and s.get("strategy"):
            out.append({
                "strategy": s["strategy"],
                "params": s.get("params") or {},
                "label": STRATEGY_LABELS.get(s["strategy"], s["strategy"]),
            })
    if not out:
        out.append({
            "strategy": f.get("strategy", STRATEGY_TARGET),
            "params": f.get("params") or {},
            "label": STRATEGY_LABELS.get(f.get("strategy", STRATEGY_TARGET), STRATEGY_TARGET),
        })
    return out


def vote_signals(signals):
    """多策略投票。signals: [信号字符串,...] → 综合信号"""
    buys = sum(1 for s in signals if s == SIGNAL_BUY)
    sells = sum(1 for s in signals if s == SIGNAL_SELL)
    total = len(signals)
    if total == 0:
        return SIGNAL_HOLD
    if buys > total / 2:
        return SIGNAL_BUY
    if sells > total / 2:
        return SIGNAL_SELL
    if buys == sells and buys > 0:
        return SIGNAL_HOLD
    return SIGNAL_HOLD


def strategy_drawdown(f, est_price, est_pct, history):
    """回撤止损: 净值从阶段高点回撤超过阈值→卖出，保护利润。"""
    threshold = float(f.get("params", {}).get("drawdown_pct", 0.10))
    if not history:
        return SIGNAL_HOLD, "无历史净值数据"
    closes = [x[1] for x in history]
    peak = max(closes)
    cur = est_price or closes[-1]
    dd = (peak - cur) / peak
    if dd >= threshold:
        return SIGNAL_SELL, "净值从阶段高点 %s 回撤 %.1f%%(阈值%.0f%%)" % (
            round(peak, 4), dd * 100, threshold * 100)
    return SIGNAL_HOLD, "距阶段高点 %s 回撤 %.1f%%(<%.0f%%)，未触发" % (
        round(peak, 4), dd * 100, threshold * 100)


def strategy_daily_drop(f, est_price, est_pct, history):
    """当日暴跌: 当日跌幅超过阈值→卖出，及时止损。"""
    threshold = float(f.get("params", {}).get("drop_pct", 0.03))
    if est_pct is not None:
        chg = est_pct
    elif len(history) >= 2:
        chg = (history[-1][1] - history[-2][1]) / history[-2][1]
    else:
        return SIGNAL_HOLD, "无当日行情"
    if chg <= -threshold:
        return SIGNAL_SELL, "当日跌幅 %.2f%% 触发暴跌线(%.1f%%)" % (chg * 100, threshold * 100)
    return SIGNAL_HOLD, "当日跌幅 %.2f%%(>%.1f%%)，未触发" % (chg * 100, threshold * 100)


def _rsi(values, period=14):
    if len(values) <= period:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    recent = changes[-period:]
    gains = sum(max(v, 0) for v in recent) / period
    losses = sum(max(-v, 0) for v in recent) / period
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def strategy_advanced(f, est_price, est_pct, history, idx_rt=None, idx_kline=None):
    """高阶多因子策略：趋势、动量、RSI、指数环境、波动与回撤综合评分。"""
    p = f.get("params", {})
    closes = [x[1] for x in history]
    if len(closes) < 60:
        return SIGNAL_HOLD, "历史数据不足60天，高阶策略暂不判断", 50, []
    cur = est_price or closes[-1]
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    prev20 = _ma(closes[:-1], 20)
    rsi = _rsi(closes, 14)
    momentum = (cur - closes[-21]) / closes[-21] if len(closes) > 21 else 0
    daily = est_pct if est_pct is not None else (cur - closes[-2]) / closes[-2]
    peak = max(closes[-60:])
    drawdown = (peak - cur) / peak if peak else 0
    score = 50
    factors = []
    if cur > ma20:
        score += 12; factors.append("站上20日线")
    else:
        score -= 12; factors.append("跌破20日线")
    if ma20 > ma60:
        score += 12; factors.append("20日线高于60日线")
    else:
        score -= 12; factors.append("20日线低于60日线")
    if momentum >= 0.05:
        score += 12; factors.append("20日动量偏强")
    elif momentum <= -0.05:
        score -= 12; factors.append("20日动量偏弱")
    else:
        factors.append("20日动量中性")
    if rsi is not None:
        if 45 <= rsi <= 70:
            score += 8; factors.append("RSI健康")
        elif rsi > 78:
            score -= 8; factors.append("RSI过热")
        elif rsi < 30:
            score += 5; factors.append("RSI超卖反弹观察")
    index_bias = None
    if idx_kline:
        ivals = [x[1] for x in idx_kline]
        ima20, ima60 = _ma(ivals, 20), _ma(ivals, 60)
        iprice = idx_rt["price"] if idx_rt else ivals[-1]
        if ima20 and ima60:
            index_bias = (iprice - ima20) / ima20
            if iprice > ima20 and ima20 > ima60:
                score += 12; factors.append("跟踪指数趋势向上")
            elif iprice < ima20 and ima20 < ima60:
                score -= 12; factors.append("跟踪指数趋势向下")
            else:
                factors.append("跟踪指数震荡")
    warn = float(p.get("drawdown_warn", 0.10))
    danger = float(p.get("drawdown_sell", 0.15))
    if drawdown >= danger:
        score -= 15; factors.append("60日回撤超风险线")
    elif drawdown >= warn:
        score -= 8; factors.append("60日回撤进入警戒")
    if daily <= -0.03:
        score -= 10; factors.append("当日跌幅较大")
    score = max(0, min(100, score))
    buy_at = int(p.get("buy_score", 65))
    sell_at = int(p.get("sell_score", 35))
    if score >= buy_at:
        signal = SIGNAL_BUY
    elif score <= sell_at:
        signal = SIGNAL_SELL
    else:
        signal = SIGNAL_HOLD
    return signal, "评分%d/100：%s" % (score, "；".join(factors)), score, factors


def run_single_strategy(strategy_cfg, est, history, idx_rt, idx_kline):
    """运行单个策略。strategy_cfg: {strategy, params, label}"""
    est_price = est["gsz"] if est and est.get("gsz") else None
    est_pct = est["gszzl"] if est else None
    if est_pct is not None:
        est_pct = est_pct / 100.0
    st = strategy_cfg["strategy"]
    if st == STRATEGY_MA:
        sig, reason = strategy_ma(strategy_cfg, est_price, est_pct, history)
    elif st == STRATEGY_INDEX_MA:
        sig, reason = strategy_index_ma(strategy_cfg, est_price, est_pct, history, idx_rt, idx_kline)
    elif st == STRATEGY_COMBO:
        sig, reason = strategy_combo(strategy_cfg, est_price, est_pct, history, idx_rt, idx_kline)
    elif st == STRATEGY_DRAWDOWN:
        sig, reason = strategy_drawdown(strategy_cfg, est_price, est_pct, history)
    elif st == STRATEGY_DAILY_DROP:
        sig, reason = strategy_daily_drop(strategy_cfg, est_price, est_pct, history)
    elif st == STRATEGY_ADVANCED:
        sig, reason, score, _ = strategy_advanced(
            strategy_cfg, est_price, est_pct, history, idx_rt, idx_kline)
        reason = reason + "，高阶多因子综合信号"
    else:
        sig, reason = strategy_target(strategy_cfg, est_price, est_pct, history)
    return sig, reason


def run_signal(f, est, history, idx_rt, idx_kline):
    est_price = est["gsz"] if est and est.get("gsz") else None
    est_pct = est["gszzl"] if est else None
    if est_pct is not None:
        est_pct = est_pct / 100.0
    strategies = get_fund_strategies(f)
    detail = []
    for sc in strategies:
        try:
            sig, reason = run_single_strategy(sc, est, history, idx_rt, idx_kline)
            score_match = re.search(r"评分(\d+)/100", reason)
            item = {"label": sc["label"], "signal": sig, "reason": reason}
            if score_match:
                item["score"] = int(score_match.group(1))
            detail.append(item)
        except Exception as e:
            detail.append({"label": sc["label"], "signal": SIGNAL_HOLD, "reason": "策略异常: %s" % e})
    sig = vote_signals([d["signal"] for d in detail])
    hold = hold_info(f)
    combined_reason = "多策略%d票: %s" % (len(detail),
        " ".join("%s(%s)" % (d["label"], d["signal"]) for d in detail))
    reason = combined_reason
    if sig == SIGNAL_SELL and hold and hold["days"] is not None:
        if hold["days"] < 7:
            wait = 7 - hold["days"]
            reason += " ⚠️ 持有仅%d天，赎回费%.1f%%，建议再等%d天(第7天)卖出更划算" % (
                hold["days"], hold["fee_rate"] * 100, wait)
        elif hold["fee_rate"] > 0.005:
            reason += " 赎回费约%.2f%%(持有%d天)" % (hold["fee_rate"] * 100, hold["days"])
    today_chg = est_pct
    if today_chg is None and idx_rt and idx_rt.get("chg_pct") is not None:
        today_chg = idx_rt["chg_pct"]
    return {
        "code": f["code"],
        "name": est["name"] if est and est["name"] else f.get("name", f["code"]),
        "strategy": "多策略",
        "strategies": detail,
        "est_price": est_price,
        "est_pct": est_pct,
        "today_chg": today_chg,
        "idx_rt": {"name": idx_rt["name"], "price": idx_rt["price"],
                   "chg_pct": idx_rt.get("chg_pct"), "time": idx_rt.get("time")}
        if idx_rt else None,
        "signal": sig,
        "reason": reason,
        "history_last": history[-1][1] if history else None,
        "history_date": history[-1][0] if history else None,
        "hold": hold,
    }


def hold_info(f):
    """根据买入日期/持仓金额计算持有信息。返回 dict 或 None。"""
    p = (f or {}).get("params") or {}
    buy_date = str(p.get("buy_date") or "").strip()
    if not buy_date:
        return None
    try:
        bd = datetime.date.fromisoformat(buy_date[:10])
    except Exception:
        return None
    today = datetime.date.today()
    days = (today - bd).days
    if days < 7:
        fee_rate = 0.015
        stage = "7日内(高费率)"
    elif days < 30:
        fee_rate = 0.0075
        stage = "7-30天"
    elif days < 365:
        fee_rate = 0.005
        stage = "30天-1年"
    elif days < 730:
        fee_rate = 0.0025
        stage = "1-2年"
    else:
        fee_rate = 0.0
        stage = "2年以上(免)"
    hold_amount = p.get("hold_amount")
    fee_est = round(hold_amount * fee_rate, 2) if hold_amount else None
    return {
        "buy_date": bd.isoformat(),
        "days": max(days, 0),
        "stage": stage,
        "fee_rate": fee_rate,
        "hold_amount": hold_amount,
        "fee_est": fee_est,
    }


def evaluate_fund(f):
    """获取行情并计算单只基金信号。返回 {ok, result} 或 {ok: False, error}。"""
    try:
        est = fetch_fund_estimate(f["code"])
        history = fetch_fund_history(f["code"])
        idx_rt = None
        idx_kline = []
        secid = None
        for sc in get_fund_strategies(f):
            s = (sc.get("params") or {}).get("index_secid")
            if s:
                secid = s
                break
        if secid:
            idx_rt = fetch_index_realtime(secid)
            idx_kline = fetch_index_kline(secid)
        return {"ok": True, "result": run_signal(f, est, history, idx_rt, idx_kline)}
    except Exception as e:
        return {"ok": False, "error": str(e), "code": f["code"], "name": f.get("name", f["code"])}


def evaluate_all(cfg):
    """计算所有基金信号。返回 (funds_out, errors)。"""
    funds_out = []
    errors = []
    for f in cfg.get("funds", []):
        r = evaluate_fund(f)
        if r["ok"]:
            funds_out.append(r["result"])
        else:
            errors.append("%s(%s): %s" % (r["name"], r["code"], r["error"]))
    return funds_out, errors


def build_message(funds_out, date_str):
    lines = []
    lines.append("【基金操作提醒】%s" % date_str)
    lines.append("今日 15:00 前可操作(场外按当日净值成交)")
    lines.append("")
    for i, o in enumerate(funds_out, 1):
        lines.append("%d. %s (%s)" % (i, o["name"], o["code"]))
        if o.get("strategies"):
            lines.append("   策略明细: " + " | ".join(
                "%s:%s" % (d["label"], d["signal"]) for d in o["strategies"]))
        if o.get("hold"):
            h = o["hold"]
            lines.append("   持仓: %s天(%s) 赎回费%.2f%%%s" % (
                h["days"], h["stage"], h["fee_rate"] * 100,
                ("，卖出约付%.2f元" % h["fee_est"]) if h["fee_est"] is not None else ""))
        if o.get("today_chg") is not None:
            lines.append("   今日实时: %s(指数估算)" % fmt_pct(o["today_chg"]))
        elif o["est_price"] is not None:
            lines.append("   当前估值: %.4f (%s)" % (o["est_price"], fmt_pct(o["est_pct"])))
        else:
            lines.append("   当前估值: 暂无(休市或无估值)，参考净值 %s (%s)"
                         % (o["history_last"], o["history_date"]))
        if o["signal"] == SIGNAL_BUY:
            mark = "▲ 建议: 买入/加仓"
        elif o["signal"] == SIGNAL_SELL:
            mark = "▼ 建议: 卖出/止盈止损"
        else:
            mark = "◆ 建议: 持有/观望"
        lines.append("   " + mark)
        lines.append("   理由: %s" % o["reason"])
        lines.append("")
    lines.append("— 数据来源: 天天基金/东方财富/新浪财经，仅供参考，不构成投资建议")
    return "\n".join(lines)


# ---------- 推送 ----------

def _post_json(url, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def send_serverchan(title, content, key):
    url = "https://sctapi.ftqq.com/%s.send" % key
    data = urllib.parse.urlencode({"title": title, "desp": content}).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def send_wecom(title, content, webhook):
    return _post_json(webhook, {"msgtype": "text", "text": {"content": title + "\n" + content}})


def send_dingtalk(title, content, webhook):
    return _post_json(webhook, {"msgtype": "text", "text": {"content": title + "\n" + content}})


def send_wxpusher(title, content, app_token, uid):
    """WxPusher 微信推送(服务号消息，免费100条/天)。"""
    return _post_json("https://wxpusher.zjiecode.com/api/send/message", {
        "appToken": app_token,
        "content": content,
        "summary": title,
        "contentType": 1,
        "uids": [uid],
    })


def notify(title, content, cfg):
    """推送结果。返回 [(channel, response_json), ...]。"""
    n = cfg.get("notify", {})
    ch = n.get("channel")
    results = []
    try:
        if ch == "serverchan" and n.get("serverchan_key"):
            results.append(("serverchan", send_serverchan(title, content, n["serverchan_key"])))
        elif ch == "wxpusher" and n.get("wxpusher_apptoken") and n.get("wxpusher_uid"):
            results.append(("wxpusher", send_wxpusher(
                title, content, n["wxpusher_apptoken"], n["wxpusher_uid"])))
        elif ch == "wecom" and n.get("wecom_webhook"):
            results.append(("wecom", send_wecom(title, content, n["wecom_webhook"])))
        elif ch == "dingtalk" and n.get("dingtalk_webhook"):
            results.append(("dingtalk", send_dingtalk(title, content, n["dingtalk_webhook"])))
        elif ch == "file":
            with open(os.path.join(BASE_DIR, "signal_report.txt"), "w", encoding="utf-8") as fh:
                fh.write(title + "\n\n" + content)
            results.append(("file", {"ok": True}))
    except Exception as e:
        results.append((ch, {"ok": False, "error": str(e)}))
    return results


# ---------- 存储层(PG/JSON 双后端) ----------
# 配置来源(优先级): 环境变量 DB_* (Docker 生产) > db.json (本地开发)
# 不再通过管理后台 UI 配置数据库

DB_CONFIG_PATH = os.path.join(BASE_DIR, "db.json")
DB_CONFIG = None
_pg_conn = None
_pg_lock = threading.Lock()
PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS funds (
    id          SERIAL PRIMARY KEY,
    code        VARCHAR(6) UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    strategy    TEXT NOT NULL DEFAULT 'target',
    params      JSONB NOT NULL DEFAULT '{}',
    strategies  JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE funds ADD COLUMN IF NOT EXISTS strategies JSONB;
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS signal_history (
    id       SERIAL PRIMARY KEY,
    run_time TIMESTAMPTZ NOT NULL DEFAULT now(),
    data     JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS invite_codes (
    code       TEXT PRIMARY KEY,
    used_by    TEXT,
    used_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_user_sessions_expiry ON user_sessions (expires_at);
CREATE TABLE IF NOT EXISTS news (
    id         SERIAL PRIMARY KEY,
    source_id  TEXT UNIQUE,
    title      TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'auto',
    category   TEXT NOT NULL DEFAULT '中性',
    industry   TEXT NOT NULL DEFAULT '综合',
    url        TEXT NOT NULL DEFAULT '',
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ai_analysis JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_news_published ON news (published_at DESC);
ALTER TABLE news ADD COLUMN IF NOT EXISTS ai_analysis JSONB;
ALTER TABLE news ADD COLUMN IF NOT EXISTS pushed_at TIMESTAMPTZ;
"""


def set_db_config(cfg):
    """保存数据库连接配置到本地 db.json(连接信息不能存库内)。"""
    global DB_CONFIG
    DB_CONFIG = dict(cfg)
    with LOCK:
        tmp = DB_CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(DB_CONFIG, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, DB_CONFIG_PATH)


def get_db_config():
    """获取数据库连接配置：环境变量 DB_* 优先，其次本地 db.json。"""
    global DB_CONFIG
    env = {k: os.environ.get(k, "").strip() for k in
           ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME")}
    if env.get("DB_HOST"):
        DB_CONFIG = {
            "host": env["DB_HOST"],
            "port": int(env["DB_PORT"] or 5432),
            "user": env.get("DB_USER") or "fund_signal",
            "password": env.get("DB_PASSWORD") or "",
            "dbname": env.get("DB_NAME") or "fund_signal",
        }
        return DB_CONFIG
    if DB_CONFIG is None:
        if os.path.exists(DB_CONFIG_PATH):
            with open(DB_CONFIG_PATH, encoding="utf-8") as fh:
                DB_CONFIG = json.load(fh)
        else:
            DB_CONFIG = {}
    return DB_CONFIG


def pg_connect():
    """建立/复用 PG 连接。连接失败返回 None。"""
    global _pg_conn
    with _pg_lock:
        c = get_db_config()
        if not c.get("host") or not c.get("dbname"):
            return None
        if _pg_conn is not None:
            try:
                _pg_conn.cursor().execute("SELECT 1")
                return _pg_conn
            except Exception:
                _pg_conn = None
        try:
            import psycopg2
            _pg_conn = psycopg2.connect(
                host=c.get("host"), port=int(c.get("port") or 5432),
                user=c.get("user") or "postgres",
                password=c.get("password") or "",
                dbname=c.get("dbname"), connect_timeout=5,
            )
            _pg_conn.autocommit = True
            with _pg_conn.cursor() as cur:
                cur.execute(PG_SCHEMA)
            return _pg_conn
        except Exception:
            _pg_conn = None
            return None


def pg_available():
    return pg_connect() is not None


def close_pg():
    global _pg_conn
    with _pg_lock:
        if _pg_conn is not None:
            try:
                _pg_conn.close()
            except Exception:
                pass
            _pg_conn = None


def _pg_query(sql, params=None):
    conn = pg_connect()
    if conn is None:
        raise RuntimeError("PostgreSQL 不可用")
    with conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.description:
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    return []


def _pg_exec(sql, params=None):
    conn = pg_connect()
    if conn is None:
        raise RuntimeError("PostgreSQL 不可用")
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def load_config():
    cfg = {"notify": dict(DEFAULT_CONFIG["notify"]), "funds": []}
    if pg_available():
        try:
            rows = _pg_query("SELECT code, name, strategy, params, strategies FROM funds ORDER BY id")
            cfg["funds"] = [{
                "id": r["code"], "code": r["code"], "name": r["name"],
                "strategy": r["strategy"],
                "params": r["params"] or {},
                "strategies": r["strategies"] or [],
            } for r in rows]
            st = _pg_query("SELECT key, value FROM settings")
            for r in st:
                if r["key"] == "notify":
                    cfg["notify"].update(r["value"] or {})
            return cfg
        except Exception:
            pass
    with LOCK:
        if not os.path.exists(DATA_PATH):
            return cfg
        with open(DATA_PATH, encoding="utf-8") as fh:
            jcfg = json.load(fh)
    jcfg.setdefault("notify", dict(DEFAULT_CONFIG["notify"]))
    jcfg.setdefault("funds", [])
    return jcfg


def save_config(cfg):
    cfg.setdefault("notify", dict(DEFAULT_CONFIG["notify"]))
    cfg.setdefault("funds", [])
    if pg_available():
        try:
            with pg_connect().cursor() as cur:
                cur.execute("DELETE FROM funds")
                for f in cfg["funds"]:
                    cur.execute(
                        "INSERT INTO funds(code, name, strategy, params, strategies)"
                        " VALUES(%s,%s,%s,%s,%s)",
                        (f["code"], f.get("name", f["code"]), f.get("strategy", "target"),
                         json.dumps(f.get("params") or {}),
                         json.dumps(f.get("strategies") or [])),
                    )
                cur.execute(
                    "INSERT INTO settings(key, value) VALUES(%s,%s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    ("notify", json.dumps(cfg["notify"])),
                )
            return
        except Exception:
            close_pg()
    with LOCK:
        tmp = DATA_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, DATA_PATH)


def load_history():
    if pg_available():
        try:
            rows = _pg_query(
                "SELECT run_time, data FROM signal_history ORDER BY run_time DESC LIMIT 50")
            return [{"time": r["run_time"].strftime("%Y-%m-%d %H:%M:%S"),
                     **r["data"]} for r in rows]
        except Exception:
            close_pg()
    with LOCK:
        if not os.path.exists(HISTORY_PATH):
            return []
        with open(HISTORY_PATH, encoding="utf-8") as fh:
            return json.load(fh)


def _session_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def save_user_session(token, username, expires_at):
    """Persist a session so a web-service restart does not sign users out."""
    try:
        _pg_exec("DELETE FROM user_sessions WHERE expires_at <= now()")
        _pg_exec(
            "INSERT INTO user_sessions(token_hash, username, expires_at) VALUES(%s,%s,%s) "
            "ON CONFLICT (token_hash) DO UPDATE SET username=EXCLUDED.username, expires_at=EXCLUDED.expires_at",
            (_session_hash(token), username, expires_at),
        )
        return True
    except Exception:
        return False


def get_user_session(token):
    try:
        rows = _pg_query(
            "SELECT username FROM user_sessions WHERE token_hash=%s AND expires_at > now()",
            (_session_hash(token),),
        )
        return rows[0]["username"] if rows else None
    except Exception:
        return None


def extend_user_session(token, expires_at):
    try:
        return _pg_exec(
            "UPDATE user_sessions SET expires_at=%s WHERE token_hash=%s AND expires_at > now()",
            (expires_at, _session_hash(token)),
        ) > 0
    except Exception:
        return False


def delete_user_session(token):
    try:
        _pg_exec("DELETE FROM user_sessions WHERE token_hash=%s", (_session_hash(token),))
    except Exception:
        pass


def append_history(entry):
    if pg_available():
        try:
            _pg_exec("INSERT INTO signal_history(data) VALUES(%s)",
                     (json.dumps(entry),))
            return
        except Exception:
            close_pg()
    with LOCK:
        hist = load_history()
        hist.append(entry)
        hist = hist[-HISTORY_LIMIT:]
        tmp = HISTORY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(hist, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, HISTORY_PATH)


def migrate_json_to_pg():
    """把 JSON 中的数据迁移到 PG。返回迁移报告。"""
    report = {"funds": 0, "history": 0, "notify": False}
    with LOCK:
        jcfg = {}
        if os.path.exists(DATA_PATH):
            with open(DATA_PATH, encoding="utf-8") as fh:
                jcfg = json.load(fh)
        jhist = []
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH, encoding="utf-8") as fh:
                jhist = json.load(fh)
    if pg_available():
        save_config(jcfg)
        report["funds"] = len(jcfg.get("funds", []))
        report["notify"] = bool(jcfg.get("notify"))
        for entry in jhist:
            _pg_exec("INSERT INTO signal_history(data) VALUES(%s)", (json.dumps(entry),))
        report["history"] = len(jhist)
    return report


def is_trading_now():
    """当前是否为交易日且处于 9:00-15:00 之间。"""
    wd = datetime.date.today().weekday()
    if wd >= 5:
        return False
    h = datetime.datetime.now().hour
    return 9 <= h < 15


# ---------- 认证 ----------

import hashlib
import hmac as _hmac
import secrets as _secrets

DEFAULT_USER = os.environ.get("FS_USER", "admin")
DEFAULT_PASS = os.environ.get("FS_PASSWORD", "change-this-password")


def public_registration_enabled():
    """Whether this deployment intentionally allows self-service signup.

    Keep the safe default: a public Git repository should not silently turn a
    deployed instance into an open registration service.
    """
    return os.environ.get("FS_ALLOW_PUBLIC_REGISTER", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def hash_password(password, salt=None):
    if salt is None:
        salt = _secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return "%s$%s" % (salt, dk.hex())


def verify_password(password, stored):
    try:
        salt, _ = stored.split("$", 1)
    except Exception:
        return False
    return _hmac.compare_digest(hash_password(password, salt), stored)


def ensure_default_user():
    """确保默认管理员存在。"""
    if not pg_available():
        return
    try:
        rows = _pg_query("SELECT username FROM users")
        if not rows:
            _pg_exec("INSERT INTO users(username, password_hash) VALUES(%s,%s)",
                     (DEFAULT_USER, hash_password(DEFAULT_PASS)))
            print("[auth] 已创建默认用户 %s" % DEFAULT_USER, flush=True)
        elif not any(r["username"] == DEFAULT_USER for r in rows):
            _pg_exec("INSERT INTO users(username, password_hash) VALUES(%s,%s)",
                     (DEFAULT_USER, hash_password(DEFAULT_PASS)))
            print("[auth] 已创建用户 %s" % DEFAULT_USER, flush=True)
    except Exception as e:
        print("[auth] 初始化失败: %s" % e, flush=True)


def check_login(username, password):
    rows = _pg_query("SELECT password_hash FROM users WHERE username=%s", (username,))
    if not rows:
        return False
    return verify_password(password, rows[0]["password_hash"])


def change_password(username, old_password, new_password):
    rows = _pg_query("SELECT password_hash FROM users WHERE username=%s", (username,))
    if not rows or not verify_password(old_password, rows[0]["password_hash"]):
        return False
    _pg_exec("UPDATE users SET password_hash=%s WHERE username=%s",
             (hash_password(new_password), username))
    return True


def create_invite_code():
    """生成邀请码。返回 code 字符串。"""
    code = _secrets.token_hex(4).upper()
    _pg_exec("INSERT INTO invite_codes(code) VALUES(%s)", (code,))
    return code


def list_invite_codes(limit=50):
    rows = _pg_query(
        "SELECT code, used_by, used_at, created_at FROM invite_codes"
        " ORDER BY created_at DESC LIMIT %s", (int(limit),))
    for r in rows:
        r["created_at"] = r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else ""
        r["used_at"] = r["used_at"].strftime("%Y-%m-%d %H:%M") if r["used_at"] else ""
        r["used"] = bool(r["used_by"])
    return rows


def register_user(username, password, invite_code):
    """Register with an invite, or explicit opt-in public registration."""
    username = (username or "").strip()
    if not username or len(username) < 2 or len(username) > 20:
        raise RuntimeError("用户名需 2-20 个字符")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", username):
        raise RuntimeError("用户名只能包含字母、数字、下划线或短横线")
    if not password or len(password) < 6:
        raise RuntimeError("密码至少 6 位")
    code = (invite_code or "").strip().upper()
    allow_public = public_registration_enabled()
    if not code and not allow_public:
        raise RuntimeError("需要邀请码（联系管理员获取）")
    if code:
        rows = _pg_query("SELECT code, used_by FROM invite_codes WHERE code=%s", (code,))
        if not rows:
            raise RuntimeError("邀请码无效")
        if rows[0]["used_by"]:
            raise RuntimeError("邀请码已被使用")
    try:
        _pg_exec("INSERT INTO users(username, password_hash) VALUES(%s,%s)",
                 (username, hash_password(password)))
    except Exception:
        raise RuntimeError("用户名已存在")
    if code:
        _pg_exec("UPDATE invite_codes SET used_by=%s, used_at=now() WHERE code=%s",
                 (username, code))
    return True


# ---------- 消息中心(利空/利好) ----------

NEWS_KEYWORDS_INDUSTRY = [
    "半导体", "芯片", "晶圆", "存储", "封测", "光刻", "刻蚀", "中芯", "华虹",
    "寒武纪", "海光", "GPU", "AI芯片", "国产替代", "光模块", "光通信", "通信",
    "5G", "6G", "中兴", "烽火", "数据中心", "算力", "服务器", "交换机", "PCB",
    "消费电子", "苹果产业链", "台积电", "阿斯麦", "英伟达", "高通", "联发科",
    "ETF", "通信设备", "运营商", "云厂商", "IDC",
]
NEWS_KEYWORDS_MACRO = [
    "央行", "降准", "降息", "LPR", "GDP", "PMI", "CPI", "PPI", "国务院",
    "证监会", "发改委", "财政部", "美联储", "关税", "汇率", "人民币",
    "半导体政策", "集成电路", "大基金", "补贴",
]
NEWS_BULLISH = [
    "增长", "突破", "中标", "订单", "合同", "涨价", "扩产", "量产", "获批",
    "政策", "支持", "上调", "盈利", "回购", "增持", "创历史", "新高", "利好",
    "加单", "满产", "供不应求", "加速", "景气", "复苏", "补贴", "国产化",
    "签单", "大基金", "降准", "降息", "放开", "准入",
]
NEWS_BEARISH = [
    "下滑", "下跌", "大跌", "暴跌", "重挫", "跳水", "走低", "减持", "诉讼",
    "调查", "处罚", "降价", "亏损", "终止", "取消", "制裁", "禁令", "风险",
    "警示", "退市", "ST", "爆雷", "利空", "不及预期", "下调", "裁员",
    "停产", "延期", "停滞", "低迷", "拖累", "疲软",
]


def classify_news(title, content):
    """按关键词自动分类: 返回 (category, industry)"""
    text = (title + " " + content)
    ind_hits = [k for k in NEWS_KEYWORDS_INDUSTRY if k.lower() in text.lower()]
    macro_hits = [k for k in NEWS_KEYWORDS_MACRO if k in text]
    bullish = [k for k in NEWS_BULLISH if k in text]
    bearish = [k for k in NEWS_BEARISH if k in text]
    if len(bullish) > len(bearish):
        category = "利好"
    elif len(bearish) > len(bullish):
        category = "利空"
    else:
        category = "中性"
    if ind_hits:
        semi = ("半导体", "芯片", "晶圆", "存储", "封测", "光刻", "刻蚀", "中芯", "华虹",
                "寒武纪", "海光", "GPU", "AI芯片", "国产替代", "台积电", "阿斯麦", "英伟达",
                "消费电子", "苹果产业链", "PCB", "ETF", "数据中心", "服务器", "算力")
        industry = "半导体" if any(s in text for s in semi) else "通信"
    elif macro_hits:
        industry = "宏观"
    else:
        industry = "综合"
    return category, industry


def fetch_news_em(limit=30):
    """东方财富 7x24 快讯。返回原始条目列表。"""
    import urllib.parse
    url = ("https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
           "?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=%d"
           "&req_trace=%s" % (limit, int(time.time() * 1000)))
    data = json.loads(http_get(url, headers={"Referer": "https://kuaixun.eastmoney.com/"}))
    lst = (data.get("data") or {}).get("fastNewsList") or []
    out = []
    for it in lst:
        out.append({
            "source_id": str(it.get("code", "")),
            "title": it.get("title", ""),
            "content": (it.get("summary") or "").replace("\n", " "),
            "time": it.get("showTime", ""),
            "url": it.get("url", ""),
        })
    return out


def fetch_news_sina(limit=30):
    """新浪 7x24 直播流。返回原始条目列表。"""
    url = ("https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=%d"
           "&zhibo_id=152&tag_id=0&dire=f&dpc=1" % limit)
    data = json.loads(http_get(url))
    lst = (((data.get("result") or {}).get("data") or {}).get("feed") or {}).get("list") or []
    out = []
    for it in lst:
        out.append({
            "source_id": "sina%s" % it.get("id", ""),
            "title": (it.get("rich_text") or "")[:80],
            "content": it.get("rich_text", ""),
            "time": it.get("create_time", ""),
            "url": "",
        })
    return out


def fetch_news_wallstreetcn(limit=30):
    """华尔街见闻全球财经流(美股/科技/AI/全球市场)。"""
    url = ("https://api-one.wallstcn.com/apiv1/content/information-flow"
           "?channel=global-channel&accept=all&limit=%d" % limit)
    data = json.loads(http_get(url))
    items = ((data.get("data") or {}).get("items") or [])
    out = []
    for it in items:
        r = it.get("resource") or {}
        nid = r.get("id")
        title = (r.get("title") or "").strip()
        if not nid or not title:
            continue
        ts = r.get("display_time")
        pub = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
        out.append({
            "source_id": "ws%s" % nid,
            "title": title[:200],
            "content": (r.get("content_short") or "").replace("\n", " ")[:2000],
            "time": pub,
            "url": r.get("uri") or "",
        })
    return out


def fetch_news_intl(limit=30):
    """东方财富国际财经(能源/大宗/全球市场)。"""
    url = ("https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
           "?client=web&biz=web_724&fastColumn=106&sortEnd=&pageSize=%d"
           "&req_trace=%s" % (limit, int(time.time() * 1000)))
    data = json.loads(http_get(url, headers={"Referer": "https://kuaixun.eastmoney.com/"}))
    lst = (data.get("data") or {}).get("fastNewsList") or []
    out = []
    for it in lst:
        out.append({
            "source_id": "em106%s" % it.get("code", ""),
            "title": it.get("title", ""),
            "content": (it.get("summary") or "").replace("\n", " "),
            "time": it.get("showTime", ""),
            "url": it.get("url", ""),
        })
    return out


def collect_news(sources=("em", "sina", "ws", "intl")):
    """抓取全部源，去重入库。返回 (新增数, 总数)。"""
    src_names = {"em": "东方财富", "sina": "新浪财经", "ws": "华尔街见闻", "intl": "东财国际"}
    fetchers = {
        "em": lambda: fetch_news_em(30),
        "sina": lambda: fetch_news_sina(30),
        "ws": lambda: fetch_news_wallstreetcn(30),
        "intl": lambda: fetch_news_intl(20),
    }
    items = []
    for src in sources:
        try:
            fetched = fetchers[src]()
            for it in fetched:
                it["src"] = src_names.get(src, src)
            items += fetched
        except Exception as e:
            print("[news] 抓取 %s 失败: %s" % (src, e), flush=True)
    added = 0
    for it in items:
        sid = it["source_id"]
        if not sid:
            continue
        try:
            exists = _pg_query("SELECT 1 FROM news WHERE source_id=%s", (sid,))
            if exists:
                continue
            category, industry = classify_news(it["title"], it["content"])
            if industry == "综合":
                continue
            if category == "中性" and industry == "宏观":
                continue
            _pg_exec(
                "INSERT INTO news(source_id, title, content, source, category, industry, url, published_at)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (sid, it["title"][:200], it["content"][:2000], it.get("src", "auto"),
                 category, industry, it["url"], it["time"] or datetime.datetime.now().isoformat()),
            )
            added += 1
        except Exception as e:
            print("[news] 入库失败: %s" % e, flush=True)
    return added, len(items)


def list_news(limit=50, category=None, industry=None, source=None):
    sql = ("SELECT id, title, content, source, category, industry, url, "
           "published_at, ai_analysis FROM news")
    conds, args = [], []
    if category:
        conds.append("category=%s"); args.append(category)
    if industry:
        conds.append("industry=%s"); args.append(industry)
    if source:
        conds.append("source=%s"); args.append(source)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY published_at DESC LIMIT %s"
    args.append(int(limit))
    rows = _pg_query(sql, args)
    for r in rows:
        ts = r["published_at"]
        r["published_at"] = ts.strftime("%Y-%m-%d %H:%M") if ts else ""
    return rows


def add_news_manual(title, content, category="中性", industry="综合"):
    _pg_exec(
        "INSERT INTO news(source_id, title, content, source, category, industry, published_at)"
        " VALUES(%s,%s,%s,%s,%s,%s,now())",
        (None, title, content, "manual", category, industry),
    )


def delete_news(news_id):
    _pg_exec("DELETE FROM news WHERE id=%s", (int(news_id),))


def news_stats():
    rows = _pg_query(
        "SELECT category, count(*) AS c FROM news GROUP BY category")
    return {r["category"]: r["c"] for r in rows}


def fetch_important_news(limit=5):
    """查询未推送的重要消息：利空(半导体/通信) 或 AI 判定卖出 或 重大利好。"""
    rows = _pg_query(
        "SELECT id, title, category, industry, ai_analysis, published_at FROM news"
        " WHERE pushed_at IS NULL AND category != '中性'"
        "   AND (industry IN ('半导体','通信') OR (ai_analysis->>'signal') = '卖出')"
        " ORDER BY published_at DESC LIMIT %s", (int(limit),))
    for r in rows:
        r["published_at"] = (r["published_at"] or "").strftime("%H:%M") if r["published_at"] else ""
    return rows


def mark_news_pushed(ids):
    if not ids:
        return
    _pg_exec("UPDATE news SET pushed_at=now() WHERE id = ANY(%s)", (list(ids),))


def push_important_news(cfg, max_items=3):
    """把重要消息推送到微信(Server酱)。返回推送条数。"""
    n = cfg.get("notify", {})
    if n.get("channel") != "serverchan" or not n.get("serverchan_key"):
        return 0
    items = fetch_important_news(limit=8)
    if not items:
        return 0
    picks = items[:max_items]
    lines = ["【基金快讯】%d 条重要消息" % len(picks), ""]
    for it in picks:
        ai = it.get("ai_analysis") or {}
        tag = "%s[%s]" % (it["category"], it["industry"])
        lines.append("▍%s %s" % (it["published_at"], tag))
        lines.append(it["title"][:90])
        if ai.get("signal"):
            lines.append("🤖 AI: %s %s %s" % (ai.get("sentiment", ""), ai.get("signal", ""), ai.get("summary", "")))
        lines.append("")
    title = "⚠️ %s %s" % ("利空" if any(i["category"] == "利空" for i in picks) else "利好", picks[0]["industry"])
    try:
        results = notify(title, "\n".join(lines), cfg)
        mark_news_pushed([i["id"] for i in picks])
        return len(picks)
    except Exception as e:
        print("[push] 重要消息推送失败: %s" % e, flush=True)
        return 0


def get_news_interval():
    """消息抓取间隔(分钟)：交易时段/非交易时段分开。后台可配置。"""
    default = {"trading": 1, "idle": 10}
    try:
        rows = _pg_query("SELECT value FROM settings WHERE key='news_interval'")
        if rows and rows[0]["value"]:
            cfg = dict(default)
            cfg.update({k: max(int(v), 1) for k, v in (rows[0]["value"] or {}).items()
                        if k in ("trading", "idle")})
            return cfg
    except Exception:
        pass
    return dict(default)


def save_news_interval(trading, idle):
    _pg_exec(
        "INSERT INTO settings(key, value) VALUES('news_interval', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (json.dumps({"trading": max(int(trading), 1), "idle": max(int(idle), 1)}),))


def is_trading_period(now=None):
    """当前是否处于 A 股交易时段(9:00-15:30 工作日)。"""
    now = now or datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 <= minutes < 15 * 60 + 30


def news_stats_by_industry():
    """按行业统计利好/利空条数。返回 {industry: {利好: n, 利空: n, 中性: n}}"""
    rows = _pg_query(
        "SELECT industry, category, count(*) AS c FROM news GROUP BY industry, category")
    out = {}
    for r in rows:
        out.setdefault(r["industry"], {})
        out[r["industry"]][r["category"]] = r["c"]
    return out


def fund_industry(fund):
    """推断基金所属行业。params.industry 优先，其次从名称/指数名匹配。"""
    p = (fund or {}).get("params") or {}
    if p.get("industry"):
        return str(p["industry"])
    text = (fund.get("name") or "") + " " + (p.get("index_name") or "")
    semi = ("半导体", "芯片", "晶圆", "电子", "科技", "算力", "AI", "人工智能",
            "纳斯达克", "纳指", "费城", "信息", "软件", "计算机", "云计算")
    comm = ("通信", "5G", "6G", "运营商", "光模块", "光通信", "中证通信",
            "云计算", "互联网", "大数据")
    if any(k in text for k in semi):
        return "半导体"
    if any(k in text for k in comm):
        return "通信"
    return "综合"


# ---------- AI 消息分析(DeepSeek) ----------

AI_DEFAULT_CONFIG = {
    "base_url": "https://api.deepseek.com",
    "api_key": "",
    "model": "deepseek-chat",
}

AI_PROMPT = """你是资深 A 股半导体/通信板块分析师。下面是一个消息数组(每条含 id/title/content)，请逐条判断对半导体/通信相关基金的影响。
只输出 JSON，格式: {"items":[{"id":消息id,"sentiment":"利好或利空或中性","signal":"买入或持有或卖出","summary":"一句话理由(25字以内)"}]}
signal 指"这条消息驱动下，持有半导体/通信基金的人应如何操作"。必须为每条消息都输出一个元素，id 必须与输入一致。"""


def get_ai_config():
    try:
        rows = _pg_query("SELECT value FROM settings WHERE key='ai_config'")
        if rows:
            cfg = dict(AI_DEFAULT_CONFIG)
            cfg.update(rows[0]["value"] or {})
            return cfg
    except Exception:
        pass
    return dict(AI_DEFAULT_CONFIG)


def save_ai_config(cfg):
    existing = get_ai_config()
    submitted_key = (cfg.get("api_key") or "").strip()
    clean = {
        "base_url": (cfg.get("base_url") or AI_DEFAULT_CONFIG["base_url"]).strip(),
        # An empty field means "keep the existing secret".  This lets the UI
        # avoid returning API keys through its GET endpoint.
        "api_key": submitted_key or existing.get("api_key", ""),
        "model": (cfg.get("model") or AI_DEFAULT_CONFIG["model"]).strip(),
    }
    _pg_exec(
        "INSERT INTO settings(key, value) VALUES('ai_config', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (json.dumps(clean),))


def ai_analyze_batch(news_items):
    """调用 DeepSeek 分析一批消息。返回 {news_id: analysis, ...}"""
    cfg = get_ai_config()
    if not cfg.get("api_key"):
        return {}
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": AI_PROMPT},
            {"role": "user", "content": json.dumps(
                [{"id": n["id"], "title": n["title"], "content": n["content"][:300]}
                 for n in news_items], ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + cfg["api_key"],
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode())
    text = data["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    parsed = json.loads(m.group(0))
    result = {}
    items = parsed.get("items") or parsed.get("result") or []
    if isinstance(items, dict):
        items = [items]
    if not items and "sentiment" in parsed and batch:
        items = [dict(parsed, id=batch[0]["id"])]
    for it in items:
        if isinstance(it, dict) and it.get("id") is not None:
            result[it["id"]] = {
                "sentiment": str(it.get("sentiment", "中性"))[:4],
                "signal": str(it.get("signal", "持有"))[:4],
                "summary": str(it.get("summary", ""))[:100],
            }
    return result


def analyze_news(limit=30, force=False):
    """批量分析未分析的消息。返回 {analyzed, failed, total}"""
    rows = _pg_query(
        "SELECT id, title, content FROM news"
        + ("" if force else " WHERE ai_analysis IS NULL")
        + " ORDER BY published_at DESC LIMIT %s", (int(limit),))
    analyzed = failed = 0
    for i in range(0, len(rows), 8):
        batch = rows[i:i + 8]
        try:
            result = ai_analyze_batch(batch)
            for r in batch:
                if r["id"] in result:
                    _pg_exec("UPDATE news SET ai_analysis=%s WHERE id=%s",
                             (json.dumps(result[r["id"]], ensure_ascii=False), r["id"]))
                    analyzed += 1
                else:
                    failed += 1
        except Exception as e:
            print("[ai] 分析批次失败: %s" % e, flush=True)
            failed += len(batch)
    return {"analyzed": analyzed, "failed": failed, "total": len(rows)}


AI_RECOMMEND_PROMPT = """你是基金配置专家。根据用户提供的基金信息(名称/代码/当前净值)，推荐一套适合普通投资者的配置。
要求:
1. strategy 从这些选: target(目标收益止损) / ma(基金均线) / index_ma(指数均线) / combo(组合评分)
2. 行业类基金(半导体/通信等)优先 combo 或 index_ma; 稳健投资者可用 target
3. params 字段参考: cost_price(持仓成本,填当前净值), target_gain(止盈线,0.10~0.20), stop_loss(止损线,-0.06~-0.12), ma_days(均线周期,默认20), index_secid(东财指数代码,格式如 1.512480 或 0.159995), index_name(指数名称)
4. 常用指数: 半导体ETF=1.512480, 通信ETF=1.515880, 芯片ETF=0.159995, 5G通信ETF=1.515050, 科创50=1.000688, 创业板指=0.399006
5. 只输出 JSON: {"strategy":"...","params":{...},"tips":"给用户的一句话说明(30字内)"}"""


def ai_recommend_fund(fund):
    """AI 根据基金信息推荐配置。返回 dict 或抛异常。"""
    cfg = get_ai_config()
    if not cfg.get("api_key"):
        raise RuntimeError("未配置 AI Key")
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    history = []
    try:
        history = fetch_fund_history(fund["code"], days=5)
    except Exception:
        pass
    nav = history[-1][1] if history else None
    info = {
        "code": fund["code"], "name": fund.get("name", ""),
        "current_nav": nav,
    }
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": AI_RECOMMEND_PROMPT},
            {"role": "user", "content": json.dumps(info, ensure_ascii=False)},
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + cfg["api_key"],
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    text = data["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("AI 返回格式异常")
    out = json.loads(m.group(0))
    out.setdefault("strategy", "combo")
    out.setdefault("params", {})
    return out


# ---------- 回测 ----------

BACKTEST_STRATEGIES = {
    "drawdown": "回撤止损(阶段高点回撤N%卖出)",
    "daily_drop": "当日暴跌(N%以上卖出)",
    "ma_cross": "短线死叉(5日线下穿10日线)",
    "ma20": "跌破20日均线",
    "target": "目标收益/止损",
    "advanced": "高阶多因子",
}


def _ma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def backtest_fund(code, strategy, params=None, days=250):
    """回测单只基金某策略。返回 {signals, stats}。
    signals: [{date, price, chg_pct, reason, after3, after5, after10}]
    统计: 有效躲过(after5<=-2%) / 误杀(after5>=+2%) / 命中率 / 平均收益
    """
    params = params or {}
    history = fetch_fund_history(code, days=max(days, 60))
    if len(history) < 30:
        return {"error": "历史数据不足 30 天"}
    dates = [d for d, _ in history]
    closes = [v for _, v in history]
    n = len(closes)
    index_by_date = {}
    if strategy == "advanced" and params.get("index_secid"):
        index_by_date = dict(fetch_index_kline(str(params["index_secid"]), days=max(days, 60)))

    sig_indices = []
    peak = 0.0
    buy_price = params.get("cost_price") or closes[0]
    last_sell = -99

    for i in range(n):
        price = closes[i]
        chg = (price - closes[i - 1]) / closes[i - 1] if i > 0 else 0.0
        peak = max(peak, price)
        dd = (peak - price) / peak if peak else 0.0
        ma5 = _ma(closes[:i + 1], 5)
        ma10 = _ma(closes[:i + 1], 10)
        ma20 = _ma(closes[:i + 1], 20)
        ma5_prev = _ma(closes[:i], 5)
        ma10_prev = _ma(closes[:i], 10)

        reason = None
        if strategy == "drawdown":
            if dd >= params.get("threshold", 0.05):
                reason = "从阶段高点 %s 回撤 %.1f%%" % (round(peak, 4), dd * 100)
        elif strategy == "daily_drop":
            if chg <= -params.get("threshold", 0.03):
                reason = "当日暴跌 %.1f%%" % (chg * 100)
        elif strategy == "ma_cross":
            if (ma5 and ma10 and ma5_prev and ma10_prev
                    and ma5 < ma10 and ma5_prev >= ma10_prev):
                reason = "5日线(%s)下穿10日线(%s)" % (round(ma5, 4), round(ma10, 4))
        elif strategy == "ma20":
            if ma20 and price < ma20:
                reason = "净值 %s 跌破20日均线 %s" % (round(price, 4), round(ma20, 4))
        elif strategy == "target":
            ret = (price - buy_price) / buy_price
            if ret >= params.get("target_gain", 0.15):
                reason = "收益率 %+.1f%% 达止盈线" % (ret * 100)
            elif ret <= params.get("stop_loss", -0.10):
                reason = "收益率 %+.1f%% 触止损线" % (ret * 100)
        elif strategy == "advanced":
            if i < 60:
                reason = None
            else:
                ma20_i = _ma(closes[:i + 1], 20)
                ma60_i = _ma(closes[:i + 1], 60)
                rsi_i = _rsi(closes[:i + 1], 14) or 50
                mom_i = (price - closes[i - 21]) / closes[i - 21]
                peak_i = max(closes[max(0, i - 59):i + 1])
                dd_i = (peak_i - price) / peak_i if peak_i else 0
                score_i = 50
                score_i += 12 if price > ma20_i else -12
                score_i += 12 if ma20_i > ma60_i else -12
                score_i += 12 if mom_i >= 0.05 else (-12 if mom_i <= -0.05 else 0)
                score_i += 8 if 45 <= rsi_i <= 70 else (-8 if rsi_i > 78 else 5 if rsi_i < 30 else 0)
                if dd_i >= params.get("drawdown_sell", 0.15): score_i -= 15
                elif dd_i >= params.get("drawdown_warn", 0.10): score_i -= 8
                if chg <= -0.03: score_i -= 10
                index_price = index_by_date.get(dates[i])
                if index_price is not None:
                    index_values = [v for d, v in index_by_date.items() if d <= dates[i]]
                    index_ma20 = _ma(index_values, 20)
                    index_ma60 = _ma(index_values, 60)
                    if index_ma20 is not None and index_ma60 is not None:
                        score_i += 12 if index_price > index_ma20 and index_ma20 > index_ma60 else (
                            -12 if index_price < index_ma20 and index_ma20 < index_ma60 else 0)
                score_i = max(0, min(100, score_i))
                if score_i <= params.get("sell_score", 35):
                    reason = "多因子评分%d，低于卖出线" % score_i
                elif score_i >= params.get("buy_score", 65):
                    reason = "多因子评分%d，高于买入线" % score_i

        if reason and i - last_sell >= 5:
            sig_indices.append(i)
            last_sell = i

    signals = []
    for idx in sig_indices:
        price = closes[idx]
        after = {}
        for k in (3, 5, 10):
            j = idx + k
            after[k] = (closes[j] - price) / price * 100 if j < n else None
        signals.append({
            "date": dates[idx],
            "price": price,
            "chg_pct": round((price - closes[idx - 1]) / closes[idx - 1] * 100, 2) if idx else 0,
            "reason": None,  # 由下面填充
            "after3": round(after[3], 2) if after[3] is not None else None,
            "after5": round(after[5], 2) if after[5] is not None else None,
            "after10": round(after[10], 2) if after[10] is not None else None,
        })

    # 重新填充 reason（避免上面重复计算）
    si = 0
    for i in range(n):
        if si < len(sig_indices) and i == sig_indices[si]:
            signals[si]["reason"] = _backtest_reason(closes, i, strategy, params)
            si += 1

    valid = [s for s in signals if s["after5"] is not None and s["after5"] <= -2]
    bad = [s for s in signals if s["after5"] is not None and s["after5"] >= 2]
    total_valid = len(valid) + len(bad)
    avg5 = [s["after5"] for s in signals if s["after5"] is not None]
    last5 = sum(avg5) / len(avg5) if avg5 else None
    hold_ret = (closes[-1] - closes[0]) / closes[0] * 100

    # 策略执行模拟: 卖出信号后 5 个交易日买回，累计轮动收益 vs 一直持有
    exec_ret = 0.0
    prev = 0
    for idx in sig_indices:
        buy_idx = idx + 5
        if buy_idx < n:
            sell_p = closes[idx]
            buy_p = closes[buy_idx]
            exec_ret += (buy_p - sell_p) / sell_p
            prev = buy_idx
    if prev < n - 1 and prev > 0:
        exec_ret += (closes[-1] - closes[prev]) / closes[prev]
    elif not sig_indices:
        exec_ret = hold_ret

    stats = {
        "signals": len(signals),
        "dodged": len(valid),
        "false_alarm": len(bad),
        "hit_rate": round(len(valid) / total_valid * 100, 1) if total_valid else None,
        "avg_after5": round(last5, 2) if last5 is not None else None,
        "hold_ret": round(hold_ret, 2),
        "strategy_ret": round(exec_ret * 100, 2),
        "period": "%s ~ %s" % (dates[0], dates[-1]),
    }
    return {"signals": signals[-20:], "stats": stats, "total_signals": len(signals)}


def _backtest_reason(closes, i, strategy, params):
    price = closes[i]
    peak = max(closes[:i + 1])
    if strategy == "drawdown":
        return "回撤 %.1f%%(高点 %s)" % ((peak - price) / peak * 100, round(peak, 4))
    if strategy == "daily_drop":
        return "当日跌幅 %.1f%%" % ((price - closes[i - 1]) / closes[i - 1] * 100)
    if strategy == "ma_cross":
        return "5日线下穿10日线"
    if strategy == "ma20":
        return "跌破20日均线"
    if strategy == "target":
        buy = params.get("cost_price") or closes[0]
        return "收益率 %+.1f%%" % ((price - buy) / buy * 100)
    if strategy == "advanced":
        return "高阶多因子触发信号"
    return ""


def run_all(cfg=None, force=False):
    """运行全部基金信号并推送。返回 (funds_out, errors, push_results)。"""
    cfg = cfg or load_config()
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    funds_out, errors = evaluate_all(cfg)
    title = "【基金操作提醒】%s" % date_str
    content = build_message(funds_out, date_str)
    if errors:
        content += "\n\n[异常] " + "; ".join(errors)
    try:
        hot = [n for n in list_news(limit=8, category="利好")][:3] + \
              [n for n in list_news(limit=8, category="利空")][:3]
        if hot:
            content += "\n\n【行业消息速递】\n"
            for n in hot:
                content += "%s[%s][%s] %s\n" % (
                    n["published_at"][5:16] if n["published_at"] else "", n["category"], n["industry"], n["title"])
    except Exception:
        pass
    push_results = []
    if funds_out and (force or is_trading_now()):
        push_results = notify(title, content, cfg)
    append_history({
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "funds": funds_out,
        "errors": errors,
    })
    return funds_out, errors, push_results, content
