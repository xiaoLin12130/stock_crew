"""6 模式感知复盘引擎：LangGraph 流水线 + 进度钩子 + 无 Key 规则引擎降级（I2）。

流水线：fetch（按 mode 分发取数）→ news → trend（并行）→ sentiment（并行）→ host
→ debate（循环 ≤max_rounds）→ report。

- 取数优先调用 I1 纯函数接口 fetch_mode_data(mode, date, ...) -> dict；
  未落地/失败时防御性 import + try/except 降级（旧工具兜底 → 明确标注缺失），绝不阻塞；
- LLM 无 Key/失败 → 确定性规则引擎，degraded=True 可见标注；
- 免责声明在组装层强制追加（ensure_disclaimer）；
- 进度钩子 _emit(stage, pct, message)：pct 单调递增；
  阶段：fetch / news / trend n/2 / sentiment n/3 / host / debate / report / done。
"""

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from langgraph.graph import END, StateGraph


class DataValidationError(RuntimeError):
    """取数校验失败：核心数据缺失，复盘终止（中文消息）。"""

from . import prompts
from .prompts import DEFAULT_MODE, DISCLAIMER, MODE_INFO
from .skills import list_skills
from .state import ReviewState

__all__ = [
    "build_graph",
    "build_result",
    "default_initial_state",
    "set_progress_callback",
    "ensure_disclaimer",
    "_emit",
]


# ═══════════════════════════════════════════════════════════════
# 进度钩子（阶段序列：fetch/news/trend n/2/sentiment n/3/host/debate/report/done）
# ═══════════════════════════════════════════════════════════════

_PROGRESS_LOCK = threading.Lock()
_PROGRESS_CALLBACK = None
_LAST_PCT = 0.0


def set_progress_callback(callback):
    """注册进度回调 callback(stage, pct, message)；None 表示关闭。"""
    global _PROGRESS_CALLBACK
    with _PROGRESS_LOCK:
        _PROGRESS_CALLBACK = callback


def _reset_progress():
    global _LAST_PCT
    with _PROGRESS_LOCK:
        _LAST_PCT = 0.0


def _emit(stage, pct, message):
    """进度钩子：pct 单调递增（clamp 到上一次值），回调异常不影响主流程。"""
    global _LAST_PCT
    try:
        pct = float(pct)
        with _PROGRESS_LOCK:
            if pct < _LAST_PCT:
                pct = _LAST_PCT
            _LAST_PCT = pct
            callback = _PROGRESS_CALLBACK
        if callback is not None:
            callback(stage, pct, message)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# LLM 防御性访问（无 Key/未安装 → 规则引擎降级）
# ═══════════════════════════════════════════════════════════════

def _llm_ready() -> bool:
    try:
        from .config import DEEPSEEK_API_KEY, llm, llm_strict
        return bool(DEEPSEEK_API_KEY and llm is not None and llm_strict is not None)
    except Exception:
        return False


def _llm_pair():
    from .config import llm, llm_strict
    return llm, llm_strict


def _callbacks():
    try:
        from langfuse.langchain import CallbackHandler
        return [CallbackHandler()]
    except Exception:
        return []


def _load_analyst_tools():
    """分析师可用工具（I1 接口）：未落地/不可用返回 (None, None)，不阻塞。"""
    try:
        from .tools.stock_data import get_stock_info, search_history
        return get_stock_info, search_history
    except Exception:
        return None, None


# ═══════════════════════════════════════════════════════════════
# Node1: 模式化取数（fetch 按 mode 分发）
# ═══════════════════════════════════════════════════════════════

def _load_pure_fetcher():
    """I1 纯函数接口（约定 fetch_mode_data(mode, date, ...) -> dict）；未落地返回 None。"""
    try:
        from .tools.stock_data import fetch_mode_data
        if callable(fetch_mode_data):
            return fetch_mode_data
    except Exception:
        pass
    return None


_LEGACY_FETCH_TIMEOUT = 20.0


def _legacy_fetch(state):
    """旧 LangChain 工具兜底：防御性 import + 20s 总预算（daemon 线程），超时放弃不阻塞。"""
    result = {}

    def _work():
        try:
            from .tools.stock_data import (
                get_index_trend,
                get_market_macro,
                get_market_micro,
                get_news_headlines,
                get_sentiment,
            )
            date = state.get("date", "")
            micro = get_market_micro.invoke({"date": date})
            trend = get_index_trend.invoke({"date": date, "days": 60})
            macro = get_market_macro.invoke({"date": date})
            sentiment = get_sentiment.invoke({"date": date})
            news = get_news_headlines.invoke({"date": date})
            market_md = (
                f"## 一、今日市场微观数据\n{micro}\n\n"
                f"## 二、近60日指数趋势\n{trend}\n\n"
                f"## 三、宏观外围数据\n{macro}\n\n"
                f"## 四、昨日涨停今日表现\n{sentiment}\n\n"
                f"## 五、今日财经资讯\n{news}"
            )
            result["value"] = {
                "market_data": market_md,
                "raw_news": news,
                "snapshot": {"legacy": True},
            }
        except Exception:
            result["value"] = None

    worker = threading.Thread(target=_work, daemon=True)
    worker.start()
    worker.join(timeout=_LEGACY_FETCH_TIMEOUT)
    return result.get("value")


