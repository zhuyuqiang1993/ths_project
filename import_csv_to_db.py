import sys
from pathlib import Path

sys.path.insert(0, "D:\\ths_project")

import pandas as pd
from loguru import logger

from config import CONFIG
from db_handler import (
    create_tables,
    save_stock_daily_to_db,
    save_sector_daily_to_db,
    save_etf_daily_to_db,
)
from sector_service import _REVERSE_CN as SECTOR_REVERSE
from etf_service import _REVERSE_CN as ETF_REVERSE

DATA_DIR = CONFIG.data_dir

_CN_STOCK = {
    "date": "date", "code": "code", "name": "name",
    "board_code": "board_code", "board_name": "board_name",
    "prev_close": "prev_close", "open": "open", "pct_chg": "pct_chg",
    "volume": "volume", "amount": "amount",
    "macd": "macd", "macd_signal": "macd_signal", "macd_hist": "macd_hist",
}


def load_stock():
    files = sorted(DATA_DIR.glob("stock_daily_*.csv"))
    if not files:
        logger.warning("未找到个股CSV")
        return
    frames = [pd.read_csv(f, encoding=CONFIG.csv_encoding) for f in files]
    df = pd.concat(frames, ignore_index=True)
    df.columns = [_CN_STOCK.get(str(c), str(c)) for c in df.columns]
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["board_code"] = df["board_code"].astype(str).str.zfill(6)
    df = df.drop_duplicates(subset=["code", "date"], keep="last")
    logger.info(f"个股CSV: {len(files)} 个文件, {len(df)} 条")
    save_stock_daily_to_db(df)


def load_sector():
    fp = DATA_DIR / "sector_daily.csv"
    if not fp.exists():
        logger.warning("未找到板块CSV")
        return
    df = pd.read_csv(fp, encoding=CONFIG.csv_encoding)
    df.rename(columns=SECTOR_REVERSE, inplace=True)
    df["board_code"] = df["board_code"].astype(str).str.zfill(6)
    df = df.drop_duplicates(subset=["board_code", "date"], keep="last")
    logger.info(f"板块CSV: {len(df)} 条")
    save_sector_daily_to_db(df)


def load_etf():
    fp = DATA_DIR / "etf_daily.csv"
    if not fp.exists():
        logger.warning("未找到ETF CSV")
        return
    df = pd.read_csv(fp, encoding=CONFIG.csv_encoding)
    df.rename(columns=ETF_REVERSE, inplace=True)
    df["code"] = df["code"].astype(str).str.zfill(6)
    df = df.drop_duplicates(subset=["code", "date"], keep="last")
    logger.info(f"ETF CSV: {len(df)} 条")
    save_etf_daily_to_db(df)


if __name__ == "__main__":
    create_tables()
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "stock"):
        load_stock()
    if which in ("all", "sector"):
        load_sector()
    if which in ("all", "etf"):
        load_etf()
