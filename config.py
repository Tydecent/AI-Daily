"""全局配置：集中管理路径、时区、DeepSeek 端点与各阶段阈值。

所有可变项都支持通过环境变量覆盖，API Key 只从环境变量读取，绝不写进代码。
"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# 加载项目根目录下的 .env（若存在）
load_dotenv(BASE_DIR / ".env")


def _env_str(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env_str(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env_str(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env_str(key, "1" if default else "0").lower()
    return raw in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# 基础配置
# ---------------------------------------------------------------------------
TZ = ZoneInfo("Asia/Shanghai")
TIMEZONE_NAME = "Asia/Shanghai"

HOST = _env_str("HOST", "0.0.0.0")
PORT = _env_int("PORT", 8000)

# ---------------------------------------------------------------------------
# DeepSeek API
# ---------------------------------------------------------------------------
DEEPSEEK_API_KEY = _env_str("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = _env_str("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")

# 端点集中定义，便于按官方文档调整（官方：/v1/tools/search 与 /v1/tools/fetch）
DEEPSEEK_SEARCH_URL = _env_str(
    "DEEPSEEK_SEARCH_URL", f"{DEEPSEEK_BASE_URL}/v1/tools/search"
)
DEEPSEEK_FETCH_URL = _env_str(
    "DEEPSEEK_FETCH_URL", f"{DEEPSEEK_BASE_URL}/v1/tools/fetch"
)
DEEPSEEK_CHAT_URL = _env_str(
    "DEEPSEEK_CHAT_URL", f"{DEEPSEEK_BASE_URL}/chat/completions"
)

CHAT_MODEL = _env_str("DEEPSEEK_CHAT_MODEL", "deepseek-chat")

HTTP_TIMEOUT_SEARCH = _env_int("HTTP_TIMEOUT_SEARCH", 40)
HTTP_TIMEOUT_FETCH = _env_int("HTTP_TIMEOUT_FETCH", 60)
HTTP_TIMEOUT_CHAT = _env_int("HTTP_TIMEOUT_CHAT", 120)

# ---------------------------------------------------------------------------
# 生成流水线参数
# ---------------------------------------------------------------------------
SEARCH_WORKERS = _env_int("SEARCH_WORKERS", 4)

# 阶段 2：机械筛选阈值
STRICT_RERANK_SCORE = _env_float("STRICT_RERANK_SCORE", 1.5)
STRICT_AUTHORITY = _env_int("STRICT_AUTHORITY", 3)
RELAXED_RERANK_SCORE = _env_float("RELAXED_RERANK_SCORE", 1.0)
RELAXED_AUTHORITY = _env_int("RELAXED_AUTHORITY", 2)

MIN_CANDIDATES = _env_int("MIN_CANDIDATES", 8)
MAX_CANDIDATES = _env_int("MAX_CANDIDATES", 12)
TITLE_SIMILARITY_THRESHOLD = _env_float("TITLE_SIMILARITY_THRESHOLD", 0.85)

# 阶段 3：选题与深读
SELECT_MIN_ITEMS = _env_int("SELECT_MIN_ITEMS", 4)
SELECT_MAX_ITEMS = _env_int("SELECT_MAX_ITEMS", 6)
FETCH_WORKERS = _env_int("FETCH_WORKERS", 5)
FETCH_MAX_CHARS = _env_int("FETCH_MAX_CHARS", 8000)
SUMMARY_MIN_WORDS = _env_int("SUMMARY_MIN_WORDS", 300)
SUMMARY_MAX_WORDS = _env_int("SUMMARY_MAX_WORDS", 500)

# 阶段 4：成稿
REPORT_MIN_WORDS = _env_int("REPORT_MIN_WORDS", 800)
REPORT_MAX_WORDS = _env_int("REPORT_MAX_WORDS", 1200)

CATEGORIES = ["模型与产品", "行业与融资", "开源与工具", "政策与治理", "研究与论文"]

# ---------------------------------------------------------------------------
# 存储与日志
# ---------------------------------------------------------------------------
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = DATA_DIR / "reports"
LATEST_PATH = DATA_DIR / "latest.json"
LOG_PATH = DATA_DIR / "app.log"

# 未配置 API Key 时是否生成“演示日报”，便于零配置预览页面样式
DEMO_MODE = _env_bool("DEMO_MODE", False)

AUTO_REFRESH_SECONDS = _env_int("AUTO_REFRESH_SECONDS", 5)
