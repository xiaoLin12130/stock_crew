# -*- coding: utf-8 -*-
"""I4 历史存储单元测试：data/reviews 读写删 / 列表 / 上下文注入 / 路径安全。

全部测试通过 STOCK_REVIEW_DATA_DIR 指向 pytest 临时目录隔离，绝不触碰仓库 data/。
"""

import json
import shutil
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from stock_review_crew.storage.reviews import (  # noqa: E402
    DEFAULT_DISCLAIMER,
    MODE_LABELS,
    context,
    delete_review,
    get_review,
    list_reviews,
    reviews_root,
    save_review,
)


@pytest.fixture()
def iso_dir():
    """隔离临时目录。

    注意：本环境（沙箱）下以 0o700 权限位创建的目录后续不可再访问
    （pytest 的 tmp_path 正是如此），因此用默认权限位自建并事后清理。
    """
    base = Path(__file__).resolve().parent.parent
    root = base / f".iso_{uuid.uuid4().hex[:12]}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def store(iso_dir, monkeypatch):
    """隔离存储根目录。"""
    root = iso_dir / "reviews"
    monkeypatch.setenv("STOCK_REVIEW_DATA_DIR", str(root))
    return root


def _meta(date="2026-07-31", time="110150", mode="intraday_am", **extra):
    meta = {
        "date": date,
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "time": time,
        "created_at": "2026-07-31T11:01:50",
        "degraded": [],
        "sources": ["test"],
        "disclaimer": DEFAULT_DISCLAIMER,
        "summary": "测试摘要",
    }
    meta.update(extra)
    return meta


EMPTY_DETAIL = {"meta": {}, "report": "", "analyses": [], "debate_history": [], "snapshot": {}}


def test_save_and_get_roundtrip(store):
    meta = _meta()
    report = {"final_report": "# 测试报告", "analysts": [{"name": "阿狼"}], "debate_history": [{"round": 1}]}
    snapshot = {"index": {"close": 3832.26}}
    record_id = save_review(meta, report, snapshot)

    assert record_id == "2026-07-31_110150"
    entry = store / "2026-07-31" / "110150"
    assert (entry / "meta.json").is_file()
    assert (entry / "report.json").is_file()
    assert (entry / "snapshot.json").is_file()

    detail = get_review("2026-07-31", "110150")
    assert detail["meta"]["date"] == "2026-07-31"
    assert detail["meta"]["summary"] == "测试摘要"
    assert detail["report"] == "# 测试报告"
    assert detail["analyses"] == [{"name": "阿狼"}]
    assert detail["debate_history"] == [{"round": 1}]
    assert detail["snapshot"] == {"index": {"close": 3832.26}}

    stored_meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
    for key in ("date", "mode", "mode_label", "time", "created_at", "degraded", "sources", "disclaimer", "summary"):
        assert key in stored_meta, f"meta 缺少契约键：{key}"


def test_save_string_report_and_defaults(store):
    record_id = save_review({"date": "2026-07-31", "time": "093000"}, "# 纯文本报告")
    assert record_id == "2026-07-31_093000"
    detail = get_review("2026-07-31", "093000")
    assert detail["report"] == "# 纯文本报告"
    assert detail["analyses"] == []
    assert detail["meta"]["mode"] == "close"
    assert detail["meta"]["mode_label"] == "收盘复盘"
    assert detail["meta"]["disclaimer"] == DEFAULT_DISCLAIMER
    assert detail["meta"]["degraded"] == []
    assert detail["meta"]["sources"] == []


def test_save_none_snapshot_writes_empty(store):
    save_review(_meta(time="100000"), "报告", None)
    snapshot = json.loads((store / "2026-07-31" / "100000" / "snapshot.json").read_text(encoding="utf-8"))
    assert snapshot == {}


def test_same_day_multiple_conflict_suffix(store):
    for _ in range(3):
        save_review(_meta(time="110150"))
    assert (store / "2026-07-31" / "110150").is_dir()
    assert (store / "2026-07-31" / "110150-2").is_dir()
    assert (store / "2026-07-31" / "110150-3").is_dir()
    record_id = save_review(_meta(time="110150"))
    assert record_id == "2026-07-31_110150-4"
    # 后缀记录可正常读取
    assert get_review("2026-07-31", "110150-2")["meta"]["time"] == "110150-2"


def test_save_defaults_date_time_now(store):
    record_id = save_review({})
    date, time = record_id.split("_")
    assert (store / date / time).is_dir()
    assert len(time) == 6 and time.isdigit()


def test_save_invalid_meta_raises_chinese(store):
    with pytest.raises(ValueError, match="日期格式"):
        save_review({"date": "2026/07/31", "time": "110150"})
    with pytest.raises(ValueError, match="时间格式"):
        save_review({"date": "2026-07-31", "time": "11:01:50"})


def test_list_grouping_and_order(store):
    save_review(_meta(date="2026-07-31", time="180502", mode="close", summary="收盘"))
    save_review(_meta(date="2026-07-31", time="103813", mode="intraday_am", summary="上午"))
    save_review(_meta(date="2026-07-30", time="170431", mode="close", summary="昨日"))

    groups = list_reviews()
    assert [g["date"] for g in groups] == ["2026-07-31", "2026-07-30"]
    assert [i["time"] for i in groups[0]["items"]] == ["180502", "103813"]
    item = groups[0]["items"][0]
    assert set(item) == {"record_id", "mode", "mode_label", "time", "created_at", "summary"}
    assert item["record_id"] == "2026-07-31_180502"
    assert item["mode"] == "close"
    assert item["mode_label"] == "收盘复盘"
    assert item["summary"] == "收盘"


