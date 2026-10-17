from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.rag.guide_catalog import GUIDE_DESTINATIONS, destination_for_guide
from app.rag.vector_db import DATA_DIR, load_guide_chunks
from app.services.fallback_candidates import extract_fallback_candidates


BACKEND_DIR = Path(__file__).resolve().parents[2]
RETRIEVAL_RULES_PATH = BACKEND_DIR / "data" / "retrieval_rules.json"
EVAL_CASES_PATH = BACKEND_DIR / "eval" / "rag_eval_cases.json"
_FALLBACK_CATEGORIES = ("spots", "meals", "hotels")


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _string_list(value: Any, label: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        errors.append(f"{label} 必须是非空字符串列表。")
        return []
    return [item.strip() for item in value]


def _read_guide_texts(errors: list[str]) -> dict[str, str]:
    guide_files = {path.name for path in DATA_DIR.glob("*.md*")}
    mapped_files = set(GUIDE_DESTINATIONS)
    for source_name in sorted(guide_files - mapped_files):
        errors.append(f"攻略文件未登记 destination 映射：{source_name}")
    for source_name in sorted(mapped_files - guide_files):
        errors.append(f"destination 映射指向不存在的攻略文件：{source_name}")

    guide_texts: dict[str, str] = {}
    for source_name, destination in GUIDE_DESTINATIONS.items():
        path = DATA_DIR / source_name
        if path.exists():
            guide_texts[destination] = path.read_text(encoding="utf-8")
    return guide_texts


def _validate_rule_list(
    rules: Any,
    label: str,
    source_text: str,
    errors: list[str],
) -> None:
    if not isinstance(rules, list) or not rules:
        errors.append(f"{label} 必须是非空规则列表。")
        return

    for index, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict):
            errors.append(f"{label} 第 {index} 条不是对象。")
            continue
        _string_list(rule.get("triggers"), f"{label} 第 {index} 条 triggers", errors)
        keywords = _string_list(rule.get("keywords"), f"{label} 第 {index} 条 keywords", errors)
        for keyword in keywords:
            if keyword not in source_text:
                errors.append(f"{label} 第 {index} 条扩展词未命中攻略：{keyword}")


def _validate_retrieval_rules(
    rules_config: Any,
    guide_texts: dict[str, str],
    errors: list[str],
) -> None:
    if not isinstance(rules_config, dict):
        errors.append("retrieval_rules.json 根节点必须是对象。")
        return

    all_guide_text = "\n".join(guide_texts.values())
    _validate_rule_list(
        rules_config.get("global_rules"), "global_rules", all_guide_text, errors
    )

    destinations = rules_config.get("destinations")
    if not isinstance(destinations, dict):
        errors.append("retrieval_rules.json 的 destinations 必须是对象。")
        return
    for destination, config in destinations.items():
        if destination not in guide_texts:
            errors.append(f"检索规则引用了未登记目的地：{destination}")
            continue
        if not isinstance(config, dict):
            errors.append(f"目的地 {destination} 的规则配置必须是对象。")
            continue
        _validate_rule_list(
            config.get("rules"),
            f"{destination} rules",
            guide_texts[destination],
            errors,
        )


def _chunk_to_rag_context(chunk: dict[str, str]) -> str:
    return f"[来源: {chunk['source']} | 标题: {chunk['title']}]\n{chunk['text']}"


def _validate_fallback_candidates(
    chunks: list[dict[str, str]],
    guide_texts: dict[str, str],
    errors: list[str],
) -> None:
    for destination, guide_text in guide_texts.items():
        destination_chunks = [
            chunk for chunk in chunks if chunk.get("destination") == destination
        ]
        if not destination_chunks:
            errors.append(f"目的地 {destination} 没有可入库的 Chunk。")
            continue

        candidates = extract_fallback_candidates(
            [_chunk_to_rag_context(chunk) for chunk in destination_chunks]
        )
        for category in _FALLBACK_CATEGORIES:
            names = candidates[category]
            if not names:
                errors.append(f"目的地 {destination} 缺少 {category} fallback 候选。")
                continue
            for name in names:
                if name not in guide_text:
                    errors.append(
                        f"目的地 {destination} 的 {category} fallback 候选未命中攻略：{name}"
                    )


def _validate_eval_cases(
    cases: Any,
    guide_texts: dict[str, str],
    chunks: list[dict[str, str]],
    errors: list[str],
) -> None:
    if not isinstance(cases, list) or not cases:
        errors.append("rag_eval_cases.json 必须是非空数组。")
        return

    titles_by_destination: dict[str, str] = {}
    for chunk in chunks:
        destination = chunk.get("destination", "")
        titles_by_destination[destination] = (
            f"{titles_by_destination.get(destination, '')}\n{chunk.get('title', '')}"
        )

    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            errors.append(f"评估样例第 {index} 条不是对象。")
            continue
        case_id = str(case.get("id", f"第 {index} 条"))
        destination = case.get("destination")
        if not isinstance(destination, str) or destination not in guide_texts:
            errors.append(f"评估样例 {case_id} 引用了未登记目的地：{destination}")
            continue

        expected_titles = _string_list(
            case.get("expected_title_keywords"),
            f"评估样例 {case_id} expected_title_keywords",
            errors,
        )
        if expected_titles and not any(
            keyword in titles_by_destination.get(destination, "")
            for keyword in expected_titles
        ):
            errors.append(f"评估样例 {case_id} 没有任何标题断言命中 {destination} 攻略。")

        required_keywords = _string_list(
            case.get("required_content_keywords"),
            f"评估样例 {case_id} required_content_keywords",
            errors,
        )
        for keyword in required_keywords:
            if keyword not in guide_texts[destination]:
                errors.append(f"评估样例 {case_id} 必需词未命中攻略：{keyword}")

        _string_list(
            case.get("noise_title_keywords"),
            f"评估样例 {case_id} noise_title_keywords",
            errors,
        )


def validate_knowledge_base() -> list[str]:
    """校验当前攻略、规则、fallback 与 RAG 评估断言是否保持一致。"""
    errors: list[str] = []
    guide_texts = _read_guide_texts(errors)
    chunks = load_guide_chunks()

    for chunk in chunks:
        expected_destination = destination_for_guide(chunk.get("source", ""))
        if not expected_destination or chunk.get("destination") != expected_destination:
            errors.append(
                "Chunk destination metadata 不合法："
                f"source={chunk.get('source')}, destination={chunk.get('destination')}"
            )

    _validate_retrieval_rules(_load_json(RETRIEVAL_RULES_PATH), guide_texts, errors)
    _validate_fallback_candidates(chunks, guide_texts, errors)
    _validate_eval_cases(_load_json(EVAL_CASES_PATH), guide_texts, chunks, errors)
    return errors
