"""A 股复盘系统数据层（I1）：6 模式化取数节点 + 超时/降级链。

设计要点（契约 docs/requirements.md §二/§四/§六）：
  - 6 模式取数节点：pre_market / auction / intraday_am / noon / intraday_pm / close；
  - 每源超时 ≤30s（SOURCE_TIMEOUT_SECONDS）；主源→备用源→本地缓存→「数据缺失/估算」；
  - 每个数据块携带 source / degraded / degraded_reason；
  - 比率一律小数进契约（上游 % 值 /100）；快照 raw 保留原值并标注单位；
  - None = 无数据（禁止 0 占位）；非有限数输出 None；
  - 所有网络调用可注入/可 monkeypatch（模块级适配函数），测试不依赖网络。

对外提供两类接口：
  - 纯函数（返回 dict）：fetch_*，供测试与 FastAPI 复用；
  - LangChain @tool 包装（返回 JSON 字符串）：get_*，供 graph.py 调用。
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import date as date_type
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd
from langchain_core.tools import tool

from .. import config
from ..knowledge_store import search as kb_search
from . import tdx_local


# ═══════════════════════════════════════════════════════════════
# 东财 HTTPS→HTTP 改写（本机网络对东财 TLS 做 SNI 层 Reset，HTTP 实测可用；
# 统一在 requests 层改写，覆盖 akshare 内部请求与本模块 _http_get_json）
# ═══════════════════════════════════════════════════════════════
def _install_eastmoney_http_rewrite() -> None:
    try:
        import requests
        import requests.sessions

        if getattr(requests.sessions, "_EM_HTTP_REWRITE", False):
            return

        _orig_request = requests.sessions.Session.request

        def _request(self, method, url, *args, **kwargs):
            if isinstance(url, str) and url.startswith("https://") and "eastmoney.com" in url:
                url = "http://" + url[len("https://"):]
            return _orig_request(self, method, url, *args, **kwargs)

        requests.sessions.Session.request = _request
        requests.sessions._EM_HTTP_REWRITE = True
    except Exception:  # pragma: no cover - 环境无 requests 时不影响
        pass


_install_eastmoney_http_rewrite()


# ═══════════════════════════════════════════════════════════════
# 常量与基础工具
# ═══════════════════════════════════════════════════════════════

SOURCE_TIMEOUT_SECONDS = 30          # 每源超时 ≤30s（契约 §六.9）
_AUCTION_STOCK_TIMEOUT = 5           # 竞价推算单个股分时超时（秒）
_AUCTION_TOTAL_BUDGET = 25           # 东财竞价推算总预算（秒），整体 ≤30s
_SECTOR_HISTORY_BUDGET = 60          # 历史板块逐行业总预算（秒）
_SENTIMENT_TDX_BUDGET = 60           # 通达信本地情绪计算总预算（秒）
_SENTIMENT_EM_STOCK_TIMEOUT = 5      # 东财情绪单个股日线超时（秒）
_SENTIMENT_EM_MAX_STOCKS = 20        # 东财情绪推算最大个股数

MODE_LABELS = {
    "pre_market": "早盘前决策",
    "auction": "竞价复盘",
    "intraday_am": "上午盘中",
    "noon": "午间复盘",
    "intraday_pm": "下午盘中",
    "close": "收盘复盘",
}

INDEX_CODES = {
    "shanghai": "sh000001",
    "shenzhen": "sz399001",
    "chuangye": "sz399006",
    "kechuang": "sh000688",
}
INDEX_NAMES = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指", "sh000688": "科创50"}
MA_PERIODS = [5, 10, 13, 20, 34, 60, 144, 250]

_DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[3] / "data_cache"
_CACHE_SCHEMA_VERSION = 2


class DataSourceError(Exception):
    """数据源失败（中文信息），由降级链捕获。"""


def _now() -> datetime:
    return datetime.now()


def _d(date: str) -> str:
    """校验 YYYY-MM-DD 并返回 YYYYMMDD；非法输入抛中文异常。"""
    try:
        return datetime.strptime(str(date), "%Y-%m-%d").strftime("%Y%m%d")
    except (TypeError, ValueError) as exc:
        raise DataSourceError(f"无效日期：{date}（应为 YYYY-MM-DD）") from exc


def _dash(yyyymmdd: str) -> str:
    s = str(yyyymmdd)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def _to_finite(value: Any) -> Optional[float]:
    """非有限数 → None（契约：None = 无数据）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _clean_val(value: Any) -> Any:
    """清洗单个值：NaN/Inf → None，尽量转原生类型。"""
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float):
        f = _to_finite(value)
        return None if f is None else f
    if isinstance(value, (int, str, bool)):
        return value
    if isinstance(value, (date_type, datetime)):
        return str(value)
    return value


def _clean_row(row: dict) -> dict:
    return {k: _clean_val(v) for k, v in row.items()}


def _dumps(obj: Any) -> str:
    """JSON 序列化：NaN/Inf → null，date/datetime → 字符串。"""
    def _convert(o):
        if isinstance(o, (datetime, date_type)):
            return str(o)
        if isinstance(o, float) and not math.isfinite(o):
            return None
        raise TypeError(f"对象 {type(o).__name__} 不可序列化")
    return json.dumps(obj, ensure_ascii=False, indent=2, default=_convert)


def _block(data: Any, source: str, degraded: bool = False,
           reasons: Optional[list[str]] = None, note: Optional[str] = None) -> dict:
    return {
        "data": data,
        "source": source,
        "degraded": degraded,
        "degraded_reason": reasons or (["数据缺失"] if degraded else []),
        "note": note,
    }


def _call_with_timeout(fn: Callable, *args, timeout: Optional[float] = None, **kwargs) -> Any:
    """超时包装：每源调用不超过 timeout（默认 SOURCE_TIMEOUT_SECONDS）。

    使用 daemon 线程实现：超时后线程在后台自行结束，不阻塞进程退出。
    """
    timeout = SOURCE_TIMEOUT_SECONDS if timeout is None else float(timeout)
    result: dict = {}

    def _runner() -> None:
        try:
            result["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - 跨线程传递原始异常
            result["error"] = exc

    worker = threading.Thread(target=_runner, name="data-source-timeout", daemon=True)
    worker.start()
    worker.join(timeout=timeout)
    if worker.is_alive():
        raise DataSourceError(f"数据源调用超时（超过{int(timeout)}秒）")
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _is_bj(code: str) -> bool:
    code = str(code)
    return code.startswith(("8", "4")) or code.startswith("92")


def _is_st(name: Any) -> bool:
    return bool(name) and "ST" in str(name).upper()


def _limit_ratio(code: str) -> float:
    """涨停幅度：主板 10%、创业板/科创板 20%（契约 §六.5）；ST/北证已在调用侧过滤。"""
    return 0.20 if str(code).startswith(("30", "68")) else 0.10


def _limit_price(pre_close: float, code: str) -> float:
    return round(pre_close * (1 + _limit_ratio(code)), 2)


def _filter_st_bj(df: pd.DataFrame, name_map: Optional[dict] = None) -> pd.DataFrame:
    """过滤 ST 与北证（按名称含 ST / 代码前缀）。"""
    if df is None or df.empty:
        return df
    out = df.copy()
    codes = out["ts_code"].astype(str) if "ts_code" in out.columns else None
    if codes is None and "代码" in out.columns:
        codes = out["代码"].astype(str)
    if codes is None:
        return out
    mask = ~codes.map(_is_bj)
    if name_map and "ts_code" in out.columns:
        mask &= out["ts_code"].map(lambda c: not _is_st(name_map.get(str(c))))
    elif "名称" in out.columns:
        mask &= ~out["名称"].map(_is_st)
    return out[mask]


def _find_prev_trading_day(date: str, days_back: int = 10) -> str:
    """向前找最近的交易日（由 _ts_daily 是否返回数据判定；仅用于需要昨收的场景）。"""
    d = datetime.strptime(date, "%Y-%m-%d")
    for _ in range(days_back):
        d = d - timedelta(days=1)
        df = _ts_daily(d.strftime("%Y%m%d"))
        if df is not None and not df.empty:
            return d.strftime("%Y-%m-%d")
    raise DataSourceError(f"未找到 {date} 之前的交易日（向前 {days_back} 天无数据）")


# ═══════════════════════════════════════════════════════════════
# HTTP 与数据源适配层（可注入 / 可 monkeypatch）
# ═══════════════════════════════════════════════════════════════

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 开盘啦竞价接口（需 KAIPANLA_COOKIE；端点如与官网调整不符，可在测试中注入）
KAIPANLA_API_URL = "https://www.kaipanla.com/quoteboard/api/auction"
# 同花顺竞价接口（公开页面数据接口，端点可能随官网调整）
THS_AUCTION_URL = "https://data.10jqka.com.cn/dataapi/rank/auction/auction_rank"


def _http_get_json(url: str, params: Optional[dict] = None,
                   headers: Optional[dict] = None, timeout: float = 15) -> Any:
    """HTTP GET → JSON。所有外部请求统一走这里，便于注入与超时控制。

    东财多子域轮换重试：本机网络对 eastmoney 连接不稳定（偶发 502/Reset），
    失败时依次尝试 80.push2 / 17.push2 / push2 子域，任一成功即返回。
    """
    import requests
    last_exc: Optional[Exception] = None
    attempts = [url]
    if "eastmoney.com" in url:
        base_variants = [
            url.replace("http://push2.eastmoney.com", "http://80.push2.eastmoney.com"),
            url.replace("http://push2.eastmoney.com", "http://17.push2.eastmoney.com"),
        ]
        attempts = [url] + [v for v in base_variants if v != url]
    for candidate in attempts:
        try:
            resp = requests.get(candidate, params=params, headers=headers or {}, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - 轮换重试，最后一次异常上抛
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def _tx_index_daily(symbol: str) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_zh_index_daily_tx(symbol=symbol)


def _em_zt_pool(date: str) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_zt_pool_em(date=date)


def _em_zt_pool_zbgc(date: str) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_zt_pool_zbgc_em(date=date)


def _em_zt_pool_dtgc(date: str) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_zt_pool_dtgc_em(date=date)


def _em_zt_pool_previous(date: str) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_zt_pool_previous_em(date=date)


def _em_spot() -> pd.DataFrame:
    import akshare as ak
    return ak.stock_zh_a_spot_em()


def _em_pre_min(symbol: str) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_zh_a_hist_pre_min_em(symbol=symbol, start_time="09:00:00", end_time="09:30:00")


def _em_stock_minute(symbol: str, period: str, start: str, end: str) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_zh_a_hist_min_em(symbol=symbol, period=period, adjust="",
                                     start_date=start, end_date=end)


def _em_index_minute(symbol: str, period: str, start: str, end: str) -> pd.DataFrame:
    import akshare as ak
    return ak.index_zh_a_hist_min_em(symbol=symbol, period=period, start_date=start, end_date=end)


def _em_stock_daily(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start_date,
                              end_date=end_date, adjust="")


def _ths_sector_summary() -> pd.DataFrame:
    import akshare as ak
    return ak.stock_board_industry_summary_ths()


def _ths_sector_names() -> pd.DataFrame:
    import akshare as ak
    return ak.stock_board_industry_name_ths()


def _ths_sector_hist(name: str, start: str, end: str) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_board_industry_index_ths(symbol=name, start_date=start, end_date=end)


def _em_sector_names() -> pd.DataFrame:
    import akshare as ak
    return ak.stock_board_industry_name_em()


def _em_sector_hist(name: str, start: str, end: str) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_board_industry_hist_em(symbol=name, start_date=start, end_date=end,
                                           period="日k", adjust="")


def _legu_activity() -> pd.DataFrame:
    import akshare as ak
    return ak.stock_market_activity_legu()


def _em_lhb(start_date: str, end_date: str) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_lhb_detail_em(start_date=start_date, end_date=end_date)


def _us_index_sina(symbol: str) -> pd.DataFrame:
    import akshare as ak
    return ak.index_us_stock_sina(symbol=symbol)


def _foreign_realtime(symbols: str) -> pd.DataFrame:
    import akshare as ak
    return ak.futures_foreign_commodity_realtime(symbol=symbols)


def _futures_main_contract(symbol: str) -> pd.DataFrame:
    import akshare as ak
    return ak.futures_main_contract(symbol=symbol)


def _news_cctv(date: str) -> pd.DataFrame:
    import akshare as ak
    return ak.news_cctv(date=date)


def _news_economic_baidu(date: str) -> pd.DataFrame:
    import akshare as ak
    return ak.news_economic_baidu(date=date)


def _news_caixin() -> pd.DataFrame:
    import akshare as ak
    return ak.stock_news_main_cx()


# ── Tushare（token 缺失/未安装 → DataSourceError，由降级链处理）──

def _ts_pro() -> Any:
    if not config.TUSHARE_TOKEN:
        raise DataSourceError("未配置 TUSHARE_TOKEN，Tushare 数据源不可用（走降级链）")
    try:
        import tushare as ts
    except ImportError as exc:
        raise DataSourceError("未安装 tushare 库，Tushare 数据源不可用（走降级链）") from exc
    # 直接传 token 构造客户端：set_token 会写用户目录 tk.csv，
    # 在只读/沙箱环境下会失败；pro_api(token) 不落盘。
    return ts.pro_api(config.TUSHARE_TOKEN)


def _ts_daily(trade_date: str) -> pd.DataFrame:
    return _ts_pro().daily(trade_date=trade_date)


def _ts_top_list(trade_date: str) -> pd.DataFrame:
    return _ts_pro().top_list(trade_date=trade_date)


def _ts_stock_basic() -> pd.DataFrame:
    return _ts_pro().stock_basic(exchange="", list_status="L",
                                 fields="ts_code,name,list_date")


# ═══════════════════════════════════════════════════════════════
# 本地缓存（降级链第三环；只读项目 data_cache/，写缓存需显式开启）
# ═══════════════════════════════════════════════════════════════

def get_cache_dir() -> Path:
    """缓存目录：默认项目 data_cache/（只读降级链）；可用 STOCK_DATA_CACHE_DIR 覆盖。"""
    override = os.getenv("STOCK_DATA_CACHE_DIR")
    return Path(override) if override else _DEFAULT_CACHE_DIR


def _cache_write_enabled() -> bool:
    """默认不写生产缓存（遵守本次写权限约束）；显式 STOCK_DATA_CACHE_ENABLED=1 开启。"""
    return os.getenv("STOCK_DATA_CACHE_ENABLED", "0") == "1"


def _cache_save(date: str, block_name: str, payload: dict) -> None:
    if not _cache_write_enabled():
        return
    try:
        d = get_cache_dir() / date
        d.mkdir(parents=True, exist_ok=True)
        (d / f"block_{block_name}.json").write_text(
            _dumps({"schema_version": _CACHE_SCHEMA_VERSION, "date": date,
                    "block": block_name, "saved_at": _now().isoformat(timespec="seconds"),
                    "data": payload}), encoding="utf-8")
    except Exception:
        pass  # 缓存失败不影响主流程


def _cache_load_block(date: str, block_name: str) -> Optional[dict]:
    """读取新版缓存块；返回 {data, source: 本地缓存} 或 None。"""
    f = get_cache_dir() / date / f"block_{block_name}.json"
    if not f.exists():
        return None
    try:
        obj = json.loads(f.read_text(encoding="utf-8"))
        if obj.get("schema_version") != _CACHE_SCHEMA_VERSION:
            return None
        data = obj.get("data")
        return {"data": data, "source": "本地缓存"} if data is not None else None
    except Exception:
        return None


def _legacy_cache_market_micro(date: str) -> Optional[dict]:
    """读取旧版 market_micro.json 缓存并换算为契约口径（% → 小数）。

    旧版缓存无 schema_version，pct_change/涨跌幅等为百分数（如 0.72 表示 0.72%），
    此处统一 /100 并标注「本地缓存(旧版换算)」。
    """
    f = get_cache_dir() / date / "market_micro.json"
    if not f.exists():
        return None
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    idx = raw.get("index")
    if isinstance(idx, dict):
        converted = {}
        for name, v in idx.items():
            if not isinstance(v, dict) or "error" in v:
                converted[name] = None
                continue
            vv = dict(v)
            for k in ("pct_change", "volume_vs_prev_pct"):
                f = _to_finite(vv.get(k))
                vv[k] = None if f is None else round(f / 100.0, 6)
            converted[name] = vv
        out["index"] = converted
    zt = raw.get("zhangting")
    if isinstance(zt, dict):
        out["zhangting"] = {
            "count": zt.get("total"),
            "tier": zt.get("tier"),
            "top_industries": zt.get("top_industries"),
            "stocks": zt.get("top20"),
            "units": {"涨跌幅": "%", "换手率": "%", "成交额": "元", "封板资金": "元"},
        }
    dt = raw.get("dieting")
    if isinstance(dt, dict):
        out["dieting"] = {"count": dt.get("total"), "stocks": dt.get("top10")}
    mb = raw.get("market_breadth")
    if isinstance(mb, dict):
        out["breadth"] = {
            "total": mb.get("total_volume"),
            "note": "旧版缓存仅有指数成交量汇总",
        }
    sectors = raw.get("sectors")
    if isinstance(sectors, dict) and sectors.get("top5"):
        out["sectors"] = {
            "top5": sectors.get("top5"), "bottom5": sectors.get("bottom5"),
            "units": {"涨跌幅": "%", "成交额": "元", "成交额变化": "%"},
        }
    return out or None


def _legacy_cache_sectors(date: str) -> Optional[dict]:
    f = get_cache_dir() / date / "sectors.json"
    if not f.exists():
        return None
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(raw, dict) and raw.get("top5"):
        return {"top5": raw.get("top5"), "bottom5": raw.get("bottom5"),
                "units": {"涨跌幅": "%", "成交额": "元", "成交额变化": "%"}}
    return None


# ═══════════════════════════════════════════════════════════════
# 指数日线/均线（通达信 akshare → 通达信本地 → 本地缓存）
# ═══════════════════════════════════════════════════════════════

def _records_from_tx(df: pd.DataFrame) -> list[dict]:
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "date": str(r["date"]),
            "open": _to_finite(r.get("open")),
            "high": _to_finite(r.get("high")),
            "low": _to_finite(r.get("low")),
            "close": _to_finite(r.get("close")),
            "volume": _to_finite(r.get("volume")),
            "amount": _to_finite(r.get("amount")),
        })
    return [r for r in rows if r["close"] is not None]


