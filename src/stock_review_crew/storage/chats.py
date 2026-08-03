"""I3 会话持久化：``data/chats/{YYYY-MM-DD}/{HHMMSS}/{meta.json, messages.json}``

约定（requirements.md §五/§六、issues I3）：
- 目录根默认 <项目根>/data/chats，可用环境变量 ``CHATS_DATA_DIR`` 覆盖（测试隔离）；
- ``session_id = "{YYYY-MM-DD}_{HHMMSS}"``，严格正则校验，防路径穿越；
- 文件损坏容错：损坏的 meta 视为会话不存在；损坏的 messages.json 降级为空列表并在
  ``get_session`` 返回 ``corrupted`` 标记；追加消息时会重建损坏文件；
- ``delete_session`` 不存在返回 False（404 语义）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

SESSION_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VALID_TARGET_TYPES = ("stock", "sector")
DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "data" / "chats"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_root(root: Optional[Any] = None) -> Path:
    """解析存储根目录：显式 root > 环境变量 CHATS_DATA_DIR > 默认 data/chats。"""
    if root is not None:
        return Path(root)
    env = os.environ.get("CHATS_DATA_DIR")
    return Path(env) if env else DEFAULT_ROOT


def is_valid_id(session_id: Any) -> bool:
    return isinstance(session_id, str) and bool(SESSION_ID_RE.match(session_id))


def _session_dir(session_id: str, root: Optional[Any] = None) -> Path:
    """校验 ID 并返回会话目录（双重防路径穿越）。"""
    if not is_valid_id(session_id):
        raise ValueError(f"非法的会话 ID：{session_id}")
    base = get_root(root).resolve()
    date_part, time_part = session_id.split("_", 1)
    target = (base / date_part / time_part).resolve()
    if not target.is_relative_to(base):
        raise ValueError("会话路径越界")
    return target


def _write_json(path: Path, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _read_json(path: Path) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def create_session(
    target_type: str,
    target: str,
    analysts: list[str],
    title: Optional[str] = None,
    root: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> dict:
    """创建会话并落盘，返回 meta。``now`` 仅供测试固定时间。"""
    if target_type not in VALID_TARGET_TYPES:
        raise ValueError(f"target_type 仅支持 {'/'.join(VALID_TARGET_TYPES)}")
    if not target or not str(target).strip():
        raise ValueError("target 不能为空")
    if not analysts:
        raise ValueError("analysts 不能为空")

    ts = now or datetime.now()
    session_id = None
    dir_path = None
    date_part = time_part = None
    for _ in range(60):  # 同一秒目录冲突时顺延一秒
        date_part = ts.strftime("%Y-%m-%d")
        time_part = ts.strftime("%H%M%S")
        candidate = f"{date_part}_{time_part}"
        d = _session_dir(candidate, root)
        if not d.exists():
            session_id, dir_path = candidate, d
            break
        ts = ts + timedelta(seconds=1)
    if session_id is None:
        raise RuntimeError("无法分配会话 ID（目录冲突）")

    dir_path.mkdir(parents=True, exist_ok=True)
    created_at = ts.isoformat(timespec="seconds")
    meta = {
        "session_id": session_id,
        "target_type": target_type,
        "target": str(target).strip(),
        "analysts": list(analysts),
        "title": title,
        "date": date_part,
        "time": time_part,
        "created_at": created_at,
        "updated_at": created_at,
    }
    _write_json(dir_path / "meta.json", meta)
    _write_json(dir_path / "messages.json", [])
    return meta


def append_message(session_id: str, message: dict, root: Optional[Any] = None) -> dict:
    """追加一条消息并刷新 updated_at；会话不存在抛 FileNotFoundError。"""
    if not isinstance(message, dict):
        raise ValueError("message 必须是 dict")
    d = _session_dir(session_id, root)
    if not (d / "meta.json").exists():
        raise FileNotFoundError("会话不存在")
    messages = _read_json(d / "messages.json")
    if not isinstance(messages, list):  # 损坏容错：重建
        messages = []
    messages.append(message)
    _write_json(d / "messages.json", messages)
    meta = _read_json(d / "meta.json")
    if isinstance(meta, dict):
        meta["updated_at"] = _now_iso()
        _write_json(d / "meta.json", meta)
    return message


def get_session(session_id: str, root: Optional[Any] = None) -> Optional[dict]:
    """返回 ``{"meta", "messages", "corrupted"?}``；不存在或 meta 损坏返回 None。"""
    try:
        d = _session_dir(session_id, root)
    except ValueError:
        return None
    meta = _read_json(d / "meta.json")
    if not isinstance(meta, dict):
        return None
    messages = _read_json(d / "messages.json")
    corrupted = []
    if not isinstance(messages, list):
        if (d / "messages.json").exists():
            corrupted.append("messages.json")
        messages = []
    result = {"meta": meta, "messages": messages}
    if corrupted:
        result["corrupted"] = corrupted
    return result


def list_sessions(
    target: Optional[str] = None,
    date: Optional[str] = None,
    root: Optional[Any] = None,
) -> list[dict]:
    """按日期、时间点倒序列出会话 meta；损坏的 meta 跳过；非法日期参数抛 ValueError。"""
    if date is not None and not DATE_RE.match(date):
        raise ValueError("日期格式应为 YYYY-MM-DD")
    base = get_root(root)
    if not base.exists():
        return []
    items = []
    for d in sorted(base.iterdir(), reverse=True):
        if not d.is_dir() or not DATE_RE.match(d.name):
            continue
        if date is not None and d.name != date:
            continue
        for t in sorted(d.iterdir(), reverse=True):
            if not t.is_dir() or not re.fullmatch(r"\d{6}", t.name):
                continue
            meta = _read_json(t / "meta.json")
            if not isinstance(meta, dict):  # 损坏容错：跳过
                continue
            if target is not None and meta.get("target") != target:
                continue
            items.append(meta)
    return items


def delete_session(session_id: str, root: Optional[Any] = None) -> bool:
    """删除会话目录；不存在或非法 ID 返回 False（404 语义）。"""
    try:
        d = _session_dir(session_id, root)
    except ValueError:
        return False
    if not d.exists():
        return False
    shutil.rmtree(d)
    return True


__all__ = [
    "create_session",
    "append_message",
    "get_session",
    "list_sessions",
    "delete_session",
    "get_root",
    "is_valid_id",
]
