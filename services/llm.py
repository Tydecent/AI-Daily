"""阶段 3/4 的大模型能力：选题、长文事实摘要、成稿。

统一封装 DeepSeek Chat API（OpenAI 兼容格式），并负责稳健的 JSON 解析。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    """大模型调用或解析失败。"""


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }


def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.3,
    max_tokens: int = 2048,
) -> str:
    """调用 Chat API，返回首个 choice 的文本内容。"""
    body = {
        "model": config.CHAT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    try:
        response = requests.post(
            config.DEEPSEEK_CHAT_URL,
            headers=_headers(),
            json=body,
            timeout=config.HTTP_TIMEOUT_CHAT,
        )
    except requests.RequestException as exc:
        raise LLMError(f"Chat 请求失败: {exc}") from exc

    if response.status_code != 200:
        raise LLMError(f"Chat HTTP {response.status_code}: {response.text[:300]}")

    try:
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Chat 响应结构异常: {exc}") from exc

    if not isinstance(content, str) or not content.strip():
        raise LLMError("Chat 返回空内容")
    return content.strip()


def extract_json(text: str) -> Any:
    """从模型输出中稳健地抽出 JSON 对象/数组。"""
    if not text:
        raise LLMError("待解析内容为空")

    candidates: list[str] = []
    fence_match = _JSON_FENCE_RE.search(text)
    if fence_match:
        candidates.append(fence_match.group(1).strip())
    candidates.append(text.strip())

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        # 退化策略：截取首个 { 到最后一个 }（或首个 [ 到最后一个 ]）
        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            start, end = candidate.find(open_ch), candidate.rfind(close_ch)
            if start != -1 and end > start:
                snippet = candidate[start : end + 1]
                try:
                    return json.loads(snippet)
                except json.JSONDecodeError:
                    # 去掉对象/数组结尾多余的逗号后再试一次
                    cleaned = re.sub(r",\s*([}\]])", r"\1", snippet)
                    try:
                        return json.loads(cleaned)
                    except json.JSONDecodeError:
                        continue

    raise LLMError("无法从模型输出中解析 JSON")


def chat_json(messages: list[dict[str, str]], **kwargs: Any) -> Any:
    return extract_json(chat(messages, **kwargs))


# ---------------------------------------------------------------------------
# 阶段 3-A：模型选题
# ---------------------------------------------------------------------------
SELECT_SYSTEM_PROMPT = """你是中文科技媒体的资深编辑，负责从候选素材中挑选当日最重要的 AI 动态。

硬性规则：
1. 只能从用户提供的候选列表中选择，不得添加、编造或改写任何 URL。
2. 每一条选择都必须给出候选列表中真实存在的 url，且 url 与标题一一对应。
3. 优先选择一手来源（官方博客、公告、论文、GitHub 仓库）、权威度高（authority 大）、与 AI 直接相关的条目。
4. 覆盖不同类别：模型与产品、行业与融资、开源与工具、政策与治理、研究与论文，避免同一类别扎堆。
5. 忽略标题党、纯营销软文、与 AI 无关的内容。

只输出严格 JSON，不要 Markdown 代码块，不要任何解释文字。格式：
{"selected":[{"url":"https://...","reason":"一句话选题理由"}]}"""


def select_items(candidates: list[dict[str, Any]], target_date: str) -> list[dict[str, Any]]:
    """让模型从候选中挑选 4–6 条。返回 [{"url", "reason"}]，URL 已做白名单校验。"""
    slim = [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "summary": (item.get("summary") or "")[:400],
            "date": item.get("date"),
            "authority": item.get("authority"),
            "rerank_score": item.get("rerank_score"),
            "topic": item.get("topic", ""),
        }
        for item in candidates
    ]
    user_prompt = (
        f"目标日期：{target_date}\n"
        f"请从中选出 {config.SELECT_MIN_ITEMS}–{config.SELECT_MAX_ITEMS} 条最重要的 AI 动态。\n"
        f"候选列表（JSON）：\n{json.dumps(slim, ensure_ascii=False, indent=1)}"
    )
    data = chat_json(
        [
            {"role": "system", "content": SELECT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=1024,
    )

    if isinstance(data, dict):
        raw_selected = data.get("selected") or data.get("items") or []
    elif isinstance(data, list):
        raw_selected = data
    else:
        raw_selected = []

    valid_urls = {item["url"] for item in slim if item.get("url")}
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw_selected:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if url not in valid_urls or url in seen:
            continue
        seen.add(url)
        selected.append({"url": url, "reason": str(entry.get("reason") or "").strip()})

    if len(selected) > config.SELECT_MAX_ITEMS:
        selected = selected[: config.SELECT_MAX_ITEMS]
    return selected


# ---------------------------------------------------------------------------
# 阶段 3-B：长文事实摘要
# ---------------------------------------------------------------------------
SUMMARY_SYSTEM_PROMPT = """你是严谨的科技编辑，负责为长文提炼事实摘要。

