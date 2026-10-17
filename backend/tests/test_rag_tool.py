from pathlib import Path
import sys


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.agents.tools import rag_tool  # noqa: E402


def test_destination_context_supplements_attraction_candidates(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []

    def fake_retrieve(*, query: str, top_k: int, destination: str):
        assert destination == "大理"
        calls.append((query, top_k))
        if "景点" in query:
            return ["confirmed attraction context"], {"prompt_tokens": 1, "completion_tokens": 0}, {"prompt_tokens": 2, "completion_tokens": 0}
        return [], {"prompt_tokens": 0, "completion_tokens": 0}, {"prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(
        rag_tool,
        "build_destination_query",
        lambda **_: ("大理 query", {"prompt_tokens": 0, "completion_tokens": 0}),
    )
    monkeypatch.setattr(rag_tool, "retrieve_travel_guide", fake_retrieve)

    contexts, _, rerank_usage, embedding_usage = rag_tool.get_destination_guide_context(
        destination="大理",
        top_k=5,
    )

    assert calls == [
        ("大理 query", 5),
        ("大理 景点 旅游景区 游览地点", 5),
        ("大理 住宿 酒店 民宿", 2),
        ("大理 餐饮 美食 餐厅", 2),
    ]
    assert "confirmed attraction context" in contexts
    assert rerank_usage["prompt_tokens"] == 1
    assert embedding_usage["prompt_tokens"] == 2


def test_note_keywords_load_destination_rules_from_data_file() -> None:
    """大理日落规则从 JSON 数据文件加载，不再依赖 Python 城市 POI 常量。"""
    rag_tool._load_retrieval_rules.cache_clear()

    keywords = rag_tool._extract_note_keywords("不想早起，下午看日落拍照", destination="大理")

    assert "日落" in keywords
    assert "洱海" in keywords
    assert "大理180度海景网红打卡地" in keywords
    assert "双廊" not in keywords
    assert "才村" not in keywords


def test_note_keywords_use_current_xiamen_cycling_terms() -> None:
    """厦门骑行扩展到当前攻略仍存在的环岛路。"""
    rag_tool._load_retrieval_rules.cache_clear()

    keywords = rag_tool._extract_note_keywords("想在海边骑行", destination="厦门")

    assert "骑行" in keywords
    assert "环岛路" in keywords
    assert "洱海生态廊道" not in keywords


def test_retrieval_rule_config_excludes_removed_poi_names() -> None:
    """配置文件不得保留已经从当前攻略实体中移除的旧 POI 名称。"""
    rag_tool._load_retrieval_rules.cache_clear()
    config = rag_tool._load_retrieval_rules()
    serialized = str(config)

    for removed_name in [
        "双廊",
        "才村",
        "龙龛",
        "喜洲古镇",
        "回民街",
        "曾厝垵",
        "洱海生态廊道",
        "蜈支洲岛",
        "第一市场",
        "大熊猫",
        "熊猫",
    ]:
        assert removed_name not in serialized
