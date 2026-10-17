"""
知识库生成脚本：高德 POI + LLM 生成攻略 markdown。

用法：
    python scripts/generate_guide.py --city 北京
    python scripts/generate_guide.py --city 杭州 --spots 6 --foods 6 --hotels 4
"""

import argparse
import json
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MAX_RETRIES,
    LLM_MODEL,
    LLM_TIMEOUT_SECONDS,
)
from app.rag.vector_db import ingest_guide_chunks_to_chroma
from app.services.map_service import search_places


DATA_DIR = BACKEND_DIR / "data"

# 中文城市名 → 英文文件名映射
CITY_EN_MAP: dict[str, str] = {
    "北京": "beijing", "上海": "shanghai", "广州": "guangzhou", "深圳": "shenzhen",
    "成都": "chengdu", "重庆": "chongqing", "杭州": "hangzhou", "西安": "xian",
    "南京": "nanjing", "武汉": "wuhan", "长沙": "changsha", "苏州": "suzhou",
    "大理": "dali", "三亚": "sanya", "厦门": "xiamen", "昆明": "kunming",
    "青岛": "qingdao", "大连": "dalian", "桂林": "guilin", "洛阳": "luoyang",
    "哈尔滨": "harbin", "拉萨": "lasa", "敦煌": "dunhuang", "丽江": "lijiang",
    "张家界": "zhangjiajie", "黄山": "huangshan", "九寨沟": "jiuzhaigou",
    "贵阳": "guiyang", "南宁": "nanning", "福州": "fuzhou", "厦门": "xiamen",
    "天津": "tianjin", "济南": "jinan", "郑州": "zhengzhou", "太原": "taiyuan",
    "石家庄": "shijiazhuang", "合肥": "hefei", "南昌": "nanchang",
    "海口": "haikou", "珠海": "zhuhai", "无锡": "wuxi", "宁波": "ningbo",
    "东莞": "dongguan", "佛山": "foshan", "温州": "wenzhou",
}


def _city_to_filename(city: str) -> str:
    """中文城市名转英文文件名，未命中时直接用原名。"""
    return CITY_EN_MAP.get(city, city)


# ── 高德 POI 拉取 ──────────────────────────────────────────────


def fetch_pois(city: str, keyword: str, page_size: int) -> list[dict]:
    """调用高德 POI 搜索，返回精简字段列表。"""
    raw = search_places(keyword=keyword, city=city, page_size=page_size)
    seen_names: set[str] = set()
    pois = []
    for item in raw:
        name = item.get("name", "")
        if name in seen_names:
            continue
        seen_names.add(name)
        pois.append(
            {
                "name": name,
                "address": item.get("address", ""),
                "type": item.get("type", ""),
                "latitude": item.get("latitude"),
                "longitude": item.get("longitude"),
            }
        )
    return pois


def fetch_pois_multi(city: str, keywords: list[str], page_size_each: int) -> list[dict]:
    """用多个关键词搜索 POI，去重合并后返回。"""
    seen_names: set[str] = set()
    all_pois: list[dict] = []
    for i, kw in enumerate(keywords):
        if i > 0:
            time.sleep(0.5)
        raw = search_places(keyword=kw, city=city, page_size=page_size_each)
        for item in raw:
            name = item.get("name", "")
            if name in seen_names:
                continue
            seen_names.add(name)
            all_pois.append(
                {
                    "name": name,
                    "address": item.get("address", ""),
                    "type": item.get("type", ""),
                    "latitude": item.get("latitude"),
                    "longitude": item.get("longitude"),
                }
            )
    return all_pois


# ── LLM 调用 ───────────────────────────────────────────────────


SYSTEM_PROMPT = """\
你是旅行攻略专家。根据以下真实 POI 数据，生成一份 Markdown 格式的旅行攻略。

输出要求：
1. 严格按以下模板结构输出，不要添加额外板块，不要输出代码块标记
2. 景点、餐饮、酒店信息必须基于提供的 POI 数据，使用真实商户名称（不要用泛称如"某酒店""某餐厅"）
3. 餐饮板块：必须覆盖不同预算区间——
   - 经济实惠（人均 30 元以下的小吃/快餐）
   - 中档特色（人均 50-150 元的特色餐厅）
   - 高端体验（人均 200 元以上的知名餐厅）
   每条写明真实餐厅名称（来自 POI 数据）、招牌菜、人均预算
4. 住宿板块：必须覆盖不同预算区间——
   - 经济型（200 元/晚以下的快捷酒店或青旅）
   - 舒适型（200-500 元/晚的商务酒店或民宿）
   - 高端型（500 元/晚以上的星级酒店或精品民宿）
   每条写明真实酒店名称（来自 POI 数据）、位置优势、价格区间
5. 行程推荐必须引用前面提到的景点和餐饮，用【名称】标注
6. 每个景点用 ### 2.x 编号，每个餐饮/住宿条目用 * 列表格式
7. 不要输出 ```markdown``` 代码块标记，直接输出 markdown 原文"""


