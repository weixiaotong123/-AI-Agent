from pathlib import Path
import sys


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app.rag.vector_db as vector_db  # noqa: E402


def _chunk(title: str, text: str, destination: str = "大理") -> dict[str, str]:
    return {
        "source": f"{destination}_guide.md",
        "title": title,
        "text": text,
        "destination": destination,
    }


def test_reciprocal_rank_fusion_merges_and_deduplicates() -> None:
    shared_chunk = _chunk("洱海", "日落观景")
    dense_only_chunk = _chunk("苍山", "自然风景")
    bm25_only_chunk = _chunk("双廊", "洱海日落")

    results = vector_db._reciprocal_rank_fusion(
        [
            [shared_chunk, dense_only_chunk],
            [shared_chunk, bm25_only_chunk],
        ],
        top_k=3,
    )

    assert results == [shared_chunk, dense_only_chunk, bm25_only_chunk]


def test_bm25_retrieval_filters_destination_and_sorts_scores(monkeypatch) -> None:
    chunks = [
        _chunk("大理古城", "历史文化"),
        _chunk("成都古镇", "历史文化", destination="成都"),
        _chunk("喜洲古镇", "历史文化"),
    ]

    class FakeIndex:
        def get_scores(self, tokens: list[str]) -> list[float]:
            assert tokens == ["古镇"]
            return [0.5, 100.0, 1.0]

    monkeypatch.setattr(vector_db, "_tokenize_bm25", lambda text: ["古镇"])
    monkeypatch.setattr(vector_db, "_get_bm25_index", lambda: (chunks, FakeIndex()))

    results = vector_db._search_guide_chunks_by_bm25(
        query="古镇", top_k=2, destination="大理"
    )

    assert [chunk["title"] for chunk in results] == ["喜洲古镇", "大理古城"]


def test_hybrid_search_fuses_both_sources_and_preserves_embedding_usage(monkeypatch) -> None:
    dense_chunks = [_chunk("洱海", "日落观景"), _chunk("苍山", "自然风景")]
    bm25_chunks = [_chunk("洱海", "日落观景"), _chunk("双廊", "洱海日落")]
    embedding_usage = {"prompt_tokens": 12, "completion_tokens": 0}

    monkeypatch.setattr(
        vector_db,
        "_search_guide_chunks_by_chroma",
        lambda query, top_k, destination: (dense_chunks, embedding_usage),
    )
    monkeypatch.setattr(
        vector_db,
        "_search_guide_chunks_by_bm25",
        lambda query, top_k, destination: bm25_chunks,
    )
    monkeypatch.setattr(
        vector_db,
        "_search_guide_chunks_by_keywords",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected fallback")),
    )

    results, usage = vector_db.search_guide_chunks_with_usage(
        query="大理 洱海 日落", top_k=3, destination="大理"
    )

    assert [chunk["title"] for chunk in results] == ["洱海", "苍山", "双廊"]
    assert usage == embedding_usage


def test_dense_mode_skips_bm25(monkeypatch) -> None:
    dense_chunk = _chunk("洱海", "日落观景")
    embedding_usage = {"prompt_tokens": 12, "completion_tokens": 0}

    monkeypatch.setattr(
        vector_db,
        "_search_guide_chunks_by_chroma",
        lambda query, top_k, destination: ([dense_chunk], embedding_usage),
    )
    monkeypatch.setattr(
        vector_db,
        "_search_guide_chunks_by_bm25",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("BM25 must be skipped")),
    )

    results, usage = vector_db.search_guide_chunks_with_usage(
        query="大理 洱海 日落", top_k=1, destination="大理", retrieval_mode="dense"
    )

    assert results == [dense_chunk]
    assert usage == embedding_usage