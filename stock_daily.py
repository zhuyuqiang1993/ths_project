import os
import ssl
import sys
import time
import re
import argparse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)

ssl._create_default_https_context = ssl._create_unverified_context

import urllib3
from urllib3.util import ssl_ as urllib3_ssl

urllib3.disable_warnings()
_orig_ctx = urllib3_ssl.create_urllib3_context
def _no_vfy(*a, **kw):
    c = _orig_ctx(*a, **kw)
    c.verify_mode = ssl.CERT_NONE
    c.check_hostname = False
    return c
urllib3_ssl.create_urllib3_context = _no_vfy

import akshare as ak
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
    CONFIG.log_dir / "stock_daily_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
)

_SESSION = requests.Session()
_SESSION.verify = False
_SESSION.trust_env = True

TODAY = datetime.now().strftime("%Y-%m-%d")
TODAY_FILE = datetime.now().strftime("%Y%m%d")


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calc_macd(df: pd.DataFrame, close_col: str = "close") -> dict:
    if df is None or len(df) < 26:
        return {"macd": None, "signal": None, "hist": None}
    close = df[close_col].astype(float)
    e12 = ema(close, 12)
    e26 = ema(close, 26)
    macd_line = e12 - e26
    signal = ema(macd_line, 9)
    hist = macd_line - signal
    return {
        "macd": round(macd_line.iloc[-1], 4),
        "signal": round(signal.iloc[-1], 4),
        "hist": round(hist.iloc[-1], 4),
    }


def get_stock_list() -> pd.DataFrame:
    logger.info("获取A股代码列表 (akshare)...")
    df = ak.stock_info_a_code_name()
    df.columns = ["code", "name"]
    df["code"] = df["code"].astype(str).str.zfill(6)
    # 过滤掉北交所 (8xxxxx, 4xxxxx)
    df = df[df["code"].str.match(r"^(0|3|6)\d{5}$")]
    logger.info(f"共 {len(df)} 只股票")
    return df


def fetch_quotes_batch(codes: list) -> list:
    query = ",".join(
        f"sh{c}" if c.startswith(("6", "9")) else f"sz{c}" for c in codes
    )
    rows = []
    try:
        r = _SESSION.get(f"https://qt.gtimg.cn/q={query}", timeout=15)
        for line in r.text.strip().split(";"):
            parts = line.split("~")
            if len(parts) < 35 or not parts[2]:
                continue
            rows.append({
                "code": parts[2],
                "prev_close": float(parts[4]) if parts[4] else 0,
                "open": float(parts[5]) if parts[5] else 0,
                "high": float(parts[33]) if parts[33] else 0,
                "low": float(parts[34]) if parts[34] else 0,
                "price": float(parts[3]) if parts[3] else 0,
                "volume": int(float(parts[6])) if parts[6] else 0,
                "amount": float(parts[7]) if parts[7] else 0,
                "pct_chg": float(parts[32]) if parts[32] else 0,
                "change": float(parts[31]) if parts[31] else 0,
            })
    except Exception:
        pass
    return rows


