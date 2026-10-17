
import json
import logging
from functools import lru_cache

# 从项目配置文件中读取各种配置
from app.config import (
    BACKEND_DIR,          # backend 目录路径
    LLM_API_KEY,          # 大模型 API Key
    LLM_BASE_URL,         # 大模型 API 地址
    LLM_MAX_RETRIES,      # LLM 请求失败后的最大重试次数
    LLM_MODEL,            # 使用的 LLM 模型名称
    LLM_TIMEOUT_SECONDS,  # LLM 请求超时时间
)

# 真正执行 RAG 检索的函数
# 注意：rag_tool.py 本身不负责向 Chroma 搜索
from app.rag.retriever import retrieve_travel_guide


# 创建日志对象，用于输出运行过程中的提示、警告、错误等信息
logger = logging.getLogger(__name__)


# 检索规则配置文件的位置
#
# 项目目录大概是：
#
# backend/
# └── data/
#     └── retrieval_rules.json
#
# 这个 JSON 文件里面保存一些“用户说了什么 → 应该增加什么检索关键词”的规则
RETRIEVAL_RULES_PATH = BACKEND_DIR / "data" / "retrieval_rules.json"


# ============================================================
# 1. 工具函数：当不同来源产生关键词以后，把这些关键词加入列表，并防止重复。
# ============================================================

def _append_unique(parts: list[str], value: str) -> None:
    """
    向 parts 列表中添加一个关键词。

    但是有两个要求：
    1. 去掉关键词前后的空格
    2. 如果关键词已经存在，就不再重复添加

    例如：

    parts = ["大理", "自然风景"]

    添加 "自然风景"
    ↓
    结果仍然是：
    ["大理", "自然风景"]

    添加 "苍山"
    ↓
    结果变成：
    ["大理", "自然风景", "苍山"]
    """

    # 去掉字符串前后的空格
    normalized = value.strip()

    # 如果关键词为空，就不添加
    if not normalized:
        return

    # 如果关键词还没有出现过，就添加进去
    if normalized not in parts:
        parts.append(normalized)


# ============================================================
# 2. 读取 retrieval_rules.json
# ============================================================

@lru_cache(maxsize=1)
def _load_retrieval_rules() -> dict:
    """
    读取检索规则 JSON 文件。

    @lru_cache(maxsize=1) 的作用：
    第一次读取 JSON 后，把结果缓存起来。

    后面再次调用这个函数时，
    不需要重新读取磁盘上的 JSON 文件，
    直接使用缓存的数据。

    如果 JSON 文件不存在、无法读取或者格式错误，
    就返回空规则，避免整个 Agent 因为配置文件出错而崩溃。
    """

    try:
        # 读取 JSON 文件内容
        #
        # read_text()：读取文本
        # encoding="utf-8"：使用 UTF-8 编码
        #
        # json.loads()：
        # 把 JSON 字符串转换成 Python 字典
        raw_data = json.loads(
            RETRIEVAL_RULES_PATH.read_text(encoding="utf-8")
        )

    # 如果文件不存在 / 无法读取
    # 或者 JSON 格式错误
    except (OSError, json.JSONDecodeError) as exc:

        # 输出警告日志
        logger.warning(
            "Unable to load retrieval rules from %s: %s",
            RETRIEVAL_RULES_PATH,
            exc
        )

        # 不让程序崩溃
        # 返回一个空的规则结构
        return {
            "global_rules": [],
            "destinations": {}
        }

    # 如果 JSON 最外层不是字典
    # 例如错误地写成了一个 list
    if not isinstance(raw_data, dict):

        logger.warning(
            "Retrieval rules must be a JSON object: %s",
            RETRIEVAL_RULES_PATH
        )

        # 同样返回空规则
        return {
            "global_rules": [],
            "destinations": {}
        }

    # JSON 格式正常，直接返回
    return raw_data


# ============================================================
# 3. 检查规则格式是否正确
# ============================================================

