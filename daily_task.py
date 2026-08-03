import sys
import time
from datetime import date, datetime

import schedule
from loguru import logger

from config import CONFIG

logger.remove()
logger.add(
    sys.stderr,
    level=CONFIG.log_level,
    format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}",
)
logger.add(
    CONFIG.log_dir / "daily_task_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
)

DAILY_RUN_TIME = "17:00"


def latest_trade_date() -> str | None:
    """最近一个交易日 (<= 今天), 跳过周末与法定节假日"""
    try:
        from trade_calendar import latest_trade_date as _latest
        return _latest()
    except Exception as e:
        logger.warning(f"获取交易日历失败: {e}")
        return None


def db_latest_date(table: str) -> str | None:
    """查询某表最新日期 (锚点)"""
    try:
        from db_handler import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT MAX(`date`) FROM {table}")
            row = cur.fetchone()
            return str(row[0]) if row and row[0] else None
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"查询 {table} 最新日期失败: {e}")
        return None


def _data_is_current(table: str, anchor: str | None) -> bool:
    """库内最新日期是否已覆盖锚点 (非交易日/无新数据则跳过更新, 幂等)"""
    if not anchor:
        return False
    latest = db_latest_date(table)
    return bool(latest and latest >= anchor)


def run_updates(anchor: str | None):
    """数据更新: 板块 -> 个股 -> ETF (以最近交易日为锚点, 无新数据自动跳过)"""
    today = date.today().strftime("%Y-%m-%d")
    steps = [
        ("板块", "sector_daily", lambda: _import_run("sector_service"), {}),
        ("个股", "stock_daily",
         lambda: _import_run("stock_daily", start_date=today, end_date=today), {}),
        ("ETF", "etf_daily", lambda: _import_run("etf_service"), {}),
    ]
    for name, table, run_fn, _ in steps:
        if _data_is_current(table, anchor):
            logger.info(f"[{name}] 数据已是最新 (锚点 {anchor}), 跳过更新")
            continue
        logger.info(f"===== [{name}] 数据更新 =====")
        try:
            run_fn()
        except Exception as e:
            logger.error(f"{name}更新失败: {e}")


def _import_run(module: str, **kwargs):
    import importlib
    mod = importlib.import_module(module)
    return mod.run(**kwargs)


def run_screens():
    """筛选: 板块 -> 股票 -> ETF"""
    logger.info("===== [A] 板块筛选 =====")
    try:
        from sector_screen import run as sector_screen_run
        sector_screen_run()
    except Exception as e:
        logger.error(f"板块筛选失败: {e}")

    logger.info("===== [B] 股票筛选 =====")
    try:
        from stock_screen import run as stock_screen_run
        stock_screen_run()
    except Exception as e:
        logger.error(f"股票筛选失败: {e}")

    logger.info("===== [C] ETF筛选 =====")
    try:
        from etf_screen import run as etf_screen_run
        etf_screen_run()
    except Exception as e:
        logger.error(f"ETF筛选失败: {e}")


def send_daily_email():
    """发送订阅邮件: 市场情绪+候选个股+候选ETF+下个交易日预测结论"""
    logger.info("===== [D] 发送订阅邮件 =====")
    try:
        from email_service import run as email_run
        email_run()
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")


def job():
    t0 = time.time()
    logger.info("=== 每日定时任务开始 ===")
    anchor = latest_trade_date()
    logger.info(f"最近交易日锚点: {anchor}")
    run_updates(anchor)
    run_screens()
    #send_daily_email()
    logger.info(f"=== 每日定时任务结束 (耗时 {round(time.time() - t0, 1)}s) ===")


if __name__ == "__main__":
    run_time = getattr(CONFIG, "daily_run_time", DAILY_RUN_TIME) or DAILY_RUN_TIME
    schedule.every().day.at(run_time).do(job)
    logger.info(f"每日定时任务已启动, 每日 {run_time} 运行: "
                f"更新板块/个股/ETF + 板块/股票/ETF筛选 + 发送订阅邮件")

    now = datetime.now().strftime("%H:%M")
    logger.info(f"当前时间: {now}")

    if len(sys.argv) > 1 and sys.argv[1] == "--now":
        logger.info("立即执行一次完整任务")
        job()
    else:
        while True:
            schedule.run_pending()
            time.sleep(60)