def build_human_prompt(
    city: str, spots: list[dict], foods: list[dict], hotels: list[dict]
) -> str:
    return f"""\
城市：{city}

景点 POI 数据（共 {len(spots)} 个）：
{json.dumps(spots, ensure_ascii=False, indent=2)}

餐饮/美食 POI 数据（共 {len(foods)} 个）—— 请使用以下真实餐厅名称推荐：
{json.dumps(foods, ensure_ascii=False, indent=2)}

酒店/民宿 POI 数据（共 {len(hotels)} 个）—— 请使用以下真实酒店名称推荐：
{json.dumps(hotels, ensure_ascii=False, indent=2)}

请按以下模板生成攻略：

# 2026 {city}深度游玩全攻略

## 1. 目的地简介
（一段城市概况，包含地理、气候、文化特色、适合人群）

## 2. 核心景点推荐 (含门票与位置信息)
### 2.1 景点名
* **位置**：地址
* **门票**：价格
* **游玩时长**：建议时长
* **简介**：景点描述
（每个景点一个 ### 小节）

## 3. 特色餐饮与预算参考
（按预算分组，每组 2-3 家餐厅，必须使用 POI 数据中的真实名称）
### 经济实惠（人均 30 元以下）
* **【餐厅名】招牌菜**：简介，人均预算 **价格**。
### 中档特色（人均 50-150 元）
* **【餐厅名】招牌菜**：简介，人均预算 **价格**。
### 高端体验（人均 200 元以上）
* **【餐厅名】招牌菜**：简介，人均预算 **价格**。

## 4. 住宿区域建议
（按预算分组，每组 2-3 家酒店，必须使用 POI 数据中的真实名称）
### 经济型（200 元/晚以下）
* **【酒店名】**：简介、位置优势。酒店预算：**价格/晚**。
### 舒适型（200-500 元/晚）
* **【酒店名】**：简介、位置优势。酒店预算：**价格/晚**。
### 高端型（500 元/晚以上）
* **【酒店名】**：简介、位置优势。酒店预算：**价格/晚**。

## 5. 经典三日行程参考 (Agent 提取样本)
* **第一天：主题**
  * 上午：活动（用【景点名】标注）
  * 下午：活动
  * 晚上：活动（可推荐具体餐厅名）
* **第二天：...**
* **第三天：...**"""


def generate_markdown(
    city: str, spots: list[dict], foods: list[dict], hotels: list[dict]
) -> str:
    """调用 LLM 生成 markdown 攻略文本。"""
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model=LLM_MODEL,
        temperature=0.3,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL or None,
        timeout=300,
        max_retries=LLM_MAX_RETRIES,
    )

    human_prompt = build_human_prompt(city, spots, foods, hotels)
    response = llm.invoke([("system", SYSTEM_PROMPT), ("human", human_prompt)])
    return response.content


# ── 保存与入库 ──────────────────────────────────────────────────


def save_guide(city: str, markdown: str) -> Path:
    """保存攻略到 backend/data/{english_name}_guide.md，返回文件路径。"""
    en_name = _city_to_filename(city)
    filename = f"{en_name}_guide.md"
    filepath = DATA_DIR / filename
    filepath.write_text(markdown, encoding="utf-8")
    return filepath


# ── 主流程 ──────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="高德 POI + LLM 生成旅行攻略")
    parser.add_argument("--city", required=True, help="城市名，如 北京、杭州")
    parser.add_argument("--spots", type=int, default=5, help="每个关键词的景点数量（默认 5）")
    parser.add_argument("--foods", type=int, default=4, help="每个关键词的餐饮数量（默认 4）")
    parser.add_argument("--hotels", type=int, default=3, help="每个关键词的酒店数量（默认 3）")
    parser.add_argument(
        "--skip-ingest", action="store_true", help="跳过 ChromaDB 入库步骤"
    )
    args = parser.parse_args()

    city = args.city

    # ① 拉取 POI 数据（多关键词搜索，获取更丰富的商户数据）
    print(f"[1/4] 正在拉取 {city} 的 POI 数据...")
    spots = fetch_pois_multi(
        city,
        [f"{city}景点", f"{city}名胜古迹", f"{city}网红打卡地"],
        args.spots,
    )
    foods = fetch_pois_multi(
        city,
        [
            f"{city}特色小吃",
            f"{city}老字号餐厅",
            f"{city}网红餐厅",
            f"{city}高端餐厅",
            f"{city}苍蝇馆子",
        ],
        args.foods,
    )
    hotels = fetch_pois_multi(
        city,
        [
            f"{city}经济型酒店",
            f"{city}民宿客栈",
            f"{city}星级酒店",
            f"{city}网红民宿",
        ],
        args.hotels,
    )
    print(f"      景点: {len(spots)} 个, 美食: {len(foods)} 个, 酒店: {len(hotels)} 个")

    if not spots and not foods and not hotels:
        print("警告: 高德未返回任何 POI 数据，将使用 LLM 通用知识生成。")

    # ② 调用 LLM 生成攻略
    print("[2/4] 正在调用 LLM 生成攻略...")
    markdown = generate_markdown(city, spots, foods, hotels)
    if not markdown or len(markdown) < 200:
        print("错误: LLM 生成内容过短或为空，已跳过保存。")
        sys.exit(1)

    # ③ 保存文件
    filepath = save_guide(city, markdown)
    print(f"[3/4] 已保存: {filepath}")

    # ④ 入库 ChromaDB
    if args.skip_ingest:
        print("[4/4] 已跳过 ChromaDB 入库。")
    else:
        print("[4/4] 正在重新入库 ChromaDB...")
        count = ingest_guide_chunks_to_chroma()
        print(f"      入库完成，当前共 {count} 个片段。")

    print(f"\n✅ {city} 攻略生成完成！")


if __name__ == "__main__":
    main()
