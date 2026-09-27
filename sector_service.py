import sys
import time
from datetime import datetime, timedelta
from typing import Optional

import akshare as ak
import pandas as pd
from loguru import logger

from config import CONFIG

logger.remove()
logger.add(
    sys.stderr,
    level=CONFIG.log_level,
    format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}",
)
logger.add(
    CONFIG.log_dir / "sector_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
)


COL_BOARD_CODE = "board_code"
COL_BOARD_NAME = "board_name"
COL_DATE = "date"
COL_LOW = "low"
COL_HIGH = "high"
COL_CLOSE = "close"
COL_VOLUME = "volume"
COL_PREV_CLOSE = "prev_close"
COL_PCT_CHG = "pct_chg"
COL_CHANGE = "change"
COL_AMOUNT = "amount"
COL_ADVANCE = "advance"
COL_DECLINE = "decline"
COL_NET_INFLOW = "net_inflow"

_CN_NAMES = {
    "board_code": "板块代码",
    "board_name": "板块名称",
    "date": "日期",
    "low": "最低点",
    "high": "最高点",
    "close": "收盘指数",
    "volume": "成交量",
    "amount": "成交额",
    "prev_close": "昨收指数",
    "pct_chg": "日涨幅",
    "change": "涨跌额",
    "advance": "上涨家数",
    "decline": "下跌家数",
    "net_inflow": "资金净流入",
}
_REVERSE_CN = {v: k for k, v in _CN_NAMES.items()}

_HIST_COLS = {
    "日期": "date",
    "开盘价": "open",
    "最高价": "high",
    "最低价": "low",
    "收盘价": "close",
    "成交量": "volume",
    "成交额": "amount",
}


def get_ths_boards(board_type: str = "industry") -> pd.DataFrame:
    if board_type == "concept":
        df = ak.stock_board_concept_name_ths()
    else:
        df = ak.stock_board_industry_name_ths()
    df.rename(columns={"name": COL_BOARD_NAME, "code": COL_BOARD_CODE}, inplace=True)
    df[COL_BOARD_CODE] = df[COL_BOARD_CODE].astype(str)
    kind = "概念" if board_type == "concept" else "行业"
    logger.info(f"同花顺{kind}板块: {len(df)} 个")
    return df


def fetch_ths_board_daily(
    board_name: str,
    start_date: str = "",
    end_date: str = "",
    board_type: str = "industry",
) -> Optional[pd.DataFrame]:
    start = start_date or "20200101"
    end = end_date or datetime.now().strftime("%Y%m%d")

    dt_start = datetime.strptime(start, "%Y%m%d")
    hist_start = (dt_start - timedelta(days=10)).strftime("%Y%m%d")

    fetch_fn = (ak.stock_board_concept_index_ths if board_type == "concept"
                else ak.stock_board_industry_index_ths)
    for attempt in range(CONFIG.retry_times):
        try:
            df = fetch_fn(
                symbol=board_name,
                start_date=hist_start,
                end_date=end,
            )
            if df is None or df.empty:
                return None
            df.rename(columns=_HIST_COLS, inplace=True)
            df[COL_DATE] = pd.to_datetime(df[COL_DATE]).dt.strftime("%Y-%m-%d")
            df.sort_values(COL_DATE, inplace=True)

            df[COL_PREV_CLOSE] = df[COL_CLOSE].shift(1)
            df[COL_PCT_CHG] = df[COL_CLOSE].pct_change() * 100
            df[COL_CHANGE] = df[COL_CLOSE].diff()

            df = df[df[COL_DATE] >= start[:4] + "-" + start[4:6] + "-" + start[6:]]
            if df.empty:
                return None

            df.drop(columns=["open"], inplace=True, errors="ignore")
            return df
        except Exception as e:
            if attempt < CONFIG.retry_times - 1:
                time.sleep(CONFIG.retry_delay)
                continue
            logger.warning(f"  {board_name} 数据获取失败: {e}")
            return None


def build_ths_sector_daily(
    boards: pd.DataFrame,
    start_date: str = "",
    end_date: str = "",
    max_boards: int = 0,
    board_type: str = "industry",
) -> pd.DataFrame:
    all_rows = []
    board_names = boards[COL_BOARD_NAME].tolist()
    if max_boards > 0:
        board_names = board_names[:max_boards]

    for i, name in enumerate(board_names, 1):
        logger.info(f"[{i}/{len(board_names)}] {name}...")
        df_daily = fetch_ths_board_daily(name, start_date, end_date, board_type)
        if df_daily is not None and not df_daily.empty:
            code = boards.loc[boards[COL_BOARD_NAME] == name, COL_BOARD_CODE].values[0]
            df_daily[COL_BOARD_CODE] = code
            df_daily[COL_BOARD_NAME] = name
            df_daily[COL_ADVANCE] = None
            df_daily[COL_DECLINE] = None
            df_daily[COL_NET_INFLOW] = None
            all_rows.append(df_daily)
            logger.info(f"  -> {name}: {len(df_daily)} 条")
        time.sleep(CONFIG.request_interval)

    if not all_rows:
        return pd.DataFrame()

    result = pd.concat(all_rows, ignore_index=True)
    cols = [
        COL_BOARD_CODE, COL_BOARD_NAME,
        COL_DATE, COL_LOW, COL_HIGH, COL_CLOSE, COL_PREV_CLOSE,
        COL_VOLUME, COL_AMOUNT,
        COL_PCT_CHG, COL_CHANGE,
        COL_ADVANCE, COL_DECLINE, COL_NET_INFLOW,
    ]
    result = result[[c for c in cols if c in result.columns]]
    result.sort_values([COL_BOARD_CODE, COL_DATE], inplace=True)
    result.reset_index(drop=True, inplace=True)
    return result


