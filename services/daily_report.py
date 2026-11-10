"""日报生成编排：四阶段流水线 + 缓存写入 + 运行状态管理。

阶段 1  Search 并发（services/search.py）
阶段 2  机械筛选与去重（本模块）
阶段 3  模型选题 + 并发 Fetch 深读（services/llm.py + services/fetch.py）
阶段 4  约束性成稿（services/llm.py），失败时规则拼接降级
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import config
from services import fetch, llm, search

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 运行状态（供 /api/status 与页面使用）
# ---------------------------------------------------------------------------
_STATE_LOCK = threading.Lock()
_GEN_LOCK = threading.Lock()  # 防止启动生成与定时任务重叠执行
_STATE: dict[str, Any] = {
    "status": "idle",  # idle | generating | ready | error
    "last_generated_at": None,
    "target_date": None,
    "message": "",
    "item_count": 0,
    "demo": False,
}


def get_state() -> dict[str, Any]:
    with _STATE_LOCK:
        return dict(_STATE)


def _set_state(**kwargs: Any) -> None:
    with _STATE_LOCK:
        _STATE.update(kwargs)


# ---------------------------------------------------------------------------
# 时间工具（全部以北京时间计算）
# ---------------------------------------------------------------------------
def now_beijing() -> datetime:
    return datetime.now(config.TZ)


def get_target_date() -> date:
    """目标日期 = 北京时间今天 - 1 天。"""
    return now_beijing().date() - timedelta(days=1)


def format_dt(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else None


def _report_title(target: date) -> str:
    return f"AI 日报 · {target.year}年{target.month}月{target.day}日"


def parse_item_datetime(value: Any) -> datetime | None:
    """把 Search 返回的 RFC3339 / 常见日期格式解析为北京时间 datetime。"""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, config.TZ)
        except (OverflowError, OSError, ValueError):
            return None

    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"

    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=config.TZ)
    return parsed.astimezone(config.TZ)


def source_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


# ---------------------------------------------------------------------------
# 阶段 2：机械筛选与去重
# ---------------------------------------------------------------------------
def _title_bigrams(title: str) -> set[str]:
    compact = "".join(ch for ch in title if ch.isalnum())
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def title_similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard 相似度。"""
    set_a, set_b = _title_bigrams(a), _title_bigrams(b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _dedupe_by_url(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for item in items:
        key = item["url"].rstrip("/")
        current = best.get(key)
        if current is None or item["rerank_score"] > current["rerank_score"]:
            best[key] = item
    return list(best.values())


def _dedupe_by_title(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for item in items:  # 已按分数降序，先入为主的是分数更高的
        matched_index = -1
        for index, existing in enumerate(kept):
            if (
                title_similarity(item["title"], existing["title"])
                > config.TITLE_SIMILARITY_THRESHOLD
            ):
                matched_index = index
                break
        if matched_index == -1:
            kept.append(item)
        elif item["authority"] > kept[matched_index]["authority"]:
            # 标题近重复时，authority 更高者胜出
            kept[matched_index] = item
    return kept


def _sort_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按分数排序；date 为空的结果降权 0.5。"""

    def key(item: dict[str, Any]) -> tuple[float, int]:
        parsed = parse_item_datetime(item.get("date"))
        score = item["rerank_score"] - (0.0 if parsed else 0.5)
        return (score, item["authority"])

    return sorted(items, key=key, reverse=True)


def filter_candidates(
    raw_items: list[dict[str, Any]], target: date
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按规则压缩候选集，返回 (候选, 统计)。"""
    stats: dict[str, Any] = {"raw": len(raw_items), "after_threshold": 0}

    def _pass_strict(item: dict[str, Any]) -> bool:
        return (
            item["rerank_score"] >= config.STRICT_RERANK_SCORE
            and item["authority"] >= config.STRICT_AUTHORITY
        )

    def _pass_relaxed(item: dict[str, Any]) -> bool:
        return (
            item["rerank_score"] >= config.RELAXED_RERANK_SCORE
            and item["authority"] >= config.RELAXED_AUTHORITY
        )

    thresholded = [item for item in raw_items if _pass_strict(item)]
    relaxed_used = False
    if len(thresholded) < config.MIN_CANDIDATES:
        thresholded = [item for item in raw_items if _pass_relaxed(item)]
        relaxed_used = True
    stats["after_threshold"] = len(thresholded)
    stats["relaxed"] = relaxed_used

    # 日期过滤：可解析时只保留目标日期当天（北京时间），无日期的保留但降权
    same_day: list[dict[str, Any]] = []
    for item in thresholded:
        parsed = parse_item_datetime(item.get("date"))
        if parsed is None:
            same_day.append(item)
            continue
        if parsed.date() == target:
            item["date"] = parsed.date().isoformat()
            same_day.append(item)
    stats["after_date"] = len(same_day)

    deduped_url = _dedupe_by_url(same_day)
    stats["after_url_dedupe"] = len(deduped_url)

    ordered = _sort_items(deduped_url)
    deduped_title = _dedupe_by_title(ordered)
    stats["after_title_dedupe"] = len(deduped_title)

    candidates = deduped_title[: config.MAX_CANDIDATES]
    stats["final"] = len(candidates)
    return candidates, stats


# ---------------------------------------------------------------------------
# 阶段 3：选题 + 并发深读
# ---------------------------------------------------------------------------
def _extract_key_points(text: str, limit: int = 3) -> list[str]:
    """从摘要/正文中抽取含数字的短句作为要点。"""
    if not text:
        return []
    sentences = [
        seg.strip(" -•\t")
        for seg in text.replace("\r", "\n").replace("\n", "。").split("。")
    ]
    points: list[str] = []
    for sentence in sentences:
        if len(sentence) < 8 or len(sentence) > 90:
            continue
        if any(ch.isdigit() for ch in sentence):
            points.append(sentence)
        if len(points) >= limit:
            break
    if not points:
        points = [s for s in sentences if 8 <= len(s) <= 90][:limit]
    return points


def _fallback_category(item: dict[str, Any]) -> str:
    """Fetch/选题信息不足时，按关键词粗分类。"""
    topic = item.get("topic")
    if topic in config.CATEGORIES:
        return topic
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    rules = [
        ("研究与论文", ("论文", "研究", "paper", "arxiv", "benchmark", "评测")),
        ("政策与治理", ("监管", "政策", "法案", "治理", "合规", "安全", "regulation", "policy")),
        ("行业与融资", ("融资", "估值", "收购", "投资", "ipo", "营收", "funding", "acquisition")),
        ("开源与工具", ("开源", "github", "huggingface", "工具", "sdk", "open source", "agent")),
        ("模型与产品", ("模型", "发布", "上线", "产品", "model", "release", "gpt", "claude")),
    ]
    for category, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return category
    return "模型与产品"


def _ensure_min_selection(
    selected: list[dict[str, Any]], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """模型选题不足下限时，按检索分数补齐，保证日报有足够内容。"""
    if len(selected) >= config.SELECT_MIN_ITEMS:
        return selected
    chosen = {entry["url"] for entry in selected}
    for item in candidates:
        if len(selected) >= config.SELECT_MIN_ITEMS:
            break
        if item["url"] in chosen:
            continue
        selected.append({"url": item["url"], "reason": "补充选题：按检索分数排序"})
        chosen.add(item["url"])
    return selected


def _deep_read(
    selected: list[dict[str, Any]], candidate_map: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """并发 Fetch 选中的 URL，产出阶段 4 的素材列表。"""
    fetch_ok, fetch_failed, dropped = 0, 0, 0
    materials: list[dict[str, Any]] = []

    def _work(entry: dict[str, Any]) -> dict[str, Any] | None:
        item = candidate_map[entry["url"]]
        result = fetch.fetch_page(entry["url"])
        note = ""
        fact_summary = ""

        if result["ok"]:
            content = result["content"]
            if len(content) > config.FETCH_MAX_CHARS:
                try:
                    fact_summary = llm.summarize_long_content(
                        item.get("title", ""), entry["url"], content
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("长文摘要失败，截断降级 %s: %s", entry["url"], exc)
                    fact_summary = content[:1500]
            else:
                fact_summary = content[: config.FETCH_MAX_CHARS]
        else:
            # 抓取失败：不重试，降级使用 Search 摘要
            if not item.get("summary"):
                logger.info("丢弃无摘要条目: %s", entry["url"])
                return None
            fact_summary = item["summary"]
            note = "未能抓取全文"

        published = parse_item_datetime(item.get("date"))
        return {
            "category": _fallback_category(item),
            "title": item.get("title", ""),
            "url": entry["url"],
            "source_domain": source_domain(entry["url"]),
            "date": published.date().isoformat() if published else "",
            "authority": item.get("authority", -1),
            "fact_summary": fact_summary.strip(),
            "key_points": _extract_key_points(fact_summary),
            "fetch_ok": result["ok"],
            "note": note,
            "reason": entry.get("reason", ""),
        }

    def _safe_work(entry: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return _work(entry)
        except Exception as exc:  # noqa: BLE001 - 单条异常不影响整批
            logger.warning("深读条目异常 %s: %s", entry.get("url"), exc)
            return None

    with ThreadPoolExecutor(max_workers=max(1, config.FETCH_WORKERS)) as pool:
        for material in pool.map(_safe_work, selected):
            if material is None:
                dropped += 1
                continue
            if material["fetch_ok"]:
                fetch_ok += 1
            else:
                fetch_failed += 1
            materials.append(material)

    stats = {
        "fetch_ok": fetch_ok,
        "fetch_failed": fetch_failed,
        "dropped": dropped,
    }
    return materials, stats


# ---------------------------------------------------------------------------
# 阶段 4：成稿与降级模板
# ---------------------------------------------------------------------------
def _count_chinese_chars(text: str) -> int:
    return len("".join(ch for ch in text if not ch.isspace()))


def _compose_note(note: str, fetch_ok: bool, authority: int) -> str:
    """统一生成条目标注：抓取失败、权威度不足等，去重后以“；”连接。"""
    parts: list[str] = []
    if note:
        parts.append(note)
    if not fetch_ok and not any("未能抓取" in part for part in parts):
        parts.append("未能抓取全文")
    if authority < 3 and not any("待核实" in part for part in parts):
        parts.append("待核实")
    return "；".join(parts)


def _sanitize_report(
    report: dict[str, Any],
    target: date,
    materials: list[dict[str, Any]],
) -> dict[str, Any]:
    """校正模型输出：分类白名单、URL 白名单、日期与权威度以素材为准。"""
    material_map = {m["url"]: m for m in materials}
    date_iso = target.isoformat()

    sections_out: list[dict[str, Any]] = []
    used_urls: set[str] = set()

    for section in report.get("sections") or []:
        if not isinstance(section, dict):
            continue
        category = str(section.get("category") or "").strip()
        if category not in config.CATEGORIES:
            category = _fallback_category(
                {"title": category, "topic": section.get("category")}
            )
        items_out: list[dict[str, Any]] = []
        for item in section.get("items") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            material = material_map.get(url)
            if not material or url in used_urls:
                continue
            used_urls.add(url)
            summary = str(item.get("summary") or "").strip()
            if not summary:
                summary = material["fact_summary"][:300]
            authority = int(material.get("authority", -1))
            note = _compose_note(
                str(item.get("note") or "").strip(), material["fetch_ok"], authority
            )
            items_out.append(
                {
                    "title": str(item.get("title") or material["title"]).strip(),
                    "summary": summary[:600],
                    "url": url,
                    "source": material["source_domain"],
                    "date": material["date"] or date_iso,
                    "authority": authority,
                    "note": note,
                }
            )
        if items_out:
            sections_out.append({"category": category, "items": items_out})

    total_items = sum(len(s["items"]) for s in sections_out)
    if total_items == 0:
        raise llm.LLMError("成稿结果不含有效条目")

    intro = str(report.get("intro") or "").strip()
    if not intro:
        intro = f"今日共收录 {total_items} 条 AI 动态。"

    return {
        "date": date_iso,
        "title": _report_title(target),
        "intro": intro,
        "sections": sections_out,
    }


def build_fallback_report(
    target: date, materials: list[dict[str, Any]], reason: str = ""
) -> dict[str, Any]:
    """模型成稿失败时，用阶段 3 素材按类别拼接降级日报。"""
    grouped: dict[str, list[dict[str, Any]]] = {c: [] for c in config.CATEGORIES}
    for material in materials:
        category = material.get("category") if material.get("category") in config.CATEGORIES else "模型与产品"
        authority = int(material.get("authority", -1))
        grouped[category].append(
            {
                "title": material.get("title", ""),
                "summary": (material.get("fact_summary") or "")[:300],
                "url": material.get("url", ""),
                "source": material.get("source_domain", ""),
                "date": material.get("date") or target.isoformat(),
                "authority": authority,
                "note": _compose_note(
                    material.get("note") or "", material.get("fetch_ok", False), authority
                ),
            }
        )

    sections = [
        {"category": category, "items": items}
        for category, items in grouped.items()
        if items
    ]
    total = sum(len(section["items"]) for section in sections)
    intro = f"今日共收录 {total} 条 AI 动态（自动排版）"
    if reason:
        intro = f"{intro}；模型成稿未成功（{reason}），以下内容直接来自抓取素材。"

    return {
        "date": target.isoformat(),
        "title": _report_title(target),
        "intro": intro,
        "sections": sections,
    }


def build_demo_report(target: date) -> dict[str, Any]:
    """未配置 API Key 时的演示日报（明确标注为演示数据，不冒充真实新闻）。"""
    date_iso = target.isoformat()
    demo_items = [
        (
            "模型与产品",
            "【演示】示例条目：某厂商发布新一代多模态大模型",
            "此处为演示内容，用于预览页面排版效果。真实运行时，该位置会展示当日检索到的模型发布或产品更新，摘要由模型基于抓取正文撰写，并保留原始链接与日期。",
            "https://example.com/ai-daily-demo/model-release",
            "example.com",
            4,
        ),
        (
            "行业与融资",
            "【演示】示例条目：AI 基础设施公司完成新一轮融资",
            "演示内容：真实运行时会展示融资轮次、金额、投资方与估值等关键数字，并给出原始报道链接。配置 DEEPSEEK_API_KEY 后即刻替换为真实日报。",
            "https://example.com/ai-daily-demo/funding",
            "example.com",
            3,
        ),
        (
            "开源与工具",
            "【演示】示例条目：开源社区更新 Agent 开发框架",
            "演示内容：真实运行时会抓取 GitHub / Hugging Face 等一手来源，摘录版本、star 数、关键特性等可核实信息。",
            "https://example.com/ai-daily-demo/open-source",
            "example.com",
            3,
        ),
        (
            "政策与治理",
            "【演示】示例条目：监管机构就 AI 安全治理征求意见",
            "演示内容：真实运行时会摘录文件名称、发布机构、生效时间与适用范围，并对单一来源标注“待核实”。",
            "https://example.com/ai-daily-demo/policy",
            "example.com",
            3,
        ),
        (
            "研究与论文",
            "【演示】示例条目：新论文提出推理效率优化方法",
            "演示内容：真实运行时会保留论文标题、机构、数据集与性能数字，链接指向论文或预印本页面。",
            "https://example.com/ai-daily-demo/paper",
            "example.com",
            3,
        ),
    ]

    sections: list[dict[str, Any]] = []
    for category, title, summary, url, domain, authority in demo_items:
        sections.append(
            {
                "category": category,
                "items": [
                    {
                        "title": title,
                        "summary": summary,
                        "url": url,
                        "source": domain,
                        "date": date_iso,
                        "authority": authority,
                        "note": "演示数据",
                    }
                ],
            }
        )

    return {
        "date": date_iso,
        "title": _report_title(target),
        "intro": "当前处于演示模式（未检测到 DEEPSEEK_API_KEY 或已开启 DEMO_MODE），页面展示的是示例排版，不是真实新闻。",
        "sections": sections,
    }


# ---------------------------------------------------------------------------
# 缓存写入
# ---------------------------------------------------------------------------
def _atomic_write_json(path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def load_latest() -> dict[str, Any] | None:
    """读取 data/latest.json，不存在或损坏时返回 None。"""
    if not config.LATEST_PATH.exists():
        return None
    try:
        with open(config.LATEST_PATH, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        logger.warning("latest.json 读取失败，按无缓存处理")
        return None


def _persist(report: dict[str, Any], target: date, meta: dict[str, Any]) -> dict[str, Any]:
    payload = dict(report)
    payload["meta"] = meta
    _atomic_write_json(config.REPORTS_DIR / f"{target.isoformat()}.json", payload)
    _atomic_write_json(config.LATEST_PATH, payload)
    return payload


def _count_items(report: dict[str, Any]) -> int:
    return sum(len(section.get("items") or []) for section in report.get("sections") or [])


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def generate_daily_report() -> dict[str, Any] | None:
    """对外入口：加互斥锁，避免启动生成与定时任务并发写缓存。"""
    if not _GEN_LOCK.acquire(blocking=False):
        logger.warning("已有生成任务在执行，本次触发被跳过")
        return None
    try:
        return _generate_daily_report_inner()
    finally:
        _GEN_LOCK.release()


def _generate_daily_report_inner() -> dict[str, Any] | None:
    """生成前一天的 AI 日报，成功返回日报 dict，失败返回 None（保留旧缓存）。"""
    target = get_target_date()
    started_at = now_beijing()
    t0 = time.perf_counter()

    _set_state(
        status="generating",
        target_date=target.isoformat(),
        message=f"正在生成 {target.isoformat()} 的日报",
    )
    logger.info("========== 开始生成日报：目标日期 %s ==========", target)

    # 演示模式：未配置 Key 或显式开启
    if not config.DEEPSEEK_API_KEY or config.DEMO_MODE:
        report = build_demo_report(target)
        meta = {
            "generated_at": format_dt(now_beijing()),
            "target_date": target.isoformat(),
            "item_count": _count_items(report),
            "demo": True,
            "degraded": False,
            "notice": "未配置 DEEPSEEK_API_KEY，当前展示演示内容。",
            "stats": {},
        }
        _persist(report, target, meta)
        _set_state(
            status="ready",
            last_generated_at=meta["generated_at"],
            message="演示日报已生成（未配置 API Key 或已开启 DEMO_MODE）",
            item_count=meta["item_count"],
            demo=True,
        )
        logger.info("演示日报已生成，耗时 %.2fs", time.perf_counter() - t0)
        return report

    # ---------------- 阶段 1 ----------------
    try:
        raw_candidates, search_stats = search.search_all(target)
    except Exception as exc:  # noqa: BLE001 - Search 全失败不应 500
        logger.exception("Search 阶段异常: %s", exc)
        raw_candidates, search_stats = [], {"queries": 0, "queries_ok": 0, "queries_failed": 0, "raw_results": 0}

    logger.info(
        "阶段1 Search：查询 %d 条（成功 %d / 失败 %d），候选 %d 条",
        search_stats.get("queries", 0),
        search_stats.get("queries_ok", 0),
        search_stats.get("queries_failed", 0),
        len(raw_candidates),
    )

    if not raw_candidates:
        message = (
            "本次更新失败，展示上次日报"
            if search_stats.get("queries_ok", 0) == 0
            else "本次未检索到符合条件的动态，展示上次日报"
        )
        logger.error("Search 未返回任何候选，%s", message)
        _set_state(status="error", message=message)
        return None

    # ---------------- 阶段 2 ----------------
    candidates, filter_stats = filter_candidates(raw_candidates, target)
    logger.info(
        "阶段2 筛选：候选 %d → 阈值后 %d → 日期后 %d → URL 去重 %d → 标题去重 %d → 最终 %d（放宽=%s）",
        filter_stats["raw"],
        filter_stats["after_threshold"],
        filter_stats["after_date"],
        filter_stats["after_url_dedupe"],
        filter_stats["after_title_dedupe"],
        filter_stats["final"],
        filter_stats.get("relaxed", False),
    )
    if not candidates:
        logger.error("阶段2 无可用候选，终止本次生成")
        _set_state(status="error", message="本次候选均不满足筛选条件，展示上次日报")
        return None

    # ---------------- 阶段 3 ----------------
    using_fallback_selection = False
    try:
        selected = llm.select_items(candidates, target.isoformat())
    except Exception as exc:  # noqa: BLE001
        logger.warning("模型选题失败，改用规则选题: %s", exc)
        selected = []

    if not selected:
        using_fallback_selection = True
        selected = [
            {"url": item["url"], "reason": "规则选题：按检索分数排序"}
            for item in candidates[: config.SELECT_MAX_ITEMS]
        ]

    candidate_map = {item["url"]: item for item in candidates}
    selected = [entry for entry in selected if entry["url"] in candidate_map]
    selected = _ensure_min_selection(selected, candidates)
    logger.info("阶段3 选题：选中 %d 条", len(selected))

    materials, fetch_stats = _deep_read(selected, candidate_map)
    logger.info(
        "阶段3 Fetch：成功 %d / 失败 %d / 丢弃 %d",
        fetch_stats["fetch_ok"],
        fetch_stats["fetch_failed"],
        fetch_stats["dropped"],
    )

    if not materials:
        logger.error("阶段3 无可用素材，终止本次生成")
        _set_state(status="error", message="抓取与摘要均失败，展示上次日报")
        return None

    # ---------------- 阶段 4 ----------------
    degraded = False
    report: dict[str, Any]
    try:
        raw_report = llm.compose_report(target.isoformat(), materials)
        report = _sanitize_report(raw_report, target, materials)
        if _count_items(report) < max(3, config.SELECT_MIN_ITEMS - 1):
            raise llm.LLMError("有效条目过少")
    except Exception as exc:  # noqa: BLE001
        logger.warning("模型成稿失败，使用规则拼接降级: %s", exc, exc_info=True)
        report = build_fallback_report(target, materials, reason="JSON 解析或校验失败")
        degraded = True

    elapsed = time.perf_counter() - t0
    item_count = _count_items(report)
    word_count = total_word_count(report)
    meta = {
        "generated_at": format_dt(now_beijing()),
        "started_at": format_dt(started_at),
        "target_date": target.isoformat(),
        "item_count": item_count,
        "word_count": word_count,
        "demo": False,
        "degraded": degraded,
        "notice": "模型成稿未成功，已使用规则拼接的降级日报。" if degraded else "",
        "stats": {
            "search_queries": search_stats.get("queries", 0),
            "search_queries_failed": search_stats.get("queries_failed", 0),
            "raw_candidates": filter_stats["raw"],
            "candidates": filter_stats["final"],
            "selected": len(selected),
            "fetch_ok": fetch_stats["fetch_ok"],
            "fetch_failed": fetch_stats["fetch_failed"],
            "fallback_selection": using_fallback_selection,
            "elapsed_seconds": round(elapsed, 2),
        },
    }
    _persist(report, target, meta)

    _set_state(
        status="ready",
        last_generated_at=meta["generated_at"],
        message="日报生成完成" if not degraded else "日报已生成（降级模式）",
        item_count=item_count,
        demo=False,
    )

    logger.info(
        "阶段4 完成：目标日期=%s 查询数=%d 候选数=%d 选中数=%d Fetch成功=%d Fetch失败=%d 条目数=%d 字数=%d 降级=%s 耗时=%.2fs",
        target.isoformat(),
        search_stats.get("queries", 0),
        filter_stats["final"],
        len(selected),
        fetch_stats["fetch_ok"],
        fetch_stats["fetch_failed"],
        item_count,
        word_count,
        degraded,
        elapsed,
    )
    return report


def total_word_count(report: dict[str, Any]) -> int:
    """粗略统计日报字数（含标题、导语与摘要）。"""
    parts = [report.get("title", ""), report.get("intro", "")]
    for section in report.get("sections") or []:
        for item in section.get("items") or []:
            parts.append(str(item.get("title", "")))
            parts.append(str(item.get("summary", "")))
    return _count_chinese_chars("".join(parts))
