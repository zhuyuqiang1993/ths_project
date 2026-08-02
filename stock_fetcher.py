import time
from datetime import datetime, date
from typing import Optional

import pandas as pd

from config import CONFIG


def get_stock_list() -> pd.DataFrame:
    from ths_client import fetch_ths_stock_spot
    df = fetch_ths_stock_spot()
    if df.empty:
        return pd.DataFrame()
    df = df[df["code"].str.match(r"^(0|3|6)\d{5}$")].reset_index(drop=True)
    if CONFIG.exclude_stock_codes:
        df = df[~df["code"].isin(CONFIG.exclude_stock_codes)]
    return df


FULL_FIELD_MAP = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
}


def fetch_stock_daily(
    code: str,
    start_date: str = "",
    end_date: str = "",
) -> Optional[pd.DataFrame]:
    start = start_date or CONFIG.start_date
    end = end_date or CONFIG.end_date or datetime.now().strftime("%Y%m%d")

    start_year = max(int(start[:4]) if len(start) >= 4 else 2020, 2020)
    end_year = int(end[:4]) if len(end) >= 4 else datetime.now().year

    for attempt in range(CONFIG.retry_times):
        try:
            from ths_client import fetch_ths_kline
            df = fetch_ths_kline(code, start_year=start_year, end_year=end_year, prefix="hs_")
            if df is None or df.empty:
                return None
            df = df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]
            if df.empty:
                return None
            df["date"] = df["date"].dt.strftime("%Y-%m-%d")
            df["code"] = code
            keep = ["code", "date"] + [c for c in FULL_FIELD_MAP.values() if c != "date"]
            df = df[[c for c in keep if c in df.columns]]
            return df
        except Exception as e:
            if attempt < CONFIG.retry_times - 1:
                time.sleep(CONFIG.retry_delay)
                continue
            raise e
