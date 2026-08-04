# -*- coding: utf-8 -*-
"""盘中实时数据快照（解决「API 只有昨日数据」问题）。

主源全部为**实时接口**（已实测 2026-08-04）：
- 指数：东财 push2 `ulist.np/get`（HTTP 改写）→ 备用腾讯 `qt.gtimg.cn`（带时间戳）；
- 板块涨幅/主力净流入：东财 `clist`（fs=m:90+t:2，fid=f62）；
- 涨跌停/炸板/封板率：同花顺 dataapi（盘中实时统计）；
- 快讯：新浪 7x24。

约定（契约 §六）：比率小数、金额元、None=「—」、每块 source/degraded 标注、
单块超时 ≤15s；龙虎榜/历史资讯天然非实时（见 docs/REALTIME.md）。
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Optional

import requests

_TIMEOUT = 10
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_EM = "http://push2.eastmoney.com/api/qt"
_INDEX_SECIDS = "1.000001,0.399001,0.399006,1.000688"
_INDEX_FIELDS = "f2,f3,f4,f12,f14,f15,f16,f17,f18"


class RealtimeError(RuntimeError):
    """实时数据错误（中文消息）。"""


def _http_json(url: str, params: Optional[dict] = None) -> Any:
    """HTTP GET → JSON；东财多子域轮换重试（本机网络对 eastmoney 偶发 502/Reset）。"""
    attempts = [url]
    if "eastmoney.com" in url:
        for host in ("http://80.push2.eastmoney.com", "http://17.push2.eastmoney.com"):
            cand = url.replace("http://push2.eastmoney.com", host)
            if cand not in attempts:
                attempts.append(cand)
    attempts = attempts[:2]  # 实时快照限时：最多 2 次尝试（10s×2=20s 上限）
    last_exc: Optional[Exception] = None
    for candidate in attempts:
        try:
            resp = requests.get(candidate, params=params, headers={"User-Agent": _UA}, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    raise RealtimeError(f"HTTP 请求失败：{last_exc}")


def _to_float(value: Any) -> Optional[float]:
    if value in (None, "-", ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_indices() -> dict[str, Any]:
    """四大指数实时（东财 fltt=2 已是小数价格；涨跌幅为百分数值 → /100 归一）。"""
    data = _http_json(
        f"{_EM}/ulist.np/get",
        {"secids": _INDEX_SECIDS, "fields": _INDEX_FIELDS, "fltt": 2, "invt": 2},
    )
    diff = (data.get("data") or {}).get("diff") or []
    rows: list[dict[str, Any]] = []
    for r in diff:
        pct = _to_float(r.get("f3"))
        rows.append(
            {
                "name": r.get("f14"),
                "code": str(r.get("f12") or ""),
                "price": _to_float(r.get("f2")),
                "pct_change": round(pct / 100.0, 6) if pct is not None else None,
                "open": _to_float(r.get("f17")),
                "high": _to_float(r.get("f15")),
                "low": _to_float(r.get("f16")),
                "pre_close": _to_float(r.get("f18")),
                "source": "东财实时",
            }
        )
    if not rows:
        raise RealtimeError("东财指数实时无数据")
    return {"indices": rows, "source": "东财实时", "degraded": False, "degraded_reason": []}


def fetch_indices_tx() -> dict[str, Any]:
    """备用：腾讯行情（qt.gtimg.cn，实时，带时间戳）。"""
    resp = requests.get(
        "http://qt.gtimg.cn/q=sh000001,sz399001,sz399006,sh000688",
        headers={"User-Agent": _UA},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    rows: list[dict[str, Any]] = []
    for line in resp.text.split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        parts = line.split("=", 1)[1].strip('"').split("~")
        if len(parts) < 35:
            continue
        pct = _to_float(parts[32])
        rows.append(
            {
                "name": parts[1],
                "code": parts[2],
                "price": _to_float(parts[3]),
                "pct_change": round(pct / 100.0, 6) if pct is not None else None,
                "pre_close": _to_float(parts[4]),
                "open": _to_float(parts[5]),
                "high": _to_float(parts[33]),
                "low": _to_float(parts[34]),
                "time": parts[30] if len(parts) > 30 else None,
                "source": "腾讯实时",
            }
        )
    if not rows:
        raise RealtimeError("腾讯指数实时无数据")
    return {"indices": rows, "source": "腾讯实时", "degraded": False, "degraded_reason": []}


def fetch_sector_flow() -> dict[str, Any]:
    """行业板块涨幅 + 主力净流入（东财 clist fid=f62，实时）。"""
    em_err: Optional[Exception] = None
    try:
        data = _http_json(
            f"{_EM}/clist/get",
            {
                "pn": 1, "pz": 60, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f62", "fs": "m:90+t:2+f:!50",
                "fields": "f3,f12,f14,f62",
            },
        )
        diff = (data.get("data") or {}).get("diff") or []
        rows = []
        for r in diff:
            pct = _to_float(r.get("f3"))
            net = _to_float(r.get("f62"))
            rows.append(
                {
                    "name": r.get("f14"),
                    "pct_change": round(pct / 100.0, 6) if pct is not None else None,
                    "net_inflow": net,  # 元
                }
            )
        if rows:
            with_pct = [r for r in rows if r.get("pct_change") is not None]
            with_flow = [r for r in rows if r.get("net_inflow") is not None]
            return {
                "top": sorted(with_pct, key=lambda x: x["pct_change"], reverse=True)[:5],
                "bottom": sorted(with_pct, key=lambda x: x["pct_change"])[:5],
                "flow_in": sorted(with_flow, key=lambda x: x["net_inflow"], reverse=True)[:5],
                "flow_out": sorted(with_flow, key=lambda x: x["net_inflow"])[:5],
                "source": "东财实时",
                "degraded": False,
                "degraded_reason": [],
                "units": {"pct_change": "小数(0.05=5%)", "net_inflow": "元"},
            }
    except Exception as exc:  # noqa: BLE001
        em_err = exc
    # 备用：浏览器解析同花顺板块页（页面 JS 签名自动执行）
    try:
        from .browser_crawler import fetch_ths_sector_rows

        rows = fetch_ths_sector_rows()
        with_pct = [r for r in rows if r.get("涨跌幅") is not None]
        with_flow = [r for r in rows if r.get("净流入") is not None]
        norm = lambda r: {  # noqa: E731
            "name": r.get("板块"),
            "pct_change": round(float(r["涨跌幅"]) / 100.0, 6),
            "net_inflow": r.get("净流入"),
        }
        return {
            "top": sorted((norm(r) for r in with_pct), key=lambda x: x["pct_change"], reverse=True)[:5],
            "bottom": sorted((norm(r) for r in with_pct), key=lambda x: x["pct_change"])[:5],
            "flow_in": sorted((norm(r) for r in with_flow), key=lambda x: x["net_inflow"] or 0, reverse=True)[:5],
            "flow_out": sorted((norm(r) for r in with_flow), key=lambda x: x["net_inflow"] or 0)[:5],
            "source": "同花顺(浏览器)",
            "degraded": False,
            "degraded_reason": [],
            "units": {"pct_change": "小数(0.05=5%)", "net_inflow": "元"},
        }
    except Exception as exc:  # noqa: BLE001
        raise RealtimeError(f"板块数据失败：东财({em_err})；浏览器({exc})") from exc


def _secid(code: str) -> str:
    code = str(code or "").strip().zfill(6)
    if code.startswith(("6", "9")):
        return f"1.{code}"
    return f"0.{code}"


def fetch_stock_quote(code: str) -> dict[str, Any]:
    """个股实时行情（东财 push2 stock/get，fltt=2 已小数化；涨跌幅百分数 → /100）。"""
    secid = _secid(code)
    data = _http_json(
        f"{_EM}/stock/get",
        {"secid": secid, "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170",
         "fltt": 2, "invt": 2},
    )
    d = data.get("data") or {}
    if not d or d.get("f43") in (None, "-"):
        raise RealtimeError(f"未查询到 {code} 的实时行情（代码可能错误或停牌）")
    pct = _to_float(d.get("f170"))
    turnover = _to_float(d.get("f169"))
    return {
        "code": str(d.get("f57") or code).zfill(6),
        "name": d.get("f58"),
        "price": _to_float(d.get("f43")),
        "pct_change": round(pct / 100.0, 6) if pct is not None else None,
        "open": _to_float(d.get("f46")),
        "high": _to_float(d.get("f44")),
        "low": _to_float(d.get("f45")),
        "pre_close": _to_float(d.get("f60")),
        "volume": _to_float(d.get("f47")),
        "amount": _to_float(d.get("f48")),
        "turnover_rate": round(turnover / 100.0, 6) if turnover is not None else None,
        "source": "东财实时",
        "units": {"pct_change": "小数(0.05=5%)", "turnover_rate": "小数"},
    }


def fetch_zt_stats(date: Optional[str] = None) -> dict[str, Any]:
    """涨跌停/炸板/封板率实时统计（同花顺 dataapi，盘中即更新）。"""
    from .ths_crawler import ThsError, build_zt_block

    d = date or datetime.now().strftime("%Y-%m-%d")
    try:
        block = build_zt_block(d)
    except ThsError as exc:
        raise RealtimeError(f"同花顺实时统计失败：{exc}") from exc
    limit_up = block.get("limit_up") or {}
    limit_down = block.get("limit_down") or {}
    zhaban = block.get("zhaban") or {}
    return {
        "limit_up_count": limit_up.get("count"),
        "limit_down_count": limit_down.get("count"),
        "zhaban_count": zhaban.get("count"),
        "touched_count": zhaban.get("touched"),
        "zhaban_rate": zhaban.get("zhaban_rate"),
        "seal_rate": block.get("seal_rate"),
        "yesterday_zt_count": block.get("yesterday_zt_count"),
        "tier": limit_up.get("tier", {}),
        "source": "同花顺实时",
        "degraded": False,
        "degraded_reason": [],
        "units": {"zhaban_rate": "小数(0.17=17%)", "seal_rate": "小数"},
    }


def fetch_news() -> dict[str, Any]:
    """财经快讯（新浪 7x24，实时滚动）。"""
    data = _http_json(
        "https://zhibo.sina.com.cn/api/zhibo/feed",
        {"page": 1, "page_size": 10, "zhibo_id": 152, "tag_id": 0, "dire": "f", "dpc": 1},
    )
    feed = ((data.get("result") or {}).get("data") or {}).get("feed") or {}
    items = feed.get("list") or []
    news = []
    for item in items:
        text = str(item.get("rich_text") or item.get("text") or "").strip()
        if text:
            news.append(
                {
                    "time": item.get("create_time"),
                    "text": text[:200],
                    "source": "新浪7x24",
                }
            )
    if not news:
        raise RealtimeError("新浪7x24快讯无数据")
    return {"news": news[:10], "source": "新浪7x24", "degraded": False, "degraded_reason": []}


def market_status(now: Optional[datetime] = None) -> dict[str, Any]:
    """交易时段判定（中国 A 股）：交易日 09:15-15:00 各阶段。"""
    now = now or datetime.now()
    weekday = now.weekday()
    hm = now.hour * 60 + now.minute
    is_trading_day = weekday < 5
    if not is_trading_day:
        phase = "非交易日"
    elif hm < 9 * 60 + 15:
        phase = "盘前"
    elif hm <= 9 * 60 + 25:
        phase = "集合竞价"
    elif hm < 9 * 60 + 30:
        phase = "开盘准备"
    elif hm <= 11 * 60 + 30:
        phase = "交易中(上午)"
    elif hm < 13 * 60:
        phase = "午间休市"
    elif hm <= 15 * 60:
        phase = "交易中(下午)"
    else:
        phase = "已收盘"
    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "is_trading_day": is_trading_day,
        "phase": phase,
    }


def fetch_realtime_snapshot() -> dict[str, Any]:
    """完整实时快照：指数/涨跌停/板块/快讯**并行**抓取（总耗时≈最慢单块，≤20s）；
    每块独立降级标注。"""
    status = market_status()
    snapshot: dict[str, Any] = {"status": status, "indices": None, "zt": None,
                                "sectors": None, "news": None, "auction": None,
                                "sources": [], "degraded": []}

    def _indices() -> None:
        try:
            snapshot["indices"] = fetch_indices()
            snapshot["sources"].append("东财实时")
        except Exception as exc:  # noqa: BLE001
            snapshot["degraded"].append(f"指数: {exc}")
            try:
                snapshot["indices"] = fetch_indices_tx()
                snapshot["sources"].append("腾讯实时")
            except Exception as exc2:  # noqa: BLE001
                snapshot["degraded"].append(f"指数备用: {exc2}")

    def _zt() -> None:
        try:
            snapshot["zt"] = fetch_zt_stats(status["date"])
            snapshot["sources"].append("同花顺实时")
        except Exception as exc:  # noqa: BLE001
            snapshot["degraded"].append(f"涨跌停统计: {exc}")

    def _sectors() -> None:
        try:
            snapshot["sectors"] = fetch_sector_flow()
            snapshot["sources"].append("东财实时")
        except Exception as exc:  # noqa: BLE001
            snapshot["degraded"].append(f"板块资金流: {exc}")

    def _news() -> None:
        try:
            snapshot["news"] = fetch_news()
            snapshot["sources"].append("新浪7x24")
        except Exception as exc:  # noqa: BLE001
            snapshot["degraded"].append(f"快讯: {exc}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fn) for fn in (_indices, _zt, _sectors, _news)]
        for f in as_completed(futures):
            f.result()  # 内部已全部兜底，不会上抛
    # 竞价窗口提示
    hm = datetime.now().hour * 60 + datetime.now().minute
    if status.get("is_trading_day") and 9 * 60 + 15 <= hm <= 9 * 60 + 25:
        snapshot["auction"] = {"window": True, "note": "当前处于集合竞价窗口（09:15-09:25），可执行竞价复盘"}
    else:
        snapshot["auction"] = {"window": False, "note": "非竞价窗口（09:15-09:25），竞价数据不可用"}
    snapshot["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return snapshot


if __name__ == "__main__":
    print(json.dumps(fetch_realtime_snapshot(), ensure_ascii=False, indent=1)[:2000])