def _compute_index_metrics(records: list[dict], date: str, days: int) -> Optional[dict]:
    """从日线记录计算截至 date（≤date 最近一条）的指数指标；无记录返回 None。"""
    idx = None
    for i, r in enumerate(records):
        if r["date"] <= date:
            idx = i
        else:
            break
    if idx is None:
        return None
    closes = [r["close"] for r in records[: idx + 1]]
    amounts = [r["amount"] or 0.0 for r in records[: idx + 1]]
    cur = records[idx]
    prev = records[idx - 1] if idx > 0 else None
    mas: dict[str, Any] = {}
    vs: dict[str, Any] = {}
    for p in MA_PERIODS:
        if len(closes) >= p:
            ma = sum(closes[-p:]) / p
            mas[f"ma{p}"] = round(ma, 2)
            vs[f"vs_ma{p}"] = round((closes[-1] - ma) / ma, 6) if ma else None
        else:
            mas[f"ma{p}"] = None
            vs[f"vs_ma{p}"] = None
    pct_changes: dict[str, Any] = {}
    for p in (5, 10, 20, 60):
        if len(closes) >= p + 1:
            pct_changes[f"pct_{p}d"] = round((closes[-1] - closes[-p - 1]) / closes[-p - 1], 6)
        else:
            pct_changes[f"pct_{p}d"] = None
    half = len(amounts) // 2
    if half and sum(amounts[half:]):
        amt_second = sum(amounts[half:])
        amt_first = sum(amounts[:half])
        vol_trend = "放量" if amt_second > amt_first * 1.1 else ("缩量" if amt_second < amt_first * 0.9 else "持平")
    else:
        vol_trend = None
    daily = []
    for r in records[max(0, idx - days + 1): idx + 1]:
        daily.append({
            "date": r["date"], "open": r["open"], "high": r["high"],
            "low": r["low"], "close": r["close"], "volume": r["volume"], "amount": r["amount"],
        })
    return {
        "close": cur["close"], "open": cur["open"], "high": cur["high"], "low": cur["low"],
        "pct_change": round((cur["close"] - prev["close"]) / prev["close"], 6) if prev else None,
        "volume": cur["volume"], "amount": cur["amount"],
        "volume_unit": "手", "amount_unit": "元",
        **mas, **vs, **pct_changes,
        "volume_trend": vol_trend,
        "daily": daily,
    }


def fetch_index_trend(date: str, days: int = 60, tdx_path: Optional[str] = None) -> dict:
    """指数日线/均线：通达信 akshare → 通达信本地 → 本地缓存 → 数据缺失。"""
    fmt = _d(date)
    errors: list[str] = []
    cached = _cache_load_block(date, "index_trend")
    indices: dict[str, Any] = {name: None for name in INDEX_CODES}
    asof = None

    # 主源：通达信 akshare
    primary_ok = True
    for name, symbol in INDEX_CODES.items():
        try:
            df = _call_with_timeout(_tx_index_daily, symbol)
            records = _records_from_tx(df)
            metrics = _compute_index_metrics(records, date, days)
            if metrics is None:
                raise DataSourceError(f"截至 {date} 无该指数日线记录")
            indices[name] = metrics
            if asof is None or metrics["daily"][-1]["date"] > asof:
                asof = metrics["daily"][-1]["date"]
        except Exception as exc:
            primary_ok = False
            errors.append(f"通达信akshare·{name}: {exc}")
    if primary_ok:
        note = None if asof == date else (f"非交易日，取最近交易日 {asof} 数据" if asof else None)
        out = {"indices": indices, "asof_date": asof, "requested_date": date,
               "source": "通达信akshare", "degraded": False, "degraded_reason": [], "note": note}
        _cache_save(date, "index_trend", out)
        return out

    # 备用源：通达信本地
    backup_ok = True
    for name, symbol in INDEX_CODES.items():
        try:
            day = tdx_local.read_day(symbol, tdx_path=tdx_path)
            records = day["records"]
            if not records:
                raise DataSourceError("日线记录为空")
            metrics = _compute_index_metrics(records, date, days)
            if metrics is None:
                raise DataSourceError(f"截至 {date} 无该指数日线记录")
            metrics["_tdx_date_end"] = day["date_end"]
            indices[name] = metrics
            if asof is None or metrics["daily"][-1]["date"] > asof:
                asof = metrics["daily"][-1]["date"]
        except Exception as exc:
            backup_ok = False
            errors.append(f"通达信本地·{name}: {exc}")
    if backup_ok:
        note = None if asof == date else (f"非交易日，取最近交易日 {asof} 数据" if asof else None)
        return {"indices": indices, "asof_date": asof, "requested_date": date,
                "source": "通达信本地", "degraded": True,
                "degraded_reason": ["通达信akshare失败，降级通达信本地"],
                "note": f"{note or ''}（本地日线最新 {asof}）"}

    # 第三环：本地缓存
    if cached is not None:
        data = cached["data"]
        return {"indices": data.get("indices"), "asof_date": data.get("asof_date"),
                "requested_date": date, "source": "本地缓存", "degraded": True,
                "degraded_reason": [f"在线数据源失败：{'；'.join(errors[:3])}", "使用本地缓存"],
                "note": data.get("note")}
    legacy = _legacy_cache_market_micro(date)
    if legacy and legacy.get("index"):
        return {"indices": legacy["index"], "asof_date": None, "requested_date": date,
                "source": "本地缓存(旧版换算)", "degraded": True,
                "degraded_reason": [f"在线数据源失败：{'；'.join(errors[:3])}", "旧版缓存换算（%→小数）"],
                "note": "旧版缓存，无均线/日线明细"}

    return {"indices": indices, "asof_date": None, "requested_date": date,
            "source": "数据缺失", "degraded": True,
            "degraded_reason": errors or ["各数据源均不可用"],
            "note": "指数日线数据缺失（在线源失败且无本地缓存）"}


# ═══════════════════════════════════════════════════════════════
# 涨停/跌停/炸板池（东财 → Tushare 计算 → 本地缓存）
# ═══════════════════════════════════════════════════════════════

