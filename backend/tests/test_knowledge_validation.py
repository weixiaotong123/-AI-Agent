from pathlib import Path
import sys


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.rag import knowledge_validation  # noqa: E402


def test_current_knowledge_base_is_consistent() -> None:
    """当前攻略、规则、fallback 和评估断言必须保持一致。"""
    assert knowledge_validation.validate_knowledge_base() == []


def test_validator_rejects_rule_keyword_missing_from_destination_source() -> None:
    """故意写入失效扩展词时，校验必须明确失败。"""
    errors: list[str] = []
    knowledge_validation._validate_retrieval_rules(
        {
            "global_rules": [
                {"triggers": ["历史"], "keywords": ["故宫"]},
            ],
            "destinations": {
                "北京": {
                    "rules": [
                        {"triggers": ["不存在"], "keywords": ["不存在的景点"]},
                    ]
                }
            },
        },
        {"北京": "故宫博物院"},
        errors,
    )

    assert errors == ["北京 rules 第 1 条扩展词未命中攻略：不存在的景点"]
