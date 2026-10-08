"""量价分析模块

对全市场个股进行量价关系分析 (锚点 = 最近交易日, 或指定日期):

量能指标
- vol_chg_pct: 当日成交量相对昨日的变化%
- vol_ratio_5: 量比 = 当日量 / 前5日均量 (不含当日)
- vol_trend:   量能趋势 = 5日均量(含今) vs 20日均量(含今) -> 放大/平稳/萎缩
- 极端量能:    天量 (>= 60日最大量的98%), 地量 (<= 60日最小量的102%)

价格指标
- pct_chg / chg_5d / chg_20d: 当日、5日、20日涨跌幅
- position_60d: 60日价格位置 = (收盘-60日最低)/(60日最高-60日最低)*100
                0 = 区间底部, 100 = 区间顶部

量价形态 (九宫格, 量变阈值 ±20%, 价变阈值 ±0.5%)
    放量上涨 / 放量滞涨 / 放量下跌
    平量上涨 / 平量盘整 / 平量下跌
    缩量上涨 / 缩量整理 / 缩量下跌

量价背离 (20日窗口)
- 顶背离: 收盘创20日新高, 但5日均量 < 20日均量*0.9 (价新高量不继)
- 底背离: 收盘创20日新低, 且5日均量 < 20日均量*0.9 (缩量阴跌, 抛压衰竭)

量价评分 vp_score (0-5)
- 量价配合度 (0-2): 近10日 "上涨放量/下跌缩量" 符合天数占比
- 量能趋势 (0-1):   5日均量/20日均量 分档
- 当日形态 (0-1):   放量上涨最佳, 放量下跌最差
- 位置背离 (0-1):   顶背离减分, 底背离加分, 高位天量减分, 低位天量加分

输出
- MySQL 表 volume_price_analysis (按 code+date upsert, 支持历史回溯)
- 日志: 形态分布 / 信号统计 / 评分TOP20 / 风险提示
- 邮件日报: email_service 的 "量价分析" 段落

用法: python volume_price.py [YYYY-MM-DD]
"""

import sys
import warnings

import pandas as pd
from loguru import logger

from config import CONFIG
from db_handler import create_tables, get_connection, save_volume_price_to_db

warnings.filterwarnings("ignore", message=".*only supports SQLAlchemy.*")

logger.remove()
logger.add(
    sys.stderr,
    level=CONFIG.log_level,
    format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}",
)
logger.add(
    CONFIG.log_dir / "volume_price_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
)

LOAD_DAYS = 120        # 加载锚点前 120 个自然日 (覆盖60日窗口+20日背离+缓冲)
MA_VOL_SHORT = 5
MA_VOL_LONG = 20
POS_WINDOW = 60        # 价格位置窗口
DIVERGE_WINDOW = 20    # 背离判定窗口
TREND_WINDOW = 10      # 量价配合度统计窗口
MIN_BARS = 10          # 最少K线数 (低于此不分析)
TOP_N = 20             # 输出TOP数量

PATTERN_SCORE = {
    "放量上涨": 1.0, "平量上涨": 0.7, "缩量上涨": 0.6,
    "缩量整理": 0.5, "平量盘整": 0.4, "放量滞涨": 0.2,
    "缩量下跌": 0.4, "平量下跌": 0.2, "放量下跌": 0.0,
    "—": 0.4,
}


