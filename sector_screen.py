"""板块筛选模块 (v2)

筛选逻辑: 多因子评分模型
- 动量 (40%): 20日收益率在全部板块中的百分位排名
- 趋势 (30%): 收盘价是否站上20日均线
- 量能 (20%): 5日均量/20日均量 (量能扩张比)
- 涨跌家数 (10%): 板块内上涨家数 > 下跌家数

候选条件: 综合得分 >= 3 (满分 5)
"""

import sys
import warnings
from datetime import date, timedelta

import pandas as pd
from loguru import logger

from config import CONFIG
from db_handler import get_connection, create_tables, save_candidate_sector_to_db

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

DATA_WINDOW = 60        # 加载历史天数 (用于计算MA20/MA60)
MOMENTUM_WINDOW = 20    # 动量回看窗口
MA_SHORT = 20
MA_LONG = 60
VOL_SHORT = 5
VOL_LONG = 20
MIN_SCORE = 3           # 候选最低分 (满分5)

_WEIGHTS = {"momentum": 0.4, "trend": 0.3, "volume": 0.2, "breadth": 0.1}


def _query(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = get_connection()
    try:
        df = pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()
    return df


def load_sector_data(days: int = DATA_WINDOW) -> pd.DataFrame:
    """加载所有板块最近 N 天数据"""
    sql = """
        SELECT board_code, board_name, date, close, volume,
               low, high, prev_close, amount, pct_chg, `change`,
               advance, decline, net_inflow
        FROM sector_daily
        WHERE date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
        ORDER BY board_code, date
    """
    return _query(sql, (days + 30,))


def _score_one_sector(grp: pd.DataFrame) -> dict:
    """对单个板块进行多因子打分"""
    grp = grp.sort_values("date").reset_index(drop=True)
    n = len(grp)

    # === 动量分 (原始: 20日收益率) ===
    if n >= MOMENTUM_WINDOW + 1:
        ret_20d = (grp["close"].iloc[-1] / grp["close"].iloc[-MOMENTUM_WINDOW] - 1) * 100
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

    # === 涨跌家数分 ===
    adv = grp["advance"].iloc[-1] if pd.notna(grp["advance"].iloc[-1]) else None
    dec = grp["decline"].iloc[-1] if pd.notna(grp["decline"].iloc[-1]) else None
    breadth_ok = (adv is not None and dec is not None and adv > dec)

    # === 5日涨幅 ===
    if n >= 6:
        chg_5d = round((grp["close"].iloc[-1] / grp["close"].iloc[-6] - 1) * 100, 4)
    else:
        chg_5d = 0

    # 取最后一日的原始行情字段
    last = grp.iloc[-1]
    return {
        "ret_20d": round(ret_20d, 4),
        "above_ma20": above_ma20,
        "vol_ratio": round(vol_ratio, 4),
        "vol_expanding": vol_expanding,
        "breadth_ok": breadth_ok,
        "chg_5d": chg_5d,
        "low": last.get("low"),
        "high": last.get("high"),
        "prev_close": last.get("prev_close"),
        "volume": last.get("volume"),
        "amount": last.get("amount"),
        "pct_chg": last.get("pct_chg"),
        "change": last.get("change"),
        "net_inflow": last.get("net_inflow"),
    }


def screen_sectors(identified_date: str = "") -> pd.DataFrame:
    """板块多因子筛选"""
    identified_at = identified_date or date.today().isoformat()
    df = load_sector_data()
    if df.empty:
        logger.warning("sector_daily 无数据")
        return pd.DataFrame()

    scores = []
    for board_code, grp in df.groupby("board_code"):
        if len(grp) < MA_SHORT:
            continue

        last = grp.sort_values("date").iloc[-1]
        sc = _score_one_sector(grp)

        scores.append({
            "board_code": board_code,
            "board_name": last["board_name"],
            "date": last["date"],
            "identified_at": identified_at,
            "close": last["close"],
            "ret_20d": sc["ret_20d"],
            "above_ma20": sc["above_ma20"],
            "vol_expanding": sc["vol_expanding"],
            "breadth_ok": sc["breadth_ok"],
            "vol_ratio": sc["vol_ratio"],
            "advance": last["advance"],
            "decline": last["decline"],
        })

    if not scores:
        return pd.DataFrame()

    result = pd.DataFrame(scores)

    # 计算动量百分位 (全局排名)
    if len(result) > 1:
        result["momentum_pct"] = result["ret_20d"].rank(pct=True) * 100
    else:
        result["momentum_pct"] = 50.0

    # 综合得分
    result["score"] = (
        (result["momentum_pct"] / 100 * 5 * _WEIGHTS["momentum"])
        + (result["above_ma20"].astype(float) * 5 * _WEIGHTS["trend"])
        + (result["vol_expanding"].astype(float) * 5 * _WEIGHTS["volume"])
        + (result["breadth_ok"].astype(float) * 5 * _WEIGHTS["breadth"])
    ).round(2)

    # 筛选候选
    candidates = result[result["score"] >= MIN_SCORE].copy()
    candidates = candidates.sort_values("score", ascending=False)

    return candidates


def run(identified_date: str = ""):
    create_tables()
    identified_at = identified_date or date.today().isoformat()
    logger.info(f"===== 板块筛选开始 (识别日期: {identified_at}) =====")

    df = screen_sectors(identified_date)
    if df.empty:
        logger.warning("===== 板块筛选结束: 无候选 =====")
        return df

    # 落库字段: 完整对应 candidate_sector DDL
    save_cols = ["board_code", "board_name", "date", "identified_at",
                 "close", "score", "ret_20d", "vol_ratio",
                 "low", "high", "prev_close", "volume", "amount",
                 "pct_chg", "change", "advance", "decline",
                 "net_inflow", "chg_5d"]
    save_df = df[[c for c in save_cols if c in df.columns]].copy()
    save_candidate_sector_to_db(save_df)

    logger.info(f"候选板块 {len(df)} 个, 已保存到 candidate_sector")
    top = df.head(20)
    for _, r in top.iterrows():
        logger.info(f"  {r['board_code']} {r['board_name']} "
                     f"得分={r['score']} 20日涨幅={r['ret_20d']}% "
                     f"站上MA20={'是' if r['above_ma20'] else '否'} "
                     f"量能比={r['vol_ratio']}")
    return df


if __name__ == "__main__":
    run()
