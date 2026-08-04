import sys
import time
from datetime import date, datetime, timedelta

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


MARKET_CLOSE = (15, 30)
SKIP_WINDOW_DAYS = 5     # 近5个交易日数据已存在则跳过更新
ENSURE_WINDOW_DAYS = 60  # 需要刷新时, 保证近60个交易日数据存在 (新筛选需MA60)


def _before_market_close() -> bool:
    """当前时间是否早于 15:30 (盘中)"""
    now = datetime.now()
    return (now.hour, now.minute) < MARKET_CLOSE


def _recent_trade_dates(anchor: str, n: int) -> list:
    """anchor 往前 n 个交易日 (含 anchor), 按升序返回"""
    from trade_calendar import get_trade_dates
    d = datetime.strptime(anchor, "%Y-%m-%d")
    lookback_start = (d - timedelta(days=n * 3 + 15)).strftime("%Y-%m-%d")
    dates = get_trade_dates(lookback_start, anchor)
    return dates[-n:]


def _has_recent_days(table: str, dates: list) -> bool:
    """表内是否已存在 dates 中全部交易日的记录"""
    if not dates:
        return False
    placeholders = ",".join(["%s"] * len(dates))
    try:
        from db_handler import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                f"SELECT COUNT(DISTINCT `date`) FROM {table} WHERE `date` IN ({placeholders})",
                tuple(dates),
            )
            return cur.fetchone()[0] >= len(dates)
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"检查 {table} 近{len(dates)}个交易日数据失败: {e}")
        return False


def run_updates(anchor: str | None):
    """数据更新: 板块 -> 个股 -> ETF (增量, 避免全量重拉)。

    - 交易日 15:30 之前: 需要拉取当日(盘中)数据, 强制刷新近10个交易日窗口
    - 其余时间: 若近5个交易日数据已存在则跳过; 否则只增量拉取近10个交易日窗口
    """
    today_str = date.today().strftime("%Y-%m-%d")
    try:
        from trade_calendar import is_trade_date
        force_refresh = is_trade_date(today_str) and _before_market_close()
    except Exception:
        force_refresh = False

    eff_anchor = anchor or today_str
    if not eff_anchor:
        logger.warning("无交易日锚点, 跳过数据更新")
        return

    mode = "盘中(15:30前)-强制拉取当日" if force_refresh else "收盘后-增量检测"
    logger.info(f"数据更新模式: {mode}, 锚点={eff_anchor}")

    steps = [
        ("板块", "sector_daily", "sector_service", "YYYYMMDD"),
        ("个股", "stock_daily", "stock_daily", "YYYY-MM-DD"),
        ("ETF", "etf_daily", "etf_service", "YYYYMMDD"),
    ]
    for name, table, module, fmt in steps:
        if not force_refresh and _has_recent_days(table, _recent_trade_dates(eff_anchor, SKIP_WINDOW_DAYS)):
            logger.info(f"[{name}] 近{SKIP_WINDOW_DAYS}个交易日数据已存在 (至 {eff_anchor}), 跳过更新")
            continue
        dates = _recent_trade_dates(eff_anchor, ENSURE_WINDOW_DAYS)
        start, end = dates[0], dates[-1]
        if fmt == "YYYYMMDD":
            start, end = start.replace("-", ""), end.replace("-", "")
        logger.info(f"===== [{name}] 增量更新: {start} ~ {end} =====")
        try:
            _import_run(module, start_date=start, end_date=end)
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