def test_list_empty_when_no_root(store):
    assert list_reviews() == []


def test_list_corrupt_meta_fallback(store):
    entry = store / "2026-07-31" / "120000"
    entry.mkdir(parents=True)
    (entry / "meta.json").write_text("{broken", encoding="utf-8")
    groups = list_reviews()
    assert len(groups) == 1
    item = groups[0]["items"][0]
    assert item["time"] == "120000"
    assert item["mode"] == "close"
    assert item["mode_label"] == "收盘复盘"


def test_get_missing_and_corrupt(store):
    assert get_review("2026-07-31", "000000") == EMPTY_DETAIL

    entry = store / "2026-07-31" / "120000"
    entry.mkdir(parents=True)
    (entry / "meta.json").write_text("{bad", encoding="utf-8")
    (entry / "report.json").write_text("[1,2", encoding="utf-8")
    (entry / "snapshot.json").write_text("null", encoding="utf-8")
    detail = get_review("2026-07-31", "120000")
    assert detail["meta"] == {}
    assert detail["report"] == ""
    assert detail["snapshot"] == {}
    assert detail["analyses"] == []
    assert detail["debate_history"] == []

    # report.json 为裸字符串时按 final_report 返回
    (entry / "report.json").write_text(json.dumps("裸字符串报告", ensure_ascii=False), encoding="utf-8")
    assert get_review("2026-07-31", "120000")["report"] == "裸字符串报告"


def test_delete_review(store):
    save_review(_meta(time="110150"))
    assert (store / "2026-07-31" / "110150").is_dir()
    assert delete_review("2026-07-31", "110150") is True
    assert not (store / "2026-07-31" / "110150").exists()
    assert delete_review("2026-07-31", "110150") is False
    assert list_reviews() == []


@pytest.mark.parametrize(
    "date,time",
    [
        ("../../etc", "110150"),
        ("2026-07-31", ".."),
        ("2026-07-31", "../secret"),
        ("2026-07-31", "110150/../x"),
        ("2026-13-99", "110150"),
        ("2026-02-30", "110150"),
        ("2026-07-31", "999999"),
        ("2026-07-31", "123456-0"),
        ("2026-07-31", "12345"),
        ("2026-07-31", "123456-"),
        ("2026-07-31", "123456-abc"),
    ],
)
def test_invalid_ids_rejected(date, time, store):
    assert get_review(date, time) == EMPTY_DETAIL
    assert delete_review(date, time) is False


def test_context_empty(store):
    assert context("2026-07-31") == {"yesterday": None, "earlier_today": []}
    assert context("bad-date") == {"yesterday": None, "earlier_today": []}
    assert context("2026-13-01") == {"yesterday": None, "earlier_today": []}


def test_context_yesterday_close_preferred(store):
    save_review(_meta(date="2026-07-30", time="143000", mode="intraday_pm", summary="午后"))
    save_review(_meta(date="2026-07-30", time="150500", mode="close", summary="收盘"))
    save_review(_meta(date="2026-07-29", time="160000", mode="close", summary="更早收盘"))
    save_review(_meta(date="2026-07-31", time="103813", mode="intraday_am", summary="当日上午"))
    save_review(_meta(date="2026-07-31", time="180502", mode="close", summary="当日收盘"))

    ctx = context("2026-07-31")
    assert ctx["yesterday"]["date"] == "2026-07-30"
    assert ctx["yesterday"]["time"] == "150500"
    assert ctx["yesterday"]["mode"] == "close"
    assert ctx["yesterday"]["summary"] == "收盘"
    assert [i["time"] for i in ctx["earlier_today"]] == ["180502", "103813"]
    assert all(i["date"] == "2026-07-31" for i in ctx["earlier_today"])


def test_context_yesterday_fallback_any_mode(store):
    save_review(_meta(date="2026-07-30", time="143000", mode="intraday_pm", summary="午后"))
    save_review(_meta(date="2026-07-28", time="160000", mode="close", summary="更早的收盘"))
    ctx = context("2026-07-31")
    # close 全局优先：最近日期无 close 时，取更早日期上的最近 close
    assert ctx["yesterday"]["mode"] == "close"
    assert ctx["yesterday"]["date"] == "2026-07-28"
    assert ctx["yesterday"]["time"] == "160000"

    # 早于该日期全部无 close → 任意模式兜底
    delete_review("2026-07-28", "160000")
    ctx2 = context("2026-07-31")
    assert ctx2["yesterday"]["mode"] == "intraday_pm"
    assert ctx2["yesterday"]["time"] == "143000"


def test_context_skips_same_date_and_corrupt(store):
    save_review(_meta(date="2026-07-31", time="110150", mode="close", summary="当日"))
    ctx = context("2026-07-31")
    assert ctx["yesterday"] is None
    assert len(ctx["earlier_today"]) == 1

    # 昨日 meta 损坏：兜底仍返回该条目（mode 取 close）
    entry = store / "2026-07-30" / "150000"
    entry.mkdir(parents=True)
    (entry / "meta.json").write_text("x", encoding="utf-8")
    ctx = context("2026-07-31")
    assert ctx["yesterday"]["time"] == "150000"
    assert ctx["yesterday"]["mode"] == "close"


def test_root_override_isolation(store):
    repo_reviews = Path(__file__).resolve().parent.parent / "data" / "reviews"
    before = {p.name for p in repo_reviews.iterdir()} if repo_reviews.is_dir() else None
    save_review(_meta())
    assert reviews_root() == store
    after = {p.name for p in repo_reviews.iterdir()} if repo_reviews.is_dir() else None
    assert after == before, "隔离失败：仓库 data/reviews 不应被本测试写入"
