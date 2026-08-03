"""竞价数据降级链测试：开盘啦 Cookie → 同花顺 → 东财推算 → 缺失标注。"""

import sys
from pathlib import Path

import pandas as pd
import pytest

SRC = Path(__file__).parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import stock_review_crew.tools.stock_data as sd  # noqa: E402

from test_data_common import clear_module_caches, make_zt_pool_df  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    clear_module_caches(sd)
    monkeypatch.setattr(sd.config, "KAIPANLA_COOKIE", None)
    monkeypatch.setattr(sd, "_http_get_json", _raise_http)
    monkeypatch.setattr(sd, "_em_zt_pool_previous", lambda date: pd.DataFrame())
    monkeypatch.setattr(sd, "_em_zt_pool", lambda date: pd.DataFrame())
    monkeypatch.setattr(sd, "_em_spot", lambda: pd.DataFrame())
    monkeypatch.setattr(sd, "_em_pre_min", lambda symbol: _raise_http())


def _raise_http(*args, **kwargs):
    raise RuntimeError("模拟网络不可用")


def test_no_cookie_silent_degrade_to_missing():
    """无 Cookie：静默跳过开盘啦，全部失败 → 数据缺失标注，不抛异常。"""
    out = sd.fetch_auction_quote("2026-07-31")
    assert out["source"] == "数据缺失"
    assert out["degraded"] is True
    assert out["count"] is None
    assert out["stocks"] == []
    reasons = "；".join(out["degraded_reason"])
    assert "未配置 KAIPANLA_COOKIE" in reasons
    assert "开盘啦" in reasons


def test_kaipanla_success_with_cookie(monkeypatch):
    """配置 Cookie 且开盘啦成功：主源数据，degraded=False，比率小数。"""
    monkeypatch.setattr(sd.config, "KAIPANLA_COOKIE", "test_cookie=1")
    calls: list[dict] = []

    def fake_http(url, params=None, headers=None, timeout=15):
        calls.append({"url": url, "params": params, "headers": headers})
        return [
            {"代码": "600519", "名称": "贵州茅台", "竞价涨幅": 1.25, "竞价金额": 123456789.0},
            {"代码": "000001", "名称": "平安银行", "竞价涨幅": -0.5, "竞价金额": 50000000.0},
        ]

    monkeypatch.setattr(sd, "_http_get_json", fake_http)
    out = sd.fetch_auction_quote("2026-07-31")
    assert out["source"] == "开盘啦"
    assert out["degraded"] is False
    assert out["count"] == 2
    assert out["high_open_count"] == 1
    assert out["low_open_count"] == 1
    assert out["stocks"][0]["pct_change"] == pytest.approx(0.0125)
    assert out["stocks"][0]["amount"] == pytest.approx(123456789.0)
    assert calls[0]["headers"]["Cookie"] == "test_cookie=1"


def test_kaipanla_failure_silent_fallback_ths(monkeypatch):
    """Cookie 失败静默降级到同花顺竞价（不抛异常）。"""
    monkeypatch.setattr(sd.config, "KAIPANLA_COOKIE", "expired=1")
    seen: list[str] = []

    def fake_http(url, params=None, headers=None, timeout=15):
        seen.append(url)
        if sd.THS_AUCTION_URL in url:
            return [{"code": "300750", "name": "宁德时代", "jjzf": 2.0, "jjje": 8e8}]
        raise RuntimeError("开盘啦失败")

    monkeypatch.setattr(sd, "_http_get_json", fake_http)
    out = sd.fetch_auction_quote("2026-07-31")
    assert out["source"] == "同花顺竞价"
    assert out["degraded"] is True
    assert out["stocks"][0]["pct_change"] == pytest.approx(0.02)
    assert "降级同花顺" in "；".join(out["degraded_reason"])


def test_em_infer_fallback(monkeypatch):
    """开盘啦/同花顺失败 → 东财 09:25 竞价柱推算（估算，带方向）。"""
    monkeypatch.setattr(sd.config, "KAIPANLA_COOKIE", "cookie=1")
    monkeypatch.setattr(sd, "_http_get_json", _raise_http)
    yesterday_pool = make_zt_pool_df([
        {"代码": "600519", "名称": "贵州茅台", "成交额": 9e9, "涨跌幅": 10.0},
        {"代码": "300750", "名称": "宁德时代", "成交额": 8e9, "涨跌幅": 20.0},
    ])
    monkeypatch.setattr(sd, "_em_zt_pool_previous", lambda date: yesterday_pool)
    spot = pd.DataFrame([
        {"代码": "600519", "名称": "贵州茅台", "昨收": 1500.0, "最新价": 1510.0},
        {"代码": "300750", "名称": "宁德时代", "昨收": 200.0, "最新价": 201.0},
    ])
    monkeypatch.setattr(sd, "_em_spot", lambda: spot)

    def fake_pre_min(symbol):
        if symbol == "600519":
            return pd.DataFrame([
                {"时间": "09:20", "开盘": 1500.0, "收盘": 1500.0, "最高": 1500.0,
                 "最低": 1500.0, "成交量": 100, "成交额": 1e7},
                {"时间": "09:25", "开盘": 1515.0, "收盘": 1515.0, "最高": 1515.0,
                 "最低": 1515.0, "成交量": 200, "成交额": 2e7},
            ])
        return pd.DataFrame([
            {"时间": "09:25", "开盘": 202.0, "收盘": 202.0, "最高": 202.0,
             "最低": 202.0, "成交量": 50, "成交额": 5e6},
        ])

    monkeypatch.setattr(sd, "_em_pre_min", fake_pre_min)
    out = sd.fetch_auction_quote("2026-07-31")
    assert out["source"] == "东财分时推算"
    assert out["degraded"] is True
    assert any("估算" in r for r in out["degraded_reason"])
    assert out["note"] and "估算" in out["note"]
    by_code = {r["code"]: r for r in out["stocks"]}
    assert by_code["600519"]["pct_change"] == pytest.approx(0.01)  # (1515-1500)/1500
    assert by_code["600519"]["direction"] == "抢筹"  # 09:25 成交额 > 09:20
    assert by_code["300750"]["pct_change"] == pytest.approx(0.01)  # (202-200)/200
    assert by_code["300750"]["amount"] == pytest.approx(5e6)


def test_all_sources_fail_marked_missing(monkeypatch):
    """全失败：返回缺失标注，绝不抛给上层。"""
    out = sd.fetch_auction_quote("2026-07-31")
    assert out["source"] == "数据缺失"
    reasons = "；".join(out["degraded_reason"])
    assert "同花顺竞价" in reasons and "东财推算" in reasons


def test_auction_mode_node_contains_block():
    """auction 模式节点：block 携带 auction 数据块。"""
    out = sd.fetch_auction("2026-07-31")
    assert out["mode"] == "auction"
    assert out["mode_label"] == "竞价复盘"
    assert "auction" in out["blocks"]
    assert out["blocks"]["auction"]["source"] == "数据缺失"
    assert out["degraded_flag"] is True


def test_auction_tool_wrapper_json():
    """@tool 接口返回可解析 JSON 字符串。"""
    result = sd.get_auction_quote.invoke({"date": "2026-07-31"})
    import json
    obj = json.loads(result)
    assert obj["source"] == "数据缺失"
    assert obj["degraded"] is True
