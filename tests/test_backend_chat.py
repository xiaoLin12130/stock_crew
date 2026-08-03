"""I5 后端聊天 API 集成测试：会话全链路 / 白名单校验 / 免责声明恒有 / 无 Key 降级 / 404。

离线确定性：
- 函数级 autouse fixture 设置 DEEPSEEK_API_KEY=""（每个测试结束自动恢复，避免污染
  依赖 .env Key 的全量套件用例）；
- 聊天数据取数/检索入口 monkeypatch；LLM 用 FakeLLM 或 None（无 Key 降级），禁止真实网络；
- CHATS_DATA_DIR 指向隔离目录（自建并清理，不用 pytest tmp_path）。
"""

import os

import json
import re
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

import stock_review_crew.chat as chat

import backend.main as bm

client = TestClient(bm.app)


@pytest.fixture(autouse=True)
def no_llm_key():
    """函数级：无 LLM Key 环境（聊天降级路径），测试结束自动恢复，不污染其他模块。"""
    had_key = "DEEPSEEK_API_KEY" in os.environ
    old_key = os.environ.get("DEEPSEEK_API_KEY")
    os.environ["DEEPSEEK_API_KEY"] = ""
    yield
    if had_key:
        os.environ["DEEPSEEK_API_KEY"] = old_key or ""
    else:
        os.environ.pop("DEEPSEEK_API_KEY", None)


class FakeLLM:
    """最小可注入 LLM：按调用顺序返回文本。"""

    def __init__(self, texts):
        self.texts = list(texts)
        self._i = 0

    def invoke(self, messages):
        text = self.texts[min(self._i, len(self.texts) - 1)]
        self._i += 1
        return type("MockAIMessage", (), {"content": text})()


class FakeStreamLLM(FakeLLM):
    """带 .stream() 的 FakeLLM：每次调用返回整段文本（单块流）。"""

    def stream(self, messages):
        result = self.invoke(messages)
        yield type("MockAIMessage", (), {"content": result.content})()


@pytest.fixture()
def iso_dirs(monkeypatch):
    """隔离存储目录（自建并清理）。"""
    base = Path(__file__).resolve().parent.parent / ".tmp" / f"chat_backend_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    chats_dir = base / "chats"
    reviews_dir = base / "reviews"
    chats_dir.mkdir(parents=True, exist_ok=True)
    reviews_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CHATS_DATA_DIR", str(chats_dir))
    monkeypatch.setenv("STOCK_REVIEW_DATA_DIR", str(reviews_dir))
    yield {"base": base, "chats": chats_dir}
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """离线确定性：数据取数/检索/历史写入全 mock；默认无 LLM（降级路径）。"""
    monkeypatch.setattr(chat, "_get_default_llm", lambda: None)
    monkeypatch.setattr(
        chat, "_fetch_target_data",
        lambda target_type, target: {"available": False, "note": "数据暂时不可用（离线测试）", "data": None},
    )
    monkeypatch.setattr(
        chat, "search_chat_history",
        lambda q, n_results=5: {"available": False, "note": "历史对话检索不可用（离线测试）", "data": None},
    )
    monkeypatch.setattr(chat, "save_chat_history", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        chat.ChatEngine, "_search_review",
        lambda self, q: {"available": False, "note": "复盘检索不可用（离线测试）", "data": None},
    )


# ── 会话全链路：建会话 → 发消息（disclaimer 断言）→ 列表过滤 → 详情 → 删除 → 404 ──