_ZT_POOL_UNITS = {"涨跌幅": "%", "换手率": "%", "成交额": "元", "封板资金": "元",
                  "炸板次数": "次", "涨停统计": "次", "流通市值": "元", "总市值": "元"}


def _pool_rows(df: pd.DataFrame, want: tuple[str, ...]) -> list[dict]:
    if df is None or df.empty:
        return []
    cols = [c for c in want if c in df.columns]
    return [_clean_row(r) for r in df[cols].to_dict(orient="records")]


def _tier_from_pool(df: pd.DataFrame) -> Optional[dict]:
    if df is None or df.empty or "连板数" not in df.columns:
        return None
    tier: dict[str, int] = {}
    for v, n in df["连板数"].value_counts().to_dict().items():
        try:
            tier[str(int(v))] = int(n)
        except (TypeError, ValueError):
            continue
    ordered = {k: tier[k] for k in sorted(tier, key=lambda x: (x == "1", int(x) if x.isdigit() else 99))}
    if "1" in ordered:
        ordered["首板"] = ordered.pop("1")
    return ordered


def _zt_pool_from_em(date: str) -> dict:
    fmt = _d(date)
    zt_df = _call_with_timeout(_em_zt_pool, fmt)
    zbgc_df = _call_with_timeout(_em_zt_pool_zbgc, fmt)
    dt_df = _call_with_timeout(_em_zt_pool_dtgc, fmt)
    if ((zt_df is None or zt_df.empty) and (zbgc_df is None or zbgc_df.empty)
            and (dt_df is None or dt_df.empty)):
        raise DataSourceError("东财涨跌停池无数据（可能非交易日或超出30交易日保留期）")
    zt_df = _filter_st_bj(zt_df)
    zbgc_df = _filter_st_bj(zbgc_df)
    dt_df = _filter_st_bj(dt_df)
    zt_rows = _pool_rows(zt_df, ("代码", "名称", "涨跌幅", "最新价", "涨停价", "换手率", "连板数",
                                 "所属行业", "成交额", "封板资金", "首次封板时间", "最后封板时间"))
    zbgc_rows = _pool_rows(zbgc_df, ("代码", "名称", "涨跌幅", "最新价", "涨停价", "换手率",
                                     "连板数", "所属行业", "成交额", "炸板次数"))
    dt_rows = _pool_rows(dt_df, ("代码", "名称", "涨跌幅", "最新价", "跌停价", "换手率", "成交额"))
    sealed = len(zt_rows)
    zhaban = len(zbgc_rows)
    touched = sealed + zhaban
    block = {
        "limit_up": {
            "count": sealed, "tier": _tier_from_pool(zt_df),
            "top_industries": (zt_df["所属行业"].value_counts().head(5).to_dict()
                               if zt_df is not None and not zt_df.empty and "所属行业" in zt_df.columns else {}),
            "stocks": zt_rows[:20],
            "units": _ZT_POOL_UNITS,
        } if zt_df is not None and not zt_df.empty else None,
        "limit_down": {"count": len(dt_rows), "stocks": dt_rows[:10], "units": _ZT_POOL_UNITS}
        if dt_df is not None and not dt_df.empty else None,
        "zhaban": {"count": zhaban, "stocks": zbgc_rows[:20], "units": _ZT_POOL_UNITS}
        if zbgc_df is not None and not zbgc_df.empty else None,
        "sealed_count": sealed, "touched_count": touched,
        "zhaban_rate": round(zhaban / touched, 6) if touched else None,
        "source": "东财", "degraded": False, "degraded_reason": [],
        "note": None,
    }
    return block


def _zt_pool_from_tushare(date: str) -> dict:
    fmt = _d(date)
    df = _filter_st_bj(_ts_daily(fmt), _ts_name_map())
    if df is None or df.empty:
        raise DataSourceError("Tushare 当日无全市场数据")
    codes = df["ts_code"].astype(str)
    ratios = codes.map(_limit_ratio)
    limit_p = (df["pre_close"] * (1 + ratios)).round(2)
    zt = df[df["close"] >= limit_p]
    dt_p = (df["pre_close"] * (1 - ratios)).round(2)
    dt = df[df["close"] <= dt_p]
    touched = df[df["high"] >= limit_p]
    sealed = df[df["close"] >= limit_p]
    zhaban = touched[~touched.index.isin(sealed.index)]
    tier = {}
    for _, r in zt.iterrows():
        tier[str(r.get("连板数", 1))] = tier.get(str(r.get("连板数", 1)), 0) + 1
    def _rows(ddf: pd.DataFrame, n: int) -> list[dict]:
        rows = []
        for _, r in ddf.head(n).iterrows():
            rows.append({"代码": str(r["ts_code"]), "名称": _ts_name_map().get(str(r["ts_code"])),
                         "收盘": float(r["close"]), "涨跌幅": float(r["pct_chg"])})
        return rows
    return {
        "limit_up": {"count": len(zt), "tier": tier or None, "top_industries": {},
                     "stocks": _rows(zt, 20), "units": {"涨跌幅": "%"}} if not zt.empty else None,
        "limit_down": {"count": len(dt), "stocks": _rows(dt, 10), "units": {"涨跌幅": "%"}}
        if not dt.empty else None,
        "zhaban": {"count": len(zhaban), "stocks": _rows(zhaban, 20), "units": {"涨跌幅": "%"}}
        if not zhaban.empty else None,
        "sealed_count": len(sealed), "touched_count": len(touched),
        "zhaban_rate": round(len(zhaban) / len(touched), 6) if len(touched) else None,
        "source": "Tushare计算", "degraded": False, "degraded_reason": [],
        "note": "Tushare 计算口径（东财池不可用时的备用源）",
    }


_ts_name_cache: dict = {}


def _ts_name_map() -> dict:
    if not _ts_name_cache:
        try:
            df = _ts_stock_basic()
            _ts_name_cache.update(dict(zip(df["ts_code"].astype(str), df["name"])))
        except Exception:
            return {}
    return _ts_name_cache


def fetch_zt_pool(date: str) -> dict:
    """涨跌停/炸板池：东财 → Tushare 计算 → 本地缓存 → 数据缺失。"""
    errors: list[str] = []
    try:
        block = _zt_pool_from_em(date)
        _cache_save(date, "zt_pool", block)
        return block
    except Exception as exc:
        errors.append(f"东财: {exc}")
    try:
        return _zt_pool_from_tushare(date)
    except Exception as exc:
        errors.append(f"Tushare计算: {exc}")
    cached = _cache_load_block(date, "zt_pool")
    if cached is not None:
        data = dict(cached["data"])
        data.update(source="本地缓存", degraded=True,
                    degraded_reason=[f"在线数据源失败：{'；'.join(errors[:3])}", "使用本地缓存"])
        return data
    return {"limit_up": None, "limit_down": None, "zhaban": None,
            "sealed_count": None, "touched_count": None, "zhaban_rate": None,
            "source": "数据缺失", "degraded": True, "degraded_reason": errors or ["各数据源均不可用"],
            "note": "涨跌停/炸板数据缺失（数据源保留期外或不可用）"}


# ═══════════════════════════════════════════════════════════════
# 全市场涨跌分布/炸板率（Tushare 计算 → 东财 → 本地缓存）
# ═══════════════════════════════════════════════════════════════

