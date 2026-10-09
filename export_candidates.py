"""导出候选代码到桌面 txt (英文逗号分隔, 仅代码)

由 daily_task 的定时任务在发送邮件后调用, 桌面同名文件会被覆盖。

- 候选股票.txt: candidate_stock (排除 3倍量法 tag=vol_3x) + candidate_stock_volrise (量增价涨)
- 候选板块.txt: candidate_sector (多因子) + candidate_sector_volrise (量增价涨)
- 候选etf.txt : candidate_etf

用法: python export_candidates.py [identified_at]   # 默认取各表最新识别日
"""
import sys
from pathlib import Path

from loguru import logger

from db_handler import get_connection

DESKTOP = Path.home() / "Desktop"


def _latest(table: str, identified_at: str = "") -> str:
    if identified_at:
        return identified_at
    df = _query(f"SELECT MAX(identified_at) AS d FROM {table}")
    if df.empty or df["d"].isna().all():
        return ""
    return str(df["d"].iloc[0])


def _query(sql: str, params: tuple = ()):
    import pandas as pd
    conn = get_connection()
    try:
        return pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()


def _pick(table: str, col: str, identified_at: str, where: str = "") -> tuple:
    """返回 (识别日, 去重后的代码列表)。

    指定 identified_at 但该日无候选时, 回退到该表最新识别日 (并告警),
    避免定时任务因筛选失败而把桌面已有文件覆盖成空文件。
    """
    latest = _latest(table, identified_at)
    if not latest:
        logger.warning(f"{table} 无数据")
        return "", []

    def _fetch(day: str):
        sql = f"SELECT DISTINCT {col} AS code FROM {table} WHERE identified_at = %s"
        if where:
            sql += f" AND {where}"
        sql += f" ORDER BY {col}"
        df = _query(sql, (day,))
        return [str(c) for c in df["code"].tolist()]

    codes = _fetch(latest)
    if not codes and identified_at:
        fallback = _latest(table, "")
        if fallback and fallback != latest:
            codes = _fetch(fallback)
            if codes:
                logger.warning(f"{table} {latest} 无候选, 回退到最新识别日 {fallback} ({len(codes)} 个)")
                latest = fallback
    logger.info(f"{table} @ {latest}: {len(codes)} 个代码")
    return latest, codes


def _write(name: str, codes: list) -> Path:
    if not codes:
        logger.warning(f"{name}: 无候选代码, 保留桌面原有文件不覆盖")
        return None
    path = DESKTOP / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(",".join(codes), encoding="utf-8")
    logger.info(f"{name}: {len(codes)} 个代码 -> {path}")
    return path


def run(identified_at: str = ""):
    # ---- 候选股票 (排除 3倍量法) ----
    l1, multi = _pick("candidate_stock", "code", identified_at, "tag <> 'vol_3x'")
    l2, volrise = _pick("candidate_stock_volrise", "code", identified_at)
    stocks = list(dict.fromkeys(multi + volrise))          # 去重并保持来源顺序
    _write("候选股票.txt", stocks)

    # ---- 候选板块 ----
    l3, sec = _pick("candidate_sector", "board_code", identified_at)
    l4, sec_v = _pick("candidate_sector_volrise", "board_code", identified_at)
    boards = list(dict.fromkeys(sec + sec_v))
    _write("候选板块.txt", boards)

    # ---- 候选 ETF ----
    l5, etfs = _pick("candidate_etf", "code", identified_at)
    _write("候选etf.txt", etfs)

    logger.info(f"识别日: 个股(多因子)={l1 or '-'} 个股(量增价涨)={l2 or '-'} "
                f"板块(多因子)={l3 or '-'} 板块(量增价涨)={l4 or '-'} ETF={l5 or '-'}")
    return stocks, boards, etfs


if __name__ == "__main__":
    # 独立运行时才配置日志; 被 daily_task 导入时沿用其 sink, 避免重复输出
    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}")
    run(sys.argv[1] if len(sys.argv) > 1 else "")
