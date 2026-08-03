"""I3 聊天引擎：会话 / 单分析师 / 多分析师交叉问答 / 历史对话检索 / 免责声明强制

契约（requirements.md §一.4 / §五 / §六、issues I3）：
- 上下文 = 分析师 skill 人格（只读 skills/**）+ 标的实时/历史数据（防御性调用 tools
  纯函数接口，I1 并行重写中，失败降级为「数据暂时不可用」）+ chroma 相关复盘结论
  （knowledge_store.search 只读调用）+ 历史消息；
- 多分析师交叉问答：用户消息后，首位分析师回答，后续逐位对上一轮回答表态
  （同意/反对/补充理由），串行执行，LLM 超时 120s（环境变量 CHAT_LLM_TIMEOUT 可配置），
  最后汇总为一条回复（含每位分析师片段 + 汇总）；单分析师模式直接由该分析师回答；
- 免责声明在组装层强制追加，响应恒含 ``disclaimer`` 字段；
- LLM 无 Key/失败 → 中文说明 + 免责声明，绝不抛异常。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

from .storage import chats as chat_storage

DISCLAIMER = "仅供参考，不构成投资建议"
DEFAULT_ANALYST_IDS = ["alang", "bingchuan", "baxiaoxian", "yangjia", "tiechui"]
DEFAULT_LLM_TIMEOUT = 120
CHROMA_COLLECTION = "chat_history"
CHROMA_CHUNK_SIZE = 400
CHROMA_CHUNK_OVERLAP = 100
HISTORY_WINDOW = 10
MAX_CONTEXT_CHARS = 1600


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ═══════════════════════════════════════════════════════════
# 分析师白名单（只读 skills/**）
# ═══════════════════════════════════════════════════════════
def get_analyst_whitelist() -> list[str]:
    """分析师白名单：5 位分析师（skills/** 与内置名单交集，排除主持人/复盘助手）。"""
    try:
        from stock_review_crew.skills import list_skills

        present = {s.get("id") for s in list_skills() if s.get("id")}
        ids = [sid for sid in DEFAULT_ANALYST_IDS if sid in present]
        if ids:
            return ids
    except Exception:
        pass
    return list(DEFAULT_ANALYST_IDS)


def load_analyst(analyst_id: str) -> Optional[dict]:
    """加载分析师 skill（含 prompt 人格）；缺失返回 None。"""
    try:
        from stock_review_crew.skills import load_skill

        return load_skill(analyst_id)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════
# LLM 调用与数据层降级
# ═══════════════════════════════════════════════════════════
def _call_llm(llm: Any, messages: list[dict]) -> str:
    """调用 LLM 并抽取文本；任何异常向上抛给调用方做降级。"""
    if llm is None:
        raise RuntimeError("LLM 未配置")
    result = llm.invoke(messages)
    if hasattr(result, "content"):
        text = result.content
    else:
        text = result
    text = str(text or "").strip()
    if not text:
        raise RuntimeError("LLM 返回空内容")
    return text


def _call_tool(func: Any, kwargs: dict) -> str:
    """兼容 langchain Tool（.invoke）与纯函数两种接口（I1 重写中）。"""
    try:
        if hasattr(func, "invoke"):
            return str(func.invoke(kwargs))
        return str(func(**kwargs))
    except TypeError:
        return str(func(kwargs))


def _fetch_target_data(target_type: str, target: str) -> dict:
    """防御性调用 I1 数据层纯函数接口；失败降级为「数据暂时不可用」。"""
    import importlib

    try:
        tools = importlib.import_module("stock_review_crew.tools.stock_data")
        today = datetime.now().strftime("%Y-%m-%d")
        if target_type == "stock":
            raw = _call_tool(tools.get_stock_info, {"code": target, "date": today})
        else:
            raw = _call_tool(tools.get_market_micro, {"date": today})
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and parsed.get("error"):
                return {"available": False, "note": f"数据暂时不可用：{parsed['error']}", "data": None}
        except (TypeError, ValueError):
            pass
        return {"available": True, "note": None, "data": raw}
    except Exception as e:
        return {"available": False, "note": f"数据暂时不可用：{e}", "data": None}


@lru_cache(maxsize=1)
def _get_default_llm() -> Any:
    """默认 LLM（DeepSeek，超时 120s，CHAT_LLM_TIMEOUT 可覆盖）；不可用返回 None。"""
    try:
        from stock_review_crew.config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
        from langchain_openai import ChatOpenAI

        timeout = float(os.environ.get("CHAT_LLM_TIMEOUT", str(DEFAULT_LLM_TIMEOUT)))
        return ChatOpenAI(
            model=DEEPSEEK_MODEL,
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
            temperature=0.7,
            timeout=timeout,
            max_retries=1,
        )
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════
# chroma chat_history 集合（复用 knowledge_store 客户端模式，不改其代码）
# ═══════════════════════════════════════════════════════════
def _get_chroma_client():
    """复用 knowledge_store 的客户端单例；测试可用环境变量 CHROMA_DB_DIR 隔离。"""
    try:
        import chromadb
        from chromadb.config import Settings

        env_dir = os.environ.get("CHROMA_DB_DIR")
        if not env_dir:
            from stock_review_crew import knowledge_store

            return knowledge_store._get_client()
        Path(env_dir).mkdir(parents=True, exist_ok=True)
        return chromadb.PersistentClient(path=env_dir, settings=Settings(anonymized_telemetry=False))
    except BaseException:  # chroma 可能 PanicException（BaseException），降级兜底
        return None


def _get_chat_history_collection():
    """chat_history 集合：优先 ONNX 嵌入，失败回退默认嵌入；全部失败返回 None。"""
    client = _get_chroma_client()
    if client is None:
        return None
    try:
        import chromadb

        embedding_function = chromadb.utils.embedding_functions.ONNXMiniLM_L6_V2()
        return client.get_or_create_collection(
            name=CHROMA_COLLECTION,
            metadata={"hnsw:space": "cosine"},
            embedding_function=embedding_function,
        )
    except BaseException:
        try:
            return client.get_or_create_collection(name=CHROMA_COLLECTION)
        except BaseException:
            return None


def _chunk_text(text: str, size: int = CHROMA_CHUNK_SIZE, overlap: int = CHROMA_CHUNK_OVERLAP) -> list[str]:
    chunks = []
    i = 0
    while i < len(text):
        chunk = text[i : i + size]
        if chunk.strip():
            chunks.append(chunk)
        i += size - overlap
    return chunks


def save_chat_history(
    session_id: str,
    role: str,
    content: str,
    analyst: Optional[str] = None,
) -> bool:
    """把一条对话消息分块写入 chroma chat_history 集合；失败静默返回 False。"""
    try:
        col = _get_chat_history_collection()
        if col is None:
            return False
        chunks = _chunk_text(str(content or ""))
        if not chunks:
            return False
        docs, metas, ids = [], [], []
        for j, chunk in enumerate(chunks):
            docs.append(chunk)
            metas.append(
                {
                    "date": session_id[:10],
                    "session_id": session_id,
                    "role": role,
                    "analyst": analyst or "",
                }
            )
            ids.append(f"{session_id.replace('-', '')}_{role}_{j}_{uuid.uuid4().hex[:8]}")
        col.add(documents=docs, metadatas=metas, ids=ids)
        return True
    except BaseException:
        return False


def search_chat_history(query: str, n_results: int = 5) -> dict:
    """检索历史对话；返回 ``{"available", "note", "data"}``，data 为 JSON 字符串或 None。"""
    try:
        col = _get_chat_history_collection()
        if col is None:
            return {"available": False, "note": "历史对话检索不可用（chroma 未就绪）", "data": None}
        results = col.query(query_texts=[query], n_results=n_results)
        if not results or not results.get("documents") or not results["documents"][0]:
            return {
                "available": True,
                "note": None,
                "data": json.dumps({"query": query, "results": [], "note": "无相关历史对话"}, ensure_ascii=False),
            }
        docs = []
        for i, (doc, meta, dist) in enumerate(
            zip(results["documents"][0], results["metadatas"][0], results["distances"][0])
        ):
            docs.append(
                {
                    "rank": i + 1,
                    "session_id": (meta or {}).get("session_id", "?"),
                    "date": (meta or {}).get("date", "?"),
                    "role": (meta or {}).get("role", "?"),
                    "content": doc,
                    "score": round(1 - dist, 3) if dist else 0,
                }
            )
        return {
            "available": True,
            "note": None,
            "data": json.dumps({"query": query, "results": docs}, ensure_ascii=False),
        }
    except BaseException as e:
        return {"available": False, "note": f"历史对话检索不可用：{e}", "data": None}


# ═══════════════════════════════════════════════════════════
# 聊天引擎
# ═══════════════════════════════════════════════════════════
class ChatEngine:
    """聊天引擎：会话管理 + 上下文组装 + 交叉问答 + 免责声明强制。"""

    def __init__(
        self,
        llm: Any = None,
        storage_root: Optional[Any] = None,
        timeout: Optional[float] = None,
        history_enabled: bool = True,
        review_searcher: Optional[Callable[[str], str]] = None,
        data_fetcher: Optional[Callable[[str, str], dict]] = None,
    ):
        self.llm = llm
        self.storage_root = storage_root
        self.timeout = float(timeout) if timeout is not None else float(
            os.environ.get("CHAT_LLM_TIMEOUT", str(DEFAULT_LLM_TIMEOUT))
        )
        self.history_enabled = history_enabled
        self.review_searcher = review_searcher
        self.data_fetcher = data_fetcher

    # ── 会话管理 ──
    def create_session(
        self,
        target_type: str,
        target: str,
        analysts: list[str],
        title: Optional[str] = None,
    ) -> dict:
        if target_type not in ("stock", "sector"):
            raise ValueError("target_type 仅支持 stock 或 sector")
        if not target or not str(target).strip():
            raise ValueError("target 不能为空")
        if not analysts:
            raise ValueError("至少选择一位分析师")
        whitelist = get_analyst_whitelist()
        unknown = [a for a in analysts if a not in whitelist]
        if unknown:
            raise ValueError(f"分析师不在白名单中：{unknown}；可选：{whitelist}")
        analysts = list(dict.fromkeys(analysts))
        meta = chat_storage.create_session(
            target_type, str(target).strip(), analysts, title=title, root=self.storage_root
        )
        return {"session_id": meta["session_id"]}

    def list_sessions(self, target: Optional[str] = None, date: Optional[str] = None) -> list[dict]:
        return chat_storage.list_sessions(target=target, date=date, root=self.storage_root)

    def get_session(self, session_id: str) -> Optional[dict]:
        session = chat_storage.get_session(session_id, root=self.storage_root)
        if session is None:
            return None
        return {"meta": session["meta"], "messages": session["messages"], "disclaimer": DISCLAIMER}

    def delete_session(self, session_id: str) -> bool:
        return chat_storage.delete_session(session_id, root=self.storage_root)

    # ── 发送消息 ──
    def send_message(self, session_id: str, content: str) -> Optional[dict]:
        """追加用户消息 → 生成回复（含免责声明）→ 落盘；会话不存在返回 None，绝不抛异常。"""
        content = str(content or "").strip()
        if not content:
            raise ValueError("消息内容不能为空")
        session = chat_storage.get_session(session_id, root=self.storage_root)
        if session is None:
            return None
        meta = session["meta"]
        degraded: list[str] = []

        user_msg = {"role": "user", "content": content, "created_at": _now_iso()}
        try:
            chat_storage.append_message(session_id, user_msg, root=self.storage_root)
        except Exception as e:
            degraded.append(f"消息持久化失败：{e}")

        prev_round_reply = None
        for m in reversed(session["messages"]):
            if m.get("role") == "assistant":
                prev_round_reply = str(m.get("content") or "")
                break

        context, issues = self._build_context(meta, content, session["messages"])
        degraded.extend(issues)

        llm = self.llm if self.llm is not None else _get_default_llm()
        if llm is None:
            degraded.append("LLM 未配置或无可用 Key")

        segments = []
        for i, analyst_id in enumerate(meta["analysts"]):
            analyst = load_analyst(analyst_id)
            if analyst is None:
                segments.append(
                    {"id": analyst_id, "name": analyst_id, "content": f"（分析师 {analyst_id} 人格信息缺失，暂无法回答）"}
                )
                degraded.append(f"分析师 {analyst_id} 人格缺失")
                continue
            name = analyst.get("name", analyst_id)
            try:
                text = self._ask_analyst(analyst, i, content, context, segments, prev_round_reply, llm)
                segments.append({"id": analyst_id, "name": name, "content": text})
            except Exception as e:
                segments.append(
                    {"id": analyst_id, "name": name, "content": f"（{name}：分析服务暂时不可用，请稍后重试）"}
                )
                degraded.append(f"分析师 {analyst_id} 生成失败：{e}")

        summary = self._make_summary(content, segments, llm, degraded)

        # 组装层：逐位片段 + 汇总 + 降级说明 + 免责声明
        if len(segments) == 1:
            body = segments[0]["content"]
        else:
            body = "\n\n".join(f"**【{s['name']}】**\n{s['content']}" for s in segments)
            body += f"\n\n**【汇总】**\n{summary}"
        if degraded:
            body += "\n\n> 降级说明：" + "；".join(dict.fromkeys(degraded))
        body += f"\n\n---\n{DISCLAIMER}"

        assistant_msg = {
            "role": "assistant",
            "content": body,
            "analysts": segments,
            "summary": summary,
            "degraded": degraded,
            "created_at": _now_iso(),
        }
        try:
            chat_storage.append_message(session_id, assistant_msg, root=self.storage_root)
        except Exception as e:
            degraded.append(f"消息持久化失败：{e}")
            assistant_msg["degraded"] = degraded

        if self.history_enabled:
            save_chat_history(session_id, "user", content)
            save_chat_history(session_id, "assistant", body, analyst="all")

        # 以内存消息为准：持久化失败时仍返回完整对话，磁盘问题不阻塞主流程
        messages = session["messages"] + [user_msg, assistant_msg]
        return {"session_id": session_id, "messages": messages, "disclaimer": DISCLAIMER}

    # ── 上下文组装 ──
    def _build_context(self, meta: dict, user_content: str, history_messages: list[dict]):
        """返回 (上下文文本, 降级说明列表)。数据/检索不可用必须可见。"""
        issues: list[str] = []
        target_type, target = meta.get("target_type", ""), meta.get("target", "")
        lines = [
            f"标的类型：{'个股' if target_type == 'stock' else '板块'}（{target_type}）",
            f"标的：{target}",
        ]
        if meta.get("title"):
            lines.append(f"会话标题：{meta['title']}")

        data = (
            self.data_fetcher(target_type, target)
            if self.data_fetcher is not None
            else _fetch_target_data(target_type, target)
        )
        if data and data.get("available"):
            lines.append("\n## 标的数据\n" + str(data.get("data") or "")[:MAX_CONTEXT_CHARS])
        else:
            note = str((data or {}).get("note") or "数据暂时不可用")
            lines.append(f"\n## 标的数据\n（{note}）")
            issues.append(note)

        review = self._search_review(user_content)
        if review.get("available"):
            lines.append("\n## 相关复盘结论\n" + str(review.get("data") or "")[:MAX_CONTEXT_CHARS])
        else:
            note = str(review.get("note") or "复盘检索不可用")
            lines.append(f"\n## 相关复盘结论\n（{note}）")
            issues.append(note)

        if self.history_enabled:
            chat_h = search_chat_history(user_content)
        else:
            chat_h = {"available": False, "note": "历史对话检索未启用", "data": None}
        if chat_h.get("available"):
            lines.append("\n## 相关历史对话\n" + str(chat_h.get("data") or "")[:MAX_CONTEXT_CHARS])
        else:
            note = str(chat_h.get("note") or "历史对话检索不可用")
            lines.append(f"\n## 相关历史对话\n（{note}）")
            if self.history_enabled:  # 主动关闭检索属于配置，不属于降级
                issues.append(note)

        lines.append("\n## 对话历史")
        for m in history_messages[-HISTORY_WINDOW:]:
            role = "用户" if m.get("role") == "user" else "分析师"
            lines.append(f"- {role}：{str(m.get('content') or '')[:300]}")
        lines.append("\n## 当前问题\n" + user_content)
        return "\n".join(lines), issues

    def _search_review(self, query: str) -> dict:
        """chroma 相关复盘结论（knowledge_store.search 只读调用）。"""
        if self.review_searcher is not None:
            try:
                text = str(self.review_searcher(query) or "")
                return {"available": True, "note": None, "data": text}
            except Exception as e:
                return {"available": False, "note": f"复盘检索不可用：{e}", "data": None}
        try:
            from stock_review_crew import knowledge_store

            text = str(knowledge_store.search(query, n_results=5) or "")
            parsed = json.loads(text)
            if isinstance(parsed, dict) and parsed.get("error"):
                return {"available": False, "note": f"复盘检索不可用：{parsed['error']}", "data": None}
            if isinstance(parsed, dict) and not parsed.get("results"):
                return {"available": True, "note": None, "data": "无相关复盘记录"}
            return {"available": True, "note": None, "data": text}
        except BaseException as e:
            return {"available": False, "note": f"复盘检索不可用：{e}", "data": None}

    # ── 交叉问答 ──
    def _ask_analyst(
        self,
        analyst: dict,
        index: int,
        user_content: str,
        context: str,
        prev_segments: list[dict],
        prev_round_reply: Optional[str],
        llm: Any,
    ) -> str:
        name = analyst.get("name", analyst.get("id", "分析师"))
        system = (
            f"{analyst.get('prompt', '')}\n\n"
            f"【本次任务】你是「{name}」，正在与用户就标的进行多轮聊天。"
            "请用中文、以你的人设与交易体系回答问题。上下文中的数据可能缺失或降级，"
            "缺失时请明确说明，禁止编造数据。"
        )
        user = context
        if prev_round_reply:
            user += f"\n\n上一轮回答摘要：{prev_round_reply[:800]}"
        if index == 0:
            user += "\n\n【指令】请直接回答用户的问题，结合上下文给出你的判断与理由（400 字以内）。"
        else:
            prev = prev_segments[-1]
            user += (
                f"\n\n【指令】请对「{prev['name']}」的回答表态（同意/反对/补充理由），"
                f"并结合你的体系补充你的结论（250 字以内）。"
            )
        return _call_llm(llm, [{"role": "system", "content": system}, {"role": "user", "content": user}])

    def _make_summary(self, user_content: str, segments: list[dict], llm: Any, degraded: list[str]) -> str:
        """单分析师直接返回其回答；多分析师调用 LLM 汇总，失败降级为中文说明。"""
        if len(segments) <= 1:
            return segments[0]["content"] if segments else "（无分析师观点）"
        if llm is None:
            degraded.append("汇总生成失败：LLM 未配置")
            return "（分析服务暂不可用，未能生成汇总）"
        payload = {"user": user_content, "analysts": [{"name": s["name"], "content": s["content"]} for s in segments]}
        try:
            return _call_llm(
                llm,
                [
                    {
                        "role": "system",
                        "content": "你是多分析师聊天的汇总角色。请用中文汇总各位分析师的回答：共同观点、分歧点、以及给用户的可执行结论。不要编造数据。",
                    },
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
            )
        except Exception as e:
            degraded.append(f"汇总生成失败：{e}")
            return "（以上为各位分析师的逐位回答，汇总生成失败——服务降级）"


# ═══════════════════════════════════════════════════════════
# 模块级便捷入口（供 I5 后端集成）
# ═══════════════════════════════════════════════════════════
_default_engine: Optional[ChatEngine] = None


def _get_default_engine() -> ChatEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = ChatEngine()
    return _default_engine


def reset_default_engine() -> None:
    """重置默认引擎（测试用）。"""
    global _default_engine
    _default_engine = None


def create_session(target_type: str, target: str, analysts: list[str], title: Optional[str] = None) -> dict:
    return _get_default_engine().create_session(target_type, target, analysts, title=title)


def send_message(session_id: str, content: str) -> Optional[dict]:
    return _get_default_engine().send_message(session_id, content)


def list_sessions(target: Optional[str] = None, date: Optional[str] = None) -> list[dict]:
    return _get_default_engine().list_sessions(target=target, date=date)


def get_session(session_id: str) -> Optional[dict]:
    return _get_default_engine().get_session(session_id)


def delete_session(session_id: str) -> bool:
    return _get_default_engine().delete_session(session_id)


__all__ = [
    "ChatEngine",
    "DISCLAIMER",
    "create_session",
    "send_message",
    "list_sessions",
    "get_session",
    "delete_session",
    "get_analyst_whitelist",
    "load_analyst",
    "save_chat_history",
    "search_chat_history",
    "reset_default_engine",
]