def test_chat_full_chain(iso_dirs, monkeypatch):
    monkeypatch.setattr(chat, "_get_default_llm", lambda: FakeLLM(["首答", "表态", "汇总"]))

    r = client.post(
        "/api/chat/sessions",
        json={"target_type": "stock", "target": "600519", "analysts": ["alang", "yangjia"], "title": "测试会话"},
    )
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{6}", sid)
    date_part = sid.split("_", 1)[0]

    # 发消息：响应恒含 disclaimer；多分析师交叉（首答/表态/汇总）
    r = client.post(f"/api/chat/sessions/{sid}/messages", json={"content": "大盘怎么看？"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"session_id", "messages", "disclaimer"}
    assert body["session_id"] == sid
    assert body["disclaimer"] == "仅供参考，不构成投资建议"
    msgs = body["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "大盘怎么看？"
    assistant = msgs[-1]
    assert assistant["role"] == "assistant"
    assert "首答" in assistant["content"]
    assert "汇总" in assistant["content"]
    assert assistant["content"].rstrip().endswith("仅供参考，不构成投资建议")

    # 会话列表（按标的/日期过滤）+ 前端 normalizeChatSessionList 字段
    r = client.get("/api/chat/sessions", params={"target": "600519", "date": date_part})
    assert r.status_code == 200
    items = r.json()
    assert len(items) >= 1
    item = items[0]
    for key in (
        "session_id", "target_type", "target", "target_name", "analysts",
        "title", "created_at", "date", "last_message", "message_count",
    ):
        assert key in item, f"列表缺少字段：{key}"
    assert item["session_id"] == sid
    assert item["target_type"] == "stock"
    assert item["target"] == "600519"
    assert item["target_name"] == "600519"
    assert item["analysts"] == ["alang", "yangjia"]
    assert item["title"] == "测试会话"
    assert item["date"] == date_part
    assert item["message_count"] == 2
    assert item["last_message"] == assistant["content"]

    # 过滤无结果 → 空列表
    r = client.get("/api/chat/sessions", params={"target": "不存在的标的", "date": date_part})
    assert r.status_code == 200
    assert r.json() == []

    # 会话详情 {meta, messages[], disclaimer}
    r = client.get(f"/api/chat/sessions/{sid}")
    assert r.status_code == 200
    detail = r.json()
    assert set(detail) == {"meta", "messages", "disclaimer"}
    assert detail["meta"]["session_id"] == sid
    assert detail["meta"]["target"] == "600519"
    assert len(detail["messages"]) == 2
    assert detail["disclaimer"] == "仅供参考，不构成投资建议"

    # 删除 204（无 body）→ 再删 404 中文
    r = client.delete(f"/api/chat/sessions/{sid}")
    assert r.status_code == 204
    assert r.content == b""
    r = client.delete(f"/api/chat/sessions/{sid}")
    assert r.status_code == 404
    assert "会话不存在" in r.json()["detail"]


# ── 无 Key 降级：中文说明 + 免责声明，不抛异常、不发网络 ──

def test_chat_no_key_degrades_with_disclaimer(iso_dirs):
    r = client.post(
        "/api/chat/sessions",
        json={"target_type": "sector", "target": "半导体", "analysts": ["alang"]},
    )
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]

    r = client.post(f"/api/chat/sessions/{sid}/messages", json={"content": "板块怎么看？"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["disclaimer"] == "仅供参考，不构成投资建议"
    assistant = body["messages"][-1]
    assert assistant["role"] == "assistant"
    assert "LLM 未配置" in assistant["content"]
    assert "降级说明" in assistant["content"]
    assert assistant["content"].rstrip().endswith("仅供参考，不构成投资建议")


# ── 参数校验 400 / 404 中文 ──

def test_chat_validation_400_404(iso_dirs):
    # 分析师白名单
    r = client.post(
        "/api/chat/sessions",
        json={"target_type": "stock", "target": "600519", "analysts": ["mystery"]},
    )
    assert r.status_code == 400
    assert "白名单" in r.json()["detail"]
    # target_type 枚举
    r = client.post(
        "/api/chat/sessions",
        json={"target_type": "bond", "target": "x", "analysts": ["alang"]},
    )
    assert r.status_code == 400
    assert "仅支持" in r.json()["detail"]
    # target / analysts 缺失
    r = client.post(
        "/api/chat/sessions",
        json={"target_type": "stock", "target": "", "analysts": ["alang"]},
    )
    assert r.status_code == 400
    assert "不能为空" in r.json()["detail"]
    r = client.post(
        "/api/chat/sessions",
        json={"target_type": "stock", "target": "600519", "analysts": "alang"},
    )
    assert r.status_code == 400
    assert "列表" in r.json()["detail"]
    # 请求体非对象
    r = client.post("/api/chat/sessions", json=[1])
    assert r.status_code == 400
    assert "JSON 对象" in r.json()["detail"]

    # 空消息 → 400；会话不存在 → 404（消息与详情/删除）
    r = client.post("/api/chat/sessions/2026-08-03_000000/messages", json={"content": "   "})
    assert r.status_code == 400
    assert "不能为空" in r.json()["detail"]
    r = client.post("/api/chat/sessions/2026-08-03_000000/messages", json={"content": "你好"})
    assert r.status_code == 404
    assert "会话不存在" in r.json()["detail"]
    r = client.get("/api/chat/sessions/2026-08-03_000000")
    assert r.status_code == 404
    assert "会话不存在" in r.json()["detail"]
    r = client.delete("/api/chat/sessions/2026-08-03_000000")
    assert r.status_code == 404
    assert "会话不存在" in r.json()["detail"]

    # 列表非法日期 → 400 中文
    r = client.get("/api/chat/sessions", params={"date": "2026/08/03"})
    assert r.status_code == 400
    assert "日期" in r.json()["detail"]


# ── 单分析师会话（normalizeChatMessage 的 analyst_name 语义）──

def test_chat_single_analyst_reply(iso_dirs, monkeypatch):
    monkeypatch.setattr(chat, "_get_default_llm", lambda: FakeLLM(["单分析师回答"]))
    r = client.post(
        "/api/chat/sessions",
        json={"target_type": "stock", "target": "600519", "analysts": ["alang"], "title": "单分析师"},
    )
    assert r.status_code == 200
    sid = r.json()["session_id"]
    r = client.post(f"/api/chat/sessions/{sid}/messages", json={"content": "明天走势？"})
    assert r.status_code == 200, r.text
    assistant = r.json()["messages"][-1]
    assert assistant["content"].rstrip().endswith("仅供参考，不构成投资建议")
    assert "单分析师回答" in assistant["content"]


# ── SSE 流式端点：事件序列完整 → done（含免责声明与全量消息）──

def test_chat_stream_sse_events(iso_dirs, monkeypatch):
    monkeypatch.setattr(chat, "_get_default_llm", lambda: FakeStreamLLM(["首答", "表态", "汇总"]))
    r = client.post(
        "/api/chat/sessions",
        json={"target_type": "stock", "target": "600519", "analysts": ["alang", "yangjia"], "title": "流式测试"},
    )
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]

    events = []
    with client.stream("POST", f"/api/chat/sessions/{sid}/messages/stream", json={"content": "大盘怎么看？"}) as resp:
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("content-type", "").startswith("text/event-stream")
        for line in resp.iter_lines():
            if not line:
                continue
            if line.startswith("event:"):
                events.append({"event": line[6:].strip(), "data": ""})
            elif line.startswith("data:"):
                if events:
                    events[-1]["data"] = line[5:].strip()

    ev_names = [e["event"] for e in events]
    for required in (
        "meta", "user_msg", "analyst_start", "analyst_delta", "analyst_end",
        "summary_start", "summary_delta", "summary_end", "done",
    ):
        assert required in ev_names, f"缺少事件 {required}: {ev_names}"
    assert ev_names[-1] == "done"

    done = json.loads(events[-1]["data"])
    assert done["session_id"] == sid
    assert done["disclaimer"] == "仅供参考，不构成投资建议"
    msgs = done["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    content = msgs[-1]["content"]
    assert content.rstrip().endswith("仅供参考，不构成投资建议")
    assert "首答" in content and "表态" in content
    assert "综合来看：" in content
    # 单块流：analyst_delta 应包含整段回答
    deltas = [e["data"] for e in events if e["event"] == "analyst_delta"]
    assert any("首答" in d for d in deltas)
