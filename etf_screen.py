import sys
import warnings
from datetime import date

import pandas as pd
from loguru import logger

from config import CONFIG
from db_handler import get_connection, create_tables, save_candidate_etf_to_db

warnings.filterwarnings("ignore", message=".*only supports SQLAlchemy.*")

logger.remove()
logger.add(
    sys.stderr,
    level=CONFIG.log_level,
    format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}",
)
logger.add(
    CONFIG.log_dir / "etf_screen_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
)

SCREEN_WINDOW = 5

_COLS = ["code", "name", "date", "close", "prev_close",
         "pct_chg", "volume", "amount", "chg_5d", "identified_at"]


def _query(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = get_connection()
    try:
        df = pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()
    return df


def load_last_n_days(n: int = SCREEN_WINDOW) -> pd.DataFrame:
    """加载每只ETF最近 n 个交易日的数据"""
    sql = f"""
        SELECT code, name, date, close, prev_close, pct_chg, volume, amount
        FROM (
            SELECT ed.*, ROW_NUMBER() OVER (
                PARTITION BY code ORDER BY date DESC) AS rn
            FROM etf_daily ed
        ) t WHERE rn <= {n}
        ORDER BY code, date
    """
    return _query(sql)


def _pass_screen(closes: list, vols: list) -> bool:
    """量价配合筛选 (与板块筛选同款):
    - 条件1: 5日收盘价上升 (close[-1] > close[0])
    - 条件2: 上涨日成交量同步增大, 下跌日成交量缩小 (逐日)
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


def screen_etfs(identified_date: str = "") -> pd.DataFrame:
    identified_at = identified_date or date.today().isoformat()
    df = load_last_n_days()
    if df.empty:
        logger.warning("etf_daily 无数据")
        return pd.DataFrame()

    candidates = []
    for code, grp in df.groupby("code"):
        grp = grp.sort_values("date").reset_index(drop=True)
        closes = grp["close"].astype(float).tolist()
        vols = grp["volume"].fillna(0).astype(float).tolist()
        if not _pass_screen(closes, vols):
            continue

        last = grp.iloc[-1]
        chg_5d = round((closes[-1] / closes[0] - 1) * 100, 4) if closes[0] else 0
        row = {
            "code": code,
            "name": last["name"],
            "date": last["date"],
            "identified_at": identified_at,
            "close": last["close"],
            "prev_close": last["prev_close"],
            "pct_chg": last["pct_chg"],
            "volume": last["volume"],
            "amount": last["amount"],
            "chg_5d": chg_5d,
        }
        candidates.append(row)

    if not candidates:
        logger.warning("无ETF通过筛选")
        return pd.DataFrame()

    result = pd.DataFrame(candidates)
    return result[_COLS]


def run(identified_date: str = ""):
    create_tables()
    logger.info(f"===== 候选ETF筛选开始 (识别日期: {identified_date or date.today().isoformat()}) =====")
    df = screen_etfs(identified_date)
    if df.empty:
        logger.warning("===== 候选ETF筛选结束: 无候选 =====")
        return df

    save_candidate_etf_to_db(df, replace_date=True)
    logger.info(f"候选ETF {len(df)} 只, 已保存到 candidate_etf")
    top = df.sort_values("chg_5d", ascending=False).head(20)
    for _, r in top.iterrows():
        logger.info(f"  {r['code']} {r['name']} 日期={r['date']} "
                    f"涨幅={r['pct_chg']}% 5日={r['chg_5d']}%")
    return df


if __name__ == "__main__":
    run()
