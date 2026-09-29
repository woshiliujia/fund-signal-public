#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Optional adapter for xalpha portfolio analytics.

The web app deliberately does not import xalpha on startup.  xalpha brings a
larger scientific-Python dependency tree and talks to third-party data
providers, so the feature must remain opt-in and must not affect the signal
service when it is unavailable.
"""
import datetime as dt
import math
import re


class XalphaIntegrationError(Exception):
    """A user-actionable error from the optional portfolio integration."""


def _load_xalpha():
    try:
        import xalpha  # type: ignore
        import pandas  # type: ignore
    except ImportError as exc:
        raise XalphaIntegrationError(
            "组合分析未安装。请在服务环境执行 pip install -r "
            "requirements-xalpha.txt 后重启服务。"
        ) from exc
    return xalpha, pandas


def status():
    """Return a JSON-safe capability status without importing it eagerly."""
    try:
        xalpha, _ = _load_xalpha()
        return {
            "available": True,
            "version": getattr(xalpha, "__version__", "unknown"),
            "message": "xalpha 组合分析已就绪。",
        }
    except XalphaIntegrationError as exc:
        return {"available": False, "message": str(exc)}


def normalise_transactions(transactions):
    """Validate and convert a compact ledger into xalpha's list format.

    ``trade`` follows xalpha's convention: a positive number is a subscription
    amount; a negative number is redeemed share count.  It is intentionally
    not inferred from the UI because that would make a portfolio calculation
    look more certain than the source ledger actually is.
    """
    if not isinstance(transactions, list) or not transactions:
        raise XalphaIntegrationError("请至少输入一条交易流水")
    if len(transactions) > 500:
        raise XalphaIntegrationError("一次最多分析 500 条交易流水")

    rows = []
    for index, item in enumerate(transactions, start=1):
        if not isinstance(item, dict):
            raise XalphaIntegrationError("第 %d 条流水格式不正确" % index)
        raw_date = str(item.get("date") or "").strip().replace("-", "/")
        try:
            parsed_date = dt.datetime.strptime(raw_date, "%Y/%m/%d").date()
        except ValueError as exc:
            raise XalphaIntegrationError(
                "第 %d 条日期应为 YYYY-MM-DD 或 YYYY/MM/DD" % index
            ) from exc
        fund = str(item.get("fund") or "").strip()
        if not re.fullmatch(r"\d{6}", fund):
            raise XalphaIntegrationError("第 %d 条基金代码应为 6 位数字" % index)
        try:
            trade = float(item.get("trade"))
        except (TypeError, ValueError) as exc:
            raise XalphaIntegrationError("第 %d 条交易数量必须是数字" % index) from exc
        if not math.isfinite(trade) or trade == 0:
            raise XalphaIntegrationError("第 %d 条交易数量必须是非零有限数" % index)
        rows.append({"date": parsed_date.strftime("%Y/%m/%d"), "fund": fund, "trade": trade})
    return rows


def _json_value(value):
    """Convert pandas/numpy/date scalar values into JSON-safe Python values."""
    if value is None:
        return None
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    try:
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        if hasattr(value, "item"):
            value = value.item()
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
    except (TypeError, ValueError):
        pass
    return value if isinstance(value, (str, int, float, bool)) else str(value)


def _frame_records(frame):
    """Turn a pandas DataFrame into a small JSON table for the browser."""
    records = []
    for item in frame.to_dict(orient="records"):
        records.append({str(key): _json_value(value) for key, value in item.items()})
    return records


def analyse_portfolio(transactions):
    """Calculate summary and money-weighted return for an imported ledger.

    xalpha may fetch fund data from third-party providers while building the
    portfolio.  Nothing is persisted by this adapter.
    """
    rows = normalise_transactions(transactions)
    xalpha, pandas = _load_xalpha()
    try:
        ledger = xalpha.record(pandas.DataFrame(rows), format="list")
        portfolio = xalpha.mul(status=ledger)
        summary = portfolio.combsummary()
        try:
            xirr = _json_value(portfolio.xirrrate())
        except Exception:
            xirr = None
    except Exception as exc:
        raise XalphaIntegrationError(
            "xalpha 无法完成本次计算。请核对基金代码、交易日期和网络后重试。"
        ) from exc

    return {
        "transactions": rows,
        "summary": _frame_records(summary),
        "xirr": xirr,
        "notice": "结果由 xalpha 与第三方数据源计算，仅供研究；交易流水未写入服务存储。",
    }
