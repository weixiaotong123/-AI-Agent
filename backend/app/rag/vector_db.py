from __future__ import annotations

from functools import lru_cache
import re
from hashlib import md5
from typing import Any

import httpx

from app.config import (
    BACKEND_DIR,
    CHROMA_COLLECTION_NAME,
    CHROMA_DB_DIR,
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_MODEL,
    LLM_API_KEY,
    LLM_BASE_URL,
)
from app.rag.guide_catalog import destination_for_guide


DATA_DIR = BACKEND_DIR / "data"


def _split_markdown_into_chunks(markdown_text: str, source_name: str) -> list[dict[str, str]]:
    """按二级、三级标题切分 Markdown，返回可检索片段。"""
    chunks: list[dict[str, str]] = []
    current_title = "文档开头"
    current_lines: list[str] = []

    for line in markdown_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## ") or stripped.startswith("### "):
            if current_lines:
                chunks.append(
                    {
                        "title": current_title,
                        "text": "\n".join(current_lines).strip(),
                        "source": source_name,
                    }
                )
                current_lines = []
            current_title = stripped.lstrip("#").strip()
        elif stripped:
            current_lines.append(stripped)

    if current_lines:
        chunks.append(
            {
                "title": current_title,
                "text": "\n".join(current_lines).strip(),
                "source": source_name,
            }
        )

    return chunks


def _build_chunk_id(source: str, title: str, text: str) -> str:
    """基于 source、title 和 text 生成稳定片段 ID。"""
    digest = md5(f"{source}|{title}|{text}".encode("utf-8")).hexdigest()
    return f"{source}_{digest}"


def _build_document_text(chunk: dict[str, str]) -> str:
    """把标题和正文拼成送入向量库的文档文本。"""
    return f"{chunk['title']}\n{chunk['text']}"


def load_guide_chunks() -> list[dict[str, str]]:
    """读取 backend/data 下的攻略文件，并切分成可检索片段。"""
    chunks: list[dict[str, str]] = []
    for guide_file in sorted(DATA_DIR.glob("*.md*")):
        destination = destination_for_guide(guide_file.name)
        if destination is None:
            raise ValueError(
                f"攻略文件缺少 destination 映射：{guide_file.name}。"
                "请先在 app/rag/guide_catalog.py 中登记该文件。"
            )
        text = guide_file.read_text(encoding="utf-8")
        raw_chunks = _split_markdown_into_chunks(text, guide_file.name)
        for chunk in raw_chunks:
            chunks.append(
                {
                    "id": _build_chunk_id(chunk["source"], chunk["title"], chunk["text"]),
                    "title": chunk["title"],
                    "text": chunk["text"],
                    "source": chunk["source"],
                    "destination": destination,
                }
            )
    return chunks


def _extract_keywords(query: str) -> list[str]:
    """把查询语句切成简单关键词，用于回退匹配。"""
    raw_keywords = re.split(r"[\s,，。；;、]+", query)
    return [keyword.strip() for keyword in raw_keywords if keyword.strip()]


def _score_chunk(query: str, chunk_text: str) -> int:
    """按关键词出现次数给片段打分。"""
    keywords = _extract_keywords(query)
    return sum(1 for keyword in keywords if keyword in chunk_text)


def _search_guide_chunks_by_keywords(
    query: str, top_k: int = 3, destination: str | None = None
) -> list[dict[str, str]]:
    """回退方案：使用关键词匹配本地攻略片段。"""
    scored_chunks: list[tuple[int, dict[str, str]]] = []
    for chunk in load_guide_chunks():
        if destination and chunk.get("destination") != destination:
            continue
        score = _score_chunk(query, _build_document_text(chunk))
        if score > 0:
            scored_chunks.append((score, chunk))

    scored_chunks.sort(key=lambda item: item[0], reverse=True)
    return [chunk for _, chunk in scored_chunks[:top_k]]


def _tokenize_bm25(text: str) -> list[str]:
    """使用 jieba 搜索模式切分中文检索文本。"""
    import jieba

    return [token.strip().lower() for token in jieba.cut_for_search(text) if token.strip()]


@lru_cache(maxsize=1)
def _get_bm25_index() -> tuple[list[dict[str, str]], Any]:
    """为本地攻略语料构建进程内 BM25 索引。"""
    from rank_bm25 import BM25Okapi

    chunks = load_guide_chunks()
    tokenized_documents = [
        _tokenize_bm25(_build_document_text(chunk)) for chunk in chunks
    ]
    return chunks, BM25Okapi(tokenized_documents)


