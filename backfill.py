"""一次性数据回补编排器。

用法:
    python backfill.py                     # 全阶段 (指数/个股/板块/ETF/资金流/概念/估值)
    python backfill.py --phases stock,purge
    python backfill.py --phases moneyflow --start 20250926 --end 20260924

阶段:
    index      指数日线 (东财, 失败切腾讯)
    stock      个股日线 250+交易日回补 + 停更区间补拉 + 历史NULL修复 (同花顺K线)
    purge      清理 stock_daily 中修复后残留的 close 为 NULL/0 的垃圾行 (真停牌)
    repair     定点修复 max(date) < end 的个股/ETF (hexin-v 偶发失效导致的批失败)
    sector     行业板块日线回补 + 涨跌家数回填 (资金流入视 moneyflow 结果)
    etf        ETF 日线回补
    moneyflow  个股资金流历史 (东财, 需东财可用; 完成后自动回填板块净流入)
    concept    概念板块日线回补 (同花顺375个概念)
    valuation  个股估值快照 PE/PB/市值/换手 写入 stock_list (东财)
    stats      打印各表填充率核验
"""
import argparse
import sys
import time
from datetime import datetime

from loguru import logger

from config import CONFIG

logger.remove()
logger.add(
    sys.stderr,
    level=CONFIG.log_level,
    format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}",
)
logger.add(
    CONFIG.log_dir / "backfill_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="60 days",
    level="DEBUG",
)

ALL_PHASES = ["index", "stock", "purge", "repair", "sector", "etf",
              "moneyflow", "concept", "valuation", "stats"]


def phase_index(start: str, end: str):
    import index_daily
    index_daily.run(start_date=start, end_date=end, force=True)


def phase_stock(start: str, end: str):
    import stock_daily
    s = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    e = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    stock_daily.run(start_date=s, end_date=e, force=True)


def phase_purge(start: str, end: str):
    from db_handler import get_connection
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT COUNT(*) FROM stock_daily "
            "WHERE `date` BETWEEN %s AND %s AND (close IS NULL OR close = 0)",
            (start, end),
        )
        n = cur.fetchone()[0]
        cur.execute(
            "DELETE FROM stock_daily "
            "WHERE `date` BETWEEN %s AND %s AND (close IS NULL OR close = 0)",
            (start, end),
        )
        conn.commit()
        logger.info(f"purge: 清理 stock_daily 垃圾行 {n} 条 (close NULL/0)")
    finally:
        cur.close()
        conn.close()


def phase_sector(start: str, end: str):
    import sector_service
    sector_service.run(start_date=start, end_date=end, force=True)


def phase_etf(start: str, end: str):
    import etf_service
    etf_service.run(start_date=start, end_date=end, force=True)


def _em_alive() -> bool:
    from eastmoney_client import em_get
    j = em_get(
        "https://push2.eastmoney.com/api/qt/clist/get",
        {"po": "1", "pz": "1", "pn": "1", "np": "1", "fltt": "2", "invt": "2",
         "fid": "f12", "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23", "fields": "f12"},
        tries=2,
    )
    return j is not None


def phase_repair(start: str, end: str):
    """定点修复: 重新抓取 max(date) < end 的代码 (hexin-v 失效导致的批失败)。"""
    from db_handler import get_connection
    conn = get_connection()
    cur = conn.cursor()
    try:
        stuck = {}
        for table in ("stock_daily", "etf_daily"):
            cur.execute(
                f"SELECT code FROM (SELECT code, MAX(`date`) m FROM {table} "
                f"GROUP BY code) t WHERE m < %s", (end,))
            stuck[table] = [str(r[0]) for r in cur.fetchall()]
            logger.info(f"repair: {table} 有 {len(stuck[table])} 只代码末端 < {end}")
    finally:
        cur.close()
        conn.close()

    if stuck["stock_daily"]:
        import stock_daily
        s = f"{start[:4]}-{start[4:6]}-{start[6:]}"
        e = f"{end[:4]}-{end[4:6]}-{end[6:]}"
        stock_daily.run(start_date=s, end_date=e, force=True,
                        only_codes=stuck["stock_daily"])
    if stuck["etf_daily"]:
        import etf_service
        etf_service.run(start_date=start, end_date=end, force=True,
                        only_codes=stuck["etf_daily"])
    phase_purge(start, end)
    # 修复后重刷板块统计 (涨跌家数)
    try:
        from sector_service import refresh_sector_stats
        s = f"{start[:4]}-{start[4:6]}-{start[6:]}"
        e = f"{end[:4]}-{end[4:6]}-{end[6:]}"
        refresh_sector_stats(s, e)
    except Exception as ex:
        logger.error(f"板块统计刷新失败: {ex}")


