import json
from pathlib import Path
import sys


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


CASES_PATH = BACKEND_DIR / "eval" / "rag_eval_cases.json"
CITY_GUIDES = {
    "大理": "dali_guide.md",
    "成都": "chengdu_guide.md",
    "西安": "xian_guide.md",
    "厦门": "xiamen_guide.md",
    "三亚": "sanya_guide.md",
    "北京": "beijing_guide.md",
}


def _load_cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def test_eval_cases_required_keywords_exist_in_destination_guides() -> None:
    """每条评估必需词必须能在对应目的地的当前攻略中找到。"""
    missing_by_case: dict[str, list[str]] = {}
    for case in _load_cases():
        destination = case["destination"]
        guide_name = CITY_GUIDES[destination]
        guide_text = (BACKEND_DIR / "data" / guide_name).read_text(encoding="utf-8")
        missing = [
            keyword
            for keyword in case.get("required_content_keywords", [])
            if keyword not in guide_text
        ]
        if missing:
            missing_by_case[case["id"]] = missing

    assert missing_by_case == {}


def test_eval_cases_use_known_destinations_and_nonempty_required_keywords() -> None:
    """评估样例必须绑定已维护的目的地，并至少断言一个事实词。"""
    for case in _load_cases():
        assert case["destination"] in CITY_GUIDES
        assert case.get("required_content_keywords")


def test_eval_cases_have_fixed_queries() -> None:
    """对照评测使用固定 query，避免 LLM 改写引入输入差异。"""
    for case in _load_cases():
        assert isinstance(case.get("query"), str)
        assert case["query"].strip()