def _parse_count(value: Any) -> Optional[int]:
    if value is None:
        return None
    s = str(value).replace(",", "").replace("，", "").strip()
    if s.endswith("%"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _breadth_from_tushare(date: str) -> dict:
    fmt = _d(date)
    df = _filter_st_bj(_ts_daily(fmt), _ts_name_map())
    if df is None or df.empty:
        raise DataSourceError("Tushare 当日无全市场数据")
    codes = df["ts_code"].astype(str)
    ratios = codes.map(_limit_ratio)
    pct = df["pct_chg"] / 100.0
    limit_p = (df["pre_close"] * (1 + ratios)).round(2)
    dt_p = (df["pre_close"] * (1 - ratios)).round(2)
    sealed = df[df["close"] >= limit_p]
    dieting = df[df["close"] <= dt_p]
    touched = df[df["high"] >= limit_p]
    zhaban = touched[~touched.index.isin(sealed.index)]
    up = int((pct > 0).sum())
    down = int((pct < 0).sum())
    flat = int((pct == 0).sum())
    is_gem = codes.str.startswith(("30", "68"))
    dist = {
        "涨停": len(sealed), "跌停": len(dieting),
        "涨5-10%": int(((pct >= 0.05) & (pct < 0.0995) & ~is_gem).sum()
                       + ((pct >= 0.10) & (pct < 0.1995) & is_gem).sum()),
        "涨2-5%": int(((pct >= 0.02) & (pct < 0.05)).sum()),
        "涨0-2%": int(((pct > 0) & (pct < 0.02)).sum()),
        "平盘": flat,
        "跌0-2%": int(((pct < 0) & (pct > -0.02)).sum()),
        "跌2-5%": int(((pct <= -0.02) & (pct > -0.05)).sum()),
        "跌5%以上": int((pct <= -0.05).sum()),
    }
    return {
        "total": len(df), "up": up, "down": down, "flat": flat,
        "up_down_ratio": round(up / down, 4) if down else None,
        "limit_up": len(sealed), "limit_down": len(dieting),
        "zhaban": len(zhaban), "touched": len(touched),
        "zhaban_rate": round(len(zhaban) / len(touched), 6) if len(touched) else None,
        "distribution": dist,
        "source": "Tushare计算", "degraded": False, "degraded_reason": [],
        "note": "全市场涨跌分布（按主板10%/创业科创20%精确口径，过滤ST/北证）",
    }


def _breadth_from_legu() -> dict:
    df = _call_with_timeout(_legu_activity)
    if df is None or df.empty:
        raise DataSourceError("东财（乐咕）市场活跃度无数据")
    m: dict[str, Any] = {}
    for _, r in df.iterrows():
        item = str(r.get("item", "")).strip()
        m[item] = r.get("value")
    up, down = _parse_count(m.get("上涨")), _parse_count(m.get("下跌"))
    zhaban, touched = _parse_count(m.get("炸板")), _parse_count(m.get("摸板"))
    return {
        "total": sum(v for v in (up, down, _parse_count(m.get("平盘"))) if v is not None) or None,
        "up": up, "down": down, "flat": _parse_count(m.get("平盘")),
        "up_down_ratio": round(up / down, 4) if up is not None and down else None,
        "limit_up": _parse_count(m.get("涨停")), "limit_down": _parse_count(m.get("跌停")),
        "zhaban": zhaban, "touched": touched,
        "zhaban_rate": round(zhaban / touched, 6) if zhaban is not None and touched else None,
        "distribution": {},
        "source": "东财", "degraded": False, "degraded_reason": [],
        "note": f"东财市场活跃度口径（统计日期 {m.get('统计日期')}），分布明细缺失",
    }


def fetch_market_breadth(date: str) -> dict:
    """涨跌家数/炸板率：Tushare 计算 → 东财（乐咕）→ 本地缓存 → 数据缺失。"""
    errors: list[str] = []
    try:
        block = _breadth_from_tushare(date)
        _cache_save(date, "breadth", block)
        return block
    except Exception as exc:
        errors.append(f"Tushare计算: {exc}")
    try:
        return _breadth_from_legu()
    except Exception as exc:
        errors.append(f"东财: {exc}")
    cached = _cache_load_block(date, "breadth")
    if cached is not None:
        data = dict(cached["data"])
        data.update(source="本地缓存", degraded=True,
                    degraded_reason=[f"在线数据源失败：{'；'.join(errors[:3])}", "使用本地缓存"])
        return data
    return {"total": None, "up": None, "down": None, "flat": None, "up_down_ratio": None,
            "limit_up": None, "limit_down": None, "zhaban": None, "touched": None,
            "zhaban_rate": None, "distribution": {},
            "source": "数据缺失", "degraded": True, "degraded_reason": errors or ["各数据源均不可用"],
            "note": "涨跌家数/炸板率数据缺失"}


# ═══════════════════════════════════════════════════════════════
# 板块涨幅/资金（同花顺 → 东财板块 → 本地缓存）
# ═══════════════════════════════════════════════════════════════

_SECTOR_UNITS = {"涨跌幅": "%", "成交额": "元", "净流入": "元", "成交额变化": "%"}


def _sector_row(row: dict, name_key: str = "板块") -> dict:
    """板块行：契约字段（小数）+ raw（上游原值，单位见块级 units）。"""
    name = row.get(name_key) or row.get("名称") or row.get("板块")
    pct = _to_finite(row.get("涨跌幅"))
    return {
        "name": name, "pct_change": round(pct / 100.0, 6) if pct is not None else None,
        "amount": _to_finite(row.get("成交额") or row.get("总成交额")),
        "net_inflow": _to_finite(row.get("净流入") or row.get("主力净流入")),
        "leading_stock": row.get("领涨股") or row.get("领涨股票"),
        "leading_pct_change": (_to_finite(row.get("领涨股-涨跌幅") or row.get("领涨股票-涨跌幅")) or 0) / 100.0
        if (_to_finite(row.get("领涨股-涨跌幅") or row.get("领涨股票-涨跌幅")) is not None) else None,
        "raw": _clean_row(row),
    }


def _sectors_from_ths_today() -> dict:
    df = _call_with_timeout(_ths_sector_summary)
    if df is None or df.empty:
        raise DataSourceError("同花顺板块实时无数据")
    df = df.dropna(subset=["涨跌幅"]) if "涨跌幅" in df.columns else df
    top = df.nlargest(5, "涨跌幅").to_dict(orient="records")
    bottom = df.nsmallest(5, "涨跌幅").to_dict(orient="records")
    flow_bottom = (df.nsmallest(5, "净流入").to_dict(orient="records")
                   if "净流入" in df.columns else [])
    return {
        "top5": [_sector_row(r) for r in top],
        "bottom5": [_sector_row(r) for r in bottom],
        "bottom5_flow": [_sector_row(r) for r in flow_bottom],
        "units": _SECTOR_UNITS,
        "source": "同花顺", "degraded": False, "degraded_reason": [],
        "note": "实时板块涨幅/资金",
    }


def _sectors_from_em_today() -> dict:
    df = _call_with_timeout(_em_sector_names)
    if df is None or df.empty:
        raise DataSourceError("东财板块实时无数据")
    df = df.dropna(subset=["涨跌幅"]) if "涨跌幅" in df.columns else df
    top = df.nlargest(5, "涨跌幅").to_dict(orient="records")
    bottom = df.nsmallest(5, "涨跌幅").to_dict(orient="records")
    return {
        "top5": [_sector_row(r) for r in top],
        "bottom5": [_sector_row(r) for r in bottom],
        "bottom5_flow": [],
        "units": _SECTOR_UNITS,
        "source": "东财", "degraded": True, "degraded_reason": ["同花顺不可用，降级东财"],
        "note": "东财板块口径（备用源）",
    }


def _sectors_from_hist(date: str) -> dict:
    """历史板块：同花顺逐行业 → 东财逐行业，带总预算；部分失败则标注。"""
    fmt = _d(date)
    prev = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y%m%d")
    deadline = time.monotonic() + _SECTOR_HISTORY_BUDGET
    errors: list[str] = []
    for source, names_fn, hist_fn in (
        ("同花顺", _ths_sector_names, _ths_sector_hist),
        ("东财", _em_sector_names, _em_sector_hist),
    ):
        try:
            names_df = _call_with_timeout(names_fn, timeout=max(5.0, deadline - time.monotonic()))
            names = [str(n) for n in names_df["name"].tolist()] if "name" in names_df.columns else \
                    [str(n) for n in names_df.iloc[:, 0].tolist()]
            if not names:
                raise DataSourceError("板块列表为空")
            ranks = []
            for name in names:
                if time.monotonic() > deadline:
                    errors.append(f"{source}: 行业遍历超预算（{_SECTOR_HISTORY_BUDGET}秒）")
                    break
                try:
                    df_i = _call_with_timeout(hist_fn, name, prev, fmt,
                                              timeout=max(1.0, min(SOURCE_TIMEOUT_SECONDS, deadline - time.monotonic())))
                    if df_i is None or len(df_i) < 2:
                        continue
                    cur, prv = df_i.iloc[-1], df_i.iloc[-2]
                    close_cur, close_prv = _to_finite(cur.get("收盘价")), _to_finite(prv.get("收盘价"))
                    amt_cur, amt_prv = _to_finite(cur.get("成交额")), _to_finite(prv.get("成交额"))
                    if close_cur is None or close_prv is None or close_prv == 0:
                        continue
                    ranks.append({
                        "name": name, "pct_change": round((close_cur - close_prv) / close_prv, 6),
                        "amount": amt_cur,
                        "amount_change_pct": round((amt_cur - amt_prv) / amt_prv, 6)
                        if amt_cur is not None and amt_prv else None,
                        "net_inflow": None, "leading_stock": None, "leading_pct_change": None,
                        "raw": {"板块": name, "收盘价": close_cur, "成交额": amt_cur},
                    })
                except Exception as exc:
                    errors.append(f"{source}·{name}: {exc}")
            if ranks:
                ranks.sort(key=lambda r: (r["pct_change"] is None, -(r["pct_change"] or 0)))
                partial = len(ranks) < len(names)
                return {
                    "top5": ranks[:5], "bottom5": ranks[-5:][::-1], "bottom5_flow": [],
                    "units": _SECTOR_UNITS, "source": source,
                    "degraded": partial or bool(errors),
                    "degraded_reason": [f"历史板块遍历 {len(ranks)}/{len(names)} 个行业"]
                    + (["部分行业失败：%s" % "；".join(errors[:3])] if errors else []),
                    "note": f"历史板块（{source}，{len(ranks)}行业）",
                }
        except Exception as exc:
            errors.append(f"{source}: {exc}")
    raise DataSourceError("历史板块数据不可用：" + "；".join(errors[:5]))


def fetch_sectors(date: str) -> dict:
    """板块涨幅/资金：同花顺 → 东财板块 → 本地缓存 → 数据缺失。"""
    today = _now().strftime("%Y-%m-%d")
    errors: list[str] = []
    if date == today:
        try:
            block = _sectors_from_ths_today()
            _cache_save(date, "sectors", block)
            return block
        except Exception as exc:
            errors.append(f"同花顺: {exc}")
        try:
            block = _sectors_from_em_today()
            _cache_save(date, "sectors", block)
            return block
        except Exception as exc:
            errors.append(f"东财: {exc}")
    else:
        try:
            block = _sectors_from_hist(date)  # 内部已按 同花顺→东财 顺序尝试
            _cache_save(date, "sectors", block)
            return block
        except Exception as exc:
            errors.append(f"历史板块: {exc}")
    cached = _cache_load_block(date, "sectors")
    if cached is not None:
        data = dict(cached["data"])
        data.update(source="本地缓存", degraded=True,
                    degraded_reason=[f"在线数据源失败：{'；'.join(errors[:3])}", "使用本地缓存"])
        return data
    legacy = _legacy_cache_sectors(date)
    if legacy is not None:
        legacy.update(source="本地缓存(旧版)", degraded=True,
                      degraded_reason=[f"在线数据源失败：{'；'.join(errors[:3])}", "旧版缓存（涨跌幅为%）"])
        return legacy
    return {"top5": [], "bottom5": [], "bottom5_flow": [], "units": _SECTOR_UNITS,
            "source": "数据缺失", "degraded": True, "degraded_reason": errors or ["各数据源均不可用"],
            "note": "板块数据缺失"}


# ═══════════════════════════════════════════════════════════════
# 竞价数据（开盘啦 Cookie → 同花顺竞价 → 东财 09:25 竞价柱推算 → 缓存）
# 契约 §四.1/§六.6：高开幅度=(竞价参考价−昨收)÷昨收；竞价金额=09:25成交额；
# 抢筹/砸盘=09:20–09:25 委托净流入方向；Cookie 未配置/失败 → 静默降级。
# ═══════════════════════════════════════════════════════════════

def _pick(row: dict, *keys: str) -> Any:
    for k in keys:
        if k in row and row[k] is not None:
            return row[k]
    return None


def _auction_records_from_kaipanla(date: str) -> list[dict]:
    cookie = config.KAIPANLA_COOKIE
    if not cookie:
        raise DataSourceError("未配置 KAIPANLA_COOKIE，跳过开盘啦（静默降级）")
    payload = _call_with_timeout(
        _http_get_json, KAIPANLA_API_URL, params={"date": _d(date)},
        headers={"Cookie": cookie, "User-Agent": _UA, "Referer": "https://www.kaipanla.com/"})
    rows = payload
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("list") or payload.get("items")
        if isinstance(rows, dict):
            rows = rows.get("list") or rows.get("items") or rows.get("data")
    if not isinstance(rows, list) or not rows:
        raise DataSourceError("开盘啦接口返回格式异常或为空")
    records = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        code = _pick(r, "代码", "code", "stock_code", "symbol")
        name = _pick(r, "名称", "name", "stock_name")
        if not code:
            continue
        pct = _to_finite(_pick(r, "竞价涨幅", "涨幅", "pct_change", "jjzf", "pct"))
        amount = _to_finite(_pick(r, "竞价金额", "金额", "amount", "jjje"))
        price = _to_finite(_pick(r, "竞价价格", "参考价", "price", "jjjg"))
        prev_close = _to_finite(_pick(r, "昨收", "pre_close", "yesterday_close"))
        records.append({
            "code": str(code).zfill(6), "name": name, "price": price,
            "prev_close": prev_close,
            "pct_change": round(pct / 100.0, 6) if pct is not None else
            (round((price - prev_close) / prev_close, 6) if price is not None and prev_close else None),
            "amount": amount,
            "direction": None,
            "raw": _clean_row(r),
        })
    if not records:
        raise DataSourceError("开盘啦接口无有效记录")
    return records


def _auction_records_from_ths(date: str) -> list[dict]:
    payload = _call_with_timeout(
        _http_get_json, THS_AUCTION_URL, params={"date": _d(date)},
        headers={"User-Agent": _UA, "Referer": "https://data.10jqka.com.cn/"})
    rows = payload
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("list") or payload.get("items")
        if isinstance(rows, dict):
            rows = rows.get("rank_list") or rows.get("list") or rows.get("items")
    if not isinstance(rows, list) or not rows:
        raise DataSourceError("同花顺竞价接口返回格式异常或为空")
    records = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        code = _pick(r, "代码", "code", "stock_code", "symbol")
        if not code:
            continue
        pct = _to_finite(_pick(r, "竞价涨幅", "涨幅", "pct_change", "jjzf"))
        amount = _to_finite(_pick(r, "竞价金额", "金额", "amount", "jjje"))
        price = _to_finite(_pick(r, "竞价价格", "参考价", "price", "jjjg"))
        prev_close = _to_finite(_pick(r, "昨收", "pre_close"))
        records.append({
            "code": str(code).zfill(6), "name": _pick(r, "名称", "name", "stock_name"),
            "price": price, "prev_close": prev_close,
            "pct_change": round(pct / 100.0, 6) if pct is not None else
            (round((price - prev_close) / prev_close, 6) if price is not None and prev_close else None),
            "amount": amount, "direction": None, "raw": _clean_row(r),
        })
    if not records:
        raise DataSourceError("同花顺竞价接口无有效记录")
    return records


def _em_auction_bar(bars: pd.DataFrame) -> Optional[dict]:
    """从东财 09:00-09:30 分时中取 09:25 竞价柱（含方向估算）。"""
    if bars is None or bars.empty:
        return None
    df = bars.copy()
    if "时间" in df.columns:
        df = df[df["时间"].astype(str) <= "09:26"]
    if df.empty:
        return None
    last = df.iloc[-1]
    direction = None
    if len(df) >= 2:
        a1 = _to_finite(df.iloc[-2].get("成交额"))
        a2 = _to_finite(last.get("成交额"))
        if a1 is not None and a2 is not None and a1 > 0:
            direction = "抢筹" if a2 >= a1 else "砸盘"
    return {
        "price": _to_finite(last.get("收盘") or last.get("最新价")),
        "amount": _to_finite(last.get("成交额")),
        "direction": direction,
    }


def _auction_from_em(date: str) -> list[dict]:
    """东财推算：以昨日涨停池为热门股候选，用 09:25 竞价柱推算高开幅度/竞价金额。"""
    fmt = _d(date)
    prev = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y%m%d")
    universe = None
    try:
        universe = _em_zt_pool_previous(prev)
    except Exception:
        pass
    if universe is None or universe.empty:
        universe = _em_zt_pool(fmt)
    if universe is None or universe.empty:
        raise DataSourceError("无热门股候选（昨日/当日涨停池均为空）")
    universe = _filter_st_bj(universe)
    if universe.empty:
        raise DataSourceError("热门股候选经 ST/北证过滤后为空")
    spot_map: dict[str, dict] = {}
    try:
        spot = _em_spot()
        if spot is not None and not spot.empty and "代码" in spot.columns:
            for _, r in spot.iterrows():
                spot_map[str(r["代码"])] = {
                    "name": r.get("名称"), "prev_close": _to_finite(r.get("昨收")),
                    "pct_change": _to_finite(r.get("涨跌幅")),
                }
    except Exception:
        pass
    order_col = "成交额" if "成交额" in universe.columns else "代码"
    top = universe.sort_values(order_col, ascending=False).head(10)
    deadline = time.monotonic() + _AUCTION_TOTAL_BUDGET
    records: list[dict] = []
    for _, r in top.iterrows():
        if time.monotonic() > deadline:
            break
        code = str(r.get("代码") or "").zfill(6)
        if not code or code == "000000":
            continue
        try:
            bars = _call_with_timeout(_em_pre_min, code,
                                      timeout=min(_AUCTION_STOCK_TIMEOUT, max(1.0, deadline - time.monotonic())))
            bar = _em_auction_bar(bars)
            if bar is None:
                continue
            info = spot_map.get(code, {})
            prev_close = info.get("prev_close")
            price = bar["price"]
            pct = round((price - prev_close) / prev_close, 6) if price is not None and prev_close else None
            records.append({
                "code": code, "name": r.get("名称") or info.get("name"),
                "price": price, "prev_close": prev_close, "pct_change": pct,
                "amount": bar["amount"], "direction": bar["direction"],
                "raw": _clean_row(r),
            })
        except Exception:
            continue  # 单只个股失败静默跳过（估算口径）
    if not records:
        raise DataSourceError("东财竞价推算无有效结果（全部个股失败）")
    return records


def _auction_summary(records: list[dict], source: str) -> dict:
    pcts = [r["pct_change"] for r in records if r.get("pct_change") is not None]
    high_open = sum(1 for p in pcts if p > 0)
    low_open = sum(1 for p in pcts if p < 0)
    by_pct = sorted([r for r in records if r.get("pct_change") is not None],
                    key=lambda r: r["pct_change"], reverse=True)
    by_amount = sorted([r for r in records if r.get("amount") is not None],
                       key=lambda r: r["amount"], reverse=True)
    return {
        "count": len(records), "high_open_count": high_open, "low_open_count": low_open,
        "top_gainers": by_pct[:10], "top_amount": by_amount[:10],
        "stocks": records,
        "units": {"pct_change": "小数(1.0=100%)", "amount": "元"},
        "source": source, "degraded": False, "degraded_reason": [], "note": None,
    }


def fetch_auction_quote(date: str) -> dict:
    """竞价数据：开盘啦 → 同花顺竞价 → 东财分时 09:25 推算 → 本地缓存 → 数据缺失。

    Cookie 未配置/失败必须静默降级（不抛异常、不中断流程），降级原因进入 degraded_reason。
    """
    errors: list[str] = []
    # 主源：开盘啦（Cookie）
    try:
        records = _call_with_timeout(_auction_records_from_kaipanla, date)
        block = _auction_summary(records, "开盘啦")
        _cache_save(date, "auction", block)
        return block
    except Exception as exc:
        errors.append(f"开盘啦: {exc}")
    # 备用1：同花顺竞价
    try:
        records = _call_with_timeout(_auction_records_from_ths, date)
        block = _auction_summary(records, "同花顺竞价")
        block["degraded"] = True
        block["degraded_reason"] = ["开盘啦不可用，降级同花顺竞价"]
        _cache_save(date, "auction", block)
        return block
    except Exception as exc:
        errors.append(f"同花顺竞价: {exc}")
    # 备用2：东财 09:25 竞价柱推算（估算）
    try:
        records = _call_with_timeout(_auction_from_em, date, timeout=_AUCTION_TOTAL_BUDGET)
        block = _auction_summary(records, "东财分时推算")
        block["degraded"] = True
        block["degraded_reason"] = ["开盘啦/同花顺均不可用，东财 09:25 竞价柱推算（估算口径）"]
        block["note"] = "估算：以昨日涨停池为候选，竞价金额/高开幅度由 09:25 分时柱推算"
        _cache_save(date, "auction", block)
        return block
    except Exception as exc:
        errors.append(f"东财推算: {exc}")
    # 第三环：本地缓存
    cached = _cache_load_block(date, "auction")
    if cached is not None:
        data = dict(cached["data"])
        data.update(source="本地缓存", degraded=True,
                    degraded_reason=[f"在线数据源失败：{'；'.join(errors[:3])}", "使用本地缓存"])
        return data
    return {"count": None, "high_open_count": None, "low_open_count": None,
            "top_gainers": [], "top_amount": [], "stocks": [],
            "units": {"pct_change": "小数(1.0=100%)", "amount": "元"},
            "source": "数据缺失", "degraded": True, "degraded_reason": errors or ["各数据源均不可用"],
            "note": "竞价数据缺失（Cookie 未配置或全部数据源失败）"}


# ═══════════════════════════════════════════════════════════════
# 分时/分钟线（东财为主，通达信本地 minline 备用）
# ═══════════════════════════════════════════════════════════════

def _is_index_symbol(symbol: str) -> bool:
    s = str(symbol).lower()
    num = s[2:] if s.startswith(("sh", "sz", "bj")) else s
    return num in {"000001", "000016", "000300", "000688", "000905", "000852"} or num.startswith("399")


def _minute_bars_from_em(symbol: str, date: str, period: int, end_time: Optional[str]) -> list[dict]:
    fmt = _d(date)
    s = str(symbol).lower()
    num = s[2:] if s.startswith(("sh", "sz", "bj")) else s
    end = end_time or "15:00:00"
    start_ts = f"{date} 09:30:00"
    end_ts = f"{date} {end}"
    if _is_index_symbol(symbol):
        df = _call_with_timeout(_em_index_minute, num, str(period), start_ts, end_ts)
    else:
        df = _call_with_timeout(_em_stock_minute, num, str(period), start_ts, end_ts)
    if df is None or df.empty:
        raise DataSourceError("东财分钟线无数据")
    rows = []
    for _, r in df.iterrows():
        t = str(r.get("时间") or "")
        rows.append({
            "time": t, "open": _to_finite(r.get("开盘")), "high": _to_finite(r.get("最高")),
            "low": _to_finite(r.get("最低")), "close": _to_finite(r.get("收盘")),
            "volume": _to_finite(r.get("成交量")), "amount": _to_finite(r.get("成交额")),
        })
    if end_time:
        rows = [r for r in rows if r["time"] and r["time"] <= end_time]
    return rows


def _minute_bars_from_tdx(symbol: str, date: str, period: int,
                          end_time: Optional[str], tdx_path: Optional[str]) -> list[dict]:
    day = tdx_local.read_minline(symbol, period=period, tdx_path=tdx_path)
    rows = []
    for r in day["records"]:
        if r["date"] != date:
            continue
        if end_time and r["time"] > end_time:
            continue
        rows.append({"time": r["time"], "open": r["open"], "high": r["high"],
                     "low": r["low"], "close": r["close"],
                     "volume": r["volume"], "amount": r["amount"]})
    if not rows:
        raise DataSourceError(f"通达信本地 {date} 无 {period} 分钟记录（可能超出本地保留期）")
    return rows


def fetch_minute_data(symbol: str = "sh000001", date: Optional[str] = None,
                      period: int = 1, end_time: Optional[str] = None,
                      tdx_path: Optional[str] = None) -> dict:
    """分时/分钟线：东财 → 通达信本地（minline）→ 本地缓存 → 数据缺失。

    symbol 支持 "sh000001"/"600519" 等；period 1/5 分钟；end_time "HH:MM[:SS]" 截至时刻。
    """
    date = date or _now().strftime("%Y-%m-%d")
    _d(date)
    if period not in (1, 5):
        raise DataSourceError(f"不支持的分钟周期：{period}（仅支持 1/5 分钟）")
    errors: list[str] = []
    try:
        bars = _minute_bars_from_em(symbol, date, period, end_time)
        if not bars:
            raise DataSourceError("截至时刻无分时数据")
        block = {"symbol": symbol, "date": date, "period": period, "end_time": end_time,
                 "bars": bars, "bar_count": len(bars),
                 "volume_unit": "手", "amount_unit": "元",
                 "source": "东财", "degraded": False, "degraded_reason": [], "note": None}
        _cache_save(date, f"minute_{symbol}_{period}", block)
        return block
    except Exception as exc:
        errors.append(f"东财: {exc}")
    try:
        bars = _minute_bars_from_tdx(symbol, date, period, end_time, tdx_path)
        block = {"symbol": symbol, "date": date, "period": period, "end_time": end_time,
                 "bars": bars, "bar_count": len(bars),
                 "volume_unit": "手", "amount_unit": "元",
                 "source": "通达信本地", "degraded": True, "degraded_reason": ["东财不可用，降级通达信本地"],
                 "note": None}
        _cache_save(date, f"minute_{symbol}_{period}", block)
        return block
    except Exception as exc:
        errors.append(f"通达信本地: {exc}")
    cached = _cache_load_block(date, f"minute_{symbol}_{period}")
    if cached is not None:
        data = dict(cached["data"])
        data.update(source="本地缓存", degraded=True,
                    degraded_reason=[f"在线数据源失败：{'；'.join(errors[:3])}", "使用本地缓存"])
        return data
    return {"symbol": symbol, "date": date, "period": period, "end_time": end_time,
            "bars": [], "bar_count": 0,
            "volume_unit": "手", "amount_unit": "元",
            "source": "数据缺失", "degraded": True, "degraded_reason": errors or ["各数据源均不可用"],
            "note": "分时数据缺失"}


# ═══════════════════════════════════════════════════════════════
# 龙虎榜（东财 → Tushare top_list）
# ═══════════════════════════════════════════════════════════════

def _lhb_from_em(date: str) -> dict:
    fmt = _d(date)
    df = _call_with_timeout(_em_lhb, fmt, fmt)
    if df is None or df.empty:
        raise DataSourceError("东财龙虎榜无数据（可能非交易日或超出保留期）")
    rows = []
    for _, r in df.iterrows():
        pct = _to_finite(r.get("涨跌幅"))
        rows.append({
            "code": str(r.get("代码") or "").zfill(6),
            "name": r.get("名称"),
            "close": _to_finite(r.get("收盘价")),
            "pct_change": round(pct / 100.0, 6) if pct is not None else None,
            "buy_amount": _to_finite(r.get("龙虎榜买入额")),
            "sell_amount": _to_finite(r.get("龙虎榜卖出额")),
            "net_amount": _to_finite(r.get("龙虎榜净买额")),
            "amount": _to_finite(r.get("龙虎榜成交额")),
            "reason": r.get("上榜原因"),
            "raw": _clean_row(r),
        })
    return {"count": len(rows), "stocks": rows,
            "units": {"pct_change": "小数(1.0=100%)", "金额": "元"},
            "source": "东财", "degraded": False, "degraded_reason": [], "note": None}


def _lhb_from_tushare(date: str) -> dict:
    fmt = _d(date)
    df = _call_with_timeout(_ts_top_list, fmt)
    if df is None or df.empty:
        raise DataSourceError("Tushare 龙虎榜无数据")
    name_map = _ts_name_map()
    rows = []
    for _, r in df.iterrows():
        pct = _to_finite(r.get("pct_change"))
        rows.append({
            "code": str(r.get("ts_code") or "").split(".")[0].zfill(6),
            "name": r.get("name") or name_map.get(str(r.get("ts_code"))),
            "close": _to_finite(r.get("close")),
            "pct_change": round(pct / 100.0, 6) if pct is not None else None,
            "buy_amount": (_to_finite(r.get("l_buy")) or 0) * 10000 if r.get("l_buy") is not None else None,
            "sell_amount": (_to_finite(r.get("l_sell")) or 0) * 10000 if r.get("l_sell") is not None else None,
            "net_amount": (_to_finite(r.get("net_amount")) or 0) * 10000 if r.get("net_amount") is not None else None,
            "amount": (_to_finite(r.get("l_amount")) or 0) * 10000 if r.get("l_amount") is not None else None,
            "reason": r.get("reason"),
            "raw": _clean_row(r),
        })
    return {"count": len(rows), "stocks": rows,
            "units": {"pct_change": "小数(1.0=100%)", "金额": "元（Tushare 万元×10000 换算）"},
            "source": "Tushare", "degraded": True, "degraded_reason": ["东财不可用，降级 Tushare top_list"],
            "note": None}


def fetch_lhb(date: str) -> dict:
    """龙虎榜：东财 → Tushare top_list → 本地缓存 → 数据缺失。"""
    errors: list[str] = []
    try:
        block = _lhb_from_em(date)
        _cache_save(date, "lhb", block)
        return block
    except Exception as exc:
        errors.append(f"东财: {exc}")
    try:
        return _lhb_from_tushare(date)
    except Exception as exc:
        errors.append(f"Tushare: {exc}")
    cached = _cache_load_block(date, "lhb")
    if cached is not None:
        data = dict(cached["data"])
        data.update(source="本地缓存", degraded=True,
                    degraded_reason=[f"在线数据源失败：{'；'.join(errors[:3])}", "使用本地缓存"])
        return data
    return {"count": None, "stocks": [], "units": {"pct_change": "小数(1.0=100%)", "金额": "元"},
            "source": "数据缺失", "degraded": True, "degraded_reason": errors or ["各数据源均不可用"],
            "note": "龙虎榜数据缺失"}


# ═══════════════════════════════════════════════════════════════
# 情绪指标（契约 §六.5）：昨日涨停今日表现 + 炸板率 + 涨跌家数比
# 口径：主板10%/创业科创20%，过滤 ST/北证；比率一律小数。
# ═══════════════════════════════════════════════════════════════

def _sentiment_from_tushare(date: str, prev_date: Optional[str] = None) -> dict:
    fmt = _d(date)
    if prev_date is None:
        prev_date = _find_prev_trading_day(date)
    prev_fmt = _d(prev_date)
    df_prev = _filter_st_bj(_ts_daily(prev_fmt), _ts_name_map())
    df_today = _filter_st_bj(_ts_daily(fmt), _ts_name_map())
    if df_prev is None or df_prev.empty:
        raise DataSourceError(f"昨日({prev_date})无全市场数据")
    if df_today is None or df_today.empty:
        raise DataSourceError(f"今日({date})无全市场数据（盘中或非交易日）")
    name_map = _ts_name_map()
    # 昨日涨停
    codes_prev = df_prev["ts_code"].astype(str)
    limit_p_prev = (df_prev["pre_close"] * (1 + codes_prev.map(_limit_ratio))).round(2)
    zt_prev = df_prev[df_prev["close"] >= limit_p_prev]
    if zt_prev.empty:
        raise DataSourceError(f"昨日({prev_date})无涨停股")
    zt_codes = set(zt_prev["ts_code"].astype(str))
    # 今日匹配
    df_match = df_today[df_today["ts_code"].astype(str).isin(zt_codes)]
    if df_match.empty:
        raise DataSourceError("昨日涨停股今日无匹配数据（可能停牌/退市）")
    pct = df_match["pct_chg"] / 100.0
    avg = float(pct.mean())
    median = float(pct.median())
    red_rate = float((pct > 0).mean())
    codes_m = df_match["ts_code"].astype(str)
    limit_p_t = (df_match["pre_close"] * (1 + codes_m.map(_limit_ratio))).round(2)
    dt_p_t = (df_match["pre_close"] * (1 - codes_m.map(_limit_ratio))).round(2)
    lianban = df_match[df_match["close"] >= limit_p_t]
    hean = df_match[df_match["close"] <= dt_p_t]
    lianban_rate = len(lianban) / len(df_match)
    hean_rate = len(hean) / len(df_match)
    best = df_match.nlargest(3, "pct_chg")
    worst = df_match.nsmallest(3, "pct_chg")
    def _mini(ddf: pd.DataFrame) -> list[dict]:
        out = []
        for _, r in ddf.iterrows():
            code = str(r["ts_code"])
            p = _to_finite(r["pct_chg"])
            out.append({"code": code, "name": name_map.get(code),
                        "pct_change": round(p / 100.0, 6) if p is not None else None,
                        "close": _to_finite(r["close"]),
                        "raw": {"pct_chg": p, "单位": "%"}})
        return out
    # 炸板（全市场口径，与涨跌家数一致）
    codes_all = df_today["ts_code"].astype(str)
    limit_p_all = (df_today["pre_close"] * (1 + codes_all.map(_limit_ratio))).round(2)
    touched = df_today[df_today["high"] >= limit_p_all]
    sealed = df_today[df_today["close"] >= limit_p_all]
    zhaban = touched[~touched.index.isin(sealed.index)]
    up = int((df_today["pct_chg"] > 0).sum())
    down = int((df_today["pct_chg"] < 0).sum())
    return {
        "yesterday": prev_date, "today": date,
        "yesterday_zt_count": len(zt_prev), "matched_today": len(df_match),
        "avg_return": round(avg, 6), "median_return": round(median, 6),
        "red_rate": round(red_rate, 6),
        "lianban_count": len(lianban), "lianban_rate": round(lianban_rate, 6),
        "hean_count": len(hean), "hean_rate": round(hean_rate, 6),
        "best3": _mini(best), "worst3": _mini(worst),
        "zhaban": len(zhaban), "touched": len(touched),
        "zhaban_rate": round(len(zhaban) / len(touched), 6) if len(touched) else None,
        "up_count": up, "down_count": down,
        "up_down_ratio": round(up / down, 4) if down else None,
        "raw": {"avg_return_pct": round(avg * 100, 2), "red_rate_pct": round(red_rate * 100, 2),
                "lianban_rate_pct": round(lianban_rate * 100, 2), "hean_rate_pct": round(hean_rate * 100, 2),
                "单位": "%"},
        "source": "Tushare计算", "degraded": False, "degraded_reason": [],
        "note": f"昨日({prev_date})涨停、今日({date})表现（精确涨停价口径，过滤ST/北证）",
    }


def _sentiment_from_em(date: str) -> dict:
    """东财备用：昨日涨停池（东财）+ 今日 spot/日线 计算。"""
    fmt = _d(date)
    prev = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    pool = _call_with_timeout(_em_zt_pool_previous, _d(prev))
    if pool is None or pool.empty:
        raise DataSourceError("东财昨日涨停池无数据（可能超出30交易日保留期）")
    pool = _filter_st_bj(pool)
    if pool.empty:
        raise DataSourceError("昨日涨停池经 ST/北证过滤后为空")
    codes = [str(c).zfill(6) for c in pool["代码"].tolist()]
    name_map = dict(zip([str(c).zfill(6) for c in pool["代码"].tolist()], pool["名称"].tolist()))
    today_str = _now().strftime("%Y-%m-%d")
    rows: list[dict] = []
    if date == today_str:
        # 盘中/当日：实时快照（涨跌幅为当日至今表现）
        spot = _call_with_timeout(_em_spot)
        if spot is None or spot.empty:
            raise DataSourceError("东财实时快照无数据")
        code_set = set(codes)
        for _, r in spot.iterrows():
            c = str(r.get("代码") or "").zfill(6)
            if c not in code_set:
                continue
            p = _to_finite(r.get("涨跌幅"))
            prev_close = _to_finite(r.get("昨收"))
            price = _to_finite(r.get("最新价"))
            rows.append({
                "code": c, "name": r.get("名称") or name_map.get(c),
                "pct_change": round(p / 100.0, 6) if p is not None else None,
                "close": price, "pre_close": prev_close,
                "raw": {"涨跌幅": p, "单位": "%", "快照时刻": _now().strftime("%H:%M:%S")},
            })
    else:
        # 历史：逐股日线（预算内）
        deadline = time.monotonic() + _AUCTION_TOTAL_BUDGET
        for code in codes[:_SENTIMENT_EM_MAX_STOCKS]:
            if time.monotonic() > deadline:
                break
            try:
                df = _call_with_timeout(_em_stock_daily, code, fmt, fmt,
                                        timeout=min(_SENTIMENT_EM_STOCK_TIMEOUT, max(1.0, deadline - time.monotonic())))
                if df is None or df.empty:
                    continue
                last = df.iloc[-1]
                p = _to_finite(last.get("涨跌幅"))
                rows.append({
                    "code": code, "name": name_map.get(code),
                    "pct_change": round(p / 100.0, 6) if p is not None else None,
                    "close": _to_finite(last.get("收盘")), "pre_close": _to_finite(last.get("昨收")),
                    "raw": {"涨跌幅": p, "单位": "%"},
                })
            except Exception:
                continue
    if not rows:
        raise DataSourceError("东财情绪推算无有效结果")
    pcts = [r["pct_change"] for r in rows if r.get("pct_change") is not None]
    avg = sum(pcts) / len(pcts) if pcts else None
    red_rate = sum(1 for p in pcts if p > 0) / len(pcts) if pcts else None
    lianban = hean = 0
    for r in rows:
        if r.get("close") is None or r.get("pre_close") is None:
            continue
        ratio = _limit_ratio(r["code"])
        if r["close"] >= round(r["pre_close"] * (1 + ratio), 2):
            lianban += 1
        if r["close"] <= round(r["pre_close"] * (1 - ratio), 2):
            hean += 1
    n = len(rows)
    return {
        "yesterday": prev, "today": date,
        "yesterday_zt_count": len(codes), "matched_today": n,
        "avg_return": round(avg, 6) if avg is not None else None,
        "median_return": None, "red_rate": round(red_rate, 6) if red_rate is not None else None,
        "lianban_count": lianban, "lianban_rate": round(lianban / n, 6) if n else None,
        "hean_count": hean, "hean_rate": round(hean / n, 6) if n else None,
        "best3": sorted([r for r in rows if r.get("pct_change") is not None],
                        key=lambda r: r["pct_change"], reverse=True)[:3],
        "worst3": sorted([r for r in rows if r.get("pct_change") is not None],
                         key=lambda r: r["pct_change"])[:3],
        "zhaban": None, "touched": None, "zhaban_rate": None,
        "up_count": None, "down_count": None, "up_down_ratio": None,
        "raw": {"avg_return_pct": round(avg * 100, 2) if avg is not None else None,
                "red_rate_pct": round(red_rate * 100, 2) if red_rate is not None else None,
                "单位": "%", "matched_total": len(codes)},
        "source": "东财", "degraded": True,
        "degraded_reason": ["Tushare 不可用，降级东财（昨日涨停池 + 今日行情）"],
        "note": "东财口径（估算），炸板率/涨跌家数见涨跌家数块",
    }


def _sentiment_from_tdx(date: str, tdx_path: Optional[str]) -> dict:
    """通达信本地兜底：扫描 sh/sz 日线文件计算昨日涨停今日表现（估算）。"""
    prev = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    deadline = time.monotonic() + _SENTIMENT_TDX_BUDGET
    zt_prev: list[tuple[str, dict, dict]] = []   # (code, prev_record, today_record)
    up = down = flat = 0
    scanned = 0
    for market, code6, path in tdx_local.iter_day_codes(tdx_path=tdx_path):
        scanned += 1
        if time.monotonic() > deadline:
            break
        try:
            recs = tdx_local.read_day_tail(f"{market}{code6}", n=3, tdx_path=tdx_path)
            # recs[-3]=前日收盘, recs[-2]=昨日(prev), recs[-1]=今日(date)
            if len(recs) < 3 or recs[-2]["date"] != prev or recs[-1]["date"] != date:
                continue
            d0, d1, d2 = recs[-3], recs[-2], recs[-1]
            pct_today = (d2["close"] - d1["close"]) / d1["close"]
            if pct_today > 0:
                up += 1
            elif pct_today < 0:
                down += 1
            else:
                flat += 1
            limit_p = round(d0["close"] * (1 + _limit_ratio(code6)), 2)
            if d1["close"] >= limit_p:
                zt_prev.append((code6, d1, d2))
        except Exception:
            continue
    if not zt_prev:
        raise DataSourceError("通达信本地无匹配日线（本地数据可能早于复盘日期）")
    rows = []
    for code, d1, d2 in zt_prev:
        p = (d2["close"] - d1["close"]) / d1["close"]
        ratio = _limit_ratio(code)
        rows.append({"code": code, "name": None, "pct_change": round(p, 6),
                     "close": d2["close"], "pre_close": d1["close"],
                     "raw": {"单位": "小数"}})
    pcts = [r["pct_change"] for r in rows]
    avg = sum(pcts) / len(pcts)
    lianban = sum(1 for r in rows if r["close"] >= round(r["pre_close"] * (1 + _limit_ratio(r["code"])), 2))
    hean = sum(1 for r in rows if r["close"] <= round(r["pre_close"] * (1 - _limit_ratio(r["code"])), 2))
    return {
        "yesterday": prev, "today": date,
        "yesterday_zt_count": len(zt_prev), "matched_today": len(rows),
        "avg_return": round(avg, 6),
        "median_return": None,
        "red_rate": round(sum(1 for p in pcts if p > 0) / len(pcts), 6),
        "lianban_count": lianban, "lianban_rate": round(lianban / len(pcts), 6),
        "hean_count": hean, "hean_rate": round(hean / len(pcts), 6),
        "best3": sorted(rows, key=lambda r: r["pct_change"], reverse=True)[:3],
        "worst3": sorted(rows, key=lambda r: r["pct_change"])[:3],
        "zhaban": None, "touched": None, "zhaban_rate": None,
        "up_count": up, "down_count": down,
        "up_down_ratio": round(up / down, 4) if down else None,
        "raw": {"avg_return_pct": round(avg * 100, 2), "单位": "%", "scanned_files": scanned},
        "source": "通达信本地", "degraded": True,
        "degraded_reason": ["Tushare/东财均不可用，通达信本地估算（无名称，无法过滤ST；已过滤北证）"],
        "note": "通达信本地估算口径",
    }


def fetch_sentiment(date: str, tdx_path: Optional[str] = None) -> dict:
    """情绪指标：Tushare 计算 → 东财 → 通达信本地 → 本地缓存 → 数据缺失。"""
    errors: list[str] = []
    try:
        block = _sentiment_from_tushare(date)
        _cache_save(date, "sentiment", block)
        return block
    except Exception as exc:
        errors.append(f"Tushare计算: {exc}")
    try:
        block = _sentiment_from_em(date)
        _cache_save(date, "sentiment", block)
        return block
    except Exception as exc:
        errors.append(f"东财: {exc}")
    try:
        block = _sentiment_from_tdx(date, tdx_path)
        _cache_save(date, "sentiment", block)
        return block
    except Exception as exc:
        errors.append(f"通达信本地: {exc}")
    cached = _cache_load_block(date, "sentiment")
    if cached is not None:
        data = dict(cached["data"])
        data.update(source="本地缓存", degraded=True,
                    degraded_reason=[f"在线数据源失败：{'；'.join(errors[:3])}", "使用本地缓存"])
        return data
    return {"yesterday": None, "today": date, "yesterday_zt_count": None, "matched_today": None,
            "avg_return": None, "median_return": None, "red_rate": None,
            "lianban_count": None, "lianban_rate": None, "hean_count": None, "hean_rate": None,
            "best3": [], "worst3": [],
            "zhaban": None, "touched": None, "zhaban_rate": None,
            "up_count": None, "down_count": None, "up_down_ratio": None,
            "raw": {"单位": "%"}, "source": "数据缺失", "degraded": True,
            "degraded_reason": errors or ["各数据源均不可用"],
            "note": "情绪指标数据缺失"}


# ═══════════════════════════════════════════════════════════════
# 宏观外围（隔夜外盘/期货，仅当天；历史补做标注缺失）
# ═══════════════════════════════════════════════════════════════

def _external_from_sina() -> dict:
    out: dict[str, Any] = {}
    try:
        df = _call_with_timeout(_us_index_sina, ".IXIC")
        if df is not None and len(df) >= 2:
            latest, prev = df.iloc[-1], df.iloc[-2]
            close = _to_finite(latest.get("close"))
            prev_close = _to_finite(prev.get("close"))
            out["nasdaq"] = {
                "close": close,
                "pct_change": round((close - prev_close) / prev_close, 6)
                if close is not None and prev_close else None,
                "unit": "小数(1.0=100%)",
            }
    except Exception:
        out["nasdaq"] = None
    try:
        df = _call_with_timeout(_foreign_realtime, "A50")
        if df is not None and not df.empty:
            row = df.iloc[-1]
            close = _to_finite(row.get("最新价") or row.get("现价") or row.get("price"))
            pct = _to_finite(row.get("涨跌幅") or row.get("pct"))
            out["a50"] = {"close": close,
                          "pct_change": round(pct / 100.0, 6) if pct is not None else None,
                          "unit": "小数(1.0=100%)"}
        else:
            out["a50"] = None
    except Exception:
        out["a50"] = None
    return out


def _futures_from_ak() -> dict:
    out: dict[str, Any] = {}
    for name in ("IH", "IM"):
        try:
            df = _call_with_timeout(_futures_main_contract, name)
            if df is not None and not df.empty:
                latest = df.iloc[-1]
                out[name] = {
                    "close": _to_finite(latest.get("close")),
                    "volume": _to_finite(latest.get("volume")),
                    "open_interest": _to_finite(latest.get("hold") or latest.get("open_interest")),
                }
            else:
                out[name] = None
        except Exception:
            out[name] = None
    return out


def fetch_market_macro(date: str) -> dict:
    """宏观外围：纳指/富时A50/股指期货；仅当天有效，历史补做标注缺失。"""
    today = _now().strftime("%Y-%m-%d")
    errors: list[str] = []
    if date != today:
        return {"external": {"nasdaq": None, "a50": None},
                "futures": {"IH": None, "IM": None},
                "source": "数据缺失", "degraded": True,
                "degraded_reason": [f"外盘/期货仅当天可用，历史补做（{date}）无数据"],
                "note": "历史复盘不支持隔夜外盘"}
    external = _external_from_sina()
    futures = _futures_from_ak()
    if not any(v is not None for v in external.values()) and not any(v is not None for v in futures.values()):
        errors.append("全部外盘/期货数据源失败")
    cached = None if errors else None
    return {"external": external, "futures": futures,
            "source": "新浪/东财", "degraded": bool(errors),
            "degraded_reason": errors or [],
            "note": None}


# ═══════════════════════════════════════════════════════════════
# 财经资讯（财新仅当天 + 央视/经济日历，支持历史）
# ═══════════════════════════════════════════════════════════════

def fetch_news_headlines(date: str) -> dict:
    """财经资讯：财新头条（仅当天）、央视要闻、经济数据日历。"""
    fmt = _d(date)
    today = _now().strftime("%Y-%m-%d")
    result: dict[str, Any] = {"caixin": [], "cctv": [], "economic_calendar": [], "note": ""}
    errors: list[str] = []
    if date == today:
        try:
            df = _call_with_timeout(_news_caixin)
            if df is not None and not df.empty:
                result["caixin"] = [
                    {"tag": str(r.get("tag") or ""), "summary": str(r.get("summary") or "")[:200]}
                    for _, r in df.head(20).iterrows()]
            else:
                errors.append("财新无数据")
        except Exception as exc:
            errors.append(f"财新: {exc}")
    else:
        result["note"] = f"财新资讯仅当天可用，历史复盘（{date}）无财新数据"
    try:
        df = _call_with_timeout(_news_cctv, fmt)
        if df is not None and not df.empty:
            result["cctv"] = [str(r.get("title") or r.get("内容") or "") for _, r in df.iterrows()]
        else:
            errors.append("央视无数据")
    except Exception as exc:
        errors.append(f"央视: {exc}")
    try:
        df = _call_with_timeout(_news_economic_baidu, fmt)
        if df is not None and not df.empty:
            high = df[df["重要性"].astype(str).isin(["3", "2"])] if "重要性" in df.columns else df
            result["economic_calendar"] = [
                {"time": str(r.get("时间") or ""), "region": str(r.get("地区") or ""),
                 "event": str(r.get("事件") or "")}
                for _, r in high.head(10).iterrows()]
    except Exception as exc:
        errors.append(f"经济日历: {exc}")
    result["source"] = "财新/央视/经济日历"
    result["degraded"] = bool(errors)
    result["degraded_reason"] = errors or []
    return result


# ═══════════════════════════════════════════════════════════════
# 个股查询（Tushare → 东财 → 通达信本地）
# ═══════════════════════════════════════════════════════════════

def _stock_info_from_tushare(code: str, date: str) -> dict:
    fmt = _d(date)
    code = str(code).zfill(6)
    suffix = ".SH" if code.startswith(("6", "5", "9")) else (".BJ" if _is_bj(code) else ".SZ")
    ts_code = code + suffix
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=180)).strftime("%Y%m%d")
    df = _call_with_timeout(_ts_daily_range, ts_code, start, fmt)
    if df is None or df.empty:
        raise DataSourceError(f"未找到 {ts_code} 的数据（可能代码错误或非交易日）")
    df = df.sort_values("trade_date")
    latest = df.iloc[-1]
    closes = [float(c) for c in df["close"].tolist()]
    ma5 = round(sum(closes[-5:]) / 5, 4) if len(closes) >= 5 else None
    ma10 = round(sum(closes[-10:]) / 10, 4) if len(closes) >= 10 else None
    name_map = _ts_name_map()
    return {
        "code": ts_code, "name": name_map.get(ts_code),
        "date": _dash(str(latest["trade_date"])),
        "open": float(latest["open"]), "high": float(latest["high"]),
        "low": float(latest["low"]), "close": float(latest["close"]),
        "pre_close": float(latest["pre_close"]),
        "volume": int(latest["vol"]), "amount": float(latest["amount"]),
        "pct_change": round(float(latest["pct_chg"]) / 100.0, 6),
        "ma5": ma5, "ma10": ma10,
        "recent5": [
            {"date": _dash(str(r["trade_date"])), "close": float(r["close"]),
             "pct_change": round(float(r["pct_chg"]) / 100.0, 6)}
            for _, r in df.tail(5).iterrows()],
        "source": "Tushare", "degraded": False, "degraded_reason": [], "note": None,
    }


