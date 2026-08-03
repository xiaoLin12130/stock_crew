"""I3 chroma chat_history 集合测试：保存/检索与降级路径。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import chromadb

from stock_review_crew import chat


def _ephemeral_chroma(monkeypatch):
    """本机沙箱中 chroma 无法新建/升级磁盘库（rust 绑定受限），用内存客户端验证集合逻辑。"""
    monkeypatch.setattr(chat, "_get_chroma_client", lambda: chromadb.EphemeralClient())


def test_save_and_search_chat_history(monkeypatch):
    _ephemeral_chroma(monkeypatch)
    ok = chat.save_chat_history("2026-08-03_143005", "user", "今天半导体板块怎么看？资金在流入吗？")
    assert ok is True
    chat.save_chat_history("2026-08-03_143005", "assistant", "半导体方向资金回流明显，值得跟踪。", analyst="alang")

    result = chat.search_chat_history("半导体资金")
    assert result["available"] is True
    data = json.loads(result["data"])
    assert data["query"] == "半导体资金"
    assert len(data["results"]) > 0
    assert data["results"][0]["session_id"] == "2026-08-03_143005"


def test_save_long_message_chunked(monkeypatch):
    _ephemeral_chroma(monkeypatch)
    long_text = "市场波动" * 300  # 超过 400 字切片阈值
    ok = chat.save_chat_history("2026-08-02_090000", "user", long_text)
    assert ok is True
    result = chat.search_chat_history("市场波动", n_results=10)
    assert result["available"] is True
    data = json.loads(result["data"])
    assert len(data["results"]) >= 2  # 多切片可被检索到


def test_chat_history_degrades_when_collection_unavailable(monkeypatch):
    monkeypatch.setattr(chat, "_get_chat_history_collection", lambda: None)
    assert chat.save_chat_history("2026-08-03_143005", "user", "x") is False
    result = chat.search_chat_history("x")
    assert result["available"] is False
    assert "不可用" in result["note"]
    assert result["data"] is None


def test_chat_history_degrades_on_query_error(monkeypatch):
    class BoomCollection:
        def query(self, **kwargs):
            raise RuntimeError("chroma 查询失败")

        def add(self, **kwargs):
            raise RuntimeError("chroma 写入失败")

    monkeypatch.setattr(chat, "_get_chat_history_collection", lambda: BoomCollection())
    assert chat.save_chat_history("2026-08-03_143005", "user", "x") is False
    result = chat.search_chat_history("x")
    assert result["available"] is False
    assert "不可用" in result["note"]
