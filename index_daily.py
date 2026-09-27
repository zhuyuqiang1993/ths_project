"""指数日线采集 (东财 push2his K线)。

覆盖: 上证指数/深证成指/创业板指/沪深300/中证500/科创50。
用法: python index_daily.py [--start_date YYYYMMDD] [--end_date YYYYMMDD]
"""
import argparse
import sys
from datetime import datetime

import pandas as pd
import requests
from loguru import logger

from config import CONFIG

logger.remove()
logger.add(
    sys.stderr,
    level=CONFIG.log_level,
    format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}",
)
logger.add(
    CONFIG.log_dir / "index_daily_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
)


def _fetch_tencent(code: str, start: str, end: str) -> pd.DataFrame:
    """腾讯K线兜底 (东财不可用时)。行序: 日期,开,收,高,低,量(手,与东财一致)。"""
    sym = ("sz" if code.startswith("399") else "sh") + code
    s = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    e = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    try:
        r = requests.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{sym},day,{s},{e},800,qfq"},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
        )
        data = r.json()["data"][sym]
        rows = data.get("qfqday") or data.get("day") or []
    except Exception as ex:
        logger.warning(f"腾讯K线失败 {code}: {ex}")
        return pd.DataFrame()
    from eastmoney_client import INDEX_MAP
    out = []
    for p in rows:
        if len(p) < 6:
            continue
        out.append({
            "code": code, "name": INDEX_MAP[code][1],
            "date": p[0], "open": float(p[1]), "close": float(p[2]),
            "high": float(p[3]), "low": float(p[4]),
            "volume": int(float(p[5])),
            "amount": None, "pct_chg": None, "change": None, "turnover": None,
        })
    return pd.DataFrame(out)


def run(start_date: str = "", end_date: str = "", force: bool = False) -> pd.DataFrame:
    """采集主要指数日线并写入 index_daily。

    Args:
        start_date: YYYYMMDD 或 YYYY-MM-DD, 默认近1年
        end_date: 同上, 默认今天
        force: True 时跳过完整性预检
    """
    CONFIG.log_dir.mkdir(parents=True, exist_ok=True)

    fmt_in = "%Y%m%d" if start_date and "-" not in start_date else "%Y-%m-%d"
    end_fmt = "%Y%m%d" if end_date and "-" not in end_date else "%Y-%m-%d"
    start = datetime.strptime(start_date, fmt_in).strftime("%Y%m%d") if start_date \
        else datetime.now().strftime("%Y0101")
    end = datetime.strptime(end_date, end_fmt).strftime("%Y%m%d") if end_date \
        else datetime.now().strftime("%Y%m%d")

    if not force:
        from db_handler import has_data_in_range
        if has_data_in_range("index_daily", start, end):
            logger.info("index_daily 数据已存在, 跳过拉取")
            return pd.DataFrame()

    from eastmoney_client import INDEX_MAP, fetch_index_kline
    frames = []
    em_available = None  # 首个指数探测东财可用性, 失败则直接走腾讯兜底
    for code in INDEX_MAP:
        df = None
        if em_available is not False:
            df = fetch_index_kline(code, start, end,
                                   tries=2 if em_available is None else 4)
            if df is None or df.empty:
                em_available = False
                logger.warning("东财指数接口不可用 (疑似限流), 后续切换腾讯源")
        if df is None or df.empty:
            df = _fetch_tencent(code, start, end)
        elif em_available is None:
            em_available = True
        if df is not None and not df.empty:
            frames.append(df)
            logger.info(f"  {INDEX_MAP[code][1]}({code}): {len(df)} 条 "
                        f"{df['date'].min()} ~ {df['date'].max()}")
        else:
            logger.warning(f"  {INDEX_MAP[code][1]}({code}): 无数据")

    if not frames:
        logger.warning("未获取到任何指数数据")
        return pd.DataFrame()

    result = pd.concat(frames, ignore_index=True)

    # 腾讯源缺涨跌幅/涨跌额, 按前收盘补算
    if result["pct_chg"].isna().any():
        result = result.sort_values(["code", "date"]).reset_index(drop=True)
        prev = result.groupby("code")["close"].shift(1)
        need = result["pct_chg"].isna() & prev.notna()
        if need.any():
            result.loc[need, "pct_chg"] = (
                (result.loc[need, "close"] - prev[need]) / prev[need] * 100
            ).round(4)
            result.loc[need, "change"] = (result.loc[need, "close"] - prev[need]).round(2)

    from trade_calendar import is_trade_date
    n_before = len(result)
    result = result[result["date"].map(is_trade_date)].reset_index(drop=True)
    logger.info(f"交易日过滤: {n_before} -> {len(result)} 条")

    try:
        from db_handler import save_index_daily_to_db
        save_index_daily_to_db(result)
    except Exception as e:
        logger.error(f"MySQL 写入失败: {e}")
        raise
    logger.info(f"完成: {len(result)} 条, {result['code'].nunique()} 个指数, "
                f"日期 {result['date'].min()} ~ {result['date'].max()}")
    return result


def main():
    parser = argparse.ArgumentParser(description="指数日线采集")
    parser.add_argument("--start_date", default="", help="起始 YYYYMMDD")
    parser.add_argument("--end_date", default="", help="结束 YYYYMMDD")
    parser.add_argument("--force", action="store_true", help="跳过完整性预检")
    args = parser.parse_args()
    run(start_date=args.start_date, end_date=args.end_date, force=args.force)


if __name__ == "__main__":
    main()