def _ts_daily_range(ts_code: str, start: str, end: str) -> pd.DataFrame:
    return _ts_pro().daily(ts_code=ts_code, start_date=start, end_date=end)


def _stock_info_from_em(code: str, date: str) -> dict:
    fmt = _d(date)
    code = str(code).zfill(6)
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=180)).strftime("%Y%m%d")
    df = _call_with_timeout(_em_stock_daily, code, start, fmt)
    if df is None or df.empty:
        raise DataSourceError(f"东财未找到 {code} 的数据")
    df = df.sort_values("日期")
    latest = df.iloc[-1]
    closes = [float(c) for c in df["收盘"].tolist()]
    ma5 = round(sum(closes[-5:]) / 5, 4) if len(closes) >= 5 else None
    ma10 = round(sum(closes[-10:]) / 10, 4) if len(closes) >= 10 else None
    pct = _to_finite(latest.get("涨跌幅"))
    return {
        "code": code, "name": None,
        "date": str(latest["日期"]),
        "open": float(latest["开盘"]), "high": float(latest["最高"]),
        "low": float(latest["最低"]), "close": float(latest["收盘"]),
        "pre_close": None,
        "volume": float(latest.get("成交量")), "amount": float(latest.get("成交额")),
        "pct_change": round(pct / 100.0, 6) if pct is not None else None,
        "ma5": ma5, "ma10": ma10,
        "recent5": [
            {"date": str(r["日期"]), "close": float(r["收盘"]),
             "pct_change": round(_to_finite(r.get("涨跌幅")) / 100.0, 6)
             if _to_finite(r.get("涨跌幅")) is not None else None}
            for _, r in df.tail(5).iterrows()],
        "source": "东财", "degraded": True, "degraded_reason": ["Tushare 不可用，降级东财"],
        "note": None,
    }


