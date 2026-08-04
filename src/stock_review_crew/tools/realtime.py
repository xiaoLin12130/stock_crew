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
import re
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
    """行业板块涨幅 + 主力净流入：东财 clist → 腾讯行业排行 → 浏览器同花顺。"""
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
    try:
        return _sector_flow_from_tx()
    except Exception as exc:  # noqa: BLE001
        tx_err = exc
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
        raise RealtimeError(f"板块数据失败：东财({em_err})；腾讯({tx_err})；浏览器({exc})") from exc


def _sector_flow_from_tx() -> dict[str, Any]:
    """备用：腾讯行业排行（proxy.finance.qq.com，实测可用；资金单位为万元）。"""
    resp = requests.get(
        "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank",
        params={"board_type": "hy", "sort_type": "price", "direct": "down",
                "offset": 0, "count": 30},
        headers={"User-Agent": _UA, "Referer": "https://gu.qq.com/"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    rank = ((resp.json().get("data") or {}).get("rank_list")) or []
    rows = []
    for r in rank:
        pct = _to_float(r.get("zdf"))
        net = _to_float(r.get("zljlr"))
        leader = r.get("lzg") or {}
        leader_pct = _to_float(leader.get("zdf"))
        rows.append(
            {
                "name": r.get("name"),
                "pct_change": round(pct / 100.0, 6) if pct is not None else None,
                "net_inflow": net * 10000 if net is not None else None,  # 腾讯为万元
                "leading_stock": leader.get("name"),
                "leading_pct_change": round(leader_pct / 100.0, 6) if leader_pct is not None else None,
            }
        )
    if not rows:
        raise RealtimeError("腾讯行业排行无数据")
    with_pct = [r for r in rows if r.get("pct_change") is not None]
    with_flow = [r for r in rows if r.get("net_inflow") is not None]
    return {
        "top": sorted(with_pct, key=lambda x: x["pct_change"], reverse=True)[:5],
        "bottom": sorted(with_pct, key=lambda x: x["pct_change"])[:5],
        "flow_in": sorted(with_flow, key=lambda x: x["net_inflow"], reverse=True)[:5],
        "flow_out": sorted(with_flow, key=lambda x: x["net_inflow"])[:5],
        "source": "腾讯行业",
        "degraded": False,
        "degraded_reason": [],
        "units": {"pct_change": "小数(0.05=5%)", "net_inflow": "元"},
    }


def _secid(code: str) -> str:
    code = str(code or "").strip().zfill(6)
    if code.startswith(("6", "9")):
        return f"1.{code}"
    return f"0.{code}"


def _tx_symbol(code: str) -> str:
    """转腾讯行情代码格式（sh600519 / sz000001 / bj830799）。"""
    code = str(code or "").strip().zfill(6)
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("4", "8", "92")):
        return f"bj{code}"
    return f"sz{code}"


def fetch_stock_quote(code: str) -> dict[str, Any]:
    """个股实时行情（东财 push2 stock/get，fltt=2 已小数化；涨跌幅百分数 → /100）。"""
    em_err: Optional[Exception] = None
    try:
        secid = _secid(code)
        data = _http_json(
            f"{_EM}/stock/get",
            {"secid": secid, "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170",
             "fltt": 2, "invt": 2},
        )
        d = data.get("data") or {}
        if d and d.get("f43") not in (None, "-"):
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
    except Exception as exc:  # noqa: BLE001
        em_err = exc
    # 备用：腾讯行情（qt.gtimg.cn，实测可用）
    try:
        import requests as _rq

        resp = _rq.get(
            f"https://qt.gtimg.cn/q={_tx_symbol(code)}",
            headers={"User-Agent": _UA},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        text = resp.text
        if "=" not in text:
            raise RealtimeError(f"腾讯行情返回异常：{code}")
        parts = text.split("=", 1)[1].strip('"').split("~")
        if len(parts) < 35:
            raise RealtimeError(f"腾讯行情字段不足：{code}")
        pct = _to_float(parts[32])
        turnover = _to_float(parts[38]) if len(parts) > 38 else None
        return {
            "code": str(parts[2]).zfill(6),
            "name": parts[1],
            "price": _to_float(parts[3]),
            "pct_change": round(pct / 100.0, 6) if pct is not None else None,
            "open": _to_float(parts[5]),
            "high": _to_float(parts[33]),
            "low": _to_float(parts[34]),
            "pre_close": _to_float(parts[4]),
            "volume": _to_float(parts[6]),
            "amount": None,
            "turnover_rate": round(turnover / 100.0, 6) if turnover is not None else None,
            "source": "腾讯实时",
            "units": {"pct_change": "小数(0.05=5%)", "turnover_rate": "小数"},
        }
    except RealtimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RealtimeError(
            f"未查询到 {code} 的实时行情：东财({em_err})；腾讯({exc})"
        ) from exc


def search_stocks(query: str) -> list[dict[str, Any]]:
    """股票名称/代码搜索：东财 suggest（主）→ 腾讯 smartbox（备）。"""
    q = str(query or "").strip()
    if not q:
        raise RealtimeError("请输入股票名称或代码")
    if re.fullmatch(r"\d{6}", q):
        code = q
        return [{"code": code, "name": None, "market": "sh" if code.startswith(("6", "9")) else "sz",
                 "type": "GP"}]
    try:
        data = _http_json(
            "http://searchapi.eastmoney.com/api/suggest/get",
            {"input": q, "type": 14, "token": "D43BF722C8E33BDC906FB84D85E326E8", "count": 8},
        )
        items = ((data.get("QuotationCodeTable") or {}).get("Data")) or []
        out = []
        for it in items:
            code = str(it.get("Code") or "").zfill(6)
            if not code or code == "000000":
                continue
            mkt = str(it.get("MktNum") or "")
            market = ("sh" if mkt == "1" or code.startswith(("6", "9"))
                      else "bj" if code.startswith(("4", "8", "92")) else "sz")
            out.append({"code": code, "name": it.get("Name"), "market": market,
                        "type": it.get("SecurityTypeName")})
        if out:
            return out
    except Exception:  # noqa: BLE001 - 走腾讯备用
        pass
    try:
        resp = requests.get(
            "https://smartbox.gtimg.cn/s3/",
            params={"v": 2, "q": q, "t": "gp"},
            headers={"User-Agent": _UA},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        m = re.search(r'v_hint="([^"]*)"', resp.text)
        out = []
        if m:
            for part in m.group(1).split("^"):
                f = part.split("~")
                if len(f) >= 3 and f[2]:
                    code = str(f[1]).zfill(6)
                    out.append({"code": code, "name": f[2], "market": f[0], "type": "GP"})
        if out:
            return out
    except Exception:  # noqa: BLE001
        pass
    raise RealtimeError(f"未找到与「{q}」匹配的股票")


def _parse_sina_batch(text: str) -> list[dict[str, Any]]:
    """解析新浪批量行情（hq.sinajs.cn）：name,open,pre_close,price,high,low,...,volume,amount,时间。"""
    out = []
    for m in re.finditer(r'hq_str_([a-z]{2}\d{6})="([^"]*)"', text):
        sym, fields = m.group(1), m.group(2)
        f = fields.split(",")
        if len(f) < 32:
            continue
        out.append({
            "code": sym[2:],
            "name": f[0],
            "open": _to_float(f[1]), "pre_close": _to_float(f[2]),
            "price": _to_float(f[3]), "high": _to_float(f[4]), "low": _to_float(f[5]),
            "volume": _to_float(f[8]), "amount": _to_float(f[9]),
        })
    return out


def fetch_sentiment_realtime(date: Optional[str] = None) -> dict[str, Any]:
    """盘中「昨日涨停今日表现」明细：东财昨日涨停池 + 新浪批量实时报价。
    口径：昨收为基准；连板率=今日再涨停÷昨日涨停；核按钮率=今日跌停÷昨日涨停；
    主板10%/创业科创20%；过滤 ST/北证。"""
    today = date or datetime.now().strftime("%Y-%m-%d")
    prev = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    resp = requests.get(
        "http://push2ex.eastmoney.com/getTopicZTPool",
        params={"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
                "Pageindex": 0, "pagesize": 600, "sort": "fbt:asc",
                "date": prev.replace("-", "")},
        headers={"User-Agent": _UA},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    pool = ((resp.json().get("data") or {}).get("pool")) or []
    if not pool:
        raise RealtimeError("东财昨日涨停池无数据（可能非交易日或超出保留期）")
    codes = [str(x.get("c") or "").zfill(6) for x in pool]
    codes = [c for c in codes if c and not c.startswith(("4", "8", "92"))]
    symbols = ",".join(("sh" if c.startswith(("6", "9")) else "sz") + c for c in codes)
    r = requests.get(
        "http://hq.sinajs.cn/list=" + symbols,
        headers={"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    quotes = _parse_sina_batch(r.text)
    if not quotes:
        raise RealtimeError("新浪批量行情无数据")
    rows = []
    for q in quotes:
        price, pre = q.get("price"), q.get("pre_close")
        if price is None or pre is None or pre <= 0:
            continue
        q["pct_change"] = round((price - pre) / pre, 6)
        rows.append(q)
    if not rows:
        raise RealtimeError("批量行情解析为空")
    n = len(rows)
    pcts = [x["pct_change"] for x in rows]
    is_ge = lambda c: c.startswith(("30", "68"))  # noqa: E731
    limit_cnt = sum(1 for x in rows
                    if x["pct_change"] >= (0.199 if is_ge(x["code"]) else 0.099))
    down_limit = sum(1 for x in rows
                     if x["pct_change"] <= (-0.199 if is_ge(x["code"]) else -0.099))
    best = sorted(rows, key=lambda x: x["pct_change"], reverse=True)[:3]
    worst = sorted(rows, key=lambda x: x["pct_change"])[:3]
    return {
        "yesterday": prev, "today": today,
        "yesterday_zt_count": len(codes), "matched_today": n,
        "avg_return": round(sum(pcts) / n, 6),
        "median_return": None,
        "red_rate": round(sum(1 for p in pcts if p > 0) / n, 6),
        "lianban_count": limit_cnt, "lianban_rate": round(limit_cnt / n, 6),
        "hean_count": down_limit, "hean_rate": round(down_limit / n, 6),
        "best3": [{"code": x["code"], "name": x["name"], "pct_change": x["pct_change"]} for x in best],
        "worst3": [{"code": x["code"], "name": x["name"], "pct_change": x["pct_change"]} for x in worst],
        "zhaban": None, "touched": None, "zhaban_rate": None,
        "source": "东财昨日池+新浪实时",
        "degraded": True,
        "degraded_reason": ["Tushare 为收盘后口径，盘中使用实时计算"],
        "note": "盘中实时口径（昨收为基准；仅覆盖昨日涨停池）",
    }


def fetch_breadth_realtime() -> dict[str, Any]:
    """全市场涨跌家数（盘中实时）：东财 clist 全 A 统计，过滤 ST/北证。"""
    data = _http_json(
        f"{_EM}/clist/get",
        {"pn": 1, "pz": 7000, "po": 1, "np": 1, "fltt": 2, "invt": 2,
         "fid": "f3", "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
         "fields": "f3,f12,f14"},
    )
    diff = (data.get("data") or {}).get("diff") or []
    up = down = flat = 0
    total = 0
    for r in diff:
        code = str(r.get("f12") or "")
        name = str(r.get("f14") or "")
        if code.startswith(("4", "8", "92")) or "ST" in name.upper() or "退" in name:
            continue
        pct = _to_float(r.get("f3"))
        if pct is None:
            continue
        total += 1
        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1
    if not total:
        raise RealtimeError("东财全市场实时统计无数据")
    return {
        "total": total, "up": up, "down": down, "flat": flat,
        "up_down_ratio": round(up / down, 4) if down else None,
        "source": "东财实时",
        "degraded": False,
        "degraded_reason": [],
        "note": "盘中实时全市场统计（过滤 ST/北证）",
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
