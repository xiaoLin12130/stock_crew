"""I2 复盘引擎测试：6 模式取数分发、全流程降级、进度钩子、辩论上限、结果契约。"""

import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

import stock_review_crew.graph as graph
from stock_review_crew.graph import (
    build_graph,
    build_result,
    default_initial_state,
    ensure_disclaimer,
)


def _fake_data():
    return {
        "date": "2026-08-03",
        "index": {
            "shanghai": {"close": 3764.0, "pct_change": 0.0052},
            "shenzhen": {"close": 12000.0, "pct_change": -0.0012},
        },
        "zhangting": {"total": 47, "tier": {"首板": 40, "2": 5, "3": 2}},
        "dieting": {"total": 3},
        "sectors": {"top5": [{"板块": "半导体", "涨跌幅": 0.02}, {"板块": "军工", "涨跌幅": 0.015}]},
        "sentiment": {
            "yesterday_zt_count": 60,
            "avg_return_pct": 1.2,
            "red_rate_pct": 55.0,
            "lianban_rate_pct": 8.0,
        },
        "breadth": {"up": 2800, "down": 1800, "zhaban_rate_pct": 18.0},
        "news": {"caixin": [{"summary": "政策利好"}, {"summary": "行业监管"}], "cctv": ["要闻一"]},
    }


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """离线约束：无 Key 走规则引擎；LLM 调用必须 mock。"""
    monkeypatch.setattr(graph, "_llm_ready", lambda: False)
    monkeypatch.setattr(graph, "_callbacks", lambda: [])
    monkeypatch.setattr(graph, "_load_analyst_tools", lambda: (None, None))


@pytest.fixture()
def fetch_mode(monkeypatch):
    def _set(fn):
        monkeypatch.setattr(graph, "_load_pure_fetcher", lambda: fn)
    return _set


def _invoke(mode="close", max_rounds=3, yesterday="", earlier="", callback=None):
    app = build_graph(progress_callback=callback)
    initial = default_initial_state(
        "2026-08-03",
        mode=mode,
        max_rounds=max_rounds,
        yesterday_report=yesterday,
        earlier_today=earlier,
    )
    return app.invoke(initial)


# ── 取数：模式分发与降级 ──

def test_fetch_dispatches_by_mode(fetch_mode):
    calls = []

    def fetcher(mode, date):
        calls.append((mode, date))
        return _fake_data()

    fetch_mode(fetcher)
    _invoke(mode="auction")
    assert calls == [("auction", "2026-08-03")]


def test_fetch_six_modes_no_block(fetch_mode):
    for mode in ("pre_market", "auction", "intraday_am", "noon", "intraday_pm", "close"):
        fetch_mode(lambda mode, date: _fake_data())
        state = _invoke(mode=mode)
        assert state["market_data"]
        assert state["snapshot"]["mode"] == mode
        assert state["snapshot"]["data"]


def test_i1_fetch_mode_data_shape_consumed(fetch_mode):
    """I1 契约结构 {mode_label, blocks, degraded[]} 直接消费：渲染/事实/降级标注。"""
    def fetcher(mode, date):
        return {
            "date": date,
            "mode": mode,
            "mode_label": "竞价复盘",
            "blocks": {
                "auction": {
                    "count": 120, "high_open_count": 80, "low_open_count": 40,
                    "source": "开盘啦", "degraded": False, "degraded_reason": [], "note": None,
                },
                "index_trend": {
                    "indices": {"shanghai": {"close": 3764.0, "pct_change": 0.0052}},
                    "source": "通达信akshare", "degraded": False, "degraded_reason": [],
                },
                "news": {"caixin": [{"summary": "政策利好"}], "cctv": [], "source": "财新"},
            },
            "degraded": ["auction: 开盘啦不可用"],
            "degraded_flag": True,
        }

    fetch_mode(fetcher)
    state = _invoke(mode="auction")
    assert state["snapshot"]["mode_label"] == "竞价复盘"
    assert any("开盘啦不可用" in n for n in state["degraded_notes"])
    assert state["degraded"] is True
    assert "来源：开盘啦" in state["market_data"]
    assert "政策利好" in state["market_data"]
    assert state["final_report"].startswith("# 2026-08-03 A股竞价复盘报告")
    # 规则事实：竞价 count 存在 → 不写“竞价数据缺失”；指数方向给出
    assert "指数方向" in state["final_report"]
    assert "竞价数据缺失" not in state["final_report"]


def test_fetch_unavailable_no_block(fetch_mode, monkeypatch):
    fetch_mode(None)
    monkeypatch.setattr(graph, "_legacy_fetch", lambda state: None)
    state = _invoke()
    assert "数据缺失" in state["market_data"] or "无数据" in state["market_data"]
    assert state["degraded"] is True
    assert state["degraded_notes"]
    assert state["final_report"]


