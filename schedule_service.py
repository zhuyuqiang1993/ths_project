import sys
import time
from datetime import datetime

import schedule
from loguru import logger

from config import CONFIG
from stock_service import run as run_once

logger.remove()
logger.add(
    sys.stderr,
    level=CONFIG.log_level,
    format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}",
)
logger.add(
    CONFIG.log_dir / "scheduler_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
)


def job():
    logger.info("=== 定时任务触发 ===")
    try:
        run_once()
    except Exception as e:
        logger.error(f"定时任务执行失败: {e}")
    logger.info("=== 定时任务结束 ===")


if __name__ == "__main__":
    run_time = CONFIG.daily_run_time
    schedule.every().day.at(run_time).do(job)
    logger.info(f"定时服务已启动, 每日 {run_time} 运行")

    now = datetime.now().strftime("%H:%M")
    logger.info(f"当前时间: {now}")

    job()

    while True:
        schedule.run_pending()
        time.sleep(60)
