from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import httpx


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import (
    EMBEDDING_MODEL,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_TIMEOUT_SECONDS,
)


def mask_api_key(value: str) -> str:
    if not value:
        return "<EMPTY>"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def response_error(response: httpx.Response) -> str:
    try:
        body: Any = response.json()
    except ValueError:
        return response.text[:500]

    error = body.get("error", body) if isinstance(body, dict) else body
    if not isinstance(error, dict):
        return str(error)[:500]

    code = error.get("code", "")
    message = error.get("message", "未知错误")
    detail = f"code={code}, message={message}" if code else f"message={message}"
    if code == "Arrearage":
        detail += "。账号存在欠费或余额不足，请先在阿里云费用与成本中结清或充值。"
    elif response.status_code in (401, 403):
        detail += "。请检查 API Key、账号状态，以及对应模型是否已开通。"
    elif response.status_code == 404:
        detail += "。请检查 LLM_BASE_URL 是否为兼容模式地址，以及模型名称是否正确。"
    return detail


def test_llm(client: httpx.Client, endpoint_base: str, headers: dict[str, str]) -> bool:
    print(f"[LLM] 请求模型: {LLM_MODEL}")
    try:
        response = client.post(
            f"{endpoint_base}/chat/completions",
            headers=headers,
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": "Reply with OK only."}],
                "max_tokens": 8,
                "temperature": 0,
            },
        )
    except httpx.HTTPError as exc:
        print(f"[LLM] 失败: 网络或连接异常: {type(exc).__name__}: {exc}")
        return False

    if response.is_success:
        data = response.json()
        choices = data.get("choices") or []
        content = ((choices[0].get("message") or {}).get("content") or "").strip() if choices else ""
        print(f"[LLM] 成功: HTTP {response.status_code}, 返回={content or '<empty>'}")
        return True

    print(f"[LLM] 失败: HTTP {response.status_code}, {response_error(response)}")
    return False


def test_embedding(client: httpx.Client, endpoint_base: str, headers: dict[str, str]) -> bool:
    print(f"[Embedding] 请求模型: {EMBEDDING_MODEL}")
    try:
        response = client.post(
            f"{endpoint_base}/embeddings",
            headers=headers,
            json={"model": EMBEDDING_MODEL, "input": "model connectivity test"},
        )
    except httpx.HTTPError as exc:
        print(f"[Embedding] 失败: 网络或连接异常: {type(exc).__name__}: {exc}")
        return False

    if response.is_success:
        data = response.json()
        items = data.get("data") or []
        vector = items[0].get("embedding") if items else None
        if isinstance(vector, list) and vector:
            print(f"[Embedding] 成功: HTTP {response.status_code}, 向量维度={len(vector)}")
            return True
        print("[Embedding] 失败: 接口返回成功，但响应中没有有效向量。")
        return False

    print(f"[Embedding] 失败: HTTP {response.status_code}, {response_error(response)}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="测试当前 .env 中的大语言模型和向量模型是否可用。")
    parser.add_argument("--skip-llm", action="store_true", help="仅测试向量模型")
    parser.add_argument("--skip-embedding", action="store_true", help="仅测试大语言模型")
    args = parser.parse_args()

    if args.skip_llm and args.skip_embedding:
        parser.error("--skip-llm 与 --skip-embedding 不能同时使用。")
    if not LLM_API_KEY:
        print("未检测到 LLM_API_KEY，请先在 backend/.env 中配置 API Key。")
        return 1

    endpoint_base = (LLM_BASE_URL or "https://api.openai.com/v1").rstrip("/")
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}

    print("=== 模型连通性测试 ===")
    print(f"Base URL: {endpoint_base}")
    print(f"API Key: {mask_api_key(LLM_API_KEY)}")
    print(f"Timeout: {LLM_TIMEOUT_SECONDS}s")
    print()

    results: list[bool] = []
    with httpx.Client(timeout=LLM_TIMEOUT_SECONDS) as client:
        if not args.skip_llm:
            results.append(test_llm(client, endpoint_base, headers))
        if not args.skip_embedding:
            results.append(test_embedding(client, endpoint_base, headers))

    print()
    if all(results):
        print("结论: 当前大语言模型和向量模型均已联通。")
        return 0

    print("结论: 至少有一个模型未联通；请根据上方错误信息处理后再次测试。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
