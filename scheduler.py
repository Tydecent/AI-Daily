"""调度：每天北京时间 07:00 生成日报，并在进程启动时后台立即生成一次。

注意：调度器只在主进程启动（app.py 的 __main__ 分支），配合
use_reloader=False，避免 Flask 调试重载导致任务重复注册。
"""

from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from services.daily_report import generate_daily_report

logger = logging.getLogger(__name__)

JOB_ID = "daily_ai_report"
_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()


def _run_job() -> None:
    try:
        generate_daily_report()
    except Exception:  # noqa: BLE001 - 定时任务异常不应终止调度器
        logger.exception("定时生成日报时发生未捕获异常")


def start_scheduler() -> BackgroundScheduler:
    """创建并启动 BackgroundScheduler（幂等，重复调用只启动一次）。"""
    global _scheduler
    with _lock:
        if _scheduler is not None and _scheduler.running:
            logger.info("调度器已在运行，跳过重复启动")
            return _scheduler

        scheduler = BackgroundScheduler(timezone=config.TIMEZONE_NAME)
        scheduler.add_job(
            _run_job,
            trigger=CronTrigger(hour=7, minute=0, timezone=config.TIMEZONE_NAME),
            id=JOB_ID,
            name="每日 07:00 生成 AI 日报",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        scheduler.start()
        _scheduler = scheduler
        logger.info(
            "调度器已启动，时区 %s，下一次运行时间：%s",
            config.TIMEZONE_NAME,
            scheduler.get_job(JOB_ID).next_run_time,
        )
        return scheduler


def start_initial_generation() -> threading.Thread:
    """启动时在后台线程立即生成一次，避免阻塞 Flask 启动。"""
    thread = threading.Thread(
        target=_run_job, name="initial-report-generation", daemon=True
    )
    thread.start()
    logger.info("已在后台线程触发启动时日报生成")
    return thread


def shutdown_scheduler() -> None:
    global _scheduler
    with _lock:
        if _scheduler is not None and _scheduler.running:
            _scheduler.shutdown(wait=False)
            logger.info("调度器已停止")
        _scheduler = None