def _valid_rules(value: object) -> list[dict[str, list[str]]]:
    """
    检查一组检索规则的格式。

    一条合法规则应该类似：

    {
        "triggers": ["亲子", "带孩子"],
        "keywords": ["亲子游", "儿童友好"]
    }

    triggers：
        用户说什么词时触发这个规则

    keywords：
        触发之后，要增加哪些检索关键词
    """

    # 如果传进来的东西不是 list
    # 说明规则格式不正确
    if not isinstance(value, list):
        return []

    # 用来保存检查通过的规则
    valid_rules: list[dict[str, list[str]]] = []

    # 一条一条检查规则
    for item in value:

        # 每条规则必须是字典
        if not isinstance(item, dict):
            continue

        # 取出 triggers
        triggers = item.get("triggers")

        # 取出 keywords
        keywords = item.get("keywords")

        # triggers 和 keywords 都必须是 list
        if not isinstance(triggers, list) or not isinstance(keywords, list):
            continue

        # triggers 和 keywords 中的每一个元素
        # 都必须是字符串
        #
        # 例如：
        # ["亲子", "带孩子"]       √
        #
        # ["亲子", 123]            ×
        if not all(
            isinstance(term, str)
            for term in [*triggers, *keywords]
        ):
            continue

        # triggers 和 keywords 都不能为空
        if triggers and keywords:

            # 保存合法规则
            valid_rules.append({
                "triggers": triggers,
                "keywords": keywords
            })

    # 返回所有合法规则
    return valid_rules


# ============================================================
# 4. 获取当前目的地对应的检索规则
# ============================================================

def _destination_rules(
    destination: str | None
) -> list[dict[str, list[str]]]:
    """
    获取检索规则。

    最终得到的规则包括两部分：

    ① 全局规则
    ② 当前城市专属规则

    例如：

    全局规则：
    “亲子” → “亲子游、儿童友好”

    大理专属规则：
    “苍山” → “苍山、徒步、自然风光”

    如果当前目的地是“大理”，
    那么两套规则都会返回。
    """

    # 读取 retrieval_rules.json
    config = _load_retrieval_rules()

    # 先获取全局规则
    rules = _valid_rules(
        config.get("global_rules")
    )

    # 如果没有目的地
    # 那么只能返回全局规则
    if not destination:
        return rules

    # 获取 destinations 配置
    destinations = config.get("destinations")

    # 如果 destinations 不是字典
    # 就只使用全局规则
    if not isinstance(destinations, dict):
        return rules

    # 遍历每个城市的配置
    #
    # 例如：
    #
    # "大理": {
    #     "rules": [...]
    # }
    #
    # "北京": {
    #     "rules": [...]
    # }
    for configured_destination, destination_config in destinations.items():

        # 判断当前配置的城市是否匹配用户目的地
        #
        # 使用：
        # configured_destination not in destination
        #
        # 可以兼容：
        #
        # 配置：大理
        # 用户输入：大理市
        #
        # 因为：
        # "大理" in "大理市" == True
        if (
            not isinstance(configured_destination, str)
            or configured_destination not in destination
        ):
            continue

        # 城市配置必须是字典
        if not isinstance(destination_config, dict):
            continue

        # 获取这个城市自己的 rules
        # 并加入到 rules 中
        rules.extend(
            _valid_rules(
                destination_config.get("rules")
            )
        )

    # 最终返回：
    #
    # 全局规则 + 当前城市规则
    return rules


# ============================================================
# 5. 从用户备注中提取额外检索关键词
# ============================================================

def _extract_note_keywords(
    special_notes: str | None,
    destination: str | None = None
) -> list[str]:
    """
    根据检索规则，从用户的特殊备注中提取关键词。

    例如用户备注：

    “希望适合带孩子，不要太累”

    如果规则是：

    triggers:
        ["带孩子"]

    keywords:
        ["亲子游", "儿童友好"]

    那么最终返回：

    ["亲子游", "儿童友好"]
    """

    # 没有备注，直接返回空列表
    if not special_notes:
        return []

    # 用来保存最终提取出来的关键词
    keywords: list[str] = []

    # 去掉备注前后的空格
    note = special_notes.strip()

    # 获取当前目的地相关的所有规则
    for rule in _destination_rules(destination):

        # 判断用户备注中是否出现了某个 trigger
        #
        # 例如：
        # trigger = "带孩子"
        # note = "希望适合带孩子"
        #
        # "带孩子" in note → True
        if any(
            trigger in note
            for trigger in rule["triggers"]
        ):

            # 如果触发了规则，
            # 就把对应 keywords 加进去
            for value in rule["keywords"]:

                # 添加关键词，并避免重复
                _append_unique(keywords, value)

    # 返回提取出来的关键词
    return keywords


# ============================================================
# 6. 创建 LLM
# ============================================================