def _phase_moneyflow_sina(start: str, end: str):
    """新浪资金流历史兜底 (东财被封时): 分页抓取并回填板块净流入。"""
    import pandas as pd
    from db_handler import (get_connection, ensure_table,
                            save_stock_moneyflow_to_db)
    from eastmoney_client import parallel_fetch
    from sina_client import fetch_moneyflow_history

    ensure_table("stock_moneyflow")
    s = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    e = f"{end[:4]}-{end[4:6]}-{end[6:]}"

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT code, COUNT(*) FROM stock_daily WHERE `date` BETWEEN %s AND %s "
        "GROUP BY code", (s, e))
    expected = {str(c): int(n) for c, n in cur.fetchall()}
    cur.close()
    conn.close()

    codes = sorted(expected)
    logger.info(f"新浪资金流历史回补: {len(codes)} 只, 区间 {s} ~ {e}")

    buf = []
    n_rows = 0
    n_ok = 0

    def _flush():
        nonlocal buf, n_rows
        if not buf:
            return
        df = pd.concat(buf, ignore_index=True)
        df = df[(df["date"] >= s) & (df["date"] <= e)]
        if not df.empty:
            save_stock_moneyflow_to_db(df)
            n_rows += len(df)
        buf = []

    results = parallel_fetch(
        lambda c: fetch_moneyflow_history(c, s, e,
                                          expected=expected.get(c, 0)),
        codes, workers=4, desc="sina-moneyflow",
    )
    for code, df in results:
        if df is not None and not df.empty:
            n_ok += 1
            buf.append(df)
        if len(buf) >= 800:
            _flush()
            logger.info(f"  资金流累计 {n_rows} 行")
    _flush()
    logger.info(f"新浪资金流完成: {n_rows} 行, {n_ok}/{len(codes)} 只")

    logger.info("回填板块净流入 (sector_daily)...")
    from sector_service import refresh_sector_stats
    refresh_sector_stats(s, e)


def phase_moneyflow(start: str, end: str):
    """个股资金流历史 (东财 fflow/kline, 单请求覆盖全部日期) -> stock_moneyflow。"""
    from db_handler import has_data_in_range
    if has_data_in_range("stock_moneyflow", start, end):
        logger.info("stock_moneyflow 区间数据已存在, 跳过")
        return

    if not _em_alive():
        logger.warning("东财接口不可用 (限流中), 使用新浪资金流兜底")
        return _phase_moneyflow_sina(start, end)

    import pandas as pd
    from eastmoney_client import fetch_stock_moneyflow_history, parallel_fetch
    from db_handler import save_stock_moneyflow_to_db, ensure_table
    ensure_table("stock_moneyflow")

    s = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    e = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    lmt = 300

    codes = _load_all_codes()
    logger.info(f"资金流历史回补: {len(codes)} 只, 区间 {s} ~ {e}, lmt={lmt}")

    buf = []
    n_rows = 0

    def _flush():
        nonlocal buf, n_rows
        if not buf:
            return
        df = pd.concat(buf, ignore_index=True)
        df = df[(df["date"] >= s) & (df["date"] <= e)]
        if not df.empty:
            save_stock_moneyflow_to_db(df)
            n_rows += len(df)
        buf = []

    results = parallel_fetch(
        lambda c: fetch_stock_moneyflow_history(c, lmt=lmt),
        codes, workers=4, desc="moneyflow",
    )
    for code, df in results:
        if df is not None and not df.empty:
            buf.append(df)
        if len(buf) >= 800:
            _flush()
            logger.info(f"  资金流累计 {n_rows} 行")
    _flush()
    logger.info(f"资金流历史完成: {n_rows} 行")

    logger.info("回填板块净流入 (sector_daily)...")
    from sector_service import refresh_sector_stats
    refresh_sector_stats(s, e)


def _load_all_codes() -> list:
    from db_handler import get_connection
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT DISTINCT code FROM stock_daily ORDER BY code")
        codes = [str(r[0]) for r in cur.fetchall()]
        return codes
    finally:
        cur.close()
        conn.close()


def phase_concept(start: str, end: str):
    import sector_service
    sector_service.run(start_date=start, end_date=end, force=True,
                       board_type="concept")


def phase_valuation(start: str, end: str):
    from eastmoney_client import fetch_stock_valuation
    from db_handler import save_stock_list_to_db
    df = fetch_stock_valuation()
    if df is None or df.empty:
        logger.warning("估值快照无数据 (东财不可用?)")
        return
    save_stock_list_to_db(df)


