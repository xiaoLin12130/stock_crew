"""Stock Review Crew 后端 API（FastAPI）：复盘异步任务 + 历史记录 + 分析师聊天 + 静态托管（I5）。

契约（docs/requirements.md §五，冻结）：
- POST /api/reviews {date, mode, max_rounds?} -> {job_id}：日期/模式/窗口校验（中文 400）；
- GET  /api/jobs/{job_id} -> 轮询契约（内部 _ 字段不外泄；终态 TTL >1h 移除、保留最近 50 条，
  进行中不删）；
- GET /api/reviews 历史分组、GET/DELETE /api/reviews/{date}/{time}、GET /api/reviews/context；
- 聊天：POST/GET/DELETE /api/chat/sessions[...]，每条消息响应恒含 disclaimer；
- GET /api/health；frontend/dist 存在时静态托管 "/"。

任务执行（线程 + 进度回调）：
校验 -> I4 context() 注入昨日/当日更早 -> I2 引擎（build_graph(progress_callback=cb) +
default_initial_state + invoke）-> save_review 落盘（meta/report/snapshot 分文件）
-> job done + result={record_id, meta, report, snapshot}。
任何异常 -> status=error + 中文 message，绝不 500 崩线程；落盘失败记录错误不阻塞返回。

并发说明：I2 的进度钩子是模块级全局实现（graph.set_progress_callback），多任务并发会互相覆盖，
因此参照 synalysis_crew 的锁策略，用 _ENGINE_LOCK 串行化引擎调用，保证每个 job 的进度回调只写
自己的 JOBS 条目（已权衡：牺牲并发吞吐换取进度正确性，规则引擎单次执行秒级，可接受）。
"""

from __future__ import annotations

import json
import logging
import math
import queue
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_review_crew import chat  # noqa: E402
from stock_review_crew import graph  # noqa: E402
from stock_review_crew.prompts import DISCLAIMER, MODE_INFO  # noqa: E402
from stock_review_crew.storage import chats as chat_storage  # noqa: E402
from stock_review_crew.storage import reviews  # noqa: E402

logger = logging.getLogger("stock_review_crew.backend")

