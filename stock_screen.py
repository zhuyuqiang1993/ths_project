"""股票筛选模块 (v2)

多因子评分模型:
- 趋势 (25%): 价格 > MA20, MA20 > MA60 (多头排列)
- 动量 (30%): RPS 20日/60日 (全市场相对强度百分位)
- 量价 (20%): 上涨日放量, 下跌日缩量 (5日窗口)
- MACD (15%): 金叉状态, 柱状图为正且增强
- 板块 (10%): 所在板块评分 >= 3

候选条件: 综合得分前 20 名 (满分 5)
"""

import sys
import warnings
from datetime import date, timedelta

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

DATA_WINDOW = 60
MA_SHORT = 20
MA_LONG = 60
RPS_WINDOWS = [20, 60]
MIN_SCORE = 3.0
MIN_SCORE_FLOOR = 1.5   # 动态阈值下限
SCORE_STEP = 0.25       # 动态阈值步长
TARGET_COUNT = 20       # 目标候选数量上限
TARGET_MIN = 15         # 目标候选数量下限 (低于此降低阈值)

_WEIGHTS = {
    "trend": 0.25,
    "momentum": 0.30,
    "volume": 0.20,
    "macd": 0.15,
    "sector": 0.10,
}


def _query(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = get_connection()
    try:
        df = pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()
    return df


def load_all_stock_data(days: int = DATA_WINDOW) -> pd.DataFrame:
    """加载所有股票最近 N 天数据 (含 MACD)"""
    sql = """
        SELECT code, name, board_code, board_name, date, close, volume,
               open, pct_chg, macd, macd_signal, macd_hist
        FROM stock_daily
        WHERE date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
        ORDER BY code, date
    """
    return _query(sql, (days + 45,))


def calc_global_rps(all_data: pd.DataFrame, windows: list = RPS_WINDOWS) -> pd.DataFrame:
    """计算全市场 RPS (每股每日期的相对强度百分位)

    返回 DataFrame: columns=[date, code, rps_20, rps_60]
    """
    close_pivot = all_data.pivot_table(index="date", columns="code", values="close")
    close_pivot = close_pivot.sort_index()

    frames = []
    for w in windows:
        if len(close_pivot) < w + 1:
            rps = pd.DataFrame(50.0, index=close_pivot.index, columns=close_pivot.columns)
        else:
            returns = close_pivot.pct_change(w)
            rps = returns.rank(axis=1, pct=True) * 100
        frame = rps.reset_index().melt(
            id_vars="date", var_name="code", value_name=f"rps_{w}"
        )
        frames.append(frame)

    out = frames[0]
    for f in frames[1:]:
        out = out.merge(f, on=["date", "code"])
    return out


def _score_one_stock(grp: pd.DataFrame, rps_row: pd.Series,
                      sector_score: float) -> dict:
    """对单个股票进行多因子打分"""
    grp = grp.sort_values("date").reset_index(drop=True)
    n = len(grp)

    # === 趋势分 (0-1) ===
    trend_score = 0.0
    if n >= MA_LONG:
        ma20 = grp["close"].rolling(MA_SHORT).mean().iloc[-1]
        ma60 = grp["close"].rolling(MA_LONG).mean().iloc[-1]
        price = grp["close"].iloc[-1]
        if price > ma20:
            trend_score += 0.5
        if ma20 > ma60:
            trend_score += 0.5

    # === 动量分: RPS (0-1) ===
    momentum_score = 0.0
    if "rps_20" in rps_row and pd.notna(rps_row["rps_20"]):
        rps20 = rps_row["rps_20"]
        if rps20 >= 80:
            momentum_score += 0.5
        elif rps20 >= 70:
            momentum_score += 0.3
    if "rps_60" in rps_row and pd.notna(rps_row["rps_60"]):
        rps60 = rps_row["rps_60"]
        if rps60 >= 70:
            momentum_score += 0.5
        elif rps60 >= 60:
            momentum_score += 0.3

    # === 量价分 (0-1) ===
    volume_score = 0.0
    if n >= 6:
        closes = grp["close"].tolist()
        vols = grp["volume"].tolist()
        vol_price_ok = 0
        vol_total = 0
        for i in range(max(1, n - 5), n):
            if vols[i] is None or vols[i - 1] is None or pd.isna(vols[i]) or pd.isna(vols[i - 1]):
                continue
            vol_total += 1
            if closes[i] > closes[i - 1] and vols[i] >= vols[i - 1]:
                vol_price_ok += 1
            elif closes[i] < closes[i - 1] and vols[i] <= vols[i - 1]:
                vol_price_ok += 1
        if vol_total > 0 and vol_price_ok / vol_total >= 0.6:
            volume_score = 1.0
        elif vol_total > 0 and vol_price_ok / vol_total >= 0.4:
            volume_score = 0.5

    # === MACD 分 (0-1), 直接用收盘价重算 (DB中signal/hist字段残缺) ===
    macd_score = 0.0
    if n >= 26:
        close_s = grp["close"].astype(float)
        e12 = close_s.ewm(span=12, adjust=False).mean()
        e26 = close_s.ewm(span=26, adjust=False).mean()
        dif = e12 - e26
        dea = dif.ewm(span=9, adjust=False).mean()
        hist = dif - dea

        macd_val = dif.iloc[-1]
        signal_val = dea.iloc[-1]
        hist_val = hist.iloc[-1]
        hist_prev = hist.iloc[-2]

        if macd_val > signal_val:
            macd_score += 0.3
        if hist_val > 0:
            macd_score += 0.35
        if hist_val > hist_prev:
            macd_score += 0.35

    # === 板块分 (0-1) ===
    sector_score_norm = min(sector_score / 5.0, 1.0) if sector_score else 0

    total = 5 * (
        trend_score * _WEIGHTS["trend"]
        + momentum_score * _WEIGHTS["momentum"]
        + volume_score * _WEIGHTS["volume"]
        + macd_score * _WEIGHTS["macd"]
        + sector_score_norm * _WEIGHTS["sector"]
    )

    return {
        "trend": round(trend_score, 2),
        "momentum": round(momentum_score, 2),
        "volume": round(volume_score, 2),
        "macd": round(macd_score, 2),
        "sector": round(sector_score_norm, 2),
        "score": round(total, 4),
    }


def _load_sector_scores(identified_at: str) -> dict:
    """加载最新板块评分"""
    sql = """
        SELECT board_code, score
        FROM candidate_sector
        WHERE identified_at = (SELECT MAX(identified_at) FROM candidate_sector)
    """
    df = _query(sql)
    if df.empty:
        return {}
    return dict(zip(df["board_code"], df["score"]))


def screen_stocks(identified_date: str = "") -> pd.DataFrame:
    """股票多因子筛选"""
    identified_at = identified_date or date.today().isoformat()

    logger.info("加载股票数据...")
    all_data = load_all_stock_data()
    if all_data.empty:
        logger.warning("stock_daily 无数据")
        return pd.DataFrame()

    logger.info("计算全市场 RPS...")
    rps_all = calc_global_rps(all_data)
    rps_lookup = rps_all.set_index(["date", "code"])

    sector_scores = _load_sector_scores(identified_at)

    scores = []
    total_stocks = all_data["code"].nunique()
    logger.info(f"共 {total_stocks} 只股票, 开始评分...")

    for i, (code, grp) in enumerate(all_data.groupby("code"), 1):
        if i % 500 == 0:
            logger.info(f"  进度: {i}/{total_stocks}")

        grp = grp.sort_values("date").reset_index(drop=True)
        if len(grp) < MA_SHORT:
            continue

        last = grp.iloc[-1]
        date_val = last["date"]

        # RPS
        rps_row = rps_lookup.loc[(date_val, code)] \
            if (date_val, code) in rps_lookup.index else pd.Series(dtype=float)

        # 板块得分
        bc = last.get("board_code", "")
        sc = _score_one_stock(grp, rps_row, sector_scores.get(bc, 0))

        # 5日涨幅
        if len(grp) >= 6:
            chg_5d = round((grp["close"].iloc[-1] / grp["close"].iloc[-6] - 1) * 100, 4)
        else:
            chg_5d = 0

        scores.append({
            "code": code,
            "name": last["name"],
            "board_code": bc,
            "board_name": last.get("board_name", ""),
            "date": date_val,
            "identified_at": identified_at,
            "open": last.get("open"),
            "close": last["close"],
            "pct_chg": last.get("pct_chg"),
            "macd": last.get("macd"),
            "chg_5d": chg_5d,
            "avg_rel": rps_row.get("rps_20", 50) if "rps_20" in rps_row.index else 50,
            "tag": "stock",
            "trend": sc["trend"],
            "momentum": sc["momentum"],
            "volume_score": sc["volume"],
            "macd_score": sc["macd"],
            "sector_score": sc["sector"],
            "score": sc["score"],
            "rps_20": rps_row.get("rps_20", 50) if "rps_20" in rps_row.index else 50,
            "rps_60": rps_row.get("rps_60", 50) if "rps_60" in rps_row.index else 50,
        })

    if not scores:
        return pd.DataFrame()

    result = pd.DataFrame(scores)
    result = result.sort_values("score", ascending=False).reset_index(drop=True)

    # 动态阈值: 取分数前 TARGET_COUNT 名作为候选 (同分截断至上限)
    if result.empty:
        logger.info(f"评分完成: 0 只")
        return result
    threshold = MIN_SCORE
    if len(result) <= TARGET_COUNT:
        candidates = result.copy()
        threshold = float(result["score"].iloc[-1])
    else:
        threshold = float(result["score"].iloc[TARGET_COUNT - 1])
        if threshold < MIN_SCORE_FLOOR:
            threshold = MIN_SCORE_FLOOR
        candidates = result[result["score"] >= threshold].copy()
        if len(candidates) > TARGET_COUNT:
            candidates = candidates.head(TARGET_COUNT)

    logger.info(f"评分完成: {len(result)} 只, 候选 {len(candidates)} 只 (阈值 {threshold})")
    return candidates


def run(identified_date: str = ""):
    create_tables()
    identified_at = identified_date or date.today().isoformat()
    logger.info(f"===== 股票筛选开始 (识别日期: {identified_at}) =====")

    df = screen_stocks(identified_date)
    if df.empty:
        logger.warning("===== 股票筛选结束: 无候选 =====")
        return df

    save_candidate_stock_to_db(df, replace_date=True)
    logger.info(f"候选股票 {len(df)} 条, 已保存到 candidate_stock")

    top = df.sort_values("score", ascending=False).head(20)
    for _, r in top.iterrows():
        logger.info(f"  {r['code']} {r['name']} [{r['board_name']}] "
                     f"得分={r['score']} RPS20={r['rps_20']:.0f} "
                     f"RPS60={r['rps_60']:.0f} "
                     f"趋势={r['trend']} 动量={r['momentum']} "
                     f"量价={r['volume_score']} MACD={r['macd_score']}")
    return df


if __name__ == "__main__":
    run()
