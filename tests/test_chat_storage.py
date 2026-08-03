"""I3 storage/chats.py 单元测试：持久化往返、过滤、删除、路径安全与损坏容错。"""

import json
import os
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from stock_review_crew.storage import chats as s


@pytest.fixture
def chat_tmp():
    """沙箱中 mkdtemp/pytest-tmp 目录带 0o700 ACL 无法二次访问，改用 Path.mkdir 自建。"""
    root = Path(__file__).resolve().parent.parent / ".tmp" / f"chat_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def test_create_get_append_roundtrip(chat_tmp):
    root = chat_tmp / "chats"
    real_now = s.create_session("stock", "600001", ["alang"], root=root)
    meta = s.create_session(
        "stock", "600519", ["alang", "yangjia"], title="测试会话",
        root=root, now=datetime(2026, 8, 3, 14, 30, 5),
    )
    sid = meta["session_id"]
    assert sid == "2026-08-03_143005"
    assert meta["target_type"] == "stock"
    assert meta["target"] == "600519"
    assert meta["analysts"] == ["alang", "yangjia"]
    assert meta["title"] == "测试会话"
    assert (root / "2026-08-03" / "143005" / "meta.json").exists()
    assert (root / "2026-08-03" / "143005" / "messages.json").exists()

    got = s.get_session(sid, root=root)
    assert got is not None
    assert got["meta"]["session_id"] == sid
    assert got["messages"] == []

    s.append_message(sid, {"role": "user", "content": "你好"}, root=root)
    s.append_message(sid, {"role": "assistant", "content": "回你好"}, root=root)
    got = s.get_session(sid, root=root)
    assert [m["role"] for m in got["messages"]] == ["user", "assistant"]
    assert [m["content"] for m in got["messages"]] == ["你好", "回你好"]
    real_got = s.get_session(real_now["session_id"], root=root)
    assert real_got["meta"]["updated_at"] >= real_got["meta"]["created_at"]


def test_list_sessions_order_and_filters(chat_tmp):
    root = chat_tmp / "chats"
    m1 = s.create_session("stock", "600519", ["alang"], now=datetime(2026, 8, 3, 9, 5, 0), root=root)
    m2 = s.create_session("stock", "600519", ["alang"], now=datetime(2026, 8, 3, 10, 15, 30), root=root)
    m3 = s.create_session("sector", "半导体", ["yangjia"], now=datetime(2026, 8, 2, 15, 0, 0), root=root)

    items = s.list_sessions(root=root)
    assert [i["session_id"] for i in items] == [m2["session_id"], m1["session_id"], m3["session_id"]]

    items = s.list_sessions(target="600519", root=root)
    assert {i["session_id"] for i in items} == {m1["session_id"], m2["session_id"]}

    items = s.list_sessions(date="2026-08-03", root=root)
    assert {i["session_id"] for i in items} == {m1["session_id"], m2["session_id"]}

    items = s.list_sessions(target="不存在", date="2026-08-03", root=root)
    assert items == []


def test_delete_session_semantics(chat_tmp):
    root = chat_tmp / "chats"
    m = s.create_session("stock", "000001", ["alang"], now=datetime(2026, 8, 3, 10, 0, 0), root=root)
    sid = m["session_id"]
    assert s.delete_session(sid, root=root) is True
    assert s.delete_session(sid, root=root) is False
    assert s.get_session(sid, root=root) is None
    assert not (root / "2026-08-03" / "100000").exists()


def test_path_traversal_rejected(chat_tmp):
    root = chat_tmp / "chats"
    assert s.get_session("../../evil", root=root) is None
    assert s.delete_session("..\\..\\evil", root=root) is False
    with pytest.raises(ValueError):
        s.append_message("../../evil", {"role": "user", "content": "x"}, root=root)
    with pytest.raises(ValueError):
        s._session_dir("2026-08-03_000000/../../x", root=root)
    with pytest.raises(ValueError):
        s._session_dir("2026-08-03_99", root=root)  # 时间分量不是 6 位数字


def test_corrupted_files_tolerated(chat_tmp):
    root = chat_tmp / "chats"
    m = s.create_session("stock", "600519", ["alang"], now=datetime(2026, 8, 3, 10, 0, 0), root=root)
    sid = m["session_id"]
    d = root / "2026-08-03" / "100000"

    # meta 损坏 → 视为不存在，列表跳过
    (d / "meta.json").write_text("{not json", encoding="utf-8")
    assert s.get_session(sid, root=root) is None
    assert s.list_sessions(root=root) == []

    # 修复 meta、破坏 messages → 详情降级为空并标记 corrupted
    (d / "meta.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    (d / "messages.json").write_text("[broken", encoding="utf-8")
    got = s.get_session(sid, root=root)
    assert got["meta"]["session_id"] == sid
    assert got["messages"] == []
    assert "messages.json" in got["corrupted"]

    # 追加消息时损坏文件被重建
    s.append_message(sid, {"role": "user", "content": "重来"}, root=root)
    got = s.get_session(sid, root=root)
    assert len(got["messages"]) == 1
    assert got["messages"][0]["content"] == "重来"
    assert "corrupted" not in got


def test_env_root_override(chat_tmp, monkeypatch):
    env_root = chat_tmp / "env_chats"
    monkeypatch.setenv("CHATS_DATA_DIR", str(env_root))
    m = s.create_session("sector", "AI", ["alang"], now=datetime(2026, 8, 3, 11, 11, 11))
    assert (env_root / "2026-08-03" / "111111" / "meta.json").exists()
    assert s.get_session(m["session_id"]) is not None

    monkeypatch.delenv("CHATS_DATA_DIR", raising=False)
    assert s.get_session(m["session_id"]) is None  # 默认根目录下不存在


def test_create_validation(chat_tmp):
    with pytest.raises(ValueError):
        s.create_session("bad", "600519", ["alang"], root=chat_tmp)
    with pytest.raises(ValueError):
        s.create_session("stock", "", ["alang"], root=chat_tmp)
    with pytest.raises(ValueError):
        s.create_session("stock", "600519", [], root=chat_tmp)
    with pytest.raises(ValueError):
        s.append_message("2026-08-03_000000", "not-dict", root=chat_tmp)


def test_list_invalid_date(chat_tmp):
    with pytest.raises(ValueError):
        s.list_sessions(date="2026/08/03", root=chat_tmp)