app = FastAPI(title="Stock Review Crew API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """参数校验错误一律中文 400（FastAPI 默认 422 英文文案不满足契约）。"""
    return JSONResponse(status_code=400, content={"detail": "请求参数格式错误，请检查请求体与字段类型"})


# ═══════════════════════════════════════════════════════════════
# 常量：模式 / 窗口 / JOBS TTL
# ═══════════════════════════════════════════════════════════════

MODES = list(MODE_INFO.keys())
MODE_LABELS = {m: MODE_INFO[m]["label"] for m in MODES}
INTRADAY_MODES = ("auction", "intraday_am", "noon", "intraday_pm")

# 模式窗口（分钟制 [start, end)，与 frontend/src/modes.js WINDOW 严格一致）
MODE_WINDOWS = {
    "pre_market": (0, 9 * 60 + 15),
    "auction": (9 * 60 + 15, 9 * 60 + 25),
    "intraday_am": (9 * 60 + 30, 11 * 60 + 30),
    "noon": (11 * 60 + 30, 13 * 60),
    "intraday_pm": (13 * 60, 15 * 60),
    "close": (15 * 60, 24 * 60),
}

JOB_TTL_SECONDS = 3600  # 终态任务（done/error）超过 1 小时移除
MAX_JOBS = 50  # JOBS 保留的终态任务上限（保留最近 N 条，进行中不删）

_lock = threading.Lock()
JOBS: dict[str, dict[str, Any]] = {}
_ENGINE_LOCK = threading.Lock()  # 串行化 I2 全局进度钩子的引擎调用（见模块 docstring）


def _now() -> datetime:
    """当前时间（测试可 monkeypatch 固定）。"""
    return datetime.now()


def _nearest_mode(minutes: int) -> str:
    """当前时间点最合适的模式（与前端 currentMode 规则一致）。"""
    if minutes < 9 * 60 + 15:
        return "pre_market"
    if minutes < 9 * 60 + 30:
        return "auction"
    if minutes < 11 * 60 + 30:
        return "intraday_am"
    if minutes < 13 * 60:
        return "noon"
    if minutes < 15 * 60:
        return "intraday_pm"
    return "close"


def _window_error(date_str: str, mode: str, now: Optional[datetime] = None) -> Optional[str]:
    """模式窗口判定：返回中文错误提示；None = 放行（历史日期补做任意模式）。"""
    now = now or _now()
    if date_str != now.strftime("%Y-%m-%d"):
        return None  # 历史日期补做放行
    minutes = now.hour * 60 + now.minute
    if now.weekday() >= 5 and mode in INTRADAY_MODES:
        return (
            "今日为非交易日（周末），盘中数据源无当日数据，复盘将按降级链标注缺失；"
            "建议切换「收盘复盘」或「早盘前决策」，或选择历史日期补做"
        )
    start, end = MODE_WINDOWS[mode]
    if start <= minutes < end:
        return None
    label = MODE_LABELS[mode]
    # 竞价刚结束（09:25-09:30 缓冲段）与已结束（09:30 后）给定向建议（requirements §二.1 示例）
    if mode == "auction" and minutes >= 9 * 60 + 30:
        return "竞价数据已结束，建议切换上午盘中或午间复盘"
    if mode == "auction" and minutes >= 9 * 60 + 25:
        return "当前处于 09:25–09:30 竞价数据刚结束的缓冲段，建议切换「竞价复盘」或「上午盘中」"
    nearest = _nearest_mode(minutes)
    return f"当前时间不在「{label}」窗口内（{MODE_INFO[mode]['window']}），建议切换为「{MODE_LABELS[nearest]}」"


def _validate_date(value: Any) -> str:
    """校验 YYYY-MM-DD 且为真实日历日期；非法抛 400 中文。"""
    s = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise HTTPException(status_code=400, detail=f"日期格式必须为 YYYY-MM-DD，收到：{s or '（空）'}")
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"不是真实存在的日期：{s}")
    return s


# ═══════════════════════════════════════════════════════════════
# JOBS：TTL / 更新 / 进度回调
# ═══════════════════════════════════════════════════════════════

def _update(job_id: str, **kwargs: Any) -> None:
    with _lock:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def _prune_jobs(now: Optional[float] = None) -> None:
    """JOBS TTL 清理：终态超过 JOB_TTL_SECONDS 移除；超过 MAX_JOBS 淘汰最旧终态；进行中不删。"""
    if now is None:
        now = time.monotonic()
    with _lock:
        stale = [
            jid
            for jid, job in JOBS.items()
            if job.get("status") in ("done", "error")
            and job.get("_finished") is not None
            and now - job["_finished"] > JOB_TTL_SECONDS
        ]
        for jid in stale:
            JOBS.pop(jid, None)
        terminal = sorted(
            (
                (jid, job.get("_finished", 0.0))
                for jid, job in JOBS.items()
                if job.get("status") in ("done", "error")
            ),
            key=lambda item: item[1],
        )
        for jid, _ in terminal[: max(0, len(terminal) - MAX_JOBS)]:
            JOBS.pop(jid, None)


def _progress_cb(job_id: str):
    """构造引擎进度回调：trend n/2、sentiment n/3 解析为累计 analysts_done/total。"""

    def progress(stage: str, pct: float, message: str) -> None:
        updates: dict[str, Any] = {"stage": stage, "pct": pct, "message": message}
        mt = re.search(r"(\d+)/(\d+)", str(message or ""))
        if mt:
            done, total = int(mt.group(1)), int(mt.group(2))
            if stage == "sentiment":
                done += 2  # 趋势派 2 人已完成，情绪派消息为组内进度 → 累计
                total += 2
            updates["analysts_done"] = done
            updates["analysts_total"] = total
        _update(job_id, **updates)

    return progress


# ═══════════════════════════════════════════════════════════════
# 快照对齐：引擎快照（I2/I1 blocks）→ 前端契约快照（normalize.js/mock.js 唯一真源）
# ═══════════════════════════════════════════════════════════════

_INDEX_NAMES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000688": "科创50",
    "sh000300": "沪深300",
}


def _find_block(data: Any, names: tuple[str, ...]) -> Any:
    if not isinstance(data, dict):
        return None
    for key in names:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pick_int(*values: Any) -> Optional[int]:
    for v in values:
        n = _as_int(v)
        if n is not None:
            return n
    return None


def _ratio(value: Any, max_abs: float = 1.5) -> Optional[float]:
    """比率归一：非有限数 → None；疑似百分数（>max_abs）÷100 为小数。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    if abs(v) > max_abs:
        return v / 100.0
    return v


def _pick_ratio(src: Any, keys: tuple[str, ...], max_abs: float = 1.5) -> Optional[float]:
    if not isinstance(src, dict):
        return None
    for key in keys:
        if src.get(key) is not None:
            value = src[key]
            if str(key).endswith("_pct"):  # 上游百分数字段（如 red_rate_pct）→ 小数
                try:
                    return float(value) / 100.0
                except (TypeError, ValueError):
                    return None
            return _ratio(value, max_abs=max_abs)
    return None


def _block_degraded(blk: Any) -> list[str]:
    if not isinstance(blk, dict):
        return []
    return [str(r) for r in (blk.get("degraded_reason") or []) if r]


def _snap_index_minute(data: Any) -> list[dict]:
    """指数分时：优先 I1 直接给出的 index_minute；否则从 minute 块 bars 组装。"""
    if isinstance(data, dict) and isinstance(data.get("index_minute"), list):
        return data["index_minute"]
    minute = _find_block(data, ("minute", "fenshi", "intraday", "minline", "index_minute_block"))
    if not isinstance(minute, dict):
        return []
    points = []
    for bar in minute.get("bars") or []:
        if not isinstance(bar, dict):
            continue
        t = bar.get("time") or bar.get("datetime")
        v = bar.get("close")
        if v is None:
            v = bar.get("value")
        if t is None or v is None:
            continue
        points.append({"time": str(t), "value": v})
    if not points:
        return []
    symbol = str(minute.get("symbol") or "")
    name = str(minute.get("name") or "") or _INDEX_NAMES.get(symbol, symbol or "指数")
    return [{"name": name, "points": points}]


def _ladder_label(key: Any) -> Optional[str]:
    k = str(key).strip()
    if k in ("首板", "1板", "1"):
        return "首板"
    if k in ("5板+", "5+", "≥5", "5板以上"):
        return "5板+"
    m = re.fullmatch(r"(\d+)板?", k)
    if m:
        n = int(m.group(1))
        return "5板+" if n >= 5 else f"{n}板"
    return None


def _ladder_stocks(sub: Any) -> list[str]:
    names = []
    for item in (sub.get("stocks") if isinstance(sub, dict) else None) or []:
        if isinstance(item, dict):
            name = item.get("名称") or item.get("name") or item.get("股票名称") or item.get("代码")
            if name is None:
                continue
            names.append(str(name))
        elif item:
            names.append(str(item))
    return names[:5]


def _ladder_rows(block: Any, direction: str) -> list[dict]:
    """涨跌停梯队：{label, count, stocks}，对齐 mock limit_ladder.up/down。"""
    if not isinstance(block, dict):
        return []
    sub = block.get("limit_up") if direction == "up" else block.get("limit_down")
    if not isinstance(sub, dict):
        sub = block  # 扁平结构兜底
    if not isinstance(sub, dict):
        return []
    tier = sub.get("tier") if isinstance(sub.get("tier"), dict) else {}
    total = _as_int(sub.get("count"))
    fallback_label = "涨停" if direction == "up" else "跌停"
    rows: list[dict] = []
    if tier:
        agg: dict[str, int] = {}
        for key, value in tier.items():
            label = _ladder_label(key)
            n = _as_int(value)
            if label and n is not None:
                agg[label] = agg.get(label, 0) + n
        for label in ("首板", "2板", "3板", "4板", "5板+"):
            if label in agg:
                rows.append(
                    {
                        "label": label,
                        "count": agg[label],
                        "stocks": _ladder_stocks(sub) if label == "首板" else [],
                    }
                )
    if not rows and total is not None:
        rows.append({"label": fallback_label, "count": total, "stocks": _ladder_stocks(sub)})
    return rows


def _snap_ladder(data: Any) -> dict:
    zt = _find_block(data, ("zhangting", "zt", "zt_pool", "limit_up"))
    dt = _find_block(data, ("dieting", "dt", "dt_pool", "limit_down"))
    # I1 的 zt_pool 块同时含 limit_up/limit_down；dieting 块缺失时从 zt_pool 兜底
    return {"up": _ladder_rows(zt, "up"), "down": _ladder_rows(dt if dt is not None else zt, "down")}


def _snap_sectors(data: Any) -> list[dict]:
    sec = _find_block(data, ("sectors", "sector", "board", "industry"))
    if not isinstance(sec, dict):
        return []
    items: list[Any] = []
    for key in ("top5", "top10", "list", "items"):
        if isinstance(sec.get(key), list):
            items = sec[key]
            break
    rows = []
    for item in items[:10]:
        if not isinstance(item, dict):
            continue
        name = item.get("板块") or item.get("name") or item.get("板块名称") or item.get("行业")
        if not name:
            continue
        pct = item.get("涨跌幅")
        if pct is None:
            pct = item.get("pct_change")
        leader = item.get("领涨股") or item.get("leader") or item.get("领涨") or ""
        rows.append({"name": str(name), "pct_change": _ratio(pct), "leader": str(leader)})
    rows.sort(key=lambda r: r["pct_change"] if r["pct_change"] is not None else -999.0, reverse=True)
    return rows


def _snap_sentiment(data: Any) -> dict:
    sent = _find_block(data, ("sentiment", "emotion", "yesterday_zt"))
    zt = _find_block(data, ("zhangting", "zt", "zt_pool", "limit_up"))
    breadth = _find_block(data, ("breadth", "market_breadth", "up_down"))
    src = sent if isinstance(sent, dict) else {}

    zt_up = zt.get("limit_up") if isinstance(zt, dict) and isinstance(zt.get("limit_up"), dict) else None
    zt_down = zt.get("limit_down") if isinstance(zt, dict) and isinstance(zt.get("limit_down"), dict) else None
    limit_up_count = _pick_int(
        src.get("limit_up_count"),
        zt_up.get("count") if zt_up else None,
        breadth.get("limit_up") if isinstance(breadth, dict) else None,
    )
    limit_down_count = _pick_int(
        src.get("limit_down_count"),
        zt_down.get("count") if zt_down else None,
        breadth.get("limit_down") if isinstance(breadth, dict) else None,
    )
    break_rate = _pick_ratio(src, ("zhaban_rate", "zhaban_rate_pct"), 1.5)
    if break_rate is None:
        break_rate = _pick_ratio(zt, ("zhaban_rate", "zhaban_rate_pct"), 1.5)
    if break_rate is None:
        break_rate = _pick_ratio(breadth, ("zhaban_rate", "zhaban_rate_pct"), 1.5)

    degraded: list[str] = []
    for blk in (src, zt, breadth):
        for reason in _block_degraded(blk):
            if reason and reason not in degraded:
                degraded.append(reason)

    up_down_ratio = None
    if src.get("up_down_ratio") is not None:
        up_down_ratio = _ratio(src.get("up_down_ratio"), max_abs=999.0)

    return {
        "up_count": _pick_int(src.get("up_count")),
        "down_count": _pick_int(src.get("down_count")),
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
        "red_rate": _pick_ratio(src, ("red_rate", "red_rate_pct"), 1.5),
        "continue_rate": _pick_ratio(src, ("lianban_rate", "lianban_rate_pct", "continue_rate"), 1.5),
        "break_rate": break_rate,
        "button_rate": _pick_ratio(src, ("hean_rate", "hean_rate_pct", "button_rate"), 1.5),
        "avg_return": _pick_ratio(src, ("avg_return", "avg_return_pct"), 1.0),
        "up_down_ratio": up_down_ratio,
        "source": str(
            src.get("source")
            or (zt.get("source") if isinstance(zt, dict) else None)
            or (breadth.get("source") if isinstance(breadth, dict) else None)
            or "数据缺失"
        ),
        "degraded": degraded,
    }


def _to_frontend_snapshot(engine_snapshot: Any) -> dict:
    """引擎快照（{date, mode, source, data: blocks, degraded}）→ 前端契约快照。

    对齐 frontend/src/mock.js buildSnapshot：顶层仅 index_minute / limit_ladder / sectors /
    sentiment / source / degraded 六个键；比率一律小数；None 语义保留。
    """
    raw = engine_snapshot if isinstance(engine_snapshot, dict) else {}
    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    sentiment = _snap_sentiment(data)
    degraded = [str(d) for d in (raw.get("degraded") or []) if d]
    for reason in sentiment.get("degraded") or []:
        if reason and reason not in degraded:
            degraded.append(reason)
    return {
        "index_minute": _snap_index_minute(data),
        "limit_ladder": _snap_ladder(data),
        "sectors": _snap_sectors(data),
        "sentiment": sentiment,
        "source": str(raw.get("source") or sentiment.get("source") or "数据缺失"),
        "degraded": degraded,
    }


def _snapshot_sources(engine_snapshot: Any) -> list[str]:
    """meta.sources[]：数据块 source 去重（含引擎整体 source）。"""
    raw = engine_snapshot if isinstance(engine_snapshot, dict) else {}
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    sources: list[str] = []
    if isinstance(data, dict):
        for blk in data.values():
            if isinstance(blk, dict) and blk.get("source"):
                s = str(blk["source"])
                if s and s not in sources:
                    sources.append(s)
    if raw.get("source"):
        s = str(raw["source"])
        if s and s not in sources:
            sources.append(s)
    return sources


def _make_summary(report: str, mode: str, date: str) -> str:
    """摘要：取报告正文第一个非标题/非引用段落；兜底模板。"""
    for line in (report or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(">"):
            continue
        s = s.lstrip("-*• ").strip()
        if s and "不构成投资建议" not in s:
            return s[:100]
    return f"{MODE_LABELS.get(mode, mode)}（{date}）：复盘完成，详见报告正文"


# ═══════════════════════════════════════════════════════════════
# 复盘任务
# ═══════════════════════════════════════════════════════════════

def _context_item_text(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    parts = []
    if item.get("mode_label"):
        parts.append(str(item["mode_label"]))
    if item.get("time"):
        parts.append(f"时间点 {item['time']}")
    if item.get("record_id"):
        parts.append(f"记录 {item['record_id']}")
    if item.get("summary"):
        parts.append(f"摘要：{item['summary']}")
    return "；".join(parts)


def _run_review_job(job_id: str, date: str, mode: str, max_rounds: int) -> None:
    """后台任务线程（daemon）：上下文注入 → 引擎 → 落盘 → done；任何异常 → error 中文。"""
    try:
        # 初始阶段用契约阶段序列首项 fetch（§五：取数/资讯/趋势派/情绪派/主持人/辩论/报告/完成）
        _update(job_id, status="running", stage="fetch", pct=1, message="正在注入历史上下文并拉取行情…")
        ctx = reviews.context(date, mode)
        initial = graph.default_initial_state(
            date,
            mode=mode,
            max_rounds=max_rounds,
            yesterday_report=_context_item_text(ctx.get("yesterday")),
            earlier_today="\n".join(
                _context_item_text(item) for item in (ctx.get("earlier_today") or [])
            ),
        )
        with _ENGINE_LOCK:
            compiled = graph.build_graph(progress_callback=_progress_cb(job_id))
            state = compiled.invoke(initial)
        report_dict = graph.build_result(state)

        now = datetime.now()
        meta = {
            "date": date,
            "mode": mode,
            "mode_label": MODE_LABELS.get(mode, mode),
            "time": now.strftime("%H%M%S"),
            "created_at": now.isoformat(timespec="seconds"),
            "degraded": list(state.get("degraded_notes") or []),
            "sources": _snapshot_sources(state.get("snapshot")),
            "disclaimer": DISCLAIMER,
            "summary": _make_summary(report_dict.get("final_report", ""), mode, date),
        }
        snapshot = _to_frontend_snapshot(state.get("snapshot") or {})
        record_id: Optional[str] = None
        try:
            record_id = reviews.save_review(meta, report_dict, snapshot)
            if record_id:
                meta["time"] = record_id.split("_", 1)[1]  # 实际落盘时间点（可能带 -N 后缀）
        except Exception as exc:  # 磁盘写失败：记录错误，不阻塞任务完成
            logger.warning("复盘落盘失败（不阻塞返回）：%s", exc)
            meta.setdefault("degraded", []).append(f"复盘记录落盘失败：{exc}")
        _update(
            job_id,
            status="done",
            pct=100,
            message="复盘完成",
            result={
                "record_id": record_id,
                "meta": meta,
                "report": report_dict.get("final_report", ""),
                "snapshot": snapshot,
            },
            _finished=time.monotonic(),
        )
    except Exception as exc:  # noqa: BLE001 - 任何异常都转为中文 error，绝不 500 崩线程
        logger.exception("复盘任务异常：job=%s", job_id)
        text = f"复盘失败：{exc}"
        _update(job_id, status="error", message=text, error=text, _finished=time.monotonic())


# ═══════════════════════════════════════════════════════════════
# 路由：健康检查 / 复盘 / 任务轮询 / 聊天 / 静态托管
# ═══════════════════════════════════════════════════════════════

@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/reviews")
def create_review(payload: Any = Body(...)) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    date = _validate_date(payload.get("date"))
    mode = str(payload.get("mode") or "").strip()
    if not mode:
        raise HTTPException(status_code=400, detail="缺少参数：mode")
    if mode not in MODES:
        raise HTTPException(status_code=400, detail=f"无效模式：{mode}（可选：{'、'.join(MODES)}）")
    max_rounds = 3
    if payload.get("max_rounds") is not None:
        try:
            max_rounds = int(payload["max_rounds"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="max_rounds 必须为整数（1-3）")
        max_rounds = max(1, min(max_rounds, 3))
    error = _window_error(date, mode)
    if error:
        raise HTTPException(status_code=400, detail=error)

    _prune_jobs()
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "pct": 0,
            "message": "等待开始",
            "analysts_done": 0,
            "analysts_total": 5,
            "result": None,
            "error": None,
            "_created": time.monotonic(),
            "_finished": None,
        }
    try:
        threading.Thread(
            target=_run_review_job,
            args=(job_id, date, mode, max_rounds),
            daemon=True,
        ).start()
    except Exception as exc:  # noqa: BLE001
        with _lock:
            JOBS.pop(job_id, None)
        raise HTTPException(status_code=500, detail=f"任务启动失败：{exc}") from exc
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    _prune_jobs()
    with _lock:
        job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {key: value for key, value in job.items() if not key.startswith("_")}


@app.get("/api/reviews")
def review_list() -> list[dict[str, Any]]:
    return reviews.list_reviews()


@app.get("/api/reviews/context")
def review_context(date: str = "", mode: str = "") -> dict[str, Any]:
    if not date:
        raise HTTPException(status_code=400, detail="缺少参数：date")
    date = _validate_date(date)
    if mode and mode not in MODES:
        raise HTTPException(status_code=400, detail=f"无效模式：{mode}（可选：{'、'.join(MODES)}）")
    return reviews.context(date, mode or None)


@app.get("/api/reviews/{date}/{time}")
def review_detail(date: str, time: str) -> dict[str, Any]:
    detail = reviews.get_review(date, time)
    if not detail.get("meta"):
        raise HTTPException(status_code=404, detail="复盘记录不存在")
    return detail


@app.delete("/api/reviews/{date}/{time}", status_code=204)
def review_delete(date: str, time: str) -> Response:
    if not reviews.delete_review(date, time):
        raise HTTPException(status_code=404, detail="复盘记录不存在")
    return Response(status_code=204)


# ── 聊天 ──

def _chat_engine():
    return chat._get_default_engine()


@app.post("/api/chat/sessions")
def create_chat_session(payload: Any = Body(...)) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    target_type = str(payload.get("target_type") or "").strip()
    target = str(payload.get("target") or "").strip()
    analysts = payload.get("analysts")
    if not isinstance(analysts, list):
        raise HTTPException(status_code=400, detail="analysts 必须为分析师 ID 列表")
    try:
        return _chat_engine().create_session(
            target_type,
            target,
            [str(a) for a in analysts],
            title=payload.get("title"),
        )
    except ValueError as exc:  # 参数/白名单错误 → 400 中文
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("创建聊天会话失败")
        raise HTTPException(status_code=500, detail=f"创建会话失败：{exc}") from exc


@app.get("/api/chat/sessions")
def chat_session_list(target: str = "", date: str = "") -> list[dict[str, Any]]:
    try:
        metas = _chat_engine().list_sessions(target=target or None, date=date or None)
    except ValueError as exc:  # 非法日期 → 400 中文
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result: list[dict[str, Any]] = []
    for meta in metas:
        session = chat_storage.get_session(meta["session_id"])
        messages = (session or {}).get("messages") or []
        result.append(
            {
                "session_id": meta.get("session_id"),
                "target_type": meta.get("target_type"),
                "target": meta.get("target"),
                "target_name": meta.get("target_name") or meta.get("target"),
                "analysts": meta.get("analysts") or [],
                "title": meta.get("title"),
                "created_at": meta.get("created_at"),
                "date": meta.get("date"),
                "last_message": messages[-1].get("content") if messages else "",
                "message_count": len(messages),
            }
        )
    return result


@app.post("/api/chat/sessions/{session_id}/messages")
def send_chat_message(session_id: str, payload: Any = Body(...)) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    try:
        result = _chat_engine().send_message(session_id, payload.get("content"))
    except ValueError as exc:  # 空消息等参数错误 → 400 中文
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return result


@app.post("/api/chat/sessions/{session_id}/messages/stream")
def stream_chat_message(session_id: str, payload: Any = Body(...)) -> StreamingResponse:
    """SSE 流式聊天：POST 后持续返回 event-stream，事件见 ChatEngine.stream_message。

    长任务在 daemon 线程执行，事件经队列转发；前端逐 token 渲染（Codex 式对话体验）。
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    content = str(payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="消息内容不能为空")
    engine = _chat_engine()
    if engine.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    events: "queue.Queue" = queue.Queue()

    def worker() -> None:
        try:
            engine.stream_message(session_id, content, emit=lambda ev, pl: events.put((ev, pl)))
        except Exception as exc:  # noqa: BLE001 - SSE 内兜底，避免断流
            try:
                events.put(("error", {"message": f"聊天失败：{exc}"}))
            except Exception:
                pass
        finally:
            events.put(("__end__", None))

    threading.Thread(target=worker, daemon=True).start()

    def gen():
        while True:
            ev, pl = events.get()
            if ev == "__end__":
                break
            yield f"event: {ev}\ndata: {json.dumps(pl, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/chat/sessions/{session_id}")
def chat_session_detail(session_id: str) -> dict[str, Any]:
    session = _chat_engine().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@app.delete("/api/chat/sessions/{session_id}", status_code=204)
def chat_session_delete(session_id: str) -> Response:
    if not _chat_engine().delete_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return Response(status_code=204)


# ── 静态托管（必须在 API 路由之后挂载）──

DIST = ROOT / "frontend" / "dist"
if DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(DIST), html=True), name="frontend")