def _build_chat_llm():
    """
    创建 ChatOpenAI 实例。

    这里的 LLM 不是用来直接回答用户问题的。

    它在这个文件中的主要任务是：

    用户旅行需求
          ↓
    LLM Query Rewrite
          ↓
    更适合 RAG 检索的关键词

    如果没有 API Key，
    或者没有安装 langchain_openai，
    就返回 None。
    """

    # 没有 API Key，无法调用 LLM
    if not LLM_API_KEY:
        return None

    try:
        # 导入 LangChain 的 ChatOpenAI
        from langchain_openai import ChatOpenAI

    # 如果没有安装这个包
    except ImportError:
        return None

    # 创建 ChatOpenAI
    return ChatOpenAI(
        model=LLM_MODEL,                 # 使用哪个模型
        temperature=0.2,                 # 温度较低，让输出更加稳定
        api_key=LLM_API_KEY,             # API Key
        base_url=LLM_BASE_URL or None,   # API 地址
        timeout=LLM_TIMEOUT_SECONDS,     # 超时时间
        max_retries=LLM_MAX_RETRIES,     # 最大重试次数
    )


# ============================================================
# 7. 获取 LLM 使用了多少 Token
# ============================================================

def _extract_token_usage(response) -> dict[str, int]:
    """
    从 LangChain 返回的 AIMessage 中提取 Token 使用量。

    最终返回：

    {
        "prompt_tokens": 输入 Token 数量,
        "completion_tokens": 输出 Token 数量
    }
    """

    # 默认都是 0
    usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0
    }

    # 获取 response 的 metadata
    metadata = getattr(
        response,
        "response_metadata",
        None
    ) or {}

    # 从 metadata 中获取 token_usage
    token_usage = metadata.get(
        "token_usage",
        {}
    )

    # 如果存在 Token 使用信息
    if token_usage:

        # 输入 Token
        usage["prompt_tokens"] = token_usage.get(
            "prompt_tokens",
            0
        )

        # 输出 Token
        usage["completion_tokens"] = token_usage.get(
            "completion_tokens",
            0
        )

    return usage


# ============================================================
# 8. 使用 LLM 改写 Query
# ============================================================

def llm_rewrite_query(
    destination: str,
    preferences: list[str] | None = None,
    pace: str | None = None,
    special_notes: str | None = None,
) -> tuple[str | None, dict[str, int]]:
    """
    使用 LLM 把用户的旅行需求改写成适合向量检索的 Query。

    返回两个东西：

    ① query
    ② token_usage

    例如用户：

    目的地：大理
    偏好：自然风景、拍照
    节奏：慢节奏
    备注：想吃当地特色美食

    LLM 可能改写成：

    大理 自然风景 洱海 苍山 摄影 慢节奏
    特色美食 餐厅

    这个 Query 最后会交给 Retriever。
    """

    # 如果 LLM 不可用
    # 返回 None + 空 Token 统计
    empty_usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0
    }

    # 创建 LLM
    llm = _build_chat_llm()

    # 没有 LLM，直接返回
    if llm is None:
        return None, empty_usage

    # --------------------------------------------------------
    # 给 LLM 的系统提示词
    # --------------------------------------------------------

    system_prompt = (
        "你是一个 RAG 检索 query 改写专家。"

        # 告诉 LLM：
        # 你不是负责直接回答用户问题，
        # 而是负责生成检索 Query
        "你的任务是把用户的旅行需求改写成适合向量检索的关键词组合。"

        # 输出格式要求
        "输出要求："

        # 只输出关键词
        "1. 只输出检索关键词，用空格分隔"

        # 不允许输出解释
        "2. 不要输出解释、标点或任何多余文字"

        # 优先生成具体信息
        "3. 关键词要具体，优先包含景点名称、活动类型、场景特征"

        # 必须包含城市
        "4. 包含目的地城市名"
    )

    # --------------------------------------------------------
    # 构造用户输入
    # --------------------------------------------------------

    # 第一个一定放目的地
    parts = [
        f"目的地：{destination}"
    ]

    # 如果用户填写了偏好
    #
    # 例如：
    # ["自然风景", "拍照"]
    #
    # 转成：
    # 偏好：自然风景、拍照
    if preferences:
        parts.append(
            f"偏好：{'、'.join(preferences)}"
        )

    # 如果填写了旅行节奏
    #
    # 例如：
    # 节奏：慢节奏
    if pace:
        parts.append(
            f"节奏：{pace}"
        )

    # 如果填写了特殊备注
    if special_notes:
        parts.append(
            f"备注：{special_notes}"
        )

    # 把上面的内容用换行连接
    #
    # 最终可能变成：
    #
    # 目的地：大理
    # 偏好：自然风景、拍照
    # 节奏：慢节奏
    # 备注：想吃当地特色美食
    human_prompt = "\n".join(parts)

    try:

        # 调用 LLM
        response = llm.invoke([
            # System Prompt
            ("system", system_prompt),

            # 用户的实际旅行需求
            ("human", human_prompt),
        ])

        # 获取 Token 使用量
        token_usage = _extract_token_usage(response)

        # 获取 LLM 输出内容
        raw_text = getattr(
            response,
            "content",
            ""
        )

        # 某些模型返回的 content 可能是 list
        if isinstance(raw_text, list):

            # 把多个文本片段拼起来
            raw_text = "".join(
                item.get("text", "")
                if isinstance(item, dict)
                else str(item)
                for item in raw_text
            )

        # 去掉前后空格
        query = raw_text.strip()

        # 如果 LLM 确实生成了 Query
        if query:

            # 记录日志
            logger.info(
                "llm_rewrite_query: input=%s -> output=%s",
                human_prompt,
                query
            )

            # 返回：
            # Query + Token 使用量
            return query, token_usage

    # 如果 LLM 调用失败
    except Exception:

        # 记录警告
        #
        # exc_info=True：
        # 同时记录详细异常信息
        logger.warning(
            "llm_rewrite_query failed, falling back to rule-based",
            exc_info=True
        )

    # LLM 失败时返回 None
    #
    # 外层函数收到 None 后，
    # 就会启动 rule-based fallback
    return None, empty_usage


