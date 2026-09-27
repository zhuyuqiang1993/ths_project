"""量增价涨筛选 (volrise_screen)

新增候选识别逻辑 (独立模块, 不修改 sector_screen / stock_screen / etf_screen
的任何原有筛选行为, 结果写入独立新表, 不触碰原 candidate_sector / candidate_stock):

候选板块 —— 扫描A股所有板块 (行业 sector_daily + 概念 concept_daily):
    最近3个交易日 **每一天都同时满足** (3/3 连续):
        板块当日涨幅 > 0          (pct_chg[d] > 0)
        且成交量天级环比 >= -5%   (volume[d]/volume[d-1]-1 >= -5%)

候选股票 —— 扫描全部个股:
    最近3个交易日 **每一天都同时满足** (3/3 连续):
        该股当日涨幅 > 0          (pct_chg[d] > 0)
        且该股成交量天级环比 >= -5%
        且同日所属行业板块涨幅 > 0

说明
- 涨幅必须每天都为正; 量允许小幅缩量 (环比 >= -5%) 但不得大幅缩量
- 环比基准取该证券自身分组内的上一交易行
- 窗口 = 全局最近3个交易日; 任一天缺数据 (停牌) 或不满足即落选
- hit_days = 窗口内满足天数 (入选者恒为3); 输出行情字段取窗口末交易日当日值
- ret_3d = 窗口末收盘 / 窗口前3交易日收盘 - 1

输出
- MySQL: candidate_sector_volrise / candidate_stock_volrise (upsert)
- 日志: 候选统计 + 板块TOP + 股票TOP
- 邮件: email_service 的 "量增价涨候选" 段落

用法: python volrise_screen.py [identified_at]
"""

import sys
import warnings
from datetime import date

import pandas as pd
from loguru import logger

from config import CONFIG
from db_handler import (
    create_tables,
    get_connection,
    save_candidate_sector_volrise,
    save_candidate_stock_volrise,
)

warnings.filterwarnings("ignore", message=".*only supports SQLAlchemy.*")

logger.remove()
logger.add(
    sys.stderr,
    level=CONFIG.log_level,
    format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}",
)
logger.add(
    CONFIG.log_dir / "volrise_screen_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
)

WINDOW_DAYS = 3       # 命中窗口: 最近3个交易日, 须每天全部满足
VOL_RING_MIN = -5.0   # 量环比下限: 每日成交量环比 >= -5% (允许小幅缩量)
LOAD_DAYS = 25        # 加载缓冲 (自然日, 覆盖窗口+环比基准+3日涨幅回看)
TOP_SECTOR = 15
TOP_STOCK = 20
TYPE_CN = {"industry": "行业", "concept": "概念"}