def _stock_info_from_tdx(code: str, date: str, tdx_path: Optional[str]) -> dict:
    day = tdx_local.read_day(code, tdx_path=tdx_path)
    recs = [r for r in day["records"] if r["date"] <= date]
    if not recs:
        raise DataSourceError(f"通达信本地截至 {date} 无 {code} 日线（可能超出本地保留期）")
    closes = [r["close"] for r in recs]
    latest = recs[-1]
    ma5 = round(sum(closes[-5:]) / 5, 4) if len(closes) >= 5 else None
    ma10 = round(sum(closes[-10:]) / 10, 4) if len(closes) >= 10 else None
    prev = recs[-2]["close"] if len(recs) >= 2 else None
    return {
        "code": code, "name": None, "date": latest["date"],
        "open": latest["open"], "high": latest["high"], "low": latest["low"],
        "close": latest["close"], "pre_close": prev,
        "volume": latest["volume"], "amount": latest["amount"],
        "pct_change": round((latest["close"] - prev) / prev, 6) if prev else None,
        "ma5": ma5, "ma10": ma10,
        "recent5": [{"date": r["date"], "close": r["close"], "pct_change": None}
                    for r in recs[-5:]],
        "source": "通达信本地", "degraded": True,
        "degraded_reason": ["Tushare/东财均不可用，降级通达信本地"], "note": None,
    }


