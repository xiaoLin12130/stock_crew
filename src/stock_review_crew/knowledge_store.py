"""历史复盘知识库 — ChromaDB + ONNX 嵌入"""

import json
from pathlib import Path

import chromadb
from chromadb.config import Settings

# 存储路径
DB_DIR = Path(__file__).parent.parent.parent / "chroma_db"

# 全局客户端（懒加载）
_client = None
_client_failed = False  # 首次失败后快速降级，避免每次调用都触发 Rust panic


def _get_client():
    global _client, _client_failed
    if _client_failed:
        return None
    if _client is None:
        try:
            DB_DIR.mkdir(parents=True, exist_ok=True)
            _client = chromadb.PersistentClient(
                path=str(DB_DIR),
                settings=Settings(anonymized_telemetry=False),
            )
        except BaseException:  # chroma 版本不匹配可能触发 Rust panic（BaseException）
            _client_failed = True
            _client = None
    return _client


def _get_collection():
    client = _get_client()
    if client is None:
        return None
    # ONNX 嵌入：all-MiniLM-L6-v2
    try:
        return client.get_or_create_collection(
            name="review_history",
            metadata={"hnsw:space": "cosine"},
            embedding_function=chromadb.utils.embedding_functions.ONNXMiniLM_L6_V2(),
        )
    except Exception:
        # fallback: 无嵌入函数
        return client.get_or_create_collection(name="review_history")


def save_analysis(date: str, skill_name: str, analysis: str, section: str = "full"):
    """
    保存一位分析师的一段分析。
    section: "full"(全篇) / "market"(大盘判断) / "plan"(操作计划)
    """
    try:
        col = _get_collection()
        if col is None:
            return
        # 切片：每 500 字一段
        chunk_size = 400
        overlap = 100
        text = analysis
        chunks = []
        i = 0
        while i < len(text):
            chunk = text[i:i + chunk_size]
            if chunk.strip():
                chunks.append(chunk)
            i += chunk_size - overlap

        for j, chunk in enumerate(chunks):
            doc_id = f"{date}_{skill_name}_{section}_{j}"
            col.add(
                documents=[chunk],
                metadatas=[{"date": date, "analyst": skill_name, "section": section}],
                ids=[doc_id],
            )
    except Exception:
        pass  # 静默失败，不影响主流程


def save_report(date: str, report: str):
    """保存最终复盘报告"""
    try:
        col = _get_collection()
        if col is None:
            return
        # 分段落存
        sections = report.split("\n## ")
        for j, sec in enumerate(sections):
            if len(sec.strip()) < 20:
                continue
            doc_id = f"{date}_report_sec{j}"
            col.add(
                documents=[sec[:1000]],
                metadatas=[{"date": date, "analyst": "report", "section": f"sec{j}"}],
                ids=[doc_id],
            )
    except Exception:
        pass


def search(query: str, n_results: int = 5) -> str:
    """
    搜索历史复盘。返回 JSON 格式结果。
    分析师可以通过 search_history 工具调用。
    """
    try:
        col = _get_collection()
        if col is None:
            return json.dumps(
                {"query": query, "results": [], "note": "历史知识库不可用（chroma 初始化失败），检索降级"},
                ensure_ascii=False,
            )
        results = col.query(query_texts=[query], n_results=n_results)

        if not results or not results.get("documents") or not results["documents"][0]:
            return json.dumps({"query": query, "results": [], "note": "无相关历史记录"}, ensure_ascii=False)

        docs = []
        for i, (doc, meta, dist) in enumerate(zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        )):
            docs.append({
                "rank": i + 1,
                "date": meta.get("date", "?"),
                "analyst": meta.get("analyst", "?"),
                "content": doc,
                "score": round(1 - dist, 3) if dist else 0,
            })

        return json.dumps({"query": query, "results": docs}, ensure_ascii=False, indent=2)
    except BaseException as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def auto_save(date: str, analyses: list[dict], final_report: str):
    """复盘结束后自动存档"""
    count = 0
    for a in analyses:
        text = a.get("analysis", "")
        if text:
            save_analysis(date, a.get("skill_name", "unknown"), text, "full")
            count += 1
    if final_report:
        save_report(date, final_report)
        count += 1
    import os
    if not os.environ.get("STREAMLIT_RUNNING"):
        print(f"📚 [知识库] 已存档 {date} 的 {count} 条分析记录")
