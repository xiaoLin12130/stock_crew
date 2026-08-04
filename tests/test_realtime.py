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