def _query(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = get_connection()
    try:
        df = pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()
    return df


def _f(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def load_window_dates(anchor: str) -> list:
    """全局最近 WINDOW_DAYS 个交易日 (升序)"""
    df = _query(
        """SELECT DISTINCT `date` FROM stock_daily WHERE `date` <= %s
           ORDER BY `date` DESC LIMIT %s""",
        (anchor, WINDOW_DAYS),
    )
    return sorted(pd.to_datetime(df["date"]).dt.date.tolist())


def load_boards(anchor: str) -> pd.DataFrame:
    """全部板块日线 (行业 + 概念)"""
    sql = """
        SELECT board_code, board_name, 'industry' AS board_type, `date`,
               close, pct_chg, volume
        FROM sector_daily WHERE `date` >= DATE_SUB(%s, INTERVAL %s DAY)
        UNION ALL
        SELECT board_code, board_name, 'concept' AS board_type, `date`,
               close, pct_chg, volume
        FROM concept_daily WHERE `date` >= DATE_SUB(%s, INTERVAL %s DAY)
        ORDER BY board_type, board_code, `date`
    """
    df = _query(sql, (anchor, LOAD_DAYS, anchor, LOAD_DAYS))
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def load_stocks(anchor: str) -> pd.DataFrame:
    sql = """
        SELECT code, name, board_code, board_name, `date`,
               close, pct_chg, volume
        FROM stock_daily
        WHERE `date` >= DATE_SUB(%s, INTERVAL %s DAY)
        ORDER BY code, `date`
    """
    df = _query(sql, (anchor, LOAD_DAYS))
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def load_sector_pcts(anchor: str) -> dict:
    """行业板块 (board_code, date) -> 当日涨幅% (供候选股票的板块条件使用)"""
    df = _query(
        """SELECT board_code, `date`, pct_chg FROM sector_daily
           WHERE `date` >= DATE_SUB(%s, INTERVAL %s DAY)""",
        (anchor, LOAD_DAYS),
    )
    if df.empty:
        return {}
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return {(str(r["board_code"]), r["date"]): _f(r["pct_chg"])
            for _, r in df.iterrows()}


def _window_daily(grp: pd.DataFrame, wset: set, extra_ok=None):
    """窗口内逐日判定 (须每天全部满足才入选)

    每日满足 = 量环比 >= VOL_RING_MIN(-5%) 且 extra_ok(i) 为真
    extra_ok(i) -> bool 附加条件 (涨幅>0等); 缺数据/停牌日直接判 False
    返回 (daily: {date: bool}, idx: {date: 行号})
    """
    n = len(grp)
    vols = grp["volume"].astype(float).tolist()
    idx = {grp["date"].iloc[i]: i for i in range(n)}
    daily = {}
    for d in sorted(wset):
        i = idx.get(d)
        if i is None or i < 1 or not vols[i - 1]:
            daily[d] = False
            continue
        ring = (vols[i] / vols[i - 1] - 1) * 100
        ok = bool(ring >= VOL_RING_MIN)
        if ok and extra_ok is not None:
            ok = bool(extra_ok(i))
        daily[d] = ok
    return daily, idx


def _ret_3d(grp: pd.DataFrame, i: int):
    if i < 3:
        return None
    c0 = _f(grp["close"].iloc[i - 3])
    c1 = _f(grp["close"].iloc[i])
    if not c0 or c1 is None:
        return None
    return round((c1 / c0 - 1) * 100, 4)


def scan_sectors(board_df: pd.DataFrame, window_dates: list) -> pd.DataFrame:
    """候选板块: 窗口3日每天 涨幅>0 且 量环比>=-5% (3/3)"""
    if board_df.empty:
        return pd.DataFrame()
    wset = set(window_dates)
    rows = []
    for (bc, bt), grp in board_df.groupby(["board_code", "board_type"]):
        grp = grp.sort_values("date").reset_index(drop=True)
        if len(grp) < 2:
            continue

        def _board_ok(i):
            pct = _f(grp["pct_chg"].iloc[i])
            return pct is not None and pct > 0

        daily, idx = _window_daily(grp, wset, extra_ok=_board_ok)
        if len(daily) < WINDOW_DAYS or not all(daily.values()):
            continue
        i = idx[max(daily)]
        row = grp.iloc[i]
        vols = grp["volume"].astype(float).tolist()
        rows.append({
            "board_code": str(bc),
            "board_name": str(row["board_name"] or ""),
            "board_type": str(bt),
            "date": row["date"],
            "close": _f(row["close"]),
            "pct_chg": _f(row["pct_chg"]),
            "volume": int(vols[i]) if vols[i] else None,
            "vol_ring_pct": round((vols[i] / vols[i - 1] - 1) * 100, 2),
            "hit_days": sum(1 for v in daily.values() if v),
            "ret_3d": _ret_3d(grp, i),
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return df.sort_values(["ret_3d", "hit_days"], ascending=False).reset_index(drop=True)


def scan_stocks(stock_df: pd.DataFrame, window_dates: list,
                sector_pcts: dict) -> pd.DataFrame:
    """候选股票: 窗口3日每天 个股涨幅>0 且 量环比>=-5% 且 板块涨幅>0 (3/3)"""
    if stock_df.empty:
        return pd.DataFrame()
    wset = set(window_dates)
    rows = []
    for code, grp in stock_df.groupby("code"):
        grp = grp.sort_values("date").reset_index(drop=True)
        if len(grp) < 2:
            continue

        def _day_ok(i):
            pct = _f(grp["pct_chg"].iloc[i])
            if pct is None or pct <= 0:
                return False
            bc = str(grp["board_code"].iloc[i] or "")
            if not bc:
                return False
            sp = sector_pcts.get((bc, grp["date"].iloc[i]))
            return sp is not None and sp > 0

        daily, idx = _window_daily(grp, wset, extra_ok=_day_ok)
        if len(daily) < WINDOW_DAYS or not all(daily.values()):
            continue
        i = idx[max(daily)]
        row = grp.iloc[i]
        vols = grp["volume"].astype(float).tolist()
        bc = str(row["board_code"] or "")
        rows.append({
            "code": str(code),
            "name": str(row["name"] or ""),
            "board_code": bc,
            "board_name": str(row["board_name"] or ""),
            "date": row["date"],
            "close": _f(row["close"]),
            "pct_chg": _f(row["pct_chg"]),
            "volume": int(vols[i]) if vols[i] else None,
            "vol_ring_pct": round((vols[i] / vols[i - 1] - 1) * 100, 2),
            "sector_pct_chg": sector_pcts.get((bc, row["date"])),
            "hit_days": sum(1 for v in daily.values() if v),
            "ret_3d": _ret_3d(grp, i),
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return df.sort_values(["vol_ring_pct", "hit_days"], ascending=False).reset_index(drop=True)


def _output(sdf: pd.DataFrame, tdf: pd.DataFrame, anchor: str,
            identified_at: str, window_dates: list):
    n_ind = int((sdf["board_type"] == "industry").sum()) if not sdf.empty else 0
    n_con = int((sdf["board_type"] == "concept").sum()) if not sdf.empty else 0
    wtxt = "~".join(str(d) for d in window_dates)
    logger.info(f"量增价涨筛选完成: 窗口 {wtxt} | 板块 {len(sdf)} 个 "
                f"(行业 {n_ind}, 概念 {n_con}) | 股票 {len(tdf)} 只 "
                f"| 识别日 {identified_at} 锚点 {anchor}")

    if not sdf.empty:
        top = sdf.sort_values(["ret_3d", "hit_days"], ascending=False).head(TOP_SECTOR)
        logger.info(f"=== 候选板块 TOP{TOP_SECTOR} (按近3日涨幅) ===")
        for _, r in top.iterrows():
            logger.info(
                f"  {r['board_code']} {r['board_name']}"
                f"[{TYPE_CN.get(r['board_type'], r['board_type'])}] "
                f"连续{r['hit_days']}/{WINDOW_DAYS}日量价齐升 @{r['date']} "
                f"涨幅={r['pct_chg']}% 量环比={r['vol_ring_pct']}% "
                f"3日涨幅={r['ret_3d']}%"
            )

    if not tdf.empty:
        top = tdf.sort_values("vol_ring_pct", ascending=False).head(TOP_STOCK)
        logger.info(f"=== 候选股票 TOP{TOP_STOCK} (按量环比) ===")
        for _, r in top.iterrows():
            logger.info(
                f"  {r['code']} {r['name']} [{r['board_name']}] "
                f"连续{r['hit_days']}/{WINDOW_DAYS}日量价齐升 @{r['date']} "
                f"涨幅={r['pct_chg']}% 量环比={r['vol_ring_pct']}% "
                f"板块涨幅={r['sector_pct_chg']}% 3日涨幅={r['ret_3d']}%"
            )


def _purge_identified(identified_at: str):
    """清空本次识别日的旧结果, 保证同日重跑幂等 (仅操作新表)"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM candidate_sector_volrise WHERE identified_at=%s",
            (identified_at,),
        )
        cursor.execute(
            "DELETE FROM candidate_stock_volrise WHERE identified_at=%s",
            (identified_at,),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def run(identified_at: str = ""):
    """量增价涨筛选入口: 扫描 -> 入库(独立新表) -> 日志输出"""
    create_tables()
    identified_at = identified_at or date.today().isoformat()
    logger.info(f"===== 量增价涨筛选开始 (识别日期: {identified_at}) =====")

    df_anchor = _query("SELECT MAX(`date`) AS d FROM stock_daily")
    if df_anchor.empty or df_anchor["d"].isna().all():
        logger.warning("stock_daily 无数据, 量增价涨筛选跳过")
        return pd.DataFrame(), pd.DataFrame()
    anchor = str(df_anchor["d"].iloc[0])

    window_dates = load_window_dates(anchor)
    if not window_dates:
        logger.warning("无可用交易日窗口, 量增价涨筛选跳过")
        return pd.DataFrame(), pd.DataFrame()

    _purge_identified(identified_at)

    logger.info("扫描全部板块 (行业+概念)...")
    sdf = scan_sectors(load_boards(anchor), window_dates)
    if not sdf.empty:
        sdf["identified_at"] = identified_at
        save_candidate_sector_volrise(sdf)
    logger.info(f"候选板块 {len(sdf)} 个, 已保存到 candidate_sector_volrise")

    logger.info("扫描全部个股 (每日: 个股涨幅>0 且 量环比>=-5% 且 板块涨幅>0)...")
    tdf = scan_stocks(load_stocks(anchor), window_dates,
                      load_sector_pcts(anchor))
    if not tdf.empty:
        tdf["identified_at"] = identified_at
        save_candidate_stock_volrise(tdf)
    logger.info(f"候选股票 {len(tdf)} 只, 已保存到 candidate_stock_volrise")

    _output(sdf, tdf, anchor, identified_at, window_dates)
    return sdf, tdf


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "")
