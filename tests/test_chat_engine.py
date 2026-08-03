"""I3 chat.py 聊天引擎单元测试：单分析师/多分析师交叉/上下文断言/降级/免责声明。"""

import json
import os
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from stock_review_crew import chat
from stock_review_crew.chat import ChatEngine, DISCLAIMER


@pytest.fixture
def chat_tmp():
    """沙箱中 mkdtemp/pytest-tmp 目录带 0o700 ACL 无法二次访问，改用 Path.mkdir 自建。"""
    root = Path(__file__).resolve().parent.parent / ".tmp" / f"chat_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


class FakeLLM:
    """最小可注入 LLM：按调用顺序返回文本；fail=True 时模拟服务故障。"""

    def __init__(self, texts=None, fail=False):
        self.texts = list(texts or ["（mock 回答）"])
        self.fail = fail
        self.calls = []
        self._i = 0

    def invoke(self, messages):
        self.calls.append(list(messages))
        if self.fail:
            raise RuntimeError("mock LLM 故障")
        text = self.texts[min(self._i, len(self.texts) - 1)]
        self._i += 1
        return type("MockAIMessage", (), {"content": text})()


def make_engine(chat_tmp, llm, analysts=("alang",), data_ok=True):
    def data_fetcher(target_type, target):
        if data_ok:
            return {
                "available": True,
                "note": None,
                "data": json.dumps({"close": 10.5, "pct_change": 0.02}, ensure_ascii=False),
            }
        return {"available": False, "note": "数据暂时不可用：mock 数据源超时", "data": None}

    return ChatEngine(
        llm=llm,
        storage_root=chat_tmp / "chats",
        history_enabled=False,
        review_searcher=lambda q: "无相关复盘记录",
        data_fetcher=data_fetcher,
    )


def test_whitelist_from_skills():
    whitelist = chat.get_analyst_whitelist()
    for aid in ("alang", "bingchuan", "baxiaoxian", "yangjia", "tiechui"):
        assert aid in whitelist


def test_create_session_validation(chat_tmp):
    e = make_engine(chat_tmp, FakeLLM())
    with pytest.raises(ValueError):
        e.create_session("bond", "600519", ["alang"])
    with pytest.raises(ValueError):
        e.create_session("stock", "", ["alang"])
    with pytest.raises(ValueError):
        e.create_session("stock", "600519", [])
    with pytest.raises(ValueError) as exc:
        e.create_session("stock", "600519", ["mystery"])
    assert "白名单" in str(exc.value)


def test_single_analyst_reply_and_context(chat_tmp):
    llm = FakeLLM(["阿狼的答案"])
    e = make_engine(chat_tmp, llm, analysts=("alang",))
    sid = e.create_session("stock", "600519", ["alang"], title="白酒")["session_id"]

    resp = e.send_message(sid, "怎么看明天走势？")
    assert resp["disclaimer"] == DISCLAIMER
    assert resp["session_id"] == sid
    assert [m["role"] for m in resp["messages"]] == ["user", "assistant"]

    assistant = resp["messages"][-1]
    assert assistant["content"].endswith(DISCLAIMER)
    assert "阿狼" in assistant["content"]
    assert assistant["summary"] == "阿狼的答案"
    assert assistant["degraded"] == []
    assert len(assistant["analysts"]) == 1
    assert assistant["analysts"][0]["id"] == "alang"

    # 上下文断言：skill 人格 + 标的数据 + 复盘结论 + 当前问题
    assert len(llm.calls) == 1  # 单分析师：仅一次 LLM 调用
    system, user = llm.calls[0][0]["content"], llm.calls[0][1]["content"]
    assert "阿狼" in system and "焚诀" in system
    assert "600519" in user
    assert '"close": 10.5' in user
    assert "相关复盘结论" in user
    assert "无相关复盘记录" in user
    assert "怎么看明天走势？" in user


def test_multi_analyst_cross_qa(chat_tmp):
    llm = FakeLLM(["首答", "表态", "汇总"])
    e = make_engine(chat_tmp, llm, analysts=("alang", "yangjia"))
    sid = e.create_session("stock", "600519", ["alang", "yangjia"])["session_id"]
    resp = e.send_message(sid, "大盘怎么看？")

    assistant = resp["messages"][-1]
    assert [a["id"] for a in assistant["analysts"]] == ["alang", "yangjia"]
    assert assistant["analysts"][0]["content"] == "首答"
    assert assistant["analysts"][1]["content"] == "表态"
    assert assistant["summary"] == "汇总"
    assert "综合来看：" in assistant["content"]
    assert "阿狼" in assistant["content"] and "炒股养家" in assistant["content"]
    assert assistant["content"].endswith(DISCLAIMER)
    assert assistant["degraded"] == []

    # 串行调用：首位直接回答，第二位对前一位表态，最后汇总
    assert len(llm.calls) == 3
    second_user = llm.calls[1][1]["content"]
    assert "表态" in second_user and "阿狼" in second_user
    summary_user = llm.calls[2][1]["content"]
    assert "首答" in summary_user and "表态" in summary_user


