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

DAILY_RUN_TIME = "08:45"


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
ENSURE_WINDOW_DAYS = 70  # 需要刷新时, 保证近60个交易日数据存在 (新筛选需MA60)


def _before_market_close() -> bool:
    """当前时间是否早于 15:30 (盘中)"""
    now = datetime.now()
    return (now.hour, now.minute) < MARKET_CLOSE


def _fetch_today_if_missing():
    """预检: 若当天是交易日且天级表中无当天数据, 先单独拉取当天数据。

    在 run_updates() 的主流程之前调用, 确保当天数据就位后再做完整性判断。
    盘中会拉到盘中快照数据, 收盘后会拉到完整日线数据。
    """
    today_str = date.today().strftime("%Y-%m-%d")
    try:
        from trade_calendar import is_trade_date
        if not is_trade_date(today_str):
            return
    except Exception:
        return

    logger.info(f"[预检] 检测当天({today_str})数据是否就位...")

    steps = [
        ("个股", "stock_daily", "stock_daily", "YYYY-MM-DD"),
        ("板块", "sector_daily", "sector_service", "YYYYMMDD"),
        ("ETF", "etf_daily", "etf_service", "YYYYMMDD"),
        ("指数", "index_daily", "index_daily", "YYYYMMDD"),
    ]
    for name, table, module, fmt in steps:
        # 检查当天是否有数据
        try:
            from db_handler import get_connection
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(f"SELECT COUNT(*) FROM {table} WHERE `date` = %s", (today_str,))
                count = cur.fetchone()[0]
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"[预检] 查询 {table} 失败: {e}")
            count = 0

        if count > 0:
            logger.info(f"[预检] [{name}] {today_str} 数据已存在 ({count}条), 跳过")
            continue

        # 当天数据不存在, 单独拉取当天
        logger.info(f"[预检] [{name}] {today_str} 数据缺失, 正在拉取...")
        try:
            if fmt == "YYYYMMDD":
                _import_run(module, start_date=today_str.replace("-", ""),
                                          end_date=today_str.replace("-", ""))
            else:
                _import_run(module, start_date=today_str, end_date=today_str)
            logger.info(f"[预检] [{name}] {today_str} 拉取完成")
        except Exception as e:
            logger.error(f"[预检] [{name}] {today_str} 拉取失败: {e}")


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
    """数据更新: 板块 -> 个股 -> ETF -> 指数 (增量, 避免全量重拉)。

    - 时间锚点统一取当日 (today_anchor), 不再回退到上一个交易日
    - 交易日 15:30 之前: 强制拉取当日盘中快照 (近ENSURE_WINDOW_DAYS个交易日窗口)
    - 收盘后: 先预检当天数据是否缺失, 缺则补拉; 再做完整性判断和增量更新
    """
    today_str = date.today().strftime("%Y-%m-%d")
    try:
        from trade_calendar import is_trade_date, today_anchor
        is_today_trade = is_trade_date(today_str)
    except Exception:
        is_today_trade = False

    # 所有模块统一锚点: 当日 (今天是交易日) 或最近交易日
    eff_anchor = anchor or today_anchor()

    if is_today_trade and _before_market_close():
        force_refresh = True   # 盘中: 每次都刷新当日快照
    else:
        # 收盘后: 先预检当天数据, 缺则补拉
        _fetch_today_if_missing()
        force_refresh = False

    if not eff_anchor:
        logger.warning("无交易日锚点, 跳过数据更新")
        return

    mode = "盘中(15:30前)-强制拉取当日" if force_refresh else "收盘后-增量检测"
    logger.info(f"数据更新模式: {mode}, 锚点={eff_anchor}")

    steps = [
        ("板块", "sector_daily", "sector_service", "YYYYMMDD"),
        ("个股", "stock_daily", "stock_daily", "YYYY-MM-DD"),
        ("ETF", "etf_daily", "etf_service", "YYYYMMDD"),
        ("指数", "index_daily", "index_daily", "YYYYMMDD"),
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

    # 收盘后: 个股资金流/估值快照 (东财) + 板块涨跌家数/资金流入回填
    if not force_refresh:
        _capture_market_stats(eff_anchor)


def _capture_market_stats(anchor: str):
    """收盘后数据快照: 个股主力资金流 + 个股估值 + 板块统计回填。

    - stock_moneyflow: 东财 clist 资金流排行 (约2个请求)
    - stock_list: 东财估值快照 PE/PB/市值/换手 (约2个请求)
    - sector_daily advance/decline/net_inflow: 由上述数据聚合回填
    """
    logger.info(f"===== [E] 市场统计快照 (锚点 {anchor}) =====")
    try:
        from eastmoney_client import fetch_stock_moneyflow_rank, fetch_stock_valuation
        from db_handler import save_stock_moneyflow_to_db, save_stock_list_to_db
        df_mf = fetch_stock_moneyflow_rank()
        if df_mf is not None and not df_mf.empty:
            df_mf["date"] = anchor
            save_stock_moneyflow_to_db(df_mf)
            logger.info(f"个股资金流快照: {len(df_mf)} 只 @ {anchor}")
        else:
            logger.warning("个股资金流排行无数据 (东财不可用?)")
        df_val = fetch_stock_valuation()
        if df_val is not None and not df_val.empty:
            save_stock_list_to_db(df_val)
            logger.info(f"个股估值快照: {len(df_val)} 只")
    except Exception as e:
        logger.error(f"资金流/估值快照失败: {e}")

    try:
        from trade_calendar import get_trade_dates
        from sector_service import refresh_sector_stats
        dates = get_trade_dates(
            (datetime.strptime(anchor, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d"),
            anchor,
        )
        if dates:
            refresh_sector_stats(dates[0], dates[-1])
    except Exception as e:
        logger.error(f"板块统计回填失败: {e}")


def _import_run(module: str, **kwargs):
    import importlib
    mod = importlib.import_module(module)
    return mod.run(**kwargs)


def run_screens(anchor: str = ""):
    """筛选与分析: 板块 -> 股票 -> ETF -> 量价分析 -> 量增价涨

    Args:
        anchor: 时间锚点 (YYYY-MM-DD), 统一传当日, 各模块不再自行取库内最新日期
    """
    if not anchor:
        from trade_calendar import today_anchor
        anchor = today_anchor()

    logger.info(f"===== [A] 板块筛选 (锚点 {anchor}) =====")
    try:
        from sector_screen import run as sector_screen_run
        sector_screen_run(identified_date=anchor)
    except Exception as e:
        logger.error(f"板块筛选失败: {e}")

    logger.info(f"===== [B] 股票筛选 (锚点 {anchor}) =====")
    try:
        from stock_screen import run as stock_screen_run
        stock_screen_run(identified_date=anchor)
    except Exception as e:
        logger.error(f"股票筛选失败: {e}")

    logger.info(f"===== [C] ETF筛选 (锚点 {anchor}) =====")
    try:
        from etf_screen import run as etf_screen_run
        etf_screen_run(identified_date=anchor)
    except Exception as e:
        logger.error(f"ETF筛选失败: {e}")

    logger.info(f"===== [D] 量价分析 (锚点 {anchor}) =====")
    try:
        from volume_price import run as volume_price_run
        volume_price_run(analysis_date=anchor)
    except Exception as e:
        logger.error(f"量价分析失败: {e}")

    logger.info(f"===== [E] 量增价涨筛选 (锚点 {anchor}) =====")
    try:
        from volrise_screen import run as volrise_run
        volrise_run(anchor=anchor)
    except Exception as e:
        logger.error(f"量增价涨筛选失败: {e}")


def send_daily_email():
    """发送订阅邮件: 市场情绪+候选个股+量价分析+量增价涨+候选ETF+下个交易日预测结论"""
    logger.info("===== [F] 发送订阅邮件 =====")
    try:
        from email_service import run as email_run
        email_run()
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")


def job():
    t0 = time.time()
    logger.info("=== 每日定时任务开始 ===")
    from trade_calendar import today_anchor
    anchor = today_anchor()
    logger.info(f"当日时间锚点: {anchor}")
    run_updates(anchor)
    run_screens(anchor)
    send_daily_email()
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
