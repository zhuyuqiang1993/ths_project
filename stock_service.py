import sys
import time
from datetime import datetime

from loguru import logger

from config import CONFIG
from stock_fetcher import get_stock_list, fetch_stock_daily, save_stock_data
from db_handler import save_stock_list_to_db, save_stock_daily_to_db

logger.remove()
logger.add(
    sys.stderr,
    level=CONFIG.log_level,
    format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}",
)
logger.add(
    CONFIG.log_dir / "service_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
)


def run():
    CONFIG.data_dir.mkdir(parents=True, exist_ok=True)
    CONFIG.log_dir.mkdir(parents=True, exist_ok=True)

    logger.info("获取全部A股股票列表...")
    stock_df = get_stock_list()
    codes = stock_df["code"].tolist()
    logger.info(f"共获取 {len(codes)} 只股票")
    save_stock_list_to_db(stock_df)

    total = len(codes)
    success = 0
    failed = []

    for i, code in enumerate(codes, 1):
        try:
            logger.info(f"[{i}/{total}] 正在获取 {code}...")
            df = fetch_stock_daily(code)
            if df is not None and not df.empty:
                save_stock_data(df, code)
                save_stock_daily_to_db(df, code)
                success += 1
                logger.info(f"  -> {code} 保存成功 ({len(df)} 条)")
            else:
                failed.append(code)
                logger.warning(f"  -> {code} 无数据")
            time.sleep(CONFIG.request_interval)
        except Exception as e:
            failed.append(code)
            logger.error(f"  -> {code} 获取失败: {e}")

    logger.info(f"运行完成: 成功 {success}/{total}, 失败 {len(failed)}")
    if failed:
        logger.info(f"失败列表: {failed}")


if __name__ == "__main__":
    run()
