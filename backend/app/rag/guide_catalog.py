from pathlib import Path


GUIDE_DESTINATIONS = {
    "beijing_guide.md": "北京",
    "chengdu_guide.md": "成都",
    "dali_guide.md": "大理",
    "sanya_guide.md": "三亚",
    "xiamen_guide.md": "厦门",
    "xian_guide.md": "西安",
}


def destination_for_guide(source_name: str) -> str | None:
    """根据攻略文件名返回规范目的地名称。"""
    return GUIDE_DESTINATIONS.get(Path(source_name).name)


def known_destinations() -> set[str]:
    """返回当前攻略目录中已维护的全部目的地。"""
    return set(GUIDE_DESTINATIONS.values())