# ============================================================
# 9. 规则式 Query Rewrite
# ============================================================

def _rule_based_query(
    destination: str,
    preferences: list[str] | None = None,
    pace: str | None = None,
    special_notes: str | None = None,
) -> str:
    """
    规则式 Query Rewrite。

    这是 LLM Query Rewrite 的 fallback。

    也就是说：

    LLM 能用
        ↓
    使用 LLM

    LLM 不能用
        ↓
    使用这里的规则生成 Query
    """

    # 第一项一定是目的地
    parts: list[str] = [
        destination
    ]

    # 添加用户偏好
    if preferences:

        for preference in preferences:

            # 加入偏好，并避免重复
            _append_unique(
                parts,
                preference
            )

    # 添加旅行节奏
    if pace:

        _append_unique(
            parts,
            pace
        )

    # 从特殊备注中提取额外关键词
    for keyword in _extract_note_keywords(
        special_notes,
        destination=destination
    ):

        _append_unique(
            parts,
            keyword
        )

    # 加入一些稳定的旅游检索关键词
    #
    # 这些词可以帮助向量数据库找到旅游攻略相关内容
    for stable_term in [
        "景点",
        "行程",
        "攻略",
        "推荐",
        "餐饮",
        "住宿"
    ]:

        _append_unique(
            parts,
            stable_term
        )

    # 把所有关键词用空格连接
    #
    # 例如：
    #
    # ["大理", "自然风景", "慢节奏", "景点", "攻略"]
    #
    # ↓
    #
    # "大理 自然风景 慢节奏 景点 攻略"
    return " ".join(
        part
        for part in parts
        if part
    ).strip()


# ============================================================
# 10. Query 构造的统一入口
# ============================================================

def build_destination_query(
    destination: str,
    preferences: list[str] | None = None,
    pace: str | None = None,
    special_notes: str | None = None,
) -> tuple[str, dict[str, int]]:
    """
    构造最终用于 RAG 检索的 Query。

    优先级：

    ① LLM Query Rewrite
             ↓
       成功 → 使用 LLM Query

    ② LLM 失败
             ↓
       使用 Rule-based Query
    """

    # 首先尝试 LLM Rewrite
    llm_query, token_usage = llm_rewrite_query(
        destination=destination,
        preferences=preferences,
        pace=pace,
        special_notes=special_notes,
    )

    # LLM 成功生成 Query
    if llm_query:

        # 直接使用 LLM 的结果
        return llm_query, token_usage

    # LLM 不可用，记录日志
    logger.info(
        "build_destination_query: "
        "LLM rewrite unavailable, using rule-based"
    )

    # 使用规则式 Query Rewrite
    return (
        _rule_based_query(
            destination=destination,
            preferences=preferences,
            pace=pace,
            special_notes=special_notes,
        ),
        {
            "prompt_tokens": 0,
            "completion_tokens": 0
        }
    )


# ============================================================
# 11. 兼容旧代码的函数
# ============================================================

def _build_destination_query(
    destination: str,
    preferences: list[str] | None = None,
    pace: str | None = None,
    special_notes: str | None = None,
) -> tuple[str, dict[str, int]]:
    """
    兼容以前的代码。

    如果旧代码还在调用：

        _build_destination_query()

    也不会报错。

    实际上真正的工作已经交给：

        build_destination_query()
    """

    return build_destination_query(
        destination=destination,
        preferences=preferences,
        pace=pace,
        special_notes=special_notes,
    )


