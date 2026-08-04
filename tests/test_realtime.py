"""实时数据快照测试：结构契约 / 降级链 / 端点（全离线，monkeypatch）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import backend.main as bm  # noqa: E402
from stock_review_crew.tools import realtime  # noqa: E402

client = TestClient(bm.app)


def _fake_indices():
    return {
        "indices": [
            {"name": "上证指数", "price": 3806.79, "pct_change": -0.0008,
             "open": 3816.37, "high": 3818.27, "low": 3799.52, "source": "东财实时"}
        ],
        "source": "东财实时", "degraded": False, "degraded_reason": [],
    }


def _fake_zt(*a, **k):
    return {
        "limit_up_count": 67, "limit_down_count": 0, "zhaban_count": 11,
        "touched_count": 78, "zhaban_rate": 0.141, "seal_rate": 0.859,
        "yesterday_zt_count": 75, "tier": {"首板": 42, "2板及以上": 25},
        "source": "同花顺实时", "degraded": False, "degraded_reason": [], "units": {},
    }


def _fake_sectors():
    return {
        "top": [{"name": "电子", "pct_change": 0.0309, "net_inflow": 1e10}],
        "bottom": [], "flow_in": [], "flow_out": [],
        "source": "东财实时", "degraded": False, "degraded_reason": [], "units": {},
    }


def _fake_news():
    return {"news": [{"time": "2026-08-04 11:00", "text": "测试快讯", "source": "新浪7x24"}],
            "source": "新浪7x24", "degraded": False, "degraded_reason": []}


def test_snapshot_structure(monkeypatch):
    monkeypatch.setattr(realtime, "fetch_indices", _fake_indices)
    monkeypatch.setattr(realtime, "fetch_zt_stats", _fake_zt)
    monkeypatch.setattr(realtime, "fetch_sector_flow", _fake_sectors)
    monkeypatch.setattr(realtime, "fetch_news", _fake_news)
    snap = realtime.fetch_realtime_snapshot()
    assert set(snap) >= {"status", "indices", "zt", "sectors", "news", "auction", "sources", "degraded"}
    assert snap["indices"]["indices"][0]["pct_change"] == pytest.approx(-0.0008)
    assert snap["zt"]["zhaban_rate"] == pytest.approx(0.141)
    assert snap["sectors"]["top"][0]["name"] == "电子"
    assert snap["news"]["news"][0]["text"] == "测试快讯"
    assert snap["sources"] == ["东财实时", "同花顺实时", "东财实时", "新浪7x24"]
    assert snap["degraded"] == []
    assert "updated_at" in snap


def test_snapshot_degrade_all(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("模拟离线")

    monkeypatch.setattr(realtime, "fetch_indices", _boom)
    monkeypatch.setattr(realtime, "fetch_indices_tx", _boom)
    monkeypatch.setattr(realtime, "fetch_zt_stats", _boom)
    monkeypatch.setattr(realtime, "fetch_sector_flow", _boom)
    monkeypatch.setattr(realtime, "fetch_news", _boom)
    snap = realtime.fetch_realtime_snapshot()
    assert snap["indices"] is None
    assert snap["zt"] is None
    assert snap["sectors"] is None
    assert snap["news"] is None
    assert len(snap["degraded"]) >= 4
    assert snap["auction"]["window"] in (True, False)


def test_realtime_endpoint(monkeypatch):
    monkeypatch.setattr(realtime, "fetch_realtime_snapshot", lambda: {"status": {"phase": "测试"}, "ok": True})
    r = client.get("/api/data/realtime")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"status": {"phase": "测试"}, "ok": True}


def test_stock_quote(monkeypatch):
    fake = {
        "data": {
            "f43": 3806.79, "f44": 3818.27, "f45": 3799.52, "f46": 3816.37,
            "f47": 100, "f48": 1e8, "f57": "600519", "f58": "贵州茅台",
            "f60": 3809.66, "f169": 0.55, "f170": -0.08,
        }
    }
    monkeypatch.setattr(realtime, "_http_json", lambda *a, **k: fake)
    q = realtime.fetch_stock_quote("600519")
    assert q["code"] == "600519"
    assert q["name"] == "贵州茅台"
    assert q["pct_change"] == pytest.approx(-0.0008)
    assert q["turnover_rate"] == pytest.approx(0.0055)


def test_quote_endpoint(monkeypatch):
    monkeypatch.setattr(
        realtime, "fetch_stock_quote",
        lambda code: {"code": code, "name": "测试股", "price": 10.0, "pct_change": 0.01},
    )
    r = client.get("/api/data/quote", params={"code": "600519"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "测试股"
    # 参数校验
    assert client.get("/api/data/quote", params={"code": "abc"}).status_code == 400
    assert client.get("/api/data/quote").status_code == 400
    # 未查到 → 404 中文
    def _boom(code):
        raise realtime.RealtimeError("未查询到该股票")

    monkeypatch.setattr(realtime, "fetch_stock_quote", _boom)
    assert client.get("/api/data/quote", params={"code": "999999"}).status_code == 404


def test_sector_flow_tx_fallback(monkeypatch):
    """东财失败 → 腾讯行业排行兜底（来源标注）。"""
    fake_rank = {
        "data": {
            "rank_list": [
                {"name": "食品饮料", "zdf": "-1.45", "zljlr": "-211.70",
                 "lzg": {"name": "莲花控股", "zdf": "10.05"}},
                {"name": "电子", "zdf": "3.09", "zljlr": "1417736.0",
                 "lzg": {"name": "沪电股份", "zdf": "6.5"}},
            ]
        }
    }

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return fake_rank

    monkeypatch.setattr(realtime, "_http_json", lambda *a, **k: (_ for _ in ()).throw(RealtimeError("东财 502")))
    monkeypatch.setattr(realtime.requests, "get", lambda *a, **k: _Resp())
    b = realtime.fetch_sector_flow()
    assert b["source"] == "腾讯行业"
    assert b["top"][0]["name"] == "电子"
    assert b["top"][0]["pct_change"] == pytest.approx(0.0309)
    assert b["bottom"][0]["name"] == "食品饮料"
    assert b["flow_in"][0]["net_inflow"] == pytest.approx(1417736.0 * 10000)


def test_search_stocks(monkeypatch):
    fake = {
        "QuotationCodeTable": {
            "Data": [
                {"Code": "600519", "Name": "贵州茅台", "MktNum": "1", "SecurityTypeName": "沪A"},
                {"Code": "000858", "Name": "五粮液", "MktNum": "0", "SecurityTypeName": "深A"},
            ],
            "TotalCount": 2,
        }
    }
    monkeypatch.setattr(realtime, "_http_json", lambda *a, **k: fake)
    out = realtime.search_stocks("茅台")
    assert out[0] == {"code": "600519", "name": "贵州茅台", "market": "sh", "type": "沪A"}
    assert out[1]["market"] == "sz"
    # 6 位代码直接返回
    out2 = realtime.search_stocks("600519")
    assert out2[0]["code"] == "600519"


def test_search_endpoint(monkeypatch):
    monkeypatch.setattr(
        realtime, "search_stocks",
        lambda q: [{"code": "600519", "name": "贵州茅台", "market": "sh", "type": "GP"}],
    )
    r = client.get("/api/data/search", params={"q": "茅台"})
    assert r.status_code == 200, r.text
    assert r.json()[0]["code"] == "600519"
    assert client.get("/api/data/search", params={"q": ""}).status_code == 400
    assert client.get("/api/data/search").status_code == 400