def test_legacy_fallback_marks_degraded(fetch_mode, monkeypatch):
    fetch_mode(None)
    monkeypatch.setattr(graph, "_legacy_fetch", lambda state: {
        "market_data": "## 一、今日市场微观数据\n{}",
        "raw_news": "{}",
    })
    state = _invoke()
    assert "今日市场微观数据" in state["market_data"]
    assert state["degraded"] is True


def test_legacy_fetch_has_timeout_budget(monkeypatch):
    """旧工具兜底有总预算：慢工具不阻塞主流程（注入假模块 + 缩短预算）。"""
    fake = types.ModuleType("stock_review_crew.tools.stock_data")

    def _slow(**kwargs):
        time.sleep(5)
        return "{}"

    for name in (
        "get_market_micro", "get_index_trend", "get_market_macro",
        "get_sentiment", "get_news_headlines", "get_stock_info", "search_history",
    ):
        setattr(fake, name, _slow)

    saved = sys.modules.get("stock_review_crew.tools.stock_data")
    sys.modules["stock_review_crew.tools.stock_data"] = fake
    old_timeout = graph._LEGACY_FETCH_TIMEOUT
    graph._LEGACY_FETCH_TIMEOUT = 0.3
    try:
        start = time.time()
        out = graph._legacy_fetch({"date": "2026-08-03"})
        elapsed = time.time() - start
    finally:
        graph._LEGACY_FETCH_TIMEOUT = old_timeout
        if saved is not None:
            sys.modules["stock_review_crew.tools.stock_data"] = saved
        else:
            sys.modules.pop("stock_review_crew.tools.stock_data", None)
    assert out is None
    assert elapsed < 2


def test_news_empty_not_block(fetch_mode):
    fetch_mode(lambda mode, date: {"index": {"shanghai": {"pct_change": 0.001}}})
    state = _invoke()
    assert "今日无重大资讯影响" in state["market_data"]


def test_news_rule_extraction_marks_degraded(fetch_mode):
    fetch_mode(lambda mode, date: {"news": {"caixin": [{"summary": "降息预期升温"}], "cctv": ["央行发布会"]}})
    state = _invoke()
    assert "降息预期升温" in state["market_data"]
    assert "降级" in state["market_data"]
    assert state["degraded"] is True


# ── 全流程（无 Key 规则引擎降级）──

def test_full_flow_rule_engine_degraded(fetch_mode):
    fetch_mode(lambda mode, date: _fake_data())
    state = _invoke()
    assert len(state["analyses"]) == 5
    groups = [a["group"] for a in state["analyses"]]
    assert groups.count("trend") == 2
    assert groups.count("sentiment") == 3
    assert state["degraded"] is True
    assert state["degraded_notes"]
    assert "仅供参考，不构成投资建议" in state["final_report"]
    assert "无昨日报告，跳过验证" in state["final_report"]
    assert len(state["debate_history"]) <= 3


def test_yesterday_missing_vs_present(fetch_mode):
    fetch_mode(lambda mode, date: _fake_data())
    missing = _invoke(yesterday="")
    assert "无昨日报告，跳过验证" in missing["final_report"]

    present = _invoke(yesterday="昨日报告：预判看多", earlier="午间复盘：维持看多")
    assert "无昨日报告，跳过验证" not in present["final_report"]
    assert "未找到本人昨日预判，无法验证" in present["final_report"]
    assert present["earlier_today"] == "午间复盘：维持看多"


def test_debate_loop_respects_max_rounds(fetch_mode):
    fetch_mode(lambda mode, date: _fake_data())
    state = _invoke(max_rounds=1)
    assert len(state["debate_history"]) <= 1
    state3 = _invoke(max_rounds=3)
    assert len(state3["debate_history"]) <= 3
    assert state3["round_count"] <= 4


def test_progress_stages_monotonic(fetch_mode):
    fetch_mode(lambda mode, date: _fake_data())
    events = []
    _invoke(max_rounds=2, callback=lambda stage, pct, msg: events.append((stage, pct, msg)))

    stages = [e[0] for e in events]
    assert stages[0] == "fetch"
    for required in ("news", "trend", "sentiment", "host", "debate", "report", "done"):
        assert required in stages
    assert stages[-1] == "done"

    pcts = [e[1] for e in events]
    assert pcts == sorted(pcts)
    assert pcts[-1] == 100.0

    msgs = "|".join(m for _, _, m in events)
    assert "趋势派 1/2" in msgs and "趋势派 2/2" in msgs
    assert "情绪派 1/3" in msgs and "情绪派 3/3" in msgs
    assert "主持人判断" in msgs and "报告生成" in msgs and "复盘完成" in msgs