def fetch_stock_info(code: str, date: str, tdx_path: Optional[str] = None) -> dict:
    """个股基本信息/近期走势：Tushare → 东财 → 通达信本地 → 数据缺失。"""
    errors: list[str] = []
    for fn, args in ((_stock_info_from_tushare, (code, date)),
                     (_stock_info_from_em, (code, date)),
                     (_stock_info_from_tdx, (code, date, tdx_path))):
        try:
            return _call_with_timeout(fn, *args)
        except Exception as exc:
            errors.append(str(exc))
    return {"code": code, "name": None, "date": date, "open": None, "high": None,
            "low": None, "close": None, "pre_close": None, "volume": None, "amount": None,
            "pct_change": None, "ma5": None, "ma10": None, "recent5": [],
            "source": "数据缺失", "degraded": True, "degraded_reason": errors or ["各数据源均不可用"],
            "note": f"个股 {code} 数据缺失"}


# ═══════════════════════════════════════════════════════════════
# 6 模式取数节点（契约 §二）
# ═══════════════════════════════════════════════════════════════

def _mode_end_time(mode: str) -> str:
    now_hm = _now().strftime("%H:%M")
    if mode == "intraday_am":
        return min(now_hm, "11:30")
    if mode == "noon":
        return "11:30"
    if mode == "intraday_pm":
        return min(now_hm, "15:00")
    return "15:00"