def test_prev_round_reply_in_context(chat_tmp):
    llm = FakeLLM(["第一轮", "第二轮"])
    e = make_engine(chat_tmp, llm, analysts=("alang",))
    sid = e.create_session("stock", "600519", ["alang"])["session_id"]
    e.send_message(sid, "第一问")
    e.send_message(sid, "第二问")

    # 第二轮的首位分析师应看到上一轮回答与历史消息
    second_user = llm.calls[1][1]["content"]
    assert "上一轮回答" in second_user
    assert "对话历史" in second_user
    assert "第一问" in second_user


def test_data_unavailable_degraded_visible(chat_tmp):
    llm = FakeLLM(["分析"])
    e = make_engine(chat_tmp, llm, analysts=("alang",), data_ok=False)
    sid = e.create_session("stock", "600519", ["alang"])["session_id"]
    resp = e.send_message(sid, "怎么看？")

    assistant = resp["messages"][-1]
    assert assistant["degraded"], "数据降级必须可见"
    assert "数据暂时不可用" in assistant["content"]
    assert "降级说明" in assistant["content"]
    user = llm.calls[0][1]["content"]
    assert "数据暂时不可用" in user


def test_llm_failure_degrades_without_raise(chat_tmp):
    llm = FakeLLM(fail=True)
    e = make_engine(chat_tmp, llm, analysts=("alang", "yangjia"))
    sid = e.create_session("stock", "600519", ["alang", "yangjia"])["session_id"]
    resp = e.send_message(sid, "怎么看？")

    assistant = resp["messages"][-1]
    assert assistant["degraded"]
    assert len(assistant["analysts"]) == 2
    assert "分析服务暂时不可用" in assistant["content"]
    assert assistant["content"].endswith(DISCLAIMER)


def test_no_llm_degrades_without_raise(chat_tmp, monkeypatch):
    monkeypatch.setattr(chat, "_get_default_llm", lambda: None)
    e = make_engine(chat_tmp, None, analysts=("alang",))
    sid = e.create_session("stock", "600519", ["alang"])["session_id"]
    resp = e.send_message(sid, "怎么看？")

    assistant = resp["messages"][-1]
    assert assistant["degraded"]
    assert "LLM 未配置" in assistant["content"]
    assert assistant["content"].endswith(DISCLAIMER)


def test_send_to_missing_session_returns_none(chat_tmp):
    e = make_engine(chat_tmp, FakeLLM())
    assert e.send_message("2099-01-01_000000", "你好") is None
    assert e.get_session("2099-01-01_000000") is None
    assert e.delete_session("2099-01-01_000000") is False


def test_empty_content_rejected(chat_tmp):
    e = make_engine(chat_tmp, FakeLLM())
    sid = e.create_session("stock", "600519", ["alang"])["session_id"]
    with pytest.raises(ValueError):
        e.send_message(sid, "   ")


def test_persistence_failure_does_not_block_reply(chat_tmp, monkeypatch):
    llm = FakeLLM(["回答"])
    e = make_engine(chat_tmp, llm, analysts=("alang",))
    sid = e.create_session("stock", "600519", ["alang"])["session_id"]

    def boom(*args, **kwargs):
        raise OSError("磁盘写入失败")

    monkeypatch.setattr(chat.chat_storage, "append_message", boom)
    resp = e.send_message(sid, "怎么看？")
    assistant = resp["messages"][-1]
    assert "消息持久化失败" in assistant["content"]
    assert assistant["content"].endswith(DISCLAIMER)


def test_engine_roundtrip_and_filters(chat_tmp):
    llm = FakeLLM(["回"])
    e = make_engine(chat_tmp, llm, analysts=("alang",))
    sid = e.create_session("sector", "半导体", ["alang"])["session_id"]
    e.send_message(sid, "问题一")
    e.send_message(sid, "问题二")

    detail = e.get_session(sid)
    assert len(detail["messages"]) == 4
    assert detail["disclaimer"] == DISCLAIMER
    assert detail["meta"]["target"] == "半导体"

    lst = e.list_sessions(target="半导体")
    assert [i["session_id"] for i in lst] == [sid]
    assert e.list_sessions(target="白酒") == []
    assert e.delete_session(sid) is True
    assert e.get_session(sid) is None


def test_module_level_functions(chat_tmp, monkeypatch):
    """模块级便捷入口（I5 后端集成用）走默认引擎 + 环境变量隔离。"""
    monkeypatch.setenv("CHATS_DATA_DIR", str(chat_tmp / "chats"))
    chat.reset_default_engine()
    try:
        llm = FakeLLM(["模块级回答"])
        monkeypatch.setattr(chat, "_get_default_llm", lambda: llm)
        monkeypatch.setattr(
            chat,
            "_fetch_target_data",
            lambda target_type, target: {"available": True, "note": None, "data": '{"close": 1.0}'},
        )
        monkeypatch.setattr("stock_review_crew.knowledge_store.search", lambda *a, **k: "无相关复盘记录")
        monkeypatch.setattr(chat, "_get_chroma_client", lambda: None)  # 默认引擎不触碰仓库 chroma_db
        resp = chat.create_session("stock", "600519", ["alang"])
        sid = resp["session_id"]
        out = chat.send_message(sid, "你好")
        assert out["disclaimer"] == DISCLAIMER
        assert out["messages"][-1]["content"].endswith(DISCLAIMER)
        assert chat.get_session(sid) is not None
        assert chat.delete_session(sid) is True
    finally:
        chat.reset_default_engine()