def _search_guide_chunks_by_bm25(
    query: str, top_k: int = 3, destination: str | None = None
) -> list[dict[str, str]]:
    """使用 BM25 从本地攻略语料中召回候选片段。"""
    chunks, bm25_index = _get_bm25_index()
    scores = bm25_index.get_scores(_tokenize_bm25(query))
    ranked_chunks = [
        (float(score), index, chunk)
        for index, (score, chunk) in enumerate(zip(scores, chunks))
        if score > 0 and (not destination or chunk.get("destination") == destination)
    ]
    ranked_chunks.sort(key=lambda item: (-item[0], item[1]))
    return [chunk for _, _, chunk in ranked_chunks[:top_k]]


def _chunk_identity(chunk: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        chunk.get("source", ""),
        chunk.get("title", ""),
        chunk.get("text", ""),
        chunk.get("destination", ""),
    )


def _reciprocal_rank_fusion(
    ranked_lists: list[list[dict[str, str]]], top_k: int, rrf_k: int = 60
) -> list[dict[str, str]]:
    """按 RRF 合并多路候选，并按片段内容去重。"""
    scores: dict[tuple[str, str, str, str], float] = {}
    chunks_by_identity: dict[tuple[str, str, str, str], dict[str, str]] = {}

    for ranked_chunks in ranked_lists:
        for rank, chunk in enumerate(ranked_chunks, start=1):
            identity = _chunk_identity(chunk)
            scores[identity] = scores.get(identity, 0.0) + 1.0 / (rrf_k + rank)
            chunks_by_identity.setdefault(identity, chunk)

    ranked_identities = sorted(scores, key=lambda identity: scores[identity], reverse=True)
    return [chunks_by_identity[identity] for identity in ranked_identities[:top_k]]


def _build_embeddings():
    """创建 embedding 模型实例。"""
    if not LLM_API_KEY:
        return None

    try:
        from langchain_openai import OpenAIEmbeddings
    except ImportError:
        return None

    try:
        return OpenAIEmbeddings(
            model=EMBEDDING_MODEL,
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL or None,
            chunk_size=EMBEDDING_BATCH_SIZE,
            check_embedding_ctx_length=False,
        )
    except TypeError:
        return OpenAIEmbeddings(
            model=EMBEDDING_MODEL,
            openai_api_key=LLM_API_KEY,
            openai_api_base=LLM_BASE_URL or None,
            chunk_size=EMBEDDING_BATCH_SIZE,
            check_embedding_ctx_length=False,
        )


def _extract_embedding_token_usage(response_data: dict) -> dict[str, int]:
    """读取 embeddings 接口返回的官方 usage；没有 usage 时保持 0。"""
    usage = response_data.get("usage") or {}
    prompt_tokens = (
        usage.get("prompt_tokens")
        or usage.get("input_tokens")
        or usage.get("input_token_count")
        or usage.get("total_tokens")
        or usage.get("total_token_count")
        or 0
    )
    return {
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": 0,
    }


def _embed_query_with_usage(query: str) -> tuple[list[float] | None, dict[str, int]]:
    """在线 query embedding：优先直接调接口拿官方 usage，失败时回退 LangChain 但 usage 为 0。"""
    empty_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    if not LLM_API_KEY:
        return None, empty_usage

    base_url = (LLM_BASE_URL or "https://api.openai.com/v1").rstrip("/")
    endpoint = f"{base_url}/embeddings"
    payload = {
        "model": EMBEDDING_MODEL,
        "input": query,
    }
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(endpoint, json=payload, headers=headers)
        if response.status_code == 200:
            data = response.json()
            items = data.get("data") or []
            if items and "embedding" in items[0]:
                usage = _extract_embedding_token_usage(data)
                print(
                    "[embedding] query embedding token: "
                    f"prompt={usage['prompt_tokens']}, completion=0, source=api"
                )
                return items[0]["embedding"], usage
            print(f"[embedding] embeddings response missing vector: {response.text[:500]}")
        else:
            print(
                "[embedding] embeddings API failed: "
                f"status_code={response.status_code}, response={response.text[:500]}"
            )
    except Exception as exc:
        print(f"[embedding] embeddings API failed: {type(exc).__name__}: {exc}")

    embeddings = _build_embeddings()
    if embeddings is None:
        return None, empty_usage
    print("[embedding] fallback to LangChain embed_query; official token usage unavailable")
    return embeddings.embed_query(query), empty_usage


