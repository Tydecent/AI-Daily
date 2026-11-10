"""Flask 入口：页面渲染 + JSON API + 启动引导（后台生成 + 定时调度）。"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from logging.handlers import RotatingFileHandler

from flask import Flask, jsonify, render_template

import config
from scheduler import start_initial_generation, start_scheduler
from services import daily_report

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def configure_logging() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.INFO

    root = logging.getLogger()
    if root.handlers:  # 避免重复添加 handler
        root.setLevel(level)
        return

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s/%(threadName)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    file_handler = RotatingFileHandler(
        config.LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    root.setLevel(level)
    logging.getLogger("apscheduler").setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# 展示辅助
# ---------------------------------------------------------------------------
WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def _display_date(date_iso: str | None) -> str:
    if not date_iso:
        return ""
    try:
        parsed = date.fromisoformat(str(date_iso))
    except ValueError:
        return str(date_iso)
    return f"{parsed.year}年{parsed.month}月{parsed.day}日 {WEEKDAY_CN[parsed.weekday()]}"


def _count_items(report: dict) -> int:
    return sum(len(section.get("items") or []) for section in report.get("sections") or [])


def build_page_context() -> dict:
    report = daily_report.load_latest()
    state = daily_report.get_state()
    expected = daily_report.get_target_date()

    generating = state.get("status") == "generating"

    if report is None:
        return {
            "report": None,
            "generating": generating,
            "state": state,
            "notice": None,
            "is_stale": False,
            "display_date": "",
            "generated_at": "",
            "item_count": 0,
            # 无缓存时：生成中或尚未开始都自动刷新；明确失败则等待用户手动重试
            "auto_refresh": 0
            if state.get("status") == "error"
            else config.AUTO_REFRESH_SECONDS,
            "expected_date": expected.isoformat(),
        }

    meta = report.get("meta") or {}
    report_date = meta.get("target_date") or report.get("date") or ""
    is_stale = bool(report_date) and report_date != expected.isoformat()

    notice = meta.get("notice") or ""

    # 本次更新失败：页面顶部明确提示“展示上次日报”
    state_message = (state.get("message") or "").strip()
    if state.get("status") == "error" and state_message:
        notice = f"{state_message}。{notice}" if notice else state_message

    if is_stale:
        stale_msg = (
            f"当前展示的是 {report_date or '上一次'} 的日报，"
            f"{expected.isoformat()} 的日报尚未生成成功。"
        )
        notice = f"{stale_msg} {notice}".strip()

    return {
        "report": report,
        "generating": generating,
        "state": state,
        "notice": notice,
        "is_stale": is_stale,
        "display_date": _display_date(report_date),
        "generated_at": meta.get("generated_at") or "",
        "item_count": meta.get("item_count") or _count_items(report),
        # 已有缓存时由页面脚本轮询 /api/status，避免整页重复刷新
        "auto_refresh": 0,
        "expected_date": expected.isoformat(),
    }


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------
def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.json.ensure_ascii = False
    app.json.sort_keys = False

    @app.route("/")
    def index():
        return render_template("index.html", **build_page_context())

    @app.route("/api/report")
    def api_report():
        report = daily_report.load_latest()
        if report is None:
            return jsonify(
                {
                    "status": "empty",
                    "message": "日报尚在生成中，请稍候",
                    "target_date": daily_report.get_target_date().isoformat(),
                }
            )
        return app.response_class(
            response=json.dumps(report, ensure_ascii=False, indent=2),
            status=200,
            mimetype="application/json; charset=utf-8",
        )

    @app.route("/api/status")
    def api_status():
        state = daily_report.get_state()
        return jsonify(
            {
                "status": state.get("status", "idle"),
                "last_generated_at": state.get("last_generated_at"),
                "target_date": state.get("target_date"),
                "item_count": state.get("item_count", 0),
                "message": state.get("message", ""),
            }
        )

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "time": datetime.now(config.TZ).isoformat()})

    return app


app = create_app()


def bootstrap() -> None:
    """启动后台生成 + 定时调度（幂等）。"""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    start_scheduler()
    start_initial_generation()


if __name__ == "__main__":
    configure_logging()
    logger.info("启动 AI 日报服务：http://%s:%s", config.HOST, config.PORT)
    bootstrap()
    app.run(
        host=config.HOST,
        port=config.PORT,
        debug=False,
        use_reloader=False,
    )


# 供 `python app.py` 之外的方式（如 gunicorn）使用：模块导入时不做重活
__all__ = ["app", "create_app", "bootstrap"]
