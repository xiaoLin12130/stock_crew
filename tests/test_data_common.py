"""数据层测试共享工具（无测试用例，仅供 test_data_*.py 导入）。

注意：本环境沙箱禁止写入 tempfile 创建的目录（WinError 5/13），
因此临时目录一律用 os.makedirs 显式创建于仓库内 .pytest_tmp/，进程退出时清理。
"""

import atexit
import json
import os
import shutil
from pathlib import Path

import pandas as pd


_TMP_ROOT = Path(__file__).parent / ".pytest_tmp"


def fresh_tmp_dir(name: str) -> Path:
    d = _TMP_ROOT / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup_tmp_root() -> None:
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


atexit.register(cleanup_tmp_root)


def seed_cache_block(cache_dir: Path, date: str, block: str, payload: dict) -> Path:
    d = cache_dir / date
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"block_{block}.json"
    f.write_text(json.dumps({
        "schema_version": 2, "date": date, "block": block,
        "saved_at": "2026-08-03T00:00:00", "data": payload,
    }, ensure_ascii=False), encoding="utf-8")
    return f


def seed_legacy_market_micro(cache_dir: Path, date: str) -> Path:
    d = cache_dir / date
    d.mkdir(parents=True, exist_ok=True)
    f = d / "market_micro.json"
    f.write_text(json.dumps({
        "index": {"shanghai": {"close": 3832.26, "open": 3833.54, "high": 3847.09,
                               "low": 3822.37, "pct_change": 0.72, "volume": 597529427.0,
                               "volume_prev": 592298923.0, "volume_vs_prev_pct": 0.9}},
        "zhangting": {"total": 5, "tier": {"首板": 4, "2": 1},
                      "top_industries": {"软件": 2}, "top20": [{"代码": "600000", "名称": "示例", "涨跌幅": 10.0}]},
        "dieting": {"total": 0, "top10": []},
        "market_breadth": {"total_volume": 1567603726.0},
    }, ensure_ascii=False), encoding="utf-8")
    return f


def seed_legacy_sectors(cache_dir: Path, date: str) -> Path:
    d = cache_dir / date
    d.mkdir(parents=True, exist_ok=True)
    f = d / "sectors.json"
    f.write_text(json.dumps({
        "top5": [{"板块": "软件", "涨跌幅": 6.29, "成交额": 1e10, "成交额变化": 45.2}],
        "bottom5": [{"板块": "保险", "涨跌幅": -0.7, "成交额": 1e9, "成交额变化": -6.2}],
        "source": "同花顺(历史)",
    }, ensure_ascii=False), encoding="utf-8")
    return f


def make_zt_pool_df(rows: list[dict]) -> pd.DataFrame:
    """东财涨停/炸板/跌停池风格 DataFrame。"""
    cols = ["代码", "名称", "涨跌幅", "最新价", "涨停价", "换手率", "连板数",
            "所属行业", "成交额", "封板资金", "首次封板时间", "最后封板时间"]
    data = []
    for r in rows:
        row = {c: r.get(c) for c in cols}
        row.update({k: v for k, v in r.items() if k not in cols})
        data.append(row)
    return pd.DataFrame(data)


def make_ts_daily_df(rows: list[dict]) -> pd.DataFrame:
    """Tushare daily 风格 DataFrame。"""
    cols = ["ts_code", "trade_date", "pre_close", "open", "high", "low", "close",
            "pct_chg", "vol", "amount"]
    data = []
    for r in rows:
        row = {c: r.get(c) for c in cols}
        row.update({k: v for k, v in r.items() if k not in cols})
        data.append(row)
    return pd.DataFrame(data)


def clear_module_caches(sd) -> None:
    """清理数据层模块级缓存，避免测试间串扰。"""
    sd._ts_name_cache.clear()