def _normalize_snapshot(data, mode, date, notes, source):
    """把取数结果包装为快照（供 I4 落盘/前端图表/规则引擎）。"""
    return {
        "date": date,
        "mode": mode,
        "source": source,
        "data": data if isinstance(data, dict) else {},
        "degraded": list(notes),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _extract_news(data):
    """从取数结果中提取资讯原始文本（JSON 字符串）。"""
    if not isinstance(data, dict):
        return "{}"
    news = data.get("news")
    if news is None:
        for key in ("headlines", "caixin", "cctv"):
            if data.get(key):
                news = data[key]
                break
    if news is None:
        return "{}"
    if isinstance(news, str):
        return news
    try:
        return json.dumps(news, ensure_ascii=False, indent=2)
    except Exception:
        return str(news)


_MODE_BLOCKS = {
    "pre_market": ["index", "trend", "external", "news"],
    "auction": ["auction", "index", "zhangting", "news"],
    "intraday_am": ["index", "intraday", "sectors", "zhangting", "sentiment", "news"],
    "noon": ["index", "intraday", "sectors", "zhangting", "sentiment", "news"],
    "intraday_pm": ["index", "intraday", "sectors", "zhangting", "sentiment", "dragon_tiger", "news"],
    "close": ["index", "zhangting", "dieting", "sectors", "sentiment", "breadth", "dragon_tiger", "news"],
}

_BLOCK_ALIASES = {
    "index": ("指数表现", ("index", "indices", "index_data", "zhishu", "index_trend")),
    "trend": ("近60日指数趋势", ("trend", "ma", "index_trend", "avg_lines")),
    "external": ("宏观外围数据", ("external", "macro", "overnight", "overseas")),
    "news": ("今日财经资讯", ("news", "headlines")),
    "zhangting": ("涨停板", ("zhangting", "zt", "zt_pool", "limit_up")),
    "dieting": ("跌停板", ("dieting", "dt", "dt_pool", "limit_down")),
    "sectors": ("板块表现", ("sectors", "sector", "board", "industry")),
    "sentiment": ("昨日涨停今日表现（情绪核心）", ("sentiment", "emotion", "yesterday_zt")),
    "breadth": ("全市场涨跌分布", ("breadth", "market_breadth", "up_down")),
    "auction": ("竞价数据", ("auction", "jingjia", "auction_data", "call_auction")),
    "intraday": ("分时数据", ("intraday", "fenshi", "minute", "minline")),
    "dragon_tiger": ("龙虎榜", ("dragon_tiger", "lhb", "longhubang")),
}


def _find_block(data, names):
    for key in names:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _fmt_value(v):
    if v is None:
        return "无数据"
    if isinstance(v, bool):
        return "是" if v else "否"
    return str(v)


def _dict_to_md(value, indent=0):
    lines = []
    prefix = "  " * indent
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ("raw", "daily"):
                continue
            if isinstance(v, (dict, list)):
                lines.append(f"{prefix}- {k}:")
                lines.extend(_dict_to_md(v, indent + 1))
            else:
                lines.append(f"{prefix}- {k}: {_fmt_value(v)}")
    elif isinstance(value, list):
        if not value:
            lines.append(f"{prefix}（空列表）")
        elif all(isinstance(x, dict) and ("time" in x or "datetime" in x or "分钟" in x)
                 for x in value[:3]):
            # 分时/分钟线：给 LLM 完整摘要（首尾+极值+采样），禁止只截前 10 根
            n = len(value)
            def _bar(b):
                return "，".join(
                    f"{k}={_fmt_value(v)}" for k, v in b.items()
                    if k != "raw" and not isinstance(v, (dict, list))
                )
            lines.append(f"{prefix}- 共 {n} 根（完整）")
            lines.append(f"{prefix}- 首根: {_bar(value[0])}")
            lines.append(f"{prefix}- 当前: {_bar(value[-1])}")
            highs = [x.get("high") or x.get("close") for x in value
                     if (x.get("high") or x.get("close")) is not None]
            lows = [x.get("low") or x.get("close") for x in value
                    if (x.get("low") or x.get("close")) is not None]
            if highs:
                lines.append(f"{prefix}- 最高: {max(highs)}，最低: {min(lows)}")
            step = max(1, n // 40)
            for item in value[::step][:40]:
                lines.append(f"{prefix}- {_bar(item)}")
        else:
            for item in value[:10]:
                if isinstance(item, dict):
                    fields = "，".join(
                        f"{k}={_fmt_value(v)}" for k, v in item.items()
                        if k not in ("raw",) and not isinstance(v, (dict, list))
                    )
                    lines.append(f"{prefix}- {fields or '（对象）'}")
                else:
                    lines.extend(_dict_to_md(item, indent + 1))
    else:
        lines.append(f"{prefix}{_fmt_value(value)}")
    return lines


def _block_md(label, value, cap=40):
    header = f"### {label}"
    if isinstance(value, dict) and value.get("source"):
        header += f"（来源：{value['source']}）"
    lines = [header, ""]
    if isinstance(value, dict):
        if value.get("degraded"):
            reasons = "；".join(str(r) for r in (value.get("degraded_reason") or []))
            lines.append(f"- 降级：{reasons or '数据源降级'}")
        if value.get("note"):
            lines.append(f"- 说明：{value['note']}")
    lines.extend(_dict_to_md(value))
    if len(lines) > cap:
        lines = lines[:cap] + ["", f"（数据过长，仅展示前 {cap - 2} 行）"]
    return "\n".join(lines)


def _render_market_markdown(data, mode, notes):
    """把取数结果渲染为 Markdown 数据块（缺失块明确标注，绝不编造）。"""
    if not isinstance(data, dict):
        return "（无数据）"
    # 旧工具兜底路径：直接复用已组合的 Markdown
    if isinstance(data.get("market_data"), str) and data.get("raw_news") is not None:
        return data["market_data"]

    mode = mode if mode in _MODE_BLOCKS else DEFAULT_MODE
    date = data.get("date") or ""
    label = MODE_INFO.get(mode, {}).get("label", mode)
    lines = [f"# {date} 行情数据快照（模式：{label}）", ""]
    if notes:
        lines.append("> 降级说明：" + "；".join(notes))
        lines.append("")
    for blk in _MODE_BLOCKS[mode]:
        block_label, names = _BLOCK_ALIASES[blk]
        value = _find_block(data, names)
        if value is None:
            lines.append(f"### {block_label}：数据缺失（无数据）")
            lines.append("")
            continue
        lines.append(_block_md(block_label, value))
        lines.append("")
    known = {n for _, names in _BLOCK_ALIASES.values() for n in names}
    extra = [k for k in data if k not in known and not str(k).startswith("_")]
    if extra:
        lines.append("### 其他数据")
        for k in extra[:5]:
            lines.append(f"- {k}: {_fmt_value(data[k])[:200]}")
    return "\n".join(lines)


def fetch_data_node(state):
    """Node1：按 mode 分发取数（I1 纯函数优先，防御性降级，绝不阻塞）。"""
    mode = state.get("mode") or DEFAULT_MODE
    date = state.get("date") or ""
    _reset_progress()
    _emit("fetch", 3, "正在拉取行情数据…")

    notes = []
    fetcher = _load_pure_fetcher()
    data = None
    source = "missing"
    if fetcher is not None:
        try:
            data = fetcher(mode=mode, date=date)
            source = "fetch_mode_data"
        except Exception as exc:
            notes.append(f"模式化取数失败：{exc}")
    if not isinstance(data, dict) or not data:
        legacy = _legacy_fetch(state)
        if isinstance(legacy, dict) and legacy.get("market_data"):
            data = legacy
            source = "legacy"
            notes.append("模式化取数不可用，已使用旧取数工具降级")
        else:
            data = {}
            notes.append("取数工具不可用/离线：行情数据缺失，禁止编造")

    # I1 契约结构：{date, mode, mode_label, blocks, degraded[], degraded_flag}
    mode_label = None
    if isinstance(data, dict) and isinstance(data.get("blocks"), dict):
        for reason in data.get("degraded") or []:
            notes.append(str(reason))
        mode_label = data.get("mode_label")
        data = data["blocks"]

    snapshot = _normalize_snapshot(data, mode, date, notes, source)
    if mode_label:
        snapshot["mode_label"] = mode_label
    raw_news = data.get("raw_news") if isinstance(data, dict) else None
    if raw_news is None:
        raw_news = _extract_news(data)
    market_md = _render_market_markdown(data, mode, notes) or "（无数据）"

    _emit("fetch", 8, "行情数据获取完成")
    return {
        "market_data": market_md,
        "raw_news": raw_news,
        "snapshot": snapshot,
        "degraded": bool(notes) or bool(state.get("degraded")),
        "degraded_notes": notes,
    }


# 各模式核心数据块：缺失（source=数据缺失）即无复盘必要（R7 取数校验）
CRITICAL_BLOCKS = {
    "pre_market": ("index_trend",),
    "auction": ("auction",),
    "intraday_am": ("index_trend", "minute"),
    "noon": ("index_trend", "minute"),
    "intraday_pm": ("index_trend", "minute"),
    "close": ("index_trend", "minute"),
}


def _block_usable(blk) -> bool:
    if blk is None:
        return False
    if isinstance(blk, dict) and blk.get("source") in ("数据缺失", "数据缺失/估算"):
        return False
    return True


def validate_data_node(state):
    mode = state.get("mode") or "close"
    snapshot = state.get("snapshot") or {}
    blocks = snapshot.get("data") if isinstance(snapshot, dict) else None
    if not isinstance(blocks, dict) or not blocks:
        return {}
    missing = [name for name in CRITICAL_BLOCKS.get(mode, ())
               if name in blocks and not _block_usable(blocks.get(name))]
    if missing:
        names = "、".join(missing)
        raise DataValidationError(
            "取数校验失败：" + names + "数据缺失（全部数据源不可用），本次复盘终止。请检查网络/代理/数据源后重试。"
        )
    return {}

def _data_router(state):
    return END if state.get("data_ok") is False else "news_analyst"


# ═══════════════════════════════════════════════════════════════
# Node1b: 资讯分析
# ═══════════════════════════════════════════════════════════════

def _rule_news_analysis(raw_news):
    """降级资讯分析：原文罗列（规则提取标题），不做 AI 多空归类，禁止编造。"""
    try:
        payload = json.loads(raw_news)
    except Exception:
        return "资讯内容无法解析（降级），无法提取利多利空。"
    lines = []
    for item in (payload.get("caixin") or [])[:5]:
        if isinstance(item, dict) and item.get("summary"):
            lines.append(f"- [财新] {str(item['summary'])[:120]}")
    for title in (payload.get("cctv") or [])[:5]:
        lines.append(f"- [央视] {title}")
    for ev in (payload.get("economic_calendar") or [])[:3]:
        if isinstance(ev, dict) and ev.get("event"):
            lines.append(f"- [经济日历] {ev.get('time', '')} {ev.get('event', '')}")
    if not lines:
        return "资讯内容为空（降级），无法提取利多利空。"
    return "### 今日要闻（规则引擎提取）\n" + "\n".join(lines)


def news_analyst_node(state):
    """Node1b：资讯分析（LLM 失败 → 规则提取 + 降级标注）。"""
    raw_news = state.get("raw_news") or ""
    _emit("news", 12, "正在分析资讯…")
    if not raw_news.strip() or raw_news.strip() == "{}":
        analysis = "今日无重大资讯影响。"
        _emit("news", 15, "资讯分析完成（无资讯）")
        return {"market_data": f"{state.get('market_data', '')}\n\n## 资讯分析\n\n{analysis}"}

    content = None
    degraded = False
    if _llm_ready():
        try:
            _, strict = _llm_pair()
            content = strict.invoke(
                [
                    {"role": "system", "content": prompts.NEWS_ANALYST_SYSTEM_PROMPT},
                    {"role": "user", "content": prompts.build_news_prompt(raw_news)},
                ],
                config={"callbacks": _callbacks()},
            ).content
        except Exception:
            content = None
    if content is None:
        content = _rule_news_analysis(raw_news)
        degraded = True
    if degraded:
        content += "\n\n> （降级模式：LLM 不可用，资讯仅原文罗列，未做 AI 利多/利空归类）"
    _emit("news", 15, "资讯分析完成")
    return {
        "market_data": f"{state.get('market_data', '')}\n\n## 资讯分析\n\n{content}",
        "degraded": bool(state.get("degraded")) or degraded,
        "degraded_notes": ["LLM 不可用/失败，资讯分析降级为规则提取"] if degraded else [],
    }


# ═══════════════════════════════════════════════════════════════
# Node2a/2b: 趋势派（并行 2 人）+ 情绪派（并行 3 人）
# ═══════════════════════════════════════════════════════════════

def _llm_analyst(state, skill, group, trend_context):
    """单分析师 LLM 推理（含工具调用循环）；任何失败返回 None → 规则引擎。"""
    try:
        llm, _ = _llm_pair()
        base_prompt = prompts.build_analyst_prompt(state, group, trend_context)
        get_stock_info, search_history = _load_analyst_tools()
        use_tools = get_stock_info is not None and search_history is not None
        runner = llm.bind_tools([get_stock_info, search_history]) if use_tools else llm

        messages = [
            {"role": "system", "content": skill.get("prompt", "")},
            {"role": "user", "content": base_prompt},
        ]
        response = runner.invoke(messages, config={"callbacks": _callbacks()})
        tool_log = []
        tool_rounds = 0
        while getattr(response, "tool_calls", None) and tool_rounds < 3:
            messages.append(response)
            for tc in response.tool_calls:
                args = tc.get("args", {}) if isinstance(tc.get("args"), dict) else {}
                tool_name = tc.get("name", "?")
                if tool_name == "search_history":
                    try:
                        result = search_history.invoke(args)
                    except BaseException as exc:  # chroma Rust panic 是 BaseException
                        result = json.dumps({"error": f"历史检索不可用：{exc}"}, ensure_ascii=False)
                    tool_log.append({"type": "search", "query": args.get("query", "?"), "result": result[:200]})
                elif tool_name == "get_stock_info":
                    try:
                        result = get_stock_info.invoke(args)
                    except BaseException as exc:
                        result = json.dumps({"error": f"个股查询不可用：{exc}"}, ensure_ascii=False)
                    tool_log.append({"code": args.get("code", "?"), "result": result[:300]})
                else:
                    tool_log.append({"type": "unknown", "result": str(args)[:200]})
                    result = str(args)
                messages.append({"role": "tool", "content": result, "tool_call_id": tc["id"]})
            response = runner.invoke(messages, config={"callbacks": _callbacks()})
            tool_rounds += 1
        return {
            "skill_name": skill.get("name", ""),
            "skill_id": skill.get("id", ""),
            "analysis": response.content,
            "tool_used": bool(tool_log),
            "tool_calls": tool_log,
            "group": group,
        }
    except BaseException:  # chroma Rust panic（PanicException）是 BaseException，必须兜住
        return None


def _as_ratio(value, key):
    """契约：比率一律小数；旧字段 *_pct 为百分数，进契约前 ÷100 归一。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not _isfinite(v):
        return None
    if str(key).endswith("_pct"):
        return v / 100.0
    return v


def _isfinite(v):
    try:
        import math
        return math.isfinite(v)
    except Exception:
        return True


def _rule_facts(snapshot):
    """从快照中提取规则引擎可用事实（只取语义明确字段，禁止编造）。"""
    if not isinstance(snapshot, dict):
        return {}
    data = snapshot.get("data") if isinstance(snapshot.get("data"), dict) else snapshot
    if not isinstance(data, dict):
        return {}
    facts = {}

    index = _find_block(data, ("index", "indices", "index_data", "zhishu", "index_trend"))
    index_dir = {}
    if isinstance(index, dict):
        index_items = index.get("indices") if isinstance(index.get("indices"), dict) else index
        for name, item in index_items.items():
            if isinstance(item, dict) and item.get("pct_change") is not None:
                try:
                    index_dir[name] = float(item["pct_change"])
                except (TypeError, ValueError):
                    continue
    facts["index"] = index_dir

    zt = _find_block(data, ("zhangting", "zt", "zt_pool", "limit_up"))
    if isinstance(zt, dict):
        limit_up = zt.get("limit_up")
        if isinstance(limit_up, dict) and limit_up.get("count") is not None:
            facts["zt_total"] = limit_up["count"]
        elif zt.get("sealed_count") is not None:
            facts["zt_total"] = zt["sealed_count"]
        elif zt.get("total") is not None:
            facts["zt_total"] = zt["total"]
        if zt.get("zhaban_rate") is not None:
            facts["zhaban_rate"] = _as_ratio(zt["zhaban_rate"], "zhaban_rate")
    dt = _find_block(data, ("dieting", "dt", "dt_pool", "limit_down"))
    if isinstance(dt, dict):
        limit_down = dt.get("limit_down")
        if isinstance(limit_down, dict) and limit_down.get("count") is not None:
            facts["dt_total"] = limit_down["count"]
        elif dt.get("total") is not None:
            facts["dt_total"] = dt["total"]

    sentiment = _find_block(data, ("sentiment", "emotion", "yesterday_zt"))
    if isinstance(sentiment, dict):
        for src_key, out_key in (
            ("avg_return_pct", "avg_return"),
            ("avg_return", "avg_return"),
            ("red_rate_pct", "red_rate"),
            ("red_rate", "red_rate"),
            ("lianban_rate_pct", "lianban_rate"),
            ("lianban_rate", "lianban_rate"),
            ("hean_rate_pct", "hean_rate"),
            ("hean_rate", "hean_rate"),
        ):
            if src_key in sentiment and sentiment[src_key] is not None and out_key not in facts:
                ratio = _as_ratio(sentiment[src_key], src_key)
                if ratio is not None:
                    facts[out_key] = ratio
        if sentiment.get("up_count") is not None:
            facts["up"] = sentiment["up_count"]
        if sentiment.get("down_count") is not None:
            facts["down"] = sentiment["down_count"]
        if sentiment.get("zhaban_rate") is not None and "zhaban_rate" not in facts:
            facts["zhaban_rate"] = _as_ratio(sentiment["zhaban_rate"], "zhaban_rate")

    breadth = _find_block(data, ("breadth", "market_breadth", "up_down"))
    if isinstance(breadth, dict):
        if breadth.get("up") is not None:
            facts["up"] = breadth["up"]
        if breadth.get("down") is not None:
            facts["down"] = breadth["down"]
        if breadth.get("up_down_ratio") is not None and "up_down_ratio" not in facts:
            try:
                facts["up_down_ratio"] = float(breadth["up_down_ratio"])
            except (TypeError, ValueError):
                pass
        if breadth.get("limit_up") is not None and "zt_total" not in facts:
            facts["zt_total"] = breadth["limit_up"]
        if breadth.get("limit_down") is not None and "dt_total" not in facts:
            facts["dt_total"] = breadth["limit_down"]
        for src_key in ("zhaban_rate_pct", "zhaban_rate"):
            if src_key in breadth and breadth[src_key] is not None:
                ratio = _as_ratio(breadth[src_key], src_key)
                if ratio is not None:
                    facts["zhaban_rate"] = ratio
                break

    sectors = _find_block(data, ("sectors", "sector", "board", "industry"))
    top = []
    if isinstance(sectors, dict) and isinstance(sectors.get("top5"), list):
        for s in sectors["top5"][:3]:
            if isinstance(s, dict):
                name = s.get("name") or s.get("板块") or s.get("板块名称")
                if name:
                    top.append(str(name))
    facts["sectors"] = top

    auction = _find_block(data, ("auction", "jingjia", "auction_data", "call_auction"))
    facts["auction_available"] = isinstance(auction, dict) and auction.get("count") is not None
    return facts


def _facts_sentence(facts):
    parts = []
    if facts.get("zt_total") is not None:
        parts.append(f"涨停 {facts['zt_total']} 家")
    if facts.get("dt_total") is not None:
        parts.append(f"跌停 {facts['dt_total']} 家")
    if facts.get("up") is not None and facts.get("down") is not None:
        parts.append(f"涨 {facts['up']} 家 / 跌 {facts['down']} 家")
    if facts.get("avg_return") is not None:
        parts.append(f"昨日涨停今日平均收益 {facts['avg_return'] * 100:.2f}%")
    if facts.get("red_rate") is not None:
        parts.append(f"昨日涨停今日红盘率 {facts['red_rate'] * 100:.1f}%")
    if facts.get("zhaban_rate") is not None:
        parts.append(f"炸板率 {facts['zhaban_rate'] * 100:.1f}%")
    if facts.get("sectors"):
        parts.append("领涨板块：" + "、".join(facts["sectors"]))
    if not parts:
        return "关键数据缺失（无数据）"
    return "；".join(parts)


_INDEX_LABELS = {"shanghai": "上证", "shenzhen": "深证", "chuangye": "创业板", "kechuang": "科创50"}


def _direction_sentence(facts):
    idx = facts.get("index") or {}
    if not idx:
        return "指数方向：无数据"
    parts = []
    for name, v in idx.items():
        label = _INDEX_LABELS.get(name, name)
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        direction = "上涨" if v > 0 else ("下跌" if v < 0 else "持平")
        parts.append(f"{label}{direction}（涨跌幅数据源值 {v}）")
    return "指数方向：" + "；".join(parts)


_RULE_PLAN = {
    "trend": "方向未确认前控制总仓位，按均线纪律执行：破位收缩、企稳再谈进攻；无明确信号时空仓等待。",
    "sentiment": "情绪数据不足或退潮时不追高、不接力；等涨停梯队与量能确认后只做前排。",
}


def _rule_analyst(state, skill, group):
    """规则引擎分析师条目：仅引用快照事实，缺失标注，绝不编造。"""
    facts = _rule_facts(state.get("snapshot"))
    info = prompts.mode_info(state.get("mode"))
    yesterday = (state.get("yesterday_report") or "").strip()
    verify = "无昨日报告，跳过验证" if not yesterday else "未找到本人昨日预判，无法验证"
    persona = (skill.get("prompt") or "").strip()
    persona_line = persona[:120] if persona else "（无角色描述）"
    analysis = (
        f"## 1、昨日复盘验证\n{verify}\n\n"
        f"## 2、今日判断（{info['focus_zh']}）\n"
        f"{persona_line}\n\n"
        f"基于降级数据快照：{_facts_sentence(facts)}\n"
        f"{_direction_sentence(facts)}\n\n"
        f"## 3、操作计划（{info['plan_zh']}）\n"
        f"{_RULE_PLAN.get(group, '')}\n"
        f"（降级模式：规则引擎生成，仅基于快照数据；数据缺失项不猜测）"
    )
    return {
        "skill_name": skill.get("name", ""),
        "skill_id": skill.get("id", ""),
        "analysis": analysis,
        "tool_used": False,
        "tool_calls": [],
        "group": group,
        "degraded": True,
    }


def _run_analysts(state, group, trend_context="", progress_label="", progress_stage=""):
    """按 group 筛选分析师并行执行；每完成一位发射进度（trend n/2 / sentiment n/3）。"""
    skills = [s for s in list_skills() if s.get("group") in (group, "both")]
    if not skills:
        return []
    total = len(skills)
    base = 20 if group == "trend" else 30

    def _invoke_one(skill, index):
        entry = None
        if _llm_ready():
            entry = _llm_analyst(state, skill, group, trend_context)
        if entry is None:
            entry = _rule_analyst(state, skill, group)
        _emit(progress_stage or group, base + (index - 1) * 5,
              f"{progress_label} {index}/{total}：{skill.get('name', '')}完成")
        return entry

    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(skills))) as pool:
        futures = {pool.submit(_invoke_one, s, i): i for i, s in enumerate(skills, 1)}
        for future in as_completed(futures):
            results.append((futures[future], future.result()))
    results.sort(key=lambda x: x[0])
    return [r for _, r in results]


def trend_analysts_node(state):
    """Node2a：趋势分析师（并行）。"""
    results = _run_analysts(state, group="trend", progress_label="趋势派", progress_stage="trend")
    degraded = any(r.get("degraded") for r in results)
    return {
        "analyses": results,
        "degraded": bool(state.get("degraded")) or degraded,
        "degraded_notes": ["趋势派 LLM 不可用/失败，分析降级为规则引擎"] if degraded else [],
    }


def sentiment_analysts_node(state):
    """Node2b：情绪分析师（并行），引用趋势派结论。"""
    trend_text = "\n".join([
        f"【{a['skill_name']}】: {a['analysis'][:500]}"
        for a in state.get("analyses", []) if a.get("group") in ("trend", "both")
    ])
    results = _run_analysts(
        state, group="sentiment", trend_context=trend_text,
        progress_label="情绪派", progress_stage="sentiment",
    )
    degraded = any(r.get("degraded") for r in results)
    return {
        "analyses": results,
        "degraded": bool(state.get("degraded")) or degraded,
        "degraded_notes": ["情绪派 LLM 不可用/失败，分析降级为规则引擎"] if degraded else [],
    }


# ═══════════════════════════════════════════════════════════════
# Node3: 主持人（分歧判断）
# ═══════════════════════════════════════════════════════════════

_BULLISH_WORDS = ("看多", "看涨", "低吸", "买入", "加仓", "进攻", "抄底", "持有", "反弹", "追涨")
_BEARISH_WORDS = ("看空", "看跌", "减仓", "卖出", "防守", "空仓", "观望", "离场", "止损", "退潮", "回避")


def _parse_host(content):
    """解析主持人两行式输出：分歧判断：有/无 + 讨论议题：<议题>。"""
    has_disagreement = False
    topic = ""
    for line in (content or "").splitlines():
        s = line.strip().lstrip("-*•").strip()
        for sep in ("：", ":"):
            if sep not in s:
                continue
            head, tail = s.split(sep, 1)
            tail = tail.strip()
            if "分歧" in head:
                has_disagreement = "有" in tail
            elif "议题" in head or "话题" in head:
                topic = tail
            break
    if not topic or topic in ("无", "有", "无分歧", "无实质分歧", "暂无"):
        return False, ""
    return has_disagreement, topic


def _rule_host(state):
    """规则版主持人：按多空关键词判断是否存在实质性分歧。"""
    text = " ".join(
        f"{a.get('skill_name', '')}{a.get('analysis', '')}"
        for a in (state.get("analyses") or [])
    )
    if any(w in text for w in _BULLISH_WORDS) and any(w in text for w in _BEARISH_WORDS):
        return True, "多空方向分歧：今日应进攻还是防守？"
    return False, ""


def host_node(state):
    """Node3：主持人判断分歧并提炼议题；LLM 失败 → 规则关键词判断。"""
    round_label = "首轮" if not state.get("debate_history") else f"第{state.get('round_count', 0) + 1}轮"
    _emit("host", 50, f"主持人判断（{round_label}）…")

    content = None
    degraded = False
    if _llm_ready():
        try:
            _, strict = _llm_pair()
            content = strict.invoke(
                [
                    {"role": "system", "content": prompts.HOST_SYSTEM_PROMPT},
                    {"role": "user", "content": prompts.build_host_prompt(state, round_label)},
                ],
                config={"callbacks": _callbacks()},
            ).content
        except Exception:
            content = None
    if content is None:
        degraded = True
        has_disagreement, topic = _rule_host(state)
    else:
        has_disagreement, topic = _parse_host(content)

    new_round = state.get("round_count", 0) + 1
    max_rounds = max(1, min(int(state.get("max_rounds") or 3), 3))
    discussion_done = (not has_disagreement) or (new_round > max_rounds) or (not topic)
    return {
        "discussion_topic": topic,
        "discussion_done": discussion_done,
        "round_count": new_round,
        "degraded": bool(state.get("degraded")) or degraded,
        "degraded_notes": ["主持人 LLM 不可用/失败，分歧判断降级为规则引擎"] if degraded else [],
    }


# ═══════════════════════════════════════════════════════════════
# Node4: 辩论（交叉引用，循环 ≤ max_rounds）
# ═══════════════════════════════════════════════════════════════

_RULE_DEBATE = {
    "alang": "趋势未破前底仓不动，回踩关键均线企稳是观察点；破位则收缩，不赌反弹。",
    "bingchuan": "方向之争不重要，逻辑在不在才重要；主升逻辑未被证伪就不必恐慌，证伪当天纠错。",
    "baxiaoxian": "情绪分歧期重仓才是大忌；断板缩量先观察，换手放量确认再低吸。",
    "yangjia": "一致转分歧是必然过程；高潮后先兑现一部分，等恐慌释放再考虑机会。",
    "tiechui": "行情是走出来的不是猜出来的；现在多看少动，等确定性信号再动手。",
}


def debate_node(state):
    """Node4：对议题逐位表态，引用上一轮他人观点（交叉引用）。"""
    topic = state.get("discussion_topic", "")
    current_round = state.get("round_count", 1) or 1
    _emit("debate", 55 + (current_round - 1) * 10, f"辩论第 {current_round} 轮…")
    if not topic:
        return {"debate_history": [{"round": current_round, "topic": "无议题", "responses": []}]}

    skills = {s["name"]: s for s in list_skills()}
    all_names = [a["skill_name"] for a in (state.get("analyses") or [])]
    all_names = list(dict.fromkeys(n for n in all_names if n in skills))

    prev_response = {}
    if state.get("debate_history"):
        last_entry = state["debate_history"][-1]
        for r in last_entry.get("responses", []):
            prev_response[r["skill_name"]] = r["response"]

    responses = []
    degraded = False
    for i, name in enumerate(all_names):
        skill = skills[name]
        others_text = "".join(
            f"\n【{other_name}的观点】\n{other_resp[:500]}\n"
            for other_name, other_resp in prev_response.items() if other_name != name
        )
        response = None
        if _llm_ready():
            try:
                llm, _ = _llm_pair()
                response = llm.invoke(
                    [
                        {"role": "system", "content": skill.get("prompt", "")},
                        {"role": "user", "content": prompts.build_debate_prompt(skill, topic, others_text)},
                    ],
                    config={"callbacks": _callbacks()},
                ).content
            except Exception:
                response = None
        if response is None:
            degraded = True
            base = _RULE_DEBATE.get(skill.get("id") or "", _RULE_DEBATE["tiechui"])
            response = f"（降级模式）关于「{topic}」：{base}"
        responses.append({
            "skill_name": name,
            "skill_id": skill.get("id", ""),
            "response": response,
        })
        _emit("debate", 56 + (current_round - 1) * 10 + i, f"辩论第 {current_round} 轮：{name}表态完成")

    return {
        "debate_history": [{"round": current_round, "topic": topic, "responses": responses}],
        "degraded": bool(state.get("degraded")) or degraded,
        "degraded_notes": ["辩论 LLM 不可用/失败，观点降级为规则引擎"] if degraded else [],
    }


# ═══════════════════════════════════════════════════════════════
# Node5: 最终报告（唯一 Markdown + 免责声明强制 + 综合标签）
# ═══════════════════════════════════════════════════════════════

def ensure_disclaimer(text):
    """程序级强制免责声明：保证出现在末尾且仅一次。"""
    text = (text or "").strip()
    if not text:
        return f"> {DISCLAIMER}"
    if text.rstrip().endswith(DISCLAIMER):
        return text
    parts = [p.strip() for p in text.split(DISCLAIMER) if p.strip()]
    body = "\n\n".join(parts)
    return f"{body}\n\n---\n\n> {DISCLAIMER}"


def _extract_tags(content):
    """从「## 综合标签」章节提取 # 标签（其他章节出现的 # 不提取）。"""
    tags = []
    m = re.search(r"##\s*综合标签(.*?)(?:\n##|\Z)", content or "", re.S)
    section = m.group(1) if m else ""
    for t in re.findall(r"#([^\s#，,。；;【】]+)", section):
        t = t.strip()
        if t and t not in tags:
            tags.append(t)
    return tags[:6]


def _sign(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0
    return 1 if v > 0 else (-1 if v < 0 else 0)


def _summary_sentence(facts):
    idx = facts.get("index") or {}
    if not idx:
        return "降级模式下仅依据有限快照，今日方向无法确认（数据不足）。"
    signs = [_sign(v) for v in idx.values()]
    up = sum(1 for s in signs if s > 0)
    down = sum(1 for s in signs if s < 0)
    if up and not down:
        return "主要指数方向偏强，但降级模式数据有限，建议以真实行情复核。"
    if down and not up:
        return "主要指数方向偏弱，降级模式下建议防守优先。"
    return "指数方向分化，降级模式下建议控制仓位、等待确认。"


def _rule_tags(state):
    """规则版综合标签（降级可见 + 方向/情绪标签，全部源自快照）。"""
    facts = _rule_facts(state.get("snapshot"))
    tags = ["降级模式"]
    signs = [_sign(v) for v in (facts.get("index") or {}).values()]
    if signs:
        up = sum(1 for s in signs if s > 0)
        down = sum(1 for s in signs if s < 0)
        if up and not down:
            tags.append("大盘偏强")
        elif down and not up:
            tags.append("大盘偏弱")
        else:
            tags.append("方向分化")
    red = facts.get("red_rate")
    if red is not None:
        tags.append("情绪回暖" if red >= 0.5 else ("情绪偏冷" if red < 0.4 else "情绪中性"))
    zhaban = facts.get("zhaban_rate")
    if zhaban is not None and zhaban >= 0.4:
        tags.append("炸板率高企")
    if not any(k for k in facts if k not in ("index", "sectors", "auction_available")):
        tags.append("数据有限")
    return tags[:6]


def _section_index(sections, *hints, default=0):
    for i, s in enumerate(sections):
        if any(h in s for h in hints):
            return i
    return default


def _rule_report(state):
    """规则版最终报告：按模式章节结构生成，缺失标注，降级可见。"""
    mode = state.get("mode") or DEFAULT_MODE
    info = prompts.mode_info(mode)
    date = state.get("date") or ""
    sections = info["sections"]
    facts = _rule_facts(state.get("snapshot"))

    content_by_title = {}

    overview = [
        f"- 复盘日期：{date}；模式：{info['label']}（{info['window']}）",
        f"- {_facts_sentence(facts)}",
        f"- {_direction_sentence(facts)}",
    ]
    if mode == "auction" and not facts.get("auction_available"):
        overview.append("- 竞价数据缺失（无 Cookie/数据源不可用），无法给出追高建议")
    content_by_title[sections[0]] = overview

    views_idx = _section_index(sections, "核心观点")
    views = []
    for a in (state.get("analyses") or []):
        views += ["", f"### {a.get('skill_name', '分析师')}", a.get("analysis", "")]
    content_by_title[sections[views_idx]] = views

    debate_idx = _section_index(sections, "分歧")
    debate_lines = []
    debate = state.get("debate_history") or []
    if debate:
        for entry in debate:
            debate_lines += ["", f"### 第{entry.get('round', '?')}轮：{entry.get('topic', '')}"]
            for r in entry.get("responses", []):
                debate_lines.append(f"- **{r.get('skill_name', '')}**：{r.get('response', '')}")
    else:
        debate_lines += ["", "本轮分析未发现实质性分歧（或分歧判断数据不足）。"]
    content_by_title[sections[debate_idx]] = debate_lines

    plan_idx = _section_index(sections, "操作", "计划", "清单", "买卖", "尾盘")
    content_by_title[sections[plan_idx]] = [
        f"- {_RULE_PLAN['trend']}",
        f"- {_RULE_PLAN['sentiment']}",
        "- 数据缺失项未做推测；请以真实行情数据为准。",
    ]

    sum_idx = _section_index(sections, "总结", "预案")
    if sum_idx != 0 or "总结" in sections[0] or "预案" in sections[0]:
        content_by_title[sections[sum_idx]] = [f"- {_summary_sentence(facts)}"]

    risk_lines = [
        "- 市场有风险，本报告基于降级规则引擎与有限快照数据生成，仅供参考。",
        "- 历史数据不代表未来表现，请独立判断，控制仓位。",
    ]
    risk_idx = _section_index(sections, "风险")
    if "风险" in sections[risk_idx]:
        content_by_title[sections[risk_idx]] = risk_lines
    elif "总结" in sections[sum_idx]:
        content_by_title[sections[sum_idx]] = content_by_title.get(sections[sum_idx], []) + ["", "**风险提示**"] + risk_lines

    tag_idx = _section_index(sections, "综合标签")
    if "综合标签" in sections[tag_idx]:
        tags = _rule_tags(state)
        content_by_title[sections[tag_idx]] = [" ".join(f"#{t}" for t in tags) if tags else "暂无"]

    lines = [
        f"# {prompts.mode_report_title(date, mode)}",
        "",
        "> （降级模式：本报告由规则引擎基于数据快照生成，缺失项标注「无数据」，不构成任何预测）",
        "",
    ]
    for s in sections:
        lines += ["", f"## {s}", ""]
        lines.extend(content_by_title.get(s, ["（降级模式：该章节无可用数据，见上方数据快照）"]))
    return "\n".join(lines)


def report_node(state):
    """Node5：LLM 汇总生成报告；失败 → 规则引擎；免责声明组装层强制。"""
    _emit("report", 85, "正在生成复盘报告…")
    report = None
    tags = []
    degraded = False
    if _llm_ready():
        try:
            llm, _ = _llm_pair()
            report = llm.invoke(
                [
                    {"role": "system", "content": prompts.REVIEW_ASSISTANT_SYSTEM_PROMPT},
                    {"role": "user", "content": prompts.build_report_prompt(state)},
                ],
                config={"callbacks": _callbacks()},
            ).content
            tags = _extract_tags(report)
        except Exception:
            report = None
    if report is None:
        degraded = True
        report = _rule_report(state)
        tags = _rule_tags(state)
    report = ensure_disclaimer(report)
    _emit("report", 90, "报告生成完成")
    _emit("done", 100, "复盘完成")
    return {
        "final_report": report,
        "overall_tags": tags or [],
        "degraded": bool(state.get("degraded")) or degraded,
        "degraded_notes": ["LLM 不可用/失败，报告由规则引擎生成"] if degraded else [],
    }


# ═══════════════════════════════════════════════════════════════
# 图构建 / 结果组装
# ═══════════════════════════════════════════════════════════════

def router(state):
    """host 节点后判断：继续辩论还是出报告（≤ max_rounds）。"""
    if state.get("discussion_done", True):
        return "report"
    max_rounds = max(1, min(int(state.get("max_rounds") or 3), 3))
    if (state.get("round_count") or 0) >= max_rounds:
        return "report"
    return "debate"


def default_initial_state(date, mode=DEFAULT_MODE, max_rounds=3,
                          yesterday_report="", earlier_today="", skill_names=None):
    """完整初始状态（I4 上下文注入：yesterday_report / earlier_today，缺失为空串）。"""
    return {
        "date": date,
        "mode": mode,
        "max_rounds": max_rounds,
        "yesterday_report": yesterday_report or "",
        "earlier_today": earlier_today or "",
        "skill_names": list(skill_names or []),
        "market_data": "",
        "raw_news": "",
        "snapshot": {},
        "analyses": [],
        "discussion_topic": "",
        "discussion_done": False,
        "round_count": 0,
        "debate_history": [],
        "final_report": "",
        "overall_tags": [],
        "disclaimer": DISCLAIMER,
        "degraded": False,
        "degraded_notes": [],
        "data_ok": True,
        "data_error": "",
    }


def build_graph(progress_callback=None):
    """构建并编译 LangGraph：fetch → news → trend → sentiment → host ⇄ debate → report。"""
    set_progress_callback(progress_callback)
    _reset_progress()
    graph = StateGraph(ReviewState)

    graph.add_node("fetch_data", fetch_data_node)
    graph.add_node("validate_data", validate_data_node)
    graph.add_node("news_analyst", news_analyst_node)
    graph.add_node("trend_analysts", trend_analysts_node)
    graph.add_node("sentiment_analysts", sentiment_analysts_node)
    graph.add_node("host", host_node)
    graph.add_node("debate", debate_node)
    graph.add_node("report", report_node)

    graph.set_entry_point("fetch_data")
    graph.add_edge("fetch_data", "validate_data")
    graph.add_conditional_edges("validate_data", _data_router, {
        "news_analyst": "news_analyst",
        END: END,
    })
    graph.add_edge("news_analyst", "trend_analysts")
    graph.add_edge("trend_analysts", "sentiment_analysts")
    graph.add_edge("sentiment_analysts", "host")
    graph.add_conditional_edges("host", router, {
        "report": "report",
        "debate": "debate",
    })
    graph.add_edge("debate", "host")
    graph.add_edge("report", END)
    return graph.compile()


def build_result(state) -> dict:
    """§五 result.report 严格结构：{final_report, analysts[], debate_history[],
    overall_tags[], disclaimer, degraded, round_count}；免责声明组装层强制。"""
    return {
        "final_report": ensure_disclaimer(state.get("final_report") or ""),
        "analyses": state.get("analyses") or [],
        "debate_history": state.get("debate_history") or [],
        "overall_tags": state.get("overall_tags") or [],
        "disclaimer": DISCLAIMER,
        "degraded": bool(state.get("degraded")),
        "round_count": state.get("round_count") or 0,
    }
