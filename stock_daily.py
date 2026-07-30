import os
import ssl
import sys
import time
import re
from datetime import datetime
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

TODAY = datetime.now().strftime("%Y%m%d")


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


def fetch_history_macd(code: str, v_code: str) -> dict:
    try:
        url = f"https://d.10jqka.com.cn/v4/line/hs_{code}/01/2026.js"
        headers = {
            "hexin-v": v_code,
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.10jqka.com.cn",
        }
        r = requests.get(url, headers=headers, cookies={"v": v_code}, timeout=10)
        m = re.search(r'"data":"([^"]+)"', r.text)
        if not m:
            return {"macd": None, "signal": None, "hist": None}
        rows = m.group(1).split(";")
        if len(rows) < 26:
            return {"macd": None, "signal": None, "hist": None}
        out = []
        for row in rows:
            parts = row.split(",")
            if len(parts) >= 5:
                out.append([parts[0], float(parts[1]), float(parts[2]),
                            float(parts[3]), float(parts[4])])
        if len(out) < 26:
            return {"macd": None, "signal": None, "hist": None}
        df = pd.DataFrame(out[-60:], columns=["date", "open", "high", "low", "close"])
        return calc_macd(df)
    except Exception:
        pass
    return {"macd": None, "signal": None, "hist": None}


def main():
    CONFIG.data_dir.mkdir(parents=True, exist_ok=True)
    CONFIG.log_dir.mkdir(parents=True, exist_ok=True)

    logger.info("===== A股日级数据采集开始 =====")

    stock_list = get_stock_list()

    quotes = fetch_all_quotes(stock_list)
    if quotes.empty:
        return

    board_map = build_board_mapping(stock_list)
    quotes["board_code"] = quotes["code"].map(
        lambda c: board_map.get(c, {}).get("board_code", "")
    )
    quotes["board_name"] = quotes["code"].map(
        lambda c: board_map.get(c, {}).get("board_name", "")
    )

    logger.info("计算MACD (同花顺K线, 多线程)...")
    codes = quotes["code"].tolist()
    macd_results = {}
    done = 0
    total = len(codes)

    batch_size = 500
    for batch_start in range(0, total, batch_size):
        batch = codes[batch_start:batch_start + batch_size]
        v_code = _get_ths_v()
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = {pool.submit(fetch_history_macd, c, v_code): c for c in batch}
            for f in as_completed(futures):
                c = futures[f]
                macd_results[c] = f.result()
                done += 1
        logger.info(f"  MACD: {done}/{total}")

    quotes["macd"] = quotes["code"].map(lambda c: macd_results.get(c, {}).get("macd"))
    quotes["macd_signal"] = quotes["code"].map(lambda c: macd_results.get(c, {}).get("signal"))
    quotes["macd_hist"] = quotes["code"].map(lambda c: macd_results.get(c, {}).get("hist"))

    output_cols = [
        "code", "name", "board_code", "board_name",
        "prev_close", "open", "pct_chg",
        "volume", "amount",
        "macd", "macd_signal", "macd_hist",
    ]
    result = quotes[[c for c in output_cols if c in quotes.columns]]
    result = result.sort_values("code").reset_index(drop=True)

    file_path = CONFIG.data_dir / f"stock_daily_{TODAY}.csv"
    result.to_csv(file_path, index=False, encoding="utf-8-sig")
    logger.info(f"已保存: {file_path} ({len(result)} 条)")

    print(f"\n成功: {len(result)} 只股票")
    print(f"输出: {file_path}")
    print(result.head(10).to_string())

    logger.info("===== A股日级数据采集结束 =====")


if __name__ == "__main__":
    main()
