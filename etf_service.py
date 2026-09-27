import os
import re
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
ssl._create_default_https_context = ssl._create_unverified_context

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
    CONFIG.log_dir / "etf_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
)

_SESSION = requests.Session()
_SESSION.verify = False

TODAY = datetime.now().strftime("%Y-%m-%d")

COL_CODE = "code"
COL_NAME = "name"
COL_DATE = "date"
COL_PREV_CLOSE = "prev_close"
COL_OPEN = "open"
COL_HIGH = "high"
COL_LOW = "low"
COL_CLOSE = "close"
COL_VOLUME = "volume"
COL_AMOUNT = "amount"
COL_PCT_CHG = "pct_chg"
COL_CHANGE = "change"

_CN_NAMES = {
    "code": "代码",
    "name": "名称",
    "date": "日期",
    "prev_close": "昨收",
    "open": "开盘",
    "high": "最高",
    "low": "最低",
    "close": "收盘",
    "volume": "成交量",
    "amount": "成交额",
    "pct_chg": "涨跌幅",
    "change": "涨跌额",
}
_REVERSE_CN = {v: k for k, v in _CN_NAMES.items()}


def _get_ths_v() -> str:
    from py_mini_racer import MiniRacer

    js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ths.js")
    if not os.path.exists(js_path):
        js_path = os.path.join(os.path.dirname(ak.__file__), "stock_feature", "ths.js")
    with open(js_path, "r", encoding="utf-8") as f:
        code = f.read()
    ctx = MiniRacer()
    ctx.eval(code)
    return ctx.call("v")


def get_etf_list() -> pd.DataFrame:
    logger.info("获取ETF列表 (同花顺)...")
    df = ak.fund_etf_category_ths(symbol="ETF")
    result = pd.DataFrame(
        {"code": df["基金代码"].astype(str).str.zfill(6),
         "name": df["基金名称"].astype(str)}
    )
    logger.info(f"共 {len(result)} 只ETF")
    return result


_DEBUG_LOGGED = {"n": 0}


def fetch_etf_daily(
    code: str,
    start_date: str = "",
    end_date: str = "",
    v_code: str = "",
) -> Optional[pd.DataFrame]:
    start = (start_date or "20200101").replace("-", "")
    end = (end_date or datetime.now().strftime("%Y%m%d")).replace("-", "")
    if not v_code:
        v_code = _get_ths_v()

    s_year = int(start[:4])
    e_year = int(end[:4])

    headers = {
        "hexin-v": v_code,
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.10jqka.com.cn",
    }

    all_rows = []
    for year in range(s_year, e_year + 1):
        url = f"https://d.10jqka.com.cn/v4/line/hs_{code}/01/{year}.js"
        try:
            r = _SESSION.get(url, headers=headers, cookies={"v": v_code}, timeout=10)
            m = re.search(r'"data":"([^"]+)"', r.text)
            if not m:
                continue
            for row in m.group(1).split(";"):
                parts = row.split(",")
                if len(parts) >= 7 and parts[0]:
                    all_rows.append([
                        parts[0], float(parts[1]), float(parts[2]),
                        float(parts[3]), float(parts[4]),
                        int(float(parts[5])) if parts[5] else 0,
                        float(parts[6]) if parts[6] else 0,
                    ])
        except Exception:
            continue
        time.sleep(0.03)

    if not all_rows:
        return None
    df = pd.DataFrame(
        all_rows,
        columns=[COL_DATE, COL_OPEN, COL_HIGH, COL_LOW, COL_CLOSE, COL_VOLUME, COL_AMOUNT],
    )
    df[COL_DATE] = pd.to_datetime(df[COL_DATE], format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=[COL_DATE]).sort_values(COL_DATE).reset_index(drop=True)

    df[COL_DATE] = df[COL_DATE].dt.strftime("%Y-%m-%d")
    df[COL_PREV_CLOSE] = df[COL_CLOSE].shift(1)
    df[COL_PCT_CHG] = df[COL_CLOSE].pct_change() * 100
    df[COL_CHANGE] = df[COL_CLOSE].diff()

    start_dt = start[:4] + "-" + start[4:6] + "-" + start[6:]
    end_dt = end[:4] + "-" + end[4:6] + "-" + end[6:]
    df = df[(df[COL_DATE] >= start_dt) & (df[COL_DATE] <= end_dt)]
    if df.empty:
        return None

    return df


