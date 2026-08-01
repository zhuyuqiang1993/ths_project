import sys
import warnings
from datetime import date, datetime

import pandas as pd
from loguru import logger

from config import CONFIG
from db_handler import get_connection, save_candidate_sector_to_db, create_tables

warnings.filterwarnings("ignore", message=".*only supports SQLAlchemy.*")

logger.remove()
logger.add(
    sys.stderr,
    level=CONFIG.log_level,
    format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}",
)
logger.add(
    CONFIG.log_dir / "sector_screen_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
)

SCREEN_WINDOW = 5

# 与 sector_daily 列名保持一致
_COLS = ["board_code", "board_name", "date", "low", "high", "close",
         "prev_close", "volume", "amount", "pct_chg", "change",
         "advance", "decline", "net_inflow"]


def load_last_n_days(n: int = SCREEN_WINDOW) -> pd.DataFrame:
    """加载每个板块最近 n 个交易日的数据"""
    sql = f"""
        SELECT board_code, board_name, date, low, high, close,
               prev_close, volume, amount, pct_chg, `change`,
               advance, decline, net_inflow
        FROM (
            SELECT sd.*, ROW_NUMBER() OVER (
                PARTITION BY board_code ORDER BY date DESC) AS rn
            FROM sector_daily sd
        ) t WHERE rn <= {n}
        ORDER BY board_code, date
    """
    conn = get_connection()
    try:
        df = pd.read_sql(sql, conn)
    finally:
        conn.close()
    return df


def _pass_screen(closes: list, vols: list) -> bool:
    """量价配合筛选:
    - 条件1: 5日收盘指数增长 (close[-1] > close[0])
    - 条件2: 指数涨时成交量同步增大, 指数跌时成交量缩小
    """
    if len(closes) < SCREEN_WINDOW:
        return False
    if closes[-1] <= closes[0]:
        return False
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            if vols[i] < vols[i - 1]:
                return False
        elif closes[i] < closes[i - 1]:
            if vols[i] > vols[i - 1]:
                return False
    return True


def screen_sectors(identified_date: str = "") -> pd.DataFrame:
    identified_at = identified_date or date.today().isoformat()
    df = load_last_n_days()
    if df.empty:
        logger.warning("sector_daily 无数据")
        return pd.DataFrame()

    candidates = []
    for board_code, grp in df.groupby("board_code"):
        grp = grp.sort_values("date").reset_index(drop=True)
        closes = grp["close"].astype(float).tolist()
        vols = grp["volume"].fillna(0).astype(float).tolist()
        if not _pass_screen(closes, vols):
            continue

        last = grp.iloc[-1]
        chg_5d = round((closes[-1] / closes[0] - 1) * 100, 4) if closes[0] else 0
        row = {
            "board_code": board_code,
            "board_name": last["board_name"],
            "date": last["date"],
            "identified_at": identified_at,
            "low": last["low"], "high": last["high"], "close": last["close"],
            "prev_close": last["prev_close"], "volume": last["volume"],
            "amount": last["amount"], "pct_chg": last["pct_chg"],
            "change": last["change"], "advance": last["advance"],
            "decline": last["decline"], "net_inflow": last["net_inflow"],
            "chg_5d": chg_5d,
        }
        candidates.append(row)
        logger.info(f"  [候选] {board_code} {last['board_name']} "
                    f"收盘={last['close']} 5日涨幅={chg_5d}%")

    if not candidates:
        logger.warning("无板块通过筛选")
        return pd.DataFrame()

    result = pd.DataFrame(candidates)
    return result[_COLS + ["identified_at", "chg_5d"]]


def run(identified_date: str = ""):
    create_tables()
    logger.info(f"===== 板块筛选开始 (识别日期: {identified_date or date.today().isoformat()}) =====")
    df = screen_sectors(identified_date)
    if df.empty:
        logger.warning("===== 板块筛选结束: 无候选 =====")
        return df

    save_candidate_sector_to_db(df)
    logger.info(f"候选板块 {len(df)} 个, 已保存到 candidate_sector")
    for _, r in df.iterrows():
        logger.info(f"  {r['board_code']} {r['board_name']} "
                    f"日期={r['date']} 5日涨幅={r['chg_5d']}%")
    return df


if __name__ == "__main__":
    run()
