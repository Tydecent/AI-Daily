"""阶段 1：多主题并行 Search，构建候选池。

对外只暴露两个函数：
- build_queries(target_date)：生成覆盖不同维度的查询列表
- search_all(target_date)：并发执行全部查询并归一化结果
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

# 查询模板：用 after:{after} 收窄时间窗，日期在运行时填入
QUERY_TEMPLATES: list[dict[str, Any]] = [
    {
        "topic": "模型与产品",
        "query": '"大模型发布 OR 模型更新 OR AI 产品" after:{after} lang:zh,en',
        "count": 12,
    },
    {
        "topic": "行业与融资",
        "query": '"AI 融资 OR 收购 OR 估值" after:{after} lang:zh,en',
        "count": 12,
    },
    {
        "topic": "开源与工具",
        "query": (
            '"开源模型 OR 开源 Agent OR AI 工具" '
            "site:github.com OR site:huggingface.co after:{after}"
        ),
        "count": 12,
    },
    {
        "topic": "政策与治理",
        "query": '"AI 监管 OR 人工智能政策 OR 安全治理" after:{after} lang:zh,en',
        "count": 10,
    },
]


def build_queries(target_date: date) -> list[dict[str, Any]]:
    """生成 Search 查询列表。after 取目标日期前一天，形成两日窗口后再按 date 精确过滤。"""
    after = (target_date - timedelta(days=1)).isoformat()
    queries: list[dict[str, Any]] = []
    for tpl in QUERY_TEMPLATES:
        queries.append(
            {
                "topic": tpl["topic"],
                "query": tpl["query"].format(after=after),
                "count": int(tpl["count"]),
            }
        )
    return queries


def ori_question_for(target_date: date) -> str:
    return f"生成 {target_date.isoformat()} 的中文 AI 日报，关注当日重要动态"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }


def _extract_results(payload: Any) -> list[dict[str, Any]]:
    """兼容多种响应包裹形式，取出 results 数组。"""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if isinstance(results, list):
        return [item for item in results if isinstance(item, dict)]
    data = payload.get("data")
    if isinstance(data, dict):
        results = data.get("results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
    return []


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_item(raw: dict[str, Any], topic: str) -> dict[str, Any]:
    """把 Search 原始结果裁剪为日报流水线需要的字段。"""
    return {
        "title": str(raw.get("title") or "").strip(),
        "url": str(raw.get("url") or "").strip(),
        "summary": str(raw.get("summary") or raw.get("snippet") or "").strip(),
        "date": raw.get("date") or None,
        "rerank_score": _to_float(raw.get("rerank_score")),
        "authority": _to_int(raw.get("authority"), -1),
        "site_name": (raw.get("site_name") or None),
        "topic": topic,
    }


def search_one(query: dict[str, Any], ori_question: str) -> list[dict[str, Any]]:
    """执行单条 Search 请求。失败时抛异常，由调用方统计。"""
    body = {
        "query": query["query"],
        "count": query["count"],
        "need_content": False,  # 正文留给 Fetch
        "ori_question": ori_question,
    }
    response = requests.post(
        config.DEEPSEEK_SEARCH_URL,
        headers=_headers(),
        json=body,
        timeout=config.HTTP_TIMEOUT_SEARCH,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Search HTTP {response.status_code}: {response.text[:200]}"
        )
    results = _extract_results(response.json())
    items = [normalize_item(item, query["topic"]) for item in results]
    # 丢弃没有标题或链接的脏数据
    return [item for item in items if item["title"] and item["url"]]


def search_all(
    target_date: date,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """并发执行全部查询，返回 (候选列表, 统计信息)。"""
    queries = build_queries(target_date)
    ori_question = ori_question_for(target_date)

    candidates: list[dict[str, Any]] = []
    ok, failed = 0, 0
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=max(1, min(config.SEARCH_WORKERS, len(queries)))) as pool:
        future_map = {
            pool.submit(search_one, query, ori_question): query for query in queries
        }
        for future in as_completed(future_map):
            query = future_map[future]
            try:
                items = future.result()
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响整体
                failed += 1
                errors.append(f"{query['topic']}: {exc}")
                logger.warning("Search 失败 [%s]: %s", query["topic"], exc)
                continue
            ok += 1
            logger.info("Search 成功 [%s]: %d 条", query["topic"], len(items))
            candidates.extend(items)

    stats = {
        "queries": len(queries),
        "queries_ok": ok,
        "queries_failed": failed,
        "raw_results": len(candidates),
        "errors": errors,
    }
    return candidates, stats