def run(start_date: str = "", end_date: str = "", max_boards: int = 0,
        force: bool = False, board_type: str = "industry"):
    """采集板块日线并写库。

    Args:
        start_date/end_date: YYYYMMDD
        max_boards: >0 时只采集前N个板块 (调试用)
        force: True 跳过 DB 完整性预检
        board_type: industry=行业板块(sector_daily) / concept=概念板块(concept_daily)
    """
    CONFIG.log_dir.mkdir(parents=True, exist_ok=True)
    table = "concept_daily" if board_type == "concept" else "sector_daily"

    # 增量优化: 检查DB中是否已有目标日期范围的数据, 有则跳过API调用
    if start_date and end_date and not force:
        from db_handler import has_data_in_range
        if has_data_in_range(table, start_date, end_date):
            logger.info(f"{table} 数据已存在, 跳过API拉取")
            return pd.DataFrame()

    boards = get_ths_boards(board_type)
    kind = "概念" if board_type == "concept" else "行业"
    logger.info(f"共 {len(boards)} 个同花顺{kind}板块")

    df = build_ths_sector_daily(boards, start_date, end_date, max_boards, board_type)
    if df.empty:
        logger.warning("未获取到任何数据")
        return df

    # 仅保留交易日, 跳过周末与中国法定节假日
    from trade_calendar import is_trade_date
    n_before = len(df)
    df = df[df[COL_DATE].map(is_trade_date)].reset_index(drop=True)
    if df.empty:
        logger.warning("过滤后无交易日数据")
        return df
    logger.info(f"交易日过滤: {n_before} -> {len(df)} 条")

    try:
        from db_handler import save_sector_daily_to_db, save_concept_daily_to_db
        if board_type == "concept":
            save_concept_daily_to_db(df)
        else:
            save_sector_daily_to_db(df)
    except Exception as e:
        logger.error(f"MySQL 写入失败: {e}")
    logger.info(f"完成: {len(df)} 条记录, "
                f"{df[COL_BOARD_CODE].nunique()} 个板块, "
                f"日期 {df[COL_DATE].min()} ~ {df[COL_DATE].max()}")

    # 行业板块: K线写库后回填涨跌家数与资金净流入
    if board_type == "industry":
        try:
            refresh_sector_stats(df[COL_DATE].min(), df[COL_DATE].max())
        except Exception as e:
            logger.error(f"板块统计回填失败: {e}")
    return df


def refresh_sector_stats(start_date: str, end_date: str):
    """回填 sector_daily 的涨跌家数与资金净流入 (幂等, 可重复执行)。

    - advance/decline: 由 stock_daily 按 board_code/date 聚合涨跌幅推导
    - net_inflow: 由 stock_moneyflow 关联 stock_daily 板块归属聚合主力净流入
    """
    s = start_date.replace("-", "")
    e = end_date.replace("-", "")
    from db_handler import (get_connection, update_sector_breadth,
                            update_sector_net_inflow, ensure_table)
    ensure_table("stock_moneyflow")  # 首次运行前表可能未建
    conn = get_connection()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                """SELECT board_code, `date`, SUM(pct_chg > 0), SUM(pct_chg < 0)
                   FROM stock_daily
                   WHERE board_code <> '' AND `date` BETWEEN %s AND %s
                     AND pct_chg IS NOT NULL
                   GROUP BY board_code, `date`""",
                (s, e),
            )
            breadth = cursor.fetchall()
            if breadth:
                update_sector_breadth(
                    [(str(r[0]), str(r[1]), int(r[2]), int(r[3])) for r in breadth]
                )

            cursor.execute(
                """SELECT sd.board_code, mf.`date`, SUM(mf.main_net_inflow)
                   FROM stock_moneyflow mf
                   JOIN stock_daily sd
                     ON sd.code = mf.code AND sd.`date` = mf.`date`
                   WHERE sd.board_code <> '' AND mf.`date` BETWEEN %s AND %s
                   GROUP BY sd.board_code, mf.`date`""",
                (s, e),
            )
            inflow = cursor.fetchall()
            if inflow:
                update_sector_net_inflow(
                    [(r[2], str(r[0]), str(r[1])) for r in inflow]
                )
            logger.info(
                f"板块统计回填完成 ({start_date}~{end_date}): "
                f"涨跌家数 {len(breadth)} 行, 资金流入 {len(inflow)} 行"
            )
        finally:
            cursor.close()
    finally:
        conn.close()


if __name__ == "__main__":
    run(start_date='20260101',end_date='20260731')