def _get_chroma_collection():
    """获取 Chroma collection。"""
    try:
        import chromadb
    except ImportError:
        return None

    client = chromadb.PersistentClient(path=str(CHROMA_DB_DIR))
    return client.get_or_create_collection(
        name=CHROMA_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def ingest_guide_chunks_to_chroma() -> int:
    """
    把本地攻略片段写入 Chroma。

    流程是：
    1. 创建 embedding 模型
    2. 获取 Chroma collection
    3. 读取并切分本地攻略
    4. 生成向量
    5. 把向量、文本和 metadata 一起写入 Chroma
    """
    embeddings = _build_embeddings()
    collection = _get_chroma_collection()
    chunks = load_guide_chunks()

    if embeddings is None:
        raise RuntimeError("当前环境缺少 embedding 能力，无法写入 Chroma。")
    if collection is None:
        raise RuntimeError("当前环境缺少 chromadb，无法写入 Chroma。")

    documents = [_build_document_text(chunk) for chunk in chunks]
    vectors = embeddings.embed_documents(documents)
    ids = [chunk["id"] for chunk in chunks]
    metadatas = [
        {
            "title": chunk["title"],
            "source": chunk["source"],
            "destination": chunk["destination"],
        }
        for chunk in chunks
    ]

    collection.upsert(
        ids=ids,
        documents=documents,
        metadatas=metadatas,
        embeddings=vectors,
    )
    return len(chunks)


def _search_guide_chunks_by_chroma(
    query: str, top_k: int = 3, destination: str | None = None
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """优先使用 Chroma 做向量检索，并返回在线 query embedding token。"""
    collection = _get_chroma_collection()
    empty_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    if collection is None:
        return [], empty_usage
    if collection.count() == 0:
        return [], empty_usage

    query_embedding, embedding_usage = _embed_query_with_usage(query)
    if query_embedding is None:
        return [], empty_usage
    query_args = {
        "query_embeddings": [query_embedding],
        "n_results": top_k,
        "include": ["documents", "metadatas"],
    }
    if destination:
        query_args["where"] = {"destination": destination}
    result = collection.query(**query_args)

    documents = result.get("documents", [[]])[0]
    metadatas = result.get("metadatas", [[]])[0]

    matched_chunks: list[dict[str, str]] = []
    for document, metadata in zip(documents, metadatas):
        title = metadata.get("title", "未命名片段") if metadata else "未命名片段"
        source = metadata.get("source", "未知来源") if metadata else "未知来源"
        chunk_destination = metadata.get("destination", "") if metadata else ""
        text = document.split("\n", 1)[1] if "\n" in document else document
        matched_chunks.append(
            {
                "title": title,
                "text": text,
                "source": source,
                "destination": chunk_destination,
            }
        )

    return matched_chunks, embedding_usage


def search_guide_chunks_with_usage(
    query: str,
    top_k: int = 3,
    destination: str | None = None,
    retrieval_mode: str = "hybrid",
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """
    从本地攻略片段里找最相关的 top_k 条结果。

    支持仅 Dense 或 Dense + BM25 融合；没有候选时回退到关键词检索。
    """
    if retrieval_mode not in {"dense", "hybrid"}:
        raise ValueError(f"Unsupported retrieval mode: {retrieval_mode}")

    empty_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    chroma_results, embedding_usage = _search_guide_chunks_by_chroma(
        query=query, top_k=top_k, destination=destination
    )
    if retrieval_mode == "dense":
        if chroma_results:
            return chroma_results, embedding_usage
        return _search_guide_chunks_by_keywords(
            query=query, top_k=top_k, destination=destination
        ), empty_usage

    bm25_results = _search_guide_chunks_by_bm25(
        query=query, top_k=top_k, destination=destination
    )
    if chroma_results or bm25_results:
        return _reciprocal_rank_fusion(
            [chroma_results, bm25_results], top_k=top_k
        ), embedding_usage
    return _search_guide_chunks_by_keywords(
        query=query, top_k=top_k, destination=destination
    ), empty_usage


def search_guide_chunks(
    query: str, top_k: int = 3, destination: str | None = None
) -> list[dict[str, str]]:
    """兼容旧调用：只返回检索片段，不返回 token usage。"""
    chunks, _ = search_guide_chunks_with_usage(
        query=query, top_k=top_k, destination=destination
    )
    return chunks