def test_all_six_modes_rule_flow(fetch_mode):
    for mode in ("pre_market", "auction", "intraday_am", "noon", "intraday_pm", "close"):
        fetch_mode(lambda mode, date: _fake_data())
        state = _invoke(mode=mode)
        result = build_result(state)
        assert result["final_report"]
        assert state["degraded"] is True


# ── LLM 路径（mock，禁止真调用）──

class _FakeResponse:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeLLM:
    def __init__(self, factory):
        self._factory = factory

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, config=None):
        return _FakeResponse(self._factory(messages))


def _llm_factory(messages):
    """按消息内容确定性返回 mock 回复（线程安全，无共享迭代器）。"""
    user = ""
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            user = m.get("content", "")
    if "## 资讯内容" in user:
        return "利多板块：无。"
    if "实质性分歧" in user:
        return "分歧判断：有\n讨论议题：多空方向分歧\n总结：观点分歧。"
    if "## 讨论议题" in user:
        return "（测试）我同意并补充：谨慎为主。"
    if "报告要求" in user:
        return "# 2026-08-03 A股收盘复盘报告\n## 一、市场概况\n略\n## 综合标签\n#测试标签A\n#测试标签B"
    return "## 1、昨日复盘验证\n无昨日报告，跳过验证\n## 2、今日判断（今日大盘判断）\n看多\n## 3、操作计划（明日计划）\n进攻"


def test_llm_path_mocked(fetch_mode, monkeypatch):
    fetch_mode(lambda mode, date: _fake_data())
    monkeypatch.setattr(graph, "_llm_ready", lambda: True)
    llm = _FakeLLM(_llm_factory)
    strict = _FakeLLM(_llm_factory)
    monkeypatch.setattr(graph, "_llm_pair", lambda: (llm, strict))

    state = _invoke(max_rounds=2)
    assert state["degraded"] is False
    assert state["final_report"].startswith("# 2026-08-03 A股收盘复盘报告")
    assert "仅供参考，不构成投资建议" in state["final_report"]
    assert state["overall_tags"] == ["测试标签A", "测试标签B"]
    assert len(state["debate_history"]) == 1
    assert state["debate_history"][0]["topic"] == "多空方向分歧"


# ── 结果契约（§五 result.report）与免责声明 ──

def test_build_result_structure():
    state = default_initial_state("2026-08-03")
    state["final_report"] = "正文"
    state["analyses"] = [{"skill_name": "阿狼", "analysis": "看多"}]
    state["debate_history"] = []
    state["overall_tags"] = ["降级模式"]
    state["degraded"] = True
    state["round_count"] = 2
    result = build_result(state)
    assert set(result.keys()) == {
        "final_report", "analyses", "debate_history", "overall_tags",
        "disclaimer", "degraded", "round_count",
    }
    assert result["disclaimer"] == "仅供参考，不构成投资建议"
    assert result["final_report"].endswith("> 仅供参考，不构成投资建议")
    assert result["degraded"] is True
    assert result["round_count"] == 2


def test_disclaimer_forced_and_idempotent():
    once = ensure_disclaimer("报告正文")
    assert once.endswith("> 仅供参考，不构成投资建议")
    assert "报告正文" in once
    assert ensure_disclaimer(once) == once
    assert ensure_disclaimer("") == "> 仅供参考，不构成投资建议"


def test_default_initial_state_full():
    s = default_initial_state("2026-08-03", mode="noon")
    assert s["mode"] == "noon"
    assert s["date"] == "2026-08-03"
    assert s["yesterday_report"] == ""
    assert s["earlier_today"] == ""
    for key in (
        "market_data", "raw_news", "snapshot", "analyses", "discussion_topic",
        "discussion_done", "round_count", "debate_history", "final_report",
        "overall_tags", "disclaimer", "degraded", "degraded_notes",
    ):
        assert key in s


def test_main_entry_partial_state_compat(fetch_mode):
    """main.py 旧 initial_state（缺 mode/earlier_today/snapshot 等）仍可运行。"""
    fetch_mode(lambda mode, date: _fake_data())
    app = build_graph()
    partial = {
        "date": "2026-08-03",
        "max_rounds": 2,
        "market_data": "",
        "analyses": [],
        "discussion_topic": "",
        "discussion_done": False,
        "round_count": 0,
        "skill_names": [],
        "debate_history": [],
        "final_report": "",
        "yesterday_report": "",
        "raw_news": "",
    }
    state = app.invoke(partial)
    assert state["final_report"]
    assert "仅供参考，不构成投资建议" in state["final_report"]