def _mode_result(date: str, mode: str, blocks: dict) -> dict:
    degraded: list[str] = []
    for name, blk in blocks.items():
        if isinstance(blk, dict) and blk.get("degraded"):
            for r in (blk.get("degraded_reason") or []):
                degraded.append(f"{name}: {r}")
    return {"date": date, "mode": mode, "mode_label": MODE_LABELS[mode],
            "blocks": blocks, "degraded": degraded, "degraded_flag": bool(degraded)}


def fetch_pre_market(date: str, tdx_path: Optional[str] = None) -> dict:
    """早盘前决策：隔夜外盘 + 昨夜消息 + 指数日线均线。"""
    _d(date)
    return _mode_result(date, "pre_market", {
        "index_trend": fetch_index_trend(date, days=60, tdx_path=tdx_path),
        "macro": fetch_market_macro(date),
        "news": fetch_news_headlines(date),
    })


def fetch_auction(date: str, tdx_path: Optional[str] = None) -> dict:
    """竞价复盘：竞价数据（开盘啦→同花顺→东财推算）。"""
    _d(date)
    return _mode_result(date, "auction", {"auction": fetch_auction_quote(date)})


def fetch_intraday_am(date: str, tdx_path: Optional[str] = None) -> dict:
    """上午盘中：上午分时（截至当前）+ 板块 + 涨停/炸板 + 情绪。"""
    _d(date)
    return _mode_result(date, "intraday_am", {
        "minute": fetch_minute_data("sh000001", date, period=1,
                                    end_time=_mode_end_time("intraday_am"), tdx_path=tdx_path),
        "sectors": fetch_sectors(date),
        "zt_pool": fetch_zt_pool(date),
        "breadth": fetch_market_breadth(date),
        "sentiment": fetch_sentiment(date, tdx_path=tdx_path),
    })


def fetch_noon(date: str, tdx_path: Optional[str] = None) -> dict:
    """午间复盘：上午分时全量 + 板块 + 涨停/炸板 + 情绪。"""
    _d(date)
    return _mode_result(date, "noon", {
        "minute": fetch_minute_data("sh000001", date, period=1, end_time="11:30",
                                    tdx_path=tdx_path),
        "sectors": fetch_sectors(date),
        "zt_pool": fetch_zt_pool(date),
        "breadth": fetch_market_breadth(date),
        "sentiment": fetch_sentiment(date, tdx_path=tdx_path),
    })


def fetch_intraday_pm(date: str, tdx_path: Optional[str] = None) -> dict:
    """下午盘中：全天分时（截至当前）+ 板块资金 + 涨停梯队/炸板 + 情绪。"""
    _d(date)
    return _mode_result(date, "intraday_pm", {
        "minute": fetch_minute_data("sh000001", date, period=1,
                                    end_time=_mode_end_time("intraday_pm"), tdx_path=tdx_path),
        "sectors": fetch_sectors(date),
        "zt_pool": fetch_zt_pool(date),
        "breadth": fetch_market_breadth(date),
        "sentiment": fetch_sentiment(date, tdx_path=tdx_path),
    })


def fetch_close(date: str, tdx_path: Optional[str] = None) -> dict:
    """收盘复盘（默认）：全天行情：指数/涨跌停/板块/情绪/资金/龙虎榜/资讯。"""
    _d(date)
    return _mode_result(date, "close", {
        "index_trend": fetch_index_trend(date, days=60, tdx_path=tdx_path),
        "minute": fetch_minute_data("sh000001", date, period=1, end_time="15:00",
                                    tdx_path=tdx_path),
        "zt_pool": fetch_zt_pool(date),
        "sectors": fetch_sectors(date),
        "breadth": fetch_market_breadth(date),
        "sentiment": fetch_sentiment(date, tdx_path=tdx_path),
        "lhb": fetch_lhb(date),
        "news": fetch_news_headlines(date),
    })


_MODE_NODES = {
    "pre_market": fetch_pre_market,
    "auction": fetch_auction,
    "intraday_am": fetch_intraday_am,
    "noon": fetch_noon,
    "intraday_pm": fetch_intraday_pm,
    "close": fetch_close,
}


def fetch_mode_data(mode: str, date: str, tdx_path: Optional[str] = None) -> dict:
    """按模式分发取数（FastAPI/引擎统一入口）。"""
    if mode not in _MODE_NODES:
        raise ValueError(f"无效模式：{mode}（可选：{', '.join(_MODE_NODES)}）")
    return _MODE_NODES[mode](date, tdx_path=tdx_path)


# ═══════════════════════════════════════════════════════════════
# LangChain @tool 包装（返回 JSON 字符串，供 graph.py 调用）
# ═══════════════════════════════════════════════════════════════

@tool
def get_market_micro(date: str) -> str:
    """获取 A 股当日微观数据：指数、涨停/跌停/炸板、涨跌家数、板块排名。

    参数: date: 日期 YYYY-MM-DD
    返回: JSON 字符串（比率均为小数，缺失为 null）
    """
    return _dumps(fetch_market_micro(date))


@tool
def get_index_trend(date: str, days: int = 60) -> str:
    """三大指数近 N 日走势 + 多周期均线（5/10/13/20/34/60/144/250）。

    参数: date: 截止日期 YYYY-MM-DD; days: 日线条数
    返回: JSON 字符串
    """
    return _dumps(fetch_index_trend(date, days=days))


@tool
def get_market_macro(date: str) -> str:
    """宏观/外围数据：纳指、富时A50、股指期货（仅当天有效）。"""
    return _dumps(fetch_market_macro(date))


@tool
def get_stock_info(code: str, date: str) -> str:
    """查询个股基本信息、近期走势与均线（Tushare→东财→通达信本地降级）。"""
    return _dumps(fetch_stock_info(code, date))


@tool
def get_sentiment(date: str) -> str:
    """情绪指标：昨日涨停今日平均收益/红盘率/连板率/核按钮率/炸板率/涨跌家数比。"""
    return _dumps(fetch_sentiment(date))


@tool
def get_news_headlines(date: str) -> str:
    """当日财经头条（财新/央视/经济日历）。"""
    return _dumps(fetch_news_headlines(date))


@tool
def get_auction_quote(date: str) -> str:
    """竞价数据：高开幅度/竞价金额/抢筹砸盘方向（开盘啦→同花顺→东财推算）。"""
    return _dumps(fetch_auction_quote(date))


@tool
def get_minute_data(symbol: str, date: str, period: int = 1, end_time: str = "") -> str:
    """分时/分钟线（东财→通达信本地）。symbol 如 sh000001/600519，period 1/5 分钟。"""
    return _dumps(fetch_minute_data(symbol, date, period=period,
                                    end_time=end_time or None))


@tool
def get_lhb(date: str) -> str:
    """龙虎榜（东财→Tushare top_list）。"""
    return _dumps(fetch_lhb(date))


@tool
def get_pre_market(date: str) -> str:
    """早盘前决策模式取数：隔夜外盘 + 昨夜消息 + 指数日线均线。"""
    return _dumps(fetch_pre_market(date))


@tool
def get_auction(date: str) -> str:
    """竞价复盘模式取数。"""
    return _dumps(fetch_auction(date))


@tool
def get_intraday_am(date: str) -> str:
    """上午盘中模式取数：上午分时 + 板块 + 涨停/炸板 + 情绪。"""
    return _dumps(fetch_intraday_am(date))


@tool
def get_noon(date: str) -> str:
    """午间复盘模式取数。"""
    return _dumps(fetch_noon(date))


@tool
def get_intraday_pm(date: str) -> str:
    """下午盘中模式取数。"""
    return _dumps(fetch_intraday_pm(date))


@tool
def get_close(date: str) -> str:
    """收盘复盘模式取数（默认）：指数/涨跌停/板块/情绪/龙虎榜/资讯。"""
    return _dumps(fetch_close(date))


@tool
def get_mode_data(mode: str, date: str) -> str:
    """按 6 模式取数：pre_market/auction/intraday_am/noon/intraday_pm/close。"""
    try:
        return _dumps(fetch_mode_data(mode, date))
    except ValueError as exc:
        return _dumps({"error": str(exc), "mode": mode, "date": date})


@tool
def search_history(query: str) -> str:
    """搜索历史复盘记录（知识库检索）。"""
    return kb_search(query)


# ═══════════════════════════════════════════════════════════════
# 当日微观（旧工具语义聚合，供 graph.py 继续使用）
# ═══════════════════════════════════════════════════════════════

def fetch_market_micro(date: str, tdx_path: Optional[str] = None) -> dict:
    """当日微观聚合：指数/涨跌停/涨跌家数/板块（兼容旧字段名）。"""
    trend = fetch_index_trend(date, days=60, tdx_path=tdx_path)
    zt = fetch_zt_pool(date)
    breadth = fetch_market_breadth(date)
    sectors = fetch_sectors(date)
    degraded: list[str] = []
    for label, blk in (("指数", trend), ("涨跌停", zt), ("涨跌家数", breadth), ("板块", sectors)):
        if blk.get("degraded"):
            for r in blk.get("degraded_reason", []):
                degraded.append(f"{label}: {r}")
    return {
        "index": trend["indices"], "index_source": trend["source"],
        "zhangting": {"total": (zt.get("limit_up") or {}).get("count"),
                      "tier": (zt.get("limit_up") or {}).get("tier"),
                      "top_industries": (zt.get("limit_up") or {}).get("top_industries"),
                      "top20": (zt.get("limit_up") or {}).get("stocks"),
                      "note": zt.get("note")},
        "dieting": {"total": (zt.get("limit_down") or {}).get("count"),
                    "top10": (zt.get("limit_down") or {}).get("stocks"),
                    "note": zt.get("note")},
        "zhaban": zt.get("zhaban"), "zhaban_rate": zt.get("zhaban_rate"),
        "market_breadth": {k: breadth.get(k) for k in
                           ("total", "up", "down", "flat", "up_down_ratio",
                            "limit_up", "limit_down", "zhaban", "touched",
                            "zhaban_rate", "distribution")},
        "sectors": sectors,
        "degraded": degraded,
        "sources": {"index": trend["source"], "zhangting": zt["source"],
                    "breadth": breadth["source"], "sectors": sectors["source"]},
    }
