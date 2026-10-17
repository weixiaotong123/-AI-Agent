from __future__ import annotations

import re


_RESTRICTION_RULES = (
    (("花生"), ("花生",)),
    (("海鲜", "鱼类", "水产"), ("海鲜", "鱼", "虾", "蟹", "贝", "牡蛎", "鱿鱼", "章鱼")),
    (("坚果", "核桃", "杏仁"), ("坚果", "核桃", "杏仁", "腰果", "榛子")),
    (("严格素食", "素食", "纯素"), ("牛肉", "羊肉", "猪肉", "鸡肉", "鸭肉", "鱼", "虾", "蟹", "肉类")),
    (("不饮酒", "禁酒", "不含酒"), ("白酒", "啤酒", "黄酒", "料酒", "酒酿", "酒")),
    (("不吃辣", "忌辣", "少辣"), ("麻辣", "香辣", "酸辣", "辣椒", "辣味")),
)
_NEGATIONS = ("不含", "不加", "不放", "不添加", "无", "避免", "忌用")
_SAFETY_WARNINGS = ("请告知", "提醒服务员", "告知服务员", "请注意", "请确认过敏", "避免食用")


def _restriction_terms(restrictions: list[str]) -> set[str]:
    terms: set[str] = set()
    for restriction in restrictions:
        for triggers, blocked_terms in _RESTRICTION_RULES:
            if any(trigger in restriction for trigger in triggers):
                terms.update(blocked_terms)
    return terms


def _contains_unnegated_term(text: str, term: str) -> bool:
    start = 0
    while True:
        index = text.find(term, start)
        if index < 0:
            return False
        prefix = text[max(0, index - 6) : index]
        if not any(negation in prefix for negation in _NEGATIONS):
            return True
        start = index + len(term)


def dietary_conflicts(text: str, restrictions: list[str]) -> list[str]:
    terms = _restriction_terms(restrictions)
    if not terms or not text:
        return []

    recommendation_clauses = re.split(r"[。！？；;\n]+", text)
    conflicts: set[str] = set()
    for clause in recommendation_clauses:
        for marker in _SAFETY_WARNINGS:
            clause = clause.split(marker, maxsplit=1)[0]
        for term in terms:
            if _contains_unnegated_term(clause, term):
                conflicts.add(term)
    return sorted(conflicts)