def fetch_all_quotes(stock_list: pd.DataFrame) -> pd.DataFrame:
    logger.info("获取A股实时行情 (腾讯)...")
    codes = stock_list["code"].tolist()
    all_rows = []
    batch_size = 80
    total = len(codes)
    for i in range(0, total, batch_size):
        batch = codes[i:i + batch_size]
        all_rows.extend(fetch_quotes_batch(batch))
        if (i // batch_size + 1) % 10 == 0:
            logger.info(f"  行情进度: {min(i+batch_size, total)}/{total}")
        time.sleep(0.15)

    df = pd.DataFrame(all_rows)
    if df.empty:
        logger.error("未获取到行情数据")
        return pd.DataFrame()
    df = df.drop_duplicates(subset="code").reset_index(drop=True)
    logger.info(f"有效行情: {len(df)} 只")
    return df


def build_board_mapping(stock_list: pd.DataFrame) -> dict:
    logger.info("构建板块映射 (同花顺)...")
    mapping = {c: {"board_code": "", "board_name": ""} for c in stock_list["code"]}
    try:
        boards = ak.stock_board_industry_name_ths()
        total = len(boards)
        for i, (_, row) in enumerate(boards.iterrows(), 1):
            b_code = str(row["code"])
            b_name = row["name"]
            try:
                url = f"https://basic.10jqka.com.cn/{b_code}/"
                r = _SESSION.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
                found = set(re.findall(r"changecode\('(\d{6})'\)", r.text))
                for c in found:
                    if c in mapping:
                        mapping[c] = {"board_code": b_code, "board_name": b_name}
            except Exception:
                pass
            if i % 15 == 0:
                logger.info(f"  板块进度: {i}/{total}")
        time.sleep(0.05)
    except Exception as e:
        logger.warning(f"板块映射构建失败: {e}")
    mapped = sum(1 for v in mapping.values() if v["board_code"])
    logger.info(f"已映射: {mapped}/{len(mapping)} 只")
    return mapping


def _get_ths_v():
    """Generate v-code for Tonghuashun API (single-use, no cache)."""
    from py_mini_racer import MiniRacer
    js_path = Path(__file__).with_name("ths.js")
    if not js_path.exists():
        js_path = Path(r"C:\Users\YY\AppData\Roaming\Python\Python314\site-packages\akshare\stock_feature\ths.js")
    with open(js_path, "r", encoding="utf-8") as f:
        code = f.read()
    ctx = MiniRacer()
    ctx.eval(code)
    return ctx.call("v")


def fetch_stock_candles(code: str, v_code: str, year: str = "2026") -> Optional[pd.DataFrame]:
    """Fetch full year candles for a stock from Tonghuashun K-line API."""
    try:
        url = f"https://d.10jqka.com.cn/v4/line/hs_{code}/01/{year}.js"
        headers = {
            "hexin-v": v_code,
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.10jqka.com.cn",
        }
        r = requests.get(url, headers=headers, cookies={"v": v_code}, timeout=10)
        m = re.search(r'"data":"([^"]+)"', r.text)
        if not m:
            return None
        out = []
        for row in m.group(1).split(";"):
            parts = row.split(",")
            if len(parts) >= 7:
                volume = int(float(parts[5])) if parts[5] else 0
                amount = float(parts[6]) if parts[6] else 0
                out.append([parts[0], float(parts[1]), float(parts[2]),
                            float(parts[3]), float(parts[4]),
                            volume, amount])
        if not out:
            return None
        df = pd.DataFrame(out, columns=["date", "open", "high", "low", "close", "volume", "amount"])
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
        return df
    except Exception:
        return None


def compute_day_data(candles: pd.DataFrame, target_date: str) -> dict:
    """Extract one date's OHLCV and compute MACD from trailing data."""
    td = pd.Timestamp(target_date)
    before = candles[candles["date"] <= td]
    if len(before) < 2:
        return {"open": None, "high": None, "low": None, "close": None,
                "prev_close": None, "pct_chg": None, "volume": None, "amount": None,
                "macd": None, "signal": None, "hist": None}

    today_row = before[before["date"] == td]
    if today_row.empty:
        return {"open": None, "high": None, "low": None, "close": None,
                "prev_close": None, "pct_chg": None, "volume": None, "amount": None,
                "macd": None, "signal": None, "hist": None}
    row = today_row.iloc[-1]

    close_val = float(row["close"])
    prev_close_val = float(before.iloc[-2]["close"]) if len(before) >= 2 else close_val
    pct_chg = round((close_val - prev_close_val) / prev_close_val * 100, 2) if prev_close_val else 0

    macd = {"macd": None, "signal": None, "hist": None}
    if len(before) >= 26:
        macd = calc_macd(before.tail(60))

    return {
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": close_val,
        "prev_close": prev_close_val,
        "pct_chg": pct_chg,
        "volume": int(float(row["volume"])),
        "amount": float(row["amount"]),
        "macd": macd["macd"],
        "signal": macd["signal"],
        "hist": macd["hist"],
    }


def run(start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """获取指定日期/区间的A股日级数据，返回包含MACD的DataFrame。

    Args:
        start_date: 起始日期 YYYY-MM-DD；与end_date相同或两者均为None时取当天
        end_date: 结束日期 YYYY-MM-DD

    Returns:
        包含 date/code/name/board_code/board_name/prev_close/open/.../macd 的DataFrame
    """
    if start_date and end_date and start_date != end_date:
        s = datetime.strptime(start_date, "%Y-%m-%d")
        e = datetime.strptime(end_date, "%Y-%m-%d")
        target_dates = [(s + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((e - s).days + 1)]
    else:
        target_dates = [start_date or end_date or TODAY]

    is_today = len(target_dates) == 1 and target_dates[0] == TODAY

    CONFIG.data_dir.mkdir(parents=True, exist_ok=True)
    CONFIG.log_dir.mkdir(parents=True, exist_ok=True)

    logger.info("===== A股日级数据采集开始 =====")

    stock_list = get_stock_list()
    board_map = build_board_mapping(stock_list)

    codes = stock_list["code"].tolist()
    total = len(codes)
    name_map = stock_list.set_index("code")["name"].to_dict()

    # Fetch all candles once per stock
    logger.info("获取同花顺K线数据...")
    candles_cache = {}
    batch_size = 200
    for batch_start in range(0, total, batch_size):
        batch = codes[batch_start:batch_start + batch_size]
        v_code = _get_ths_v()
        results = {}
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = {pool.submit(fetch_stock_candles, c, v_code): c for c in batch}
            for f in as_completed(futures):
                c = futures[f]
                results[c] = f.result()
        for c, df in results.items():
            if df is not None:
                candles_cache[c] = df
        logger.info(f"  K线: {min(batch_start+batch_size, total)}/{total}")
    logger.info(f"K线获取完成: {len(candles_cache)}/{total} 只")

    # Build rows for each target date
    all_rows = []
    for target_date in target_dates:
        logger.info(f"处理日期 {target_date}...")

        if is_today:
            quotes_df = fetch_all_quotes(stock_list)
            if quotes_df.empty:
                return pd.DataFrame()
            for _, q in quotes_df.iterrows():
                c = q["code"]
                candle = candles_cache.get(c)
                day_data = compute_day_data(candle, target_date) if candle is not None else {}
                all_rows.append({
                    "date": target_date,
                    "code": c,
                    "name": name_map.get(c, ""),
                    "board_code": board_map.get(c, {}).get("board_code", ""),
                    "board_name": board_map.get(c, {}).get("board_name", ""),
                    "prev_close": q["prev_close"],
                    "open": q["open"],
                    "pct_chg": q["pct_chg"],
                    "volume": q["volume"],
                    "amount": q["amount"],
                    "macd": day_data.get("macd"),
                    "macd_signal": day_data.get("signal"),
                    "macd_hist": day_data.get("hist"),
                })
        else:
            for c in codes:
                candle = candles_cache.get(c)
                if candle is None:
                    continue
                day_data = compute_day_data(candle, target_date)
                all_rows.append({
                    "date": target_date,
                    "code": c,
                    "name": name_map.get(c, ""),
                    "board_code": board_map.get(c, {}).get("board_code", ""),
                    "board_name": board_map.get(c, {}).get("board_name", ""),
                    **day_data,
                })

    if not all_rows:
        logger.error("无数据可输出")
        return pd.DataFrame()

    result = pd.DataFrame(all_rows)
    output_cols = [
        "date", "code", "name", "board_code", "board_name",
        "prev_close", "open", "pct_chg",
        "volume", "amount",
        "macd", "macd_signal", "macd_hist",
    ]
    result = result[[c for c in output_cols if c in result.columns]]
    result = result.sort_values(["date", "code"]).reset_index(drop=True)

    if is_today:
        file_path = CONFIG.data_dir / f"stock_daily_{TODAY_FILE}.csv"
    else:
        file_path = CONFIG.data_dir / f"stock_daily_{target_dates[0]}_{target_dates[-1]}.csv"
    result.to_csv(file_path, index=False, encoding="utf-8-sig")
    logger.info(f"已保存: {file_path} ({len(result)} 条)")

    print(f"\n成功: {len(result)} 条记录, {result['date'].nunique()} 个交易日")
    print(f"输出: {file_path}")
    print(result.head(10).to_string())
    logger.info("===== A股日级数据采集结束 =====")

    return result


def main():
    parser = argparse.ArgumentParser(description="A股日级数据采集")
    parser.add_argument("--start_date", default=None, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end_date", default=None, help="结束日期 YYYY-MM-DD")
    args = parser.parse_args()
    run(start_date=args.start_date, end_date=args.end_date)


if __name__ == "__main__":
    main()
