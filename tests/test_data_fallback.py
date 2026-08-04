"""R4 复盘链备用源单测（全离线 mock，覆盖 指数/分时/板块/资讯 兜底解析）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import requests  # noqa: E402
import pytest  # noqa: E402

from stock_review_crew.tools import realtime  # noqa: E402
from stock_review_crew.tools import stock_data as sd  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_records_from_em_kline(monkeypatch):
    payload = {
        "data": {
            "klines": [
                "2026-07-31,3833.54,3832.26,3847.09,3822.37,597529427,1187681546393.30",
                "2026-08-04,3816.37,3806.24,3818.27,3799.52,301852224,554765075968.70",
            ]
        }
    }
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(payload))
    rows = sd._records_from_em_kline("sh000001")
    assert rows[-1]["date"] == "2026-08-04"
    assert rows[-1]["close"] == pytest.approx(3806.24)
    assert rows[-1]["open"] == pytest.approx(3816.37)
    assert rows[-1]["high"] == pytest.approx(3818.27)
    assert rows[-1]["low"] == pytest.approx(3799.52)


def test_records_from_tx_kline(monkeypatch):
    payload = {
        "data": {
            "sh000001": {
                "day": [
                    ["2026-07-31", "3833.54", "3832.26", "3847.09", "3822.37", "597529427"],
                    ["2026-08-04", "3816.37", "3806.24", "3818.27", "3799.52", "301852224"],
                ]
            }
        }
    }
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(payload))
    rows = sd._records_from_tx_kline("sh000001")
    assert len(rows) == 2
    assert rows[-1]["close"] == pytest.approx(3806.24)


def test_minute_bars_from_tx(monkeypatch):
    payload = {
        "data": {
            "sh000001": {
                "data": {
                    "date": "20260804",
                    "data": [
                        "0930 3816.37 123456 9876543210.00",
                        "0931 3815.00 45678 3456789012.00",
                        "1130 3809.66 99999 8888888888.00",
                    ],
                }
            }
        }
    }
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(payload))
    bars = sd._minute_bars_from_tx("sh000001", "2026-08-04", 1, "09:31:00")
    assert len(bars) == 2
    assert bars[0]["time"] == "0930"
    assert bars[-1]["time"] == "0931"
    assert bars[0]["close"] == pytest.approx(3816.37)
    # 日期不匹配（历史补做）→ 明确报错，禁止用错误日期数据
    with pytest.raises(sd.DataSourceError, match="仅当日可用"):
        sd._minute_bars_from_tx("sh000001", "2026-07-31", 1, "15:00:00")


def test_sectors_from_tx_today(monkeypatch):
    fake = {
        "top": [{"name": "电子", "pct_change": 0.0309, "net_inflow": 1.4e10,
                 "leading_stock": "沪电股份", "leading_pct_change": 0.065}],
        "bottom": [], "flow_in": [], "flow_out": [],
        "source": "腾讯行业", "degraded": False, "degraded_reason": [], "units": {},
    }
    monkeypatch.setattr(realtime, "_sector_flow_from_tx", lambda: fake)
    b = sd._sectors_from_tx_today()
    assert b["source"] == "腾讯行业"
    assert b["top5"][0]["name"] == "电子"
    assert b["top5"][0]["pct_change"] == pytest.approx(0.0309)
    assert b["top5"][0]["leading_stock"] == "沪电股份"


def test_news_sina_fallback(monkeypatch):
    monkeypatch.setattr(
        realtime, "fetch_news",
        lambda: {"news": [{"time": "11:00", "text": "测试快讯", "source": "新浪7x24"}],
                 "source": "新浪7x24", "degraded": False, "degraded_reason": []},
    )
    # 财新/央视全部失败 → 降级新浪7x24 并注明
    monkeypatch.setattr(sd, "_news_caixin", lambda: (_ for _ in ()).throw(RuntimeError("失败")))
    monkeypatch.setattr(sd, "_news_cctv", lambda d: (_ for _ in ()).throw(RuntimeError("失败")))
    monkeypatch.setattr(sd, "_news_economic_baidu", lambda d: (_ for _ in ()).throw(RuntimeError("失败")))
    result = sd.fetch_news_headlines("2026-08-04")
    assert result["realtime"][0]["text"] == "测试快讯"
    assert "新浪7x24" in result["note"]