要求：
1. 只使用原文中出现的以下信息：关键数字、日期、公司名、模型名、指标、结论。
2. 不得引入原文没有的信息，不做推测、不写评论、不使用外部知识。
3. 输出 300–500 字的中文连续段落，不分点、不写标题、不加 Markdown 标记。
4. 原文若为英文，请译写为中文，专有名词保留英文原文。"""


def summarize_long_content(title: str, url: str, content: str) -> str:
    """对超长正文生成 300–500 字事实摘要。"""
    text = content[: config.FETCH_MAX_CHARS * 3]  # 兜底截断，避免超出上下文
    user_prompt = (
        f"标题：{title}\n链接：{url}\n\n网页正文（Markdown）：\n{text}\n\n"
        f"请输出 {config.SUMMARY_MIN_WORDS}–{config.SUMMARY_MAX_WORDS} 字的事实摘要。"
    )
    return chat(
        [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=1200,
    )


# ---------------------------------------------------------------------------
# 阶段 4：约束性成稿
# ---------------------------------------------------------------------------
COMPOSE_SYSTEM_PROMPT = f"""你是中文 AI 日报的主编，负责把已核实的素材组织成一份结构化日报。

硬性规则（违反即视为失败）：
1. 每一条动态只能基于“提供的素材”撰写，不得引入素材之外的事实、数据或背景知识。
2. 每条动态必须保留素材中给出的原始 URL 与日期，URL 不得改写、不得拼接、不得编造。
3. 分类只能取以下五类之一：{" / ".join(config.CATEGORIES)}；素材里已有 category 时优先沿用。
4. 每条 summary 为 2–4 句中文，信息密度高，突出“发生了什么 + 关键数字/主体 + 为什么重要（仅限素材内信息）”。
5. 若素材中 authority 小于 3，或该条仅来自单一来源，在该条的 note 字段写“待核实”。
6. 若素材中标记了抓取失败（fetch_ok 为 false），note 字段写“未能抓取全文”，摘要只能依据 Search 摘要。
7. 全文总长度控制在 {config.REPORT_MIN_WORDS}–{config.REPORT_MAX_WORDS} 字（含标题与摘要）。
8. 只输出严格 JSON，不要 Markdown 代码块，不要前后缀说明文字。

输出 JSON 结构：
{{
  "date": "YYYY-MM-DD",
  "title": "AI 日报 · YYYY年M月D日",
  "intro": "一句话概括当日 AI 领域整体态势",
  "sections": [
    {{
      "category": "模型与产品",
      "items": [
        {{
          "title": "条目标题",
          "summary": "2–4 句中文摘要",
          "url": "原始链接",
          "source": "来源域名",
          "date": "YYYY-MM-DD",
          "authority": 3,
          "note": "可选：待核实/未能抓取全文"
        }}
      ]
    }}
  ]
}}"""


def compose_report(target_date: str, materials: list[dict[str, Any]]) -> dict[str, Any]:
    """调用模型生成最终日报 JSON。解析失败时抛 LLMError，由上层降级。"""
    user_prompt = (
        f"目标日期：{target_date}\n"
        f"素材（JSON 数组，每项含 category/title/url/source_domain/date/authority/"
        f"fact_summary/key_points/fetch_ok）：\n"
        f"{json.dumps(materials, ensure_ascii=False, indent=1)}\n\n"
        f"请输出 {target_date} 的中文 AI 日报 JSON。"
    )
    data = chat_json(
        [
            {"role": "system", "content": COMPOSE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.35,
        max_tokens=4096,
    )
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        raise LLMError("成稿 JSON 结构不符合预期")
    return data