def _query(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = get_connection()
    try:
        df = pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()
    return df


def _f(val):
    """转 float, None/NaN -> None"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def load_window(anchor: str) -> pd.DataFrame:
    """加载锚点(含)之前 LOAD_DAYS 自然日的全市场日线"""
    sql = """
        SELECT code, name, board_code, board_name, date, high, low, close,
               pct_chg, volume, amount
        FROM stock_daily
        WHERE date <= %s AND date >= DATE_SUB(%s, INTERVAL %s DAY)
        ORDER BY code, date
    """
    df = _query(sql, (anchor, anchor, LOAD_DAYS))
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def _classify_pattern(vol_chg_pct, pct_chg) -> str:
    """量价九宫格形态分类"""
    if vol_chg_pct is None or pct_chg is None:
        return "—"
    if vol_chg_pct >= 20:
        if pct_chg >= 1:
            return "放量上涨"
        if pct_chg <= -1:
            return "放量下跌"
        return "放量滞涨"
    if vol_chg_pct <= -20:
        if pct_chg >= 0.5:
            return "缩量上涨"
        if pct_chg <= -0.5:
            return "缩量下跌"
        return "缩量整理"
    if pct_chg >= 0.5:
        return "平量上涨"
    if pct_chg <= -0.5:
        return "平量下跌"
    return "平量盘整"


def _match_ratio(closes: list, vols: list) -> float:
    """近 TREND_WINDOW 日 "上涨放量/下跌缩量" 符合占比"""
    ok = 0
    total = 0
    for i in range(max(1, len(closes) - TREND_WINDOW), len(closes)):
        if not vols[i] or not vols[i - 1]:
            continue
        total += 1
        if closes[i] > closes[i - 1] and vols[i] >= vols[i - 1]:
            ok += 1
        elif closes[i] < closes[i - 1] and vols[i] <= vols[i - 1]:
            ok += 1
    return ok / total if total else 0.0


def analyze_one(grp: pd.DataFrame) -> dict | None:
    """单只股票量价分析 (grp: 按日期升序, 末行必须为锚点当日)"""
    n = len(grp)
    if n < MIN_BARS:
        return None

    closes = grp["close"].astype(float).tolist()
    vols = grp["volume"].astype(float).tolist()
    highs = grp["high"].astype(float).tolist()
    lows = grp["low"].astype(float).tolist()
    last = grp.iloc[-1]

    close = closes[-1]
    pct_chg = _f(last["pct_chg"])
    volume = int(vols[-1])
    amount = _f(last["amount"])

    # --- 量能指标 ---
    prev_vol = vols[-2] if n >= 2 else 0
    vol_chg_pct = round((volume / prev_vol - 1) * 100, 2) if prev_vol > 0 else None

    base5 = vols[-(MA_VOL_SHORT + 1):-1]
    vol_ma5 = sum(base5) / len(base5) if base5 else 0
    vol_ratio_5 = round(volume / vol_ma5, 2) if vol_ma5 > 0 else None

    ma5_in = sum(vols[-MA_VOL_SHORT:]) / min(MA_VOL_SHORT, n)
    win_long = min(MA_VOL_LONG, n)
    ma20 = sum(vols[-win_long:]) / win_long
    trend_ratio = (ma5_in / ma20) if ma20 > 0 else None
    if trend_ratio is None:
        vol_trend = "平稳"
    elif trend_ratio >= 1.15:
        vol_trend = "放大"
    elif trend_ratio <= 0.85:
        vol_trend = "萎缩"
    else:
        vol_trend = "平稳"

    # --- 价格指标 ---
    chg_5d = round((close / closes[-6] - 1) * 100, 4) if n >= 6 else None
    chg_20d = round((close / closes[-21] - 1) * 100, 4) if n >= 21 else None
    pos_win = min(POS_WINDOW, n)
    h60, l60 = max(highs[-pos_win:]), min(lows[-pos_win:])
    position_60d = round((close - l60) / (h60 - l60) * 100, 2) if h60 > l60 else 50.0

    # --- 形态 ---
    vol_pattern = _classify_pattern(vol_chg_pct, pct_chg)

    # --- 量价背离 (20日) ---
    divergence = "无"
    if n >= DIVERGE_WINDOW + 1:
        prior = closes[-(DIVERGE_WINDOW + 1):-1]
        new_high = close >= max(prior)
        new_low = close <= min(prior)
        vol_ma5_all = sum(vols[-MA_VOL_SHORT:]) / MA_VOL_SHORT
        vol_ma20_all = sum(vols[-MA_VOL_LONG:]) / MA_VOL_LONG
        shrink = vol_ma20_all > 0 and vol_ma5_all < vol_ma20_all * 0.9
        if new_high and close > max(prior) and shrink:
            divergence = "顶背离"
        elif new_low and close < min(prior) and shrink:
            divergence = "底背离"

    # --- 天量 / 地量 (60日) ---
    ext_win = min(POS_WINDOW, n)
    v60 = vols[-ext_win:]
    is_volume_peak = 1 if max(v60) > 0 and volume >= max(v60) * 0.98 else 0
    is_volume_trough = 1 if min(v60) > 0 and volume <= min(v60) * 1.02 else 0

    # --- 量价评分 (0-5) ---
    match = _match_ratio(closes, vols)
    if match >= 0.7:
        s1 = 2.0
    elif match >= 0.5:
        s1 = 1.4
    elif match >= 0.3:
        s1 = 0.8
    else:
        s1 = 0.3

    if trend_ratio is None:
        s2 = 0.4
    elif trend_ratio >= 1.3:
        s2 = 1.0
    elif trend_ratio >= 1.0:
        s2 = 0.7
    elif trend_ratio >= 0.7:
        s2 = 0.4
    else:
        s2 = 0.1

    s3 = PATTERN_SCORE.get(vol_pattern, 0.4)

    s4 = 0.5
    if divergence == "顶背离":
        s4 -= 0.4
    elif divergence == "底背离":
        s4 += 0.2
    if is_volume_peak:
        if position_60d >= 80:
            s4 -= 0.3
        elif position_60d <= 30:
            s4 += 0.3
    s4 = max(0.0, min(1.0, s4))

    return {
        "code": str(last["code"]),
        "name": str(last["name"] or ""),
        "board_code": str(last["board_code"] or ""),
        "board_name": str(last["board_name"] or ""),
        "date": last["date"],
        "close": close,
        "pct_chg": pct_chg,
        "chg_5d": chg_5d,
        "chg_20d": chg_20d,
        "volume": volume,
        "amount": amount,
        "vol_chg_pct": vol_chg_pct,
        "vol_ratio_5": vol_ratio_5,
        "vol_trend": vol_trend,
        "position_60d": position_60d,
        "vol_pattern": vol_pattern,
        "divergence": divergence,
        "is_volume_peak": is_volume_peak,
        "is_volume_trough": is_volume_trough,
        "vp_score": round(s1 + s2 + s3 + s4, 2),
    }


def _fmt_pct(v) -> str:
    return "" if v is None else f"{v}%"


def output_results(result: pd.DataFrame, anchor: str):
    """日志输出: 形态分布 / 信号统计 / 评分TOP20 / 风险提示"""
    n = len(result)
    logger.info(f"量价分析完成: {n} 只 @ {anchor}")

    dist = result["vol_pattern"].value_counts()
    logger.info("量价形态分布: " + " | ".join(
        f"{k} {v}" for k, v in dist.items()))

    n_peak = int(result["is_volume_peak"].sum())
    n_trough = int(result["is_volume_trough"].sum())
    n_top = int((result["divergence"] == "顶背离").sum())
    n_bottom = int((result["divergence"] == "底背离").sum())
    logger.info(f"信号统计: 天量 {n_peak} 只, 地量 {n_trough} 只, "
                f"顶背离 {n_top} 只, 底背离 {n_bottom} 只")

    top = result.sort_values("vp_score", ascending=False).head(TOP_N)
    logger.info(f"=== 量价评分 TOP{TOP_N} ===")
    for _, r in top.iterrows():
        logger.info(
            f"  {r['code']} {r['name']} [{r['board_name']}] "
            f"得分={r['vp_score']} 涨幅={_fmt_pct(r['pct_chg'])} "
            f"量比={r['vol_ratio_5']} 位置={r['position_60d']} "
            f"形态={r['vol_pattern']} 背离={r['divergence']}"
            + (" 天量" if r["is_volume_peak"] else "")
            + (" 地量" if r["is_volume_trough"] else "")
        )

    risk = result[
        (result["vol_pattern"] == "放量下跌")
        | (result["divergence"] == "顶背离")
    ].sort_values("pct_chg").head(10)
    if not risk.empty:
        logger.info("=== 量价风险提示 (放量下跌/顶背离) ===")
        for _, r in risk.iterrows():
            logger.info(
                f"  {r['code']} {r['name']} [{r['board_name']}] "
                f"涨幅={_fmt_pct(r['pct_chg'])} 位置={r['position_60d']} "
                f"形态={r['vol_pattern']} 背离={r['divergence']}"
            )

    logger.info("量价分析结果已写入 MySQL 表 volume_price_analysis")


def run(analysis_date: str = "") -> pd.DataFrame:
    """量价分析主入口: 分析 -> 入库 -> 输出

    Args:
        analysis_date: 分析锚点 (YYYY-MM-DD), 默认当日最新交易日 (today_anchor)
    """
    create_tables()

    # 锚点统一取当日最新交易日; 库内尚无当日数据时回退到库内最新 (并告警)
    from trade_calendar import today_anchor
    analysis_date = analysis_date or today_anchor()
    df_anchor = _query(
        "SELECT MAX(`date`) AS d FROM stock_daily WHERE `date` <= %s",
        (analysis_date,),
    )
    if df_anchor.empty or df_anchor["d"].isna().all():
        logger.warning("stock_daily 无数据, 量价分析跳过")
        return pd.DataFrame()
    latest = str(df_anchor["d"].iloc[0])
    if latest != analysis_date:
        logger.warning(f"stock_daily 尚无 {analysis_date} 数据, 锚点回退到库内最新 {latest}")
        analysis_date = latest

    logger.info(f"===== 量价分析开始 (交易日 {analysis_date}) =====")
    data = load_window(analysis_date)
    if data.empty:
        logger.warning(f"{analysis_date} 之前无股票数据, 量价分析跳过")
        return pd.DataFrame()

    rows = []
    total = data["code"].nunique()
    for i, (code, grp) in enumerate(data.groupby("code"), 1):
        # 仅分析锚点当日有行情的股票 (停牌跳过)
        if grp["date"].iloc[-1].isoformat() != analysis_date:
            continue
        grp = grp.sort_values("date").reset_index(drop=True)
        r = analyze_one(grp)
        if r:
            rows.append(r)
        if i % 2000 == 0:
            logger.info(f"  进度: {i}/{total}")

    if not rows:
        logger.warning("量价分析无结果")
        return pd.DataFrame()

    result = pd.DataFrame(rows)
    save_volume_price_to_db(result)
    output_results(result, analysis_date)
    return result


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "")