def phase_stats(start: str, end: str):
    from db_handler import get_connection
    conn = get_connection()
    cur = conn.cursor()
    try:
        logger.info("===== 数据质量核验 =====")
        for table in ("stock_daily", "sector_daily", "etf_daily",
                      "index_daily", "stock_moneyflow", "concept_daily",
                      "stock_list"):
            try:
                if table == "stock_list":
                    cur.execute("SELECT COUNT(*), SUM(latest_price IS NULL), "
                                "SUM(pe_dynamic IS NULL) FROM stock_list")
                    total, null_price, null_pe = cur.fetchone()
                    logger.info(f"[{table}] {total} 行, latest_price NULL {null_price}, "
                                f"pe NULL {null_pe}")
                    continue
                cur.execute(
                    f"SELECT MIN(`date`), MAX(`date`), COUNT(DISTINCT `date`), COUNT(*) "
                    f"FROM {table} WHERE `date` BETWEEN %s AND %s", (start, end))
                dmin, dmax, n_dates, n_rows = cur.fetchone()
                logger.info(f"[{table}] {dmin} ~ {dmax}, {n_dates} 个交易日, {n_rows} 行")
            except Exception as e:
                logger.info(f"[{table}] {e}")

        cur.execute(
            "SELECT COUNT(*), SUM(close IS NULL), SUM(volume IS NULL), "
            "SUM(board_code = '') FROM stock_daily WHERE `date` BETWEEN %s AND %s",
            (start, end))
        total, null_close, null_vol, empty_board = cur.fetchone()
        logger.info(f"[stock_daily 质量] 总 {total}, close NULL {null_close}, "
                    f"volume NULL {null_vol}, board_code 空 {empty_board}")

        cur.execute(
            "SELECT COUNT(*), SUM(advance IS NULL), SUM(net_inflow IS NULL) "
            "FROM sector_daily WHERE `date` BETWEEN %s AND %s", (start, end))
        total, null_adv, null_in = cur.fetchone()
        logger.info(f"[sector_daily 质量] 总 {total}, advance NULL {null_adv}, "
                    f"net_inflow NULL {null_in}")

        cur.execute(
            "SELECT COUNT(DISTINCT code), COUNT(*), SUM(close IS NULL) "
            "FROM stock_daily WHERE `date` BETWEEN %s AND %s", (start, end))
        n_codes, n_rows, null_close = cur.fetchone()
        # 250+ 交易日核验: 有多少只股票覆盖 >= 250 个交易日
        cur.execute(
            "SELECT COUNT(*) FROM (SELECT code FROM stock_daily "
            "WHERE `date` BETWEEN %s AND %s GROUP BY code "
            "HAVING COUNT(*) >= 250) t", (start, end))
        n_250 = cur.fetchone()[0]
        logger.info(f"[P2-2 核验] 覆盖股票 {n_codes} 只, 其中 >=250 交易日: {n_250} 只")
    finally:
        cur.close()
        conn.close()


PHASE_FN = {
    "index": phase_index,
    "stock": phase_stock,
    "purge": phase_purge,
    "repair": phase_repair,
    "sector": phase_sector,
    "etf": phase_etf,
    "moneyflow": phase_moneyflow,
    "concept": phase_concept,
    "valuation": phase_valuation,
    "stats": phase_stats,
}


def main():
    parser = argparse.ArgumentParser(description="数据回补编排器")
    parser.add_argument("--phases", default=",".join(ALL_PHASES),
                        help="逗号分隔的阶段列表")
    parser.add_argument("--start", default="20250926")
    parser.add_argument("--end", default="20260924")
    args = parser.parse_args()

    phases = [p.strip() for p in args.phases.split(",") if p.strip()]
    unknown = [p for p in phases if p not in PHASE_FN]
    if unknown:
        print(f"未知阶段: {unknown}, 可选: {ALL_PHASES}")
        sys.exit(1)

    CONFIG.log_dir.mkdir(parents=True, exist_ok=True)
    for p in phases:
        t0 = time.time()
        logger.info(f"===== 阶段 [{p}] 开始 ({args.start} ~ {args.end}) =====")
        try:
            PHASE_FN[p](args.start, args.end)
            logger.info(f"===== 阶段 [{p}] 完成, 耗时 {round(time.time()-t0, 1)}s =====")
        except Exception as e:
            logger.exception(f"===== 阶段 [{p}] 失败: {e} =====")


if __name__ == "__main__":
    main()