def build_etf_daily(
    etf_list: pd.DataFrame,
    start_date: str = "",
    end_date: str = "",
    max_etfs: int = 0,
    only_codes: list = None,
) -> pd.DataFrame:
    codes = etf_list[COL_CODE].tolist()
    name_map = etf_list.set_index(COL_CODE)[COL_NAME].to_dict()

    if only_codes:
        keep = set(str(c) for c in only_codes)
        codes = [c for c in codes if str(c) in keep]
        logger.info(f"定点修复模式: 仅处理 {len(codes)} 只ETF (指定 {len(keep)})")

    if max_etfs > 0:
        codes = codes[:max_etfs]

    total = len(codes)
    logger.info(f"开始获取 {total} 只ETF日线...")

    # 分轮抓取: hexin-v 偶发失效会导致整批为空, 失败代码用新v_code重试
    ok_map = {}
    batch_size = 100
    pending = list(codes)
    for p in range(3):
        if not pending:
            break
        for batch_start in range(0, len(pending), batch_size):
            batch = pending[batch_start:batch_start + batch_size]
            v_code = _get_ths_v()
            results = {}
            with ThreadPoolExecutor(max_workers=20) as pool:
                futures = {
                    pool.submit(fetch_etf_daily, c, start_date, end_date, v_code): c
                    for c in batch
                }
                for f in as_completed(futures):
                    c = futures[f]
                    try:
                        results[c] = f.result()
                    except Exception:
                        results[c] = None
            for c, df in results.items():
                if df is not None and not df.empty:
                    ok_map[c] = df
            n_ok = sum(1 for df in results.values() if df is not None and not df.empty)
            logger.info(f"  ETF进度: {min(batch_start+batch_size, len(pending))}"
                        f"/{len(pending)} (成功{n_ok})")
        still_failed = [c for c in pending if ok_map.get(c) is None]
        if still_failed and p < 2:
            logger.warning(f"  {len(still_failed)} 只ETF无数据, 第{p+2}轮重试...")
            time.sleep(3)
            pending = still_failed
        else:
            if still_failed:
                logger.warning(f"  仍有 {len(still_failed)} 只ETF无数据")
            pending = []

    if not ok_map:
        return pd.DataFrame()

    all_rows = []
    for c, df in ok_map.items():
        df[COL_CODE] = c
        df[COL_NAME] = name_map.get(c, c)
        all_rows.append(df)

    result = pd.concat(all_rows, ignore_index=True)
    cols = [
        COL_CODE, COL_NAME, COL_DATE,
        COL_PREV_CLOSE, COL_OPEN, COL_HIGH, COL_LOW, COL_CLOSE,
        COL_VOLUME, COL_AMOUNT, COL_PCT_CHG, COL_CHANGE,
    ]
    result = result[[c for c in cols if c in result.columns]]
    result.sort_values([COL_CODE, COL_DATE], inplace=True)
    result.reset_index(drop=True, inplace=True)
    return result


def run(start_date: str = "", end_date: str = "", max_etfs: int = 0,
        force: bool = False, only_codes: list = None):
    CONFIG.log_dir.mkdir(parents=True, exist_ok=True)

    # 增量优化: 检查DB中是否已有目标日期范围的数据, 有则跳过API调用
    if start_date and end_date and not force:
        from db_handler import has_data_in_range
        if has_data_in_range("etf_daily", start_date, end_date):
            logger.info("etf_daily 数据已存在, 跳过API拉取")
            return pd.DataFrame()

    etf_list = get_etf_list()
    logger.info(f"共 {len(etf_list)} 只ETF")

    df = build_etf_daily(etf_list, start_date, end_date, max_etfs, only_codes)
    if df.empty:
        logger.warning("未获取到任何数据")
        return df

    # 仅保留交易日, 跳过周末与中国法定节假日
    from trade_calendar import is_trade_date
    n_before = len(df)
    df = df[df[COL_DATE].map(is_trade_date)].reset_index(drop=True)
    if df.empty:
        logger.warning("过滤后无交易日数据")
        return df
    logger.info(f"交易日过滤: {n_before} -> {len(df)} 条")

    try:
        from db_handler import save_etf_daily_to_db
        save_etf_daily_to_db(df)
    except Exception as e:
        logger.error(f"MySQL 写入失败: {e}")
    logger.info(f"完成: {len(df)} 条记录, "
                f"{df[COL_CODE].nunique()} 只ETF, "
                f"日期 {df[COL_DATE].min()} ~ {df[COL_DATE].max()}")
    return df


if __name__ == "__main__":
    run(start_date='20260101',end_date='20260731')