# ============================================================
# 12. 获取目的地的 RAG 上下文
# ============================================================

def get_destination_guide_context(
    destination: str,
    preferences: list[str] | None = None,
    pace: str | None = None,
    special_notes: str | None = None,
    top_k: int = 5,
) -> tuple[
    list[str],
    dict[str, int],
    dict[str, int],
    dict[str, int]
]:
    """
    获取某个城市的旅游攻略 RAG 上下文。

    整个函数可以理解成：

    用户需求
       ↓
    Query Rewrite
       ↓
    RAG 检索
       ↓
    补充住宿信息
       ↓
    补充餐饮信息
       ↓
    去重
       ↓
    返回最终 contexts

    最终返回四个东西：

    1. contexts
       检索到的攻略文本

    2. rewrite_usage
       Query Rewrite 使用的 Token

    3. rerank_usage
       Rerank 使用的 Token

    4. embedding_usage
       Embedding 使用的 Token
    """

    # --------------------------------------------------------
    # 第一步：生成 Query
    # --------------------------------------------------------

    query, rewrite_usage = build_destination_query(
        destination=destination,
        preferences=preferences,
        pace=pace,
        special_notes=special_notes,
    )

    # --------------------------------------------------------
    # 第二步：真正执行 RAG 检索
    # --------------------------------------------------------
    #
    # 注意：
    # 这里才调用真正的 Retriever。
    #
    # rag_tool.py 本身不负责：
    # - embedding
    # - Chroma 搜索
    # - rerank
    #
    # 这些工作交给：
    #
    # retrieve_travel_guide()
    #

    contexts, rerank_usage, embedding_usage = retrieve_travel_guide(
        query=query,                # 上面生成的 Query
        top_k=top_k,                # 返回前几个结果
        destination=destination     # 限制在当前城市
    )

    # --------------------------------------------------------
    # 第三步：准备去重集合
    # --------------------------------------------------------

    # contexts 是一个 list
    #
    # 转成 set 后，可以快速判断：
    # 某个 chunk 是否已经存在
    existing_set = set(contexts)

    # --------------------------------------------------------
    # 第四步：按实体类别补充检索景点、住宿和餐饮
    # --------------------------------------------------------

    # 主 Query 不一定能检索到足够的：
    #
    # 景点、酒店 / 民宿
    #
    # 餐厅 / 美食
    #
    # 所以这里按类别额外执行检索。
    supplement_queries = [
        (f"{destination} 景点 旅游景区 游览地点", top_k),
        (f"{destination} 住宿 酒店 民宿", 2),
        (f"{destination} 餐饮 美食 餐厅", 2),
    ]
    for supplement_query, supplement_top_k in supplement_queries:

        # 再次调用 Retriever
        #
        # 景点查询使用主检索的 top_k，尽量为多日行程补足真实候选。
        extra_contexts, extra_rerank, extra_embed = retrieve_travel_guide(
            query=supplement_query,
            top_k=supplement_top_k,
            destination=destination
        )

        # ----------------------------------------------------
        # 第五步：把补充检索结果加入 contexts
        # ----------------------------------------------------

        for ctx in extra_contexts:

            # 如果这个 chunk 之前没有出现过
            if ctx not in existing_set:

                # 加入最终 contexts
                contexts.append(ctx)

                # 同时加入 set
                # 防止后面再次添加
                existing_set.add(ctx)

        # ----------------------------------------------------
        # 第六步：统计额外检索消耗的 Token
        # ----------------------------------------------------

        # 累加住宿 / 餐饮检索产生的 Rerank Token
        rerank_usage["prompt_tokens"] += (
            extra_rerank.get(
                "prompt_tokens",
                0
            )
        )

        rerank_usage["completion_tokens"] += (
            extra_rerank.get(
                "completion_tokens",
                0
            )
        )

        # 累加住宿 / 餐饮检索产生的 Embedding Token
        embedding_usage["prompt_tokens"] += (
            extra_embed.get(
                "prompt_tokens",
                0
            )
        )

        embedding_usage["completion_tokens"] += (
            extra_embed.get(
                "completion_tokens",
                0
            )
        )

    # --------------------------------------------------------
    # 第七步：返回最终结果
    # --------------------------------------------------------

    return (
        contexts,          # 最终 RAG 上下文
        rewrite_usage,     # Query Rewrite Token
        rerank_usage,      # Rerank Token
        embedding_usage    # Embedding Token
    )
