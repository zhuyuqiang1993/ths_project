import sys
import warnings
from datetime import date, datetime

import pandas as pd
from loguru import logger

from config import CONFIG
from db_handler import (
    get_connection,
    create_tables,
    save_candidate_stock_to_db,
)

warnings.filterwarnings("ignore", message=".*only supports SQLAlchemy.*")

logger.remove()
logger.add(
    sys.stderr,
    level=CONFIG.log_level,
    format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}",
)
logger.add(
    CONFIG.log_dir / "stock_screen_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
)

SCREEN_WINDOW = 5
PASS_DAYS = 3
UP_AMPLITUDE_THRESHOLD = 5.0


def _query(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = get_connection()
    try:
        df = pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()
    return df


def get_candidate_sectors() -> pd.DataFrame:
    """取最新一次识别出的候选板块"""
    df = _query(
        """SELECT board_code, board_name FROM candidate_sector
           WHERE identified_at = (SELECT MAX(identified_at) FROM candidate_sector)"""
    )
    return df


def get_non_candidate_sectors() -> pd.DataFrame:
    """取非候选板块 (全部板块 - 最新候选板块)"""
    df = _query(
        """SELECT DISTINCT sd.board_code, sd.board_name
           FROM sector_daily sd
           WHERE sd.board_code NOT IN (
               SELECT board_code FROM candidate_sector
               WHERE identified_at = (SELECT MAX(identified_at) FROM candidate_sector)
           )"""
    )
    return df


def get_sector_last_n_days(board_code: str, n: int = SCREEN_WINDOW) -> pd.DataFrame:
    return _query(
        """SELECT date, pct_chg FROM sector_daily
           WHERE board_code = %s ORDER BY date DESC LIMIT %s""",
        (board_code, n),
    )


def get_board_stocks(board_code: str, dates: list) -> pd.DataFrame:
    placeholders = ",".join(["%s"] * len(dates))
    return _query(
        f"""SELECT code, name, date, pct_chg, prev_close, volume, macd
            FROM stock_daily
            WHERE board_code = %s AND date IN ({placeholders})
            ORDER BY code, date""",
        (board_code, *dates),
    )


def _pass_vol_price(closes: list, vols: list) -> bool:
    """量价配合筛选 (5日窗口):
    - 上涨日 (close[i] > close[i-1]):
        放量 (volume[i] >= volume[i-1]) 通过;
        缩量上涨 (volume[i] < volume[i-1]) 允许, 但当日涨幅必须 > 5% (例外不限天数)
    - 下跌日 (close[i] < close[i-1]): 必须缩量 (volume[i] <= volume[i-1]),
      判定周期内所有下跌日均需满足
    - 平盘日不参与判断; 缺量的日子跳过
    """
    for i in range(1, len(closes)):
        if vols[i] is None or vols[i - 1] is None or pd.isna(vols[i]) or pd.isna(vols[i - 1]):
            continue
        if closes[i] > closes[i - 1]:
            if vols[i] < vols[i - 1] and (closes[i] / closes[i - 1] - 1) * 100 <= UP_AMPLITUDE_THRESHOLD:
                return False
        elif closes[i] < closes[i - 1]:
            if vols[i] > vols[i - 1]:
                return False
    return True


def _count_relative_strength_days(stock_pcts: list, sector_pcts: list) -> int:
    """统计近5日个股强于板块的天数, 每满足一日计1:
    - 板块涨(sp>0): 个股也涨, 且涨幅 > 板块涨幅 (stp > sp)
    - 板块跌(sp<0): 个股也跌, 且跌幅 < 板块跌幅 (sp < stp < 0)
    - 板块平盘(sp==0): 该日不计
    """
    ok = 0
    for sp, stp in zip(sector_pcts, stock_pcts):
        if sp is None or stp is None or (isinstance(stp, float) and pd.isna(stp)):
            continue
        if sp > 0:
            if stp > sp:
                ok += 1
        elif sp < 0:
            if stp < 0 and stp > sp:
                ok += 1
    return ok


def _screen_one_board(board_code: str, board_name: str, dates: list,
                      sector_pcts: list, identified_at: str,
                      with_macd: bool, check_vol: bool) -> list:
    """筛选单个板块下满足条件的个股。
    - with_macd: 是否要求 MACD>0 (strict/strong 要求, vol_price 不要求)
    - check_vol: 是否要求量价配合 (strict/vol_price 要求, strong 不要求)
    返回候选 dict 列表 (含 tag)
    """
    stocks = get_board_stocks(board_code, dates)
    if stocks.empty:
        return []

    out = []
    for code, grp in stocks.groupby("code"):
        grp = grp.set_index("date").reindex(dates).reset_index()
        if grp["pct_chg"].isna().any() or grp["prev_close"].isna().any():
            continue
        stock_pcts = grp["pct_chg"].astype(float).tolist()

        # close = prev_close * (1 + pct_chg/100)
        closes = [
            float(prev) * (1 + float(pct) / 100)
            for prev, pct in zip(grp["prev_close"].astype(float), grp["pct_chg"].astype(float))
        ]
        vols = grp["volume"].tolist()

        last = grp.iloc[-1]
        macd = last["macd"]
        if with_macd and (macd is None or (isinstance(macd, float) and pd.isna(macd)) or macd <= 0):
            continue

        ok_days = _count_relative_strength_days(stock_pcts, sector_pcts)
        avg_rel = round(sum(s - b for s, b in zip(stock_pcts, sector_pcts)) / SCREEN_WINDOW, 4)

        tags = set()
        if with_macd:
            # strict: 最近交易日强于板块 + 多数日强于板块(>=3/5) + 量价(>=3/5) + MACD>0
            # strong: 多数日强于板块(>=3/5) + MACD>0
            if stock_pcts[-1] > sector_pcts[-1] and ok_days >= PASS_DAYS \
                    and (not check_vol or _pass_vol_price(closes, vols)):
                tags.add("strict")
            if ok_days >= PASS_DAYS:
                tags.add("strong")
        else:
            # vol_price: 最近交易日强于板块 + 多数日强于板块(>=3/5) + 量价(>=3/5), 不要求MACD
            if stock_pcts[-1] > sector_pcts[-1] and ok_days >= PASS_DAYS \
                    and (not check_vol or _pass_vol_price(closes, vols)):
                tags.add("vol_price")

        if not tags:
            continue

        close = round(closes[-1], 2)
        chg_5d = round((closes[-1] / closes[0] - 1) * 100, 4) if closes[0] else 0

        for tag in sorted(tags):
            out.append({
                "code": code,
                "name": last["name"],
                "board_code": board_code,
                "board_name": board_name,
                "date": dates[-1],
                "identified_at": identified_at,
                "close": close,
                "pct_chg": last["pct_chg"],
                "macd": macd,
                "chg_5d": chg_5d,
                "avg_rel": avg_rel,
                "tag": tag,
            })
    return out


def screen_candidate_stocks(identified_date: str = "") -> pd.DataFrame:
    """候选板块: 严格筛选(strict) + 强于板块(strong)"""
    identified_at = identified_date or date.today().isoformat()
    sectors = get_candidate_sectors()
    if sectors.empty:
        logger.warning("候选板块为空，请先运行 sector_screen.py")
        return pd.DataFrame()

    all_candidates = []
    for _, sector in sectors.iterrows():
        board_code = sector["board_code"]
        board_name = sector["board_name"]

        sector_df = get_sector_last_n_days(board_code)
        if len(sector_df) < SCREEN_WINDOW:
            logger.debug(f"  {board_code} 板块数据不足5日, 跳过")
            continue
        sector_df = sector_df.sort_values("date").reset_index(drop=True)
        dates = sector_df["date"].tolist()
        sector_pcts = sector_df["pct_chg"].astype(float).tolist()

        res = _screen_one_board(board_code, board_name, dates, sector_pcts,
                                identified_at, with_macd=True, check_vol=True)
        all_candidates.extend(res)
        strict_cnt = sum(1 for r in res if r["tag"] == "strict")
        strong_cnt = sum(1 for r in res if r["tag"] == "strong")
        logger.info(f"  [{board_code}] {board_name}: 严格筛选 {strict_cnt} 只, 强于板块 {strong_cnt} 只")

    if not all_candidates:
        logger.warning("无个股通过筛选")
        return pd.DataFrame()
    return pd.DataFrame(all_candidates)


def screen_vol_price_stocks(identified_date: str = "") -> pd.DataFrame:
    """非候选板块: 量价股票(vol_price), 严格条件但不过滤 MACD"""
    identified_at = identified_date or date.today().isoformat()
    sectors = get_non_candidate_sectors()
    if sectors.empty:
        logger.warning("无非候选板块")
        return pd.DataFrame()

    all_candidates = []
    for _, sector in sectors.iterrows():
        board_code = sector["board_code"]
        board_name = sector["board_name"]

        sector_df = get_sector_last_n_days(board_code)
        if len(sector_df) < SCREEN_WINDOW:
            continue
        sector_df = sector_df.sort_values("date").reset_index(drop=True)
        dates = sector_df["date"].tolist()
        sector_pcts = sector_df["pct_chg"].astype(float).tolist()

        res = _screen_one_board(board_code, board_name, dates, sector_pcts,
                                identified_at, with_macd=False, check_vol=True)
        all_candidates.extend(res)

    if not all_candidates:
        logger.warning("非候选板块中无量价股票通过筛选")
        return pd.DataFrame()
    return pd.DataFrame(all_candidates)


def run(identified_date: str = ""):
    create_tables()
    identified_at = identified_date or date.today().isoformat()
    logger.info(f"===== 候选股票筛选开始 (识别日期: {identified_at}) =====")

    df_strict = screen_candidate_stocks(identified_at)
    df_vol = screen_vol_price_stocks(identified_at)

    frames = [df for df in (df_strict, df_vol) if not df.empty]
    if not frames:
        logger.warning("===== 候选股票筛选结束: 无候选 =====")
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    save_candidate_stock_to_db(df, replace_date=True)
    logger.info(f"候选股票 {len(df)} 条, 已保存到 candidate_stock "
                f"({'; '.join(f'{t}={int((df['tag']==t).sum())}' for t in sorted(df['tag'].unique()))})")
    top = df.sort_values("avg_rel", ascending=False).head(20)
    for _, r in top.iterrows():
        logger.info(f"  [{r['tag']}] {r['code']} {r['name']} [{r['board_name']}] "
                    f"涨幅={r['pct_chg']}% 5日={r['chg_5d']}% macd={r['macd']}")
    return df


if __name__ == "__main__":
    run()
