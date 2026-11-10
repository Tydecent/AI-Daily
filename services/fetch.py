"""阶段 3 的抓取能力：调用 DeepSeek Fetch API 把网页转成 Markdown 正文。

重试策略与官方文档一致：
- 403 / 404 / 超时：不重试（同一 URL 重试大概率仍然失败，直接降级到 Search 摘要）
- 408 / 429 / 5xx：等待 1 秒后重试一次
"""

from __future__ import annotations

import logging
import time

import requests

import config

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }


def _post_fetch(url: str) -> requests.Response:
    return requests.post(
        config.DEEPSEEK_FETCH_URL,
        headers=_headers(),
        json={"url": url},
        timeout=config.HTTP_TIMEOUT_FETCH,
    )


def fetch_page(url: str) -> dict:
    """抓取单个 URL。

    返回 dict：
        ok: bool
        title: str
        content: str  (Markdown)
        error: str | None
        status: int | None
    """
    if not url:
        return {"ok": False, "title": "", "content": "", "error": "空 URL", "status": None}

    last_error = ""
    last_status: int | None = None

    for attempt in (1, 2):
        try:
            response = _post_fetch(url)
        except requests.Timeout:
            # 超时不重试，避免整批任务被拖死
            return {
                "ok": False,
                "title": "",
                "content": "",
                "error": "抓取超时",
                "status": None,
            }
        except requests.RequestException as exc:
            return {
                "ok": False,
                "title": "",
                "content": "",
                "error": f"网络错误: {exc}",
                "status": None,
            }

        last_status = response.status_code
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                return {
                    "ok": False,
                    "title": "",
                    "content": "",
                    "error": "响应不是合法 JSON",
                    "status": 200,
                }
            content = payload.get("content") or ""
            title = payload.get("title") or ""
            if not isinstance(content, str) or not content.strip():
                return {
                    "ok": False,
                    "title": str(title).strip(),
                    "content": "",
                    "error": "正文为空",
                    "status": 200,
                }
            return {
                "ok": True,
                "title": str(title).strip(),
                "content": content.strip(),
                "error": None,
                "status": 200,
            }

        last_error = f"HTTP {response.status_code}"
        logger.info("Fetch 失败(%s) %s: HTTP %s", attempt, url, response.status_code)

        if response.status_code in RETRYABLE_STATUS and attempt == 1:
            time.sleep(1.0)
            continue
        break

    return {
        "ok": False,
        "title": "",
        "content": "",
        "error": last_error or "抓取失败",
        "status": last_status,
    }
