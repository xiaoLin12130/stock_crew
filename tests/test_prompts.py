"""I2 提示词测试：6 模式模板、昨日/更早复盘对照注入、主持人/复盘助手角色。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from stock_review_crew import prompts


def _state(**overrides):
    base = {
        "date": "2026-08-03",
        "mode": "close",
        "market_data": "指数快照内容",
        "yesterday_report": "",
        "earlier_today": "",
        "analyses": [{"skill_name": "阿狼", "analysis": "看多"}],
        "debate_history": [],
        "degraded": False,
    }
    base.update(overrides)
    return base


def test_six_modes_defined():
    assert set(prompts.MODES) == {
        "pre_market", "auction", "intraday_am", "noon", "intraday_pm", "close",
    }
    assert prompts.DEFAULT_MODE == "close"
    for mode in prompts.MODES:
        info = prompts.MODE_INFO[mode]
        assert info["label"]
        assert info["desc"]
        assert len(info["sections"]) >= 6
        assert any("核心观点" in s for s in info["sections"])
        assert any("分歧" in s for s in info["sections"])
        assert any("风险" in s for s in info["sections"])


def test_analyst_prompt_injection():
    p = prompts.build_analyst_prompt(_state(
        yesterday_report="昨日：预判看多",
        earlier_today="午间：维持看多",
    ))
    assert "2026-08-03" in p
    assert "收盘复盘" in p
    assert "指数快照内容" in p
    assert "昨日：预判看多" in p
    assert "午间：维持看多" in p
    assert "## 1、昨日复盘验证" in p
    assert "## 2、今日判断" in p
    assert "## 3、操作计划" in p


def test_yesterday_missing_skip_rule():
    p = prompts.build_analyst_prompt(_state(mode="auction"))
    assert "（无昨日报告）" in p
    assert "无昨日报告，跳过验证" in p
    assert "（无当日更早复盘）" in p


def test_all_modes_analyst_prompt():
    for mode in prompts.MODES:
        p = prompts.build_analyst_prompt(_state(mode=mode))
        assert prompts.MODE_INFO[mode]["label"] in p
        assert "无昨日报告，跳过验证" in p
        assert prompts.MODE_INFO[mode]["focus_zh"] in p


def test_sentiment_gets_trend_context():
    p = prompts.build_analyst_prompt(_state(), group="sentiment", trend_context="阿狼：趋势偏多")
    assert "阿狼：趋势偏多" in p
    assert "趋势分析师的大盘判断" in p
    p2 = prompts.build_analyst_prompt(_state(), group="trend", trend_context="不应出现")
    assert "趋势分析师的大盘判断" not in p2


def test_unknown_mode_falls_back_to_close():
    p = prompts.build_analyst_prompt(_state(mode="bad_mode"))
    assert "收盘复盘" in p
    assert prompts.mode_info(None)["label"] == "收盘复盘"


def test_news_prompt():
    p = prompts.build_news_prompt("财新：降息预期升温")
    assert "资讯内容" in p
    assert "财新：降息预期升温" in p
    assert "利多板块及个股" in p


def test_host_role_and_prompt():
    assert "分歧判断" in prompts.HOST_SYSTEM_PROMPT
    assert "讨论议题" in prompts.HOST_SYSTEM_PROMPT
    assert "实质性分歧" in prompts.HOST_SYSTEM_PROMPT
    p = prompts.build_host_prompt(_state(), "首轮")
    assert "首轮" in p
    assert "阿狼" in p


def test_debate_prompt_cross_reference_and_anti_fabrication():
    skill = {"name": "阿狼", "prompt": "趋势派"}
    p = prompts.build_debate_prompt(skill, "多空方向分歧", "养家：看空")
    assert "多空方向分歧" in p
    assert "养家：看空" in p
    assert "不要编造" in p
    assert "不要做模拟交易" in p


def test_report_prompt_close_keeps_legacy_sections():
    p = prompts.build_report_prompt(_state())
    assert "## 二、各分析师核心观点" in p
    assert "## 四、明日操作计划" in p
    assert "唯一一份" in p
    assert "禁止编造" in p


def test_report_prompt_all_modes_sections():
    for mode in prompts.MODES:
        p = prompts.build_report_prompt(_state(mode=mode))
        for s in prompts.MODE_INFO[mode]["sections"]:
            assert f"## {s}" in p


def test_report_title():
    assert prompts.mode_report_title("2026-08-03", "close") == "2026-08-03 A股收盘复盘报告"
    assert prompts.mode_report_title("2026-08-03", "auction") == "2026-08-03 A股竞价复盘报告"


def test_assistant_role_and_disclaimer():
    assert "复盘" in prompts.REVIEW_ASSISTANT_SYSTEM_PROMPT
    assert "Markdown" in prompts.REVIEW_ASSISTANT_SYSTEM_PROMPT
    assert prompts.DISCLAIMER == "仅供参考，不构成投资建议"


def test_role_json_backs_prompts():
    """主持人/复盘助手角色 JSON 与 prompts 生效文本一致（JSON 为真源）。"""
    from stock_review_crew.skills import load_skill
    host = load_skill("host")
    assert host and host["prompt"] == prompts.HOST_SYSTEM_PROMPT
    assistant = load_skill("review_assistant")
    assert assistant and assistant["prompt"] == prompts.REVIEW_ASSISTANT_SYSTEM_PROMPT
