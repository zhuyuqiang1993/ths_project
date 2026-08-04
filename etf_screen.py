"""ETF筛选模块 (v2)

筛选逻辑: 多因子评分模型
- 动量 (40%): 20日收益率在全部ETF中的百分位排名
- 趋势 (30%): 收盘价站上20日均线
- 量能 (20%): 5日均量/20日均量 (量能扩张比)

候选条件: 综合得分 >= 3.0 (满分 4.5)
"""

import sys
import warnings
from datetime import date, timedelta

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

DATA_WINDOW = 60
MA_SHORT = 20
VOL_SHORT = 5
VOL_LONG = 20
MIN_SCORE = 3.0

_WEIGHTS = {"momentum": 0.40, "trend": 0.30, "volume": 0.20}


def _query(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = get_connection()
    try:
        df = pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()
    return df


def load_etf_data(days: int = DATA_WINDOW) -> pd.DataFrame:
    """加载所有ETF最近 N 天数据"""
    sql = """
        SELECT code, name, date, close, volume, pct_chg
        FROM etf_daily
        WHERE date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
        ORDER BY code, date
    """
    return _query(sql, (days + 30,))


def _score_one_etf(grp: pd.DataFrame) -> dict:
    """对单个ETF进行多因子打分"""
    grp = grp.sort_values("date").reset_index(drop=True)
    n = len(grp)

    # === 动量分: 20日收益率 ===
    if n >= 21:
        ret_20d = (grp["close"].iloc[-1] / grp["close"].iloc[-20] - 1) * 100
    else:
        ret_20d = 0.0

    # === 趋势分: 收盘 > MA20 ===
    if n >= MA_SHORT:
        ma20 = grp["close"].rolling(MA_SHORT).mean().iloc[-1]
        above_ma20 = grp["close"].iloc[-1] > ma20
    else:
        above_ma20 = False

    # === 量能分: 5日均量/20日均量 ===
    if n >= VOL_LONG:
        vol_5 = grp["volume"].tail(VOL_SHORT).mean()
        vol_20 = grp["volume"].tail(VOL_LONG).mean()
        vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 0
        vol_expanding = vol_ratio > 1.1
    else:
        vol_ratio = 0
        vol_expanding = False

    # === 5日涨幅 ===
    if n >= 6:
        chg_5d = round((grp["close"].iloc[-1] / grp["close"].iloc[-6] - 1) * 100, 4)
    else:
        chg_5d = 0

    return {
        "ret_20d": round(ret_20d, 4),
        "above_ma20": above_ma20,
        "vol_ratio": round(vol_ratio, 4),
        "vol_expanding": vol_expanding,
        "chg_5d": chg_5d,
    }


def screen_etfs(identified_date: str = "") -> pd.DataFrame:
    """ETF多因子筛选"""
    identified_at = identified_date or date.today().isoformat()

    df = load_etf_data()
    if df.empty:
        logger.warning("etf_daily 无数据")
        return pd.DataFrame()

    scores = []
    for code, grp in df.groupby("code"):
        if len(grp) < MA_SHORT:
            continue

        last = grp.sort_values("date").iloc[-1]
        sc = _score_one_etf(grp)

        scores.append({
            "code": code,
            "name": last["name"],
            "date": last["date"],
            "identified_at": identified_at,
            "close": last["close"],
            "pct_chg": last.get("pct_chg"),
            "ret_20d": sc["ret_20d"],
            "above_ma20": sc["above_ma20"],
            "vol_expanding": sc["vol_expanding"],
            "vol_ratio": sc["vol_ratio"],
            "chg_5d": sc["chg_5d"],
        })

    if not scores:
        return pd.DataFrame()

    result = pd.DataFrame(scores)

    # 计算动量百分位
    if len(result) > 1:
        result["momentum_pct"] = result["ret_20d"].rank(pct=True) * 100
    else:
        result["momentum_pct"] = 50.0

    # 综合得分
    result["score"] = (
        (result["momentum_pct"] / 100 * 5 * _WEIGHTS["momentum"])
        + (result["above_ma20"].astype(float) * 5 * _WEIGHTS["trend"])
        + (result["vol_expanding"].astype(float) * 5 * _WEIGHTS["volume"])
    ).round(2)

    candidates = result[result["score"] >= MIN_SCORE].copy()
    candidates = candidates.sort_values("score", ascending=False)
    return candidates


def run(identified_date: str = ""):
    create_tables()
    identified_at = identified_date or date.today().isoformat()
    logger.info(f"===== ETF筛选开始 (识别日期: {identified_at}) =====")

    df = screen_etfs(identified_date)
    if df.empty:
        logger.warning("===== ETF筛选结束: 无候选 =====")
        return df

    save_cols = ["code", "name", "date", "identified_at", "close",
                 "pct_chg", "score", "ret_20d", "vol_ratio", "chg_5d"]
    save_df = df[[c for c in save_cols if c in df.columns]].copy()
    save_candidate_etf_to_db(save_df, replace_date=True)

    logger.info(f"候选ETF {len(df)} 只, 已保存到 candidate_etf")
    top = df.sort_values("score", ascending=False).head(20)
    for _, r in top.iterrows():
        logger.info(f"  {r['code']} {r['name']} "
                     f"得分={r['score']} 20日涨幅={r['ret_20d']}% "
                     f"站上MA20={'是' if r['above_ma20'] else '否'} "
                     f"量能比={r['vol_ratio']}")
    return df


if __name__ == "__main__":
    run()
