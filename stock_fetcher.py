import time
from datetime import datetime, date
from typing import Optional

import akshare as ak
import pandas as pd

from config import CONFIG


def get_stock_list() -> pd.DataFrame:
    df = ak.stock_zh_a_spot_em()
    cols_mapping = {
        "代码": "code",
        "名称": "name",
        "最新价": "latest_price",
        "涨跌幅": "pct_chg",
        "涨跌额": "price_change",
        "成交量": "volume",
        "成交额": "amount",
        "振幅": "amplitude",
        "最高": "high",
        "最低": "low",
        "今开": "open",
        "昨收": "prev_close",
        "量比": "volume_ratio",
        "换手率": "turnover_rate",
        "市盈率-动态": "pe_dynamic",
        "市净率": "pb",
        "总市值": "total_mv",
        "流通市值": "float_mv",
        "60日涨跌幅": "pct_chg_60d",
        "5分钟涨跌": "pct_chg_5m",
    }
    df.rename(columns=cols_mapping, inplace=True)
    df = df[[c for c in cols_mapping.values() if c in df.columns]]
    if CONFIG.exclude_stock_codes:
        df = df[~df["code"].isin(CONFIG.exclude_stock_codes)]
    df.to_csv(CONFIG.stock_list_path, index=False, encoding=CONFIG.csv_encoding)
    return df


FULL_FIELD_MAP = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "振幅": "amplitude",
    "涨跌幅": "pct_chg",
    "涨跌额": "price_change",
    "换手率": "turnover_rate",
}


def fetch_stock_daily(
    code: str,
    start_date: str = "",
    end_date: str = "",
) -> Optional[pd.DataFrame]:
    start = start_date or CONFIG.start_date
    end = end_date or CONFIG.end_date or datetime.now().strftime("%Y%m%d")

    for attempt in range(CONFIG.retry_times):
        try:
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start,
                end_date=end,
                adjust="qfq",
            )
            if df is None or df.empty:
                return None
            df.rename(columns=FULL_FIELD_MAP, inplace=True)
            df["code"] = code
            cols = ["code", "date"] + [
                c for c in FULL_FIELD_MAP.values() if c in df.columns
            ]
            df = df[[c for c in cols if c in df.columns]]
            return df
        except Exception as e:
            if attempt < CONFIG.retry_times - 1:
                time.sleep(CONFIG.retry_delay)
                continue
            raise e


def save_stock_data(df: pd.DataFrame, code: str):
    file_path = CONFIG.data_dir / f"{code}.csv"
    if file_path.exists():
        existing = pd.read_csv(file_path, encoding=CONFIG.csv_encoding, dtype={"code": str})
        existing["date"] = pd.to_datetime(existing["date"]).dt.strftime("%Y-%m-%d")
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        combined = pd.concat([existing, df], ignore_index=True)
        combined.drop_duplicates(subset=["code", "date"], keep="last", inplace=True)
        combined.sort_values("date", inplace=True)
        combined.to_csv(file_path, index=False, encoding=CONFIG.csv_encoding)
    else:
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        df.sort_values("date", inplace=True)
        df.to_csv(file_path, index=False, encoding=CONFIG.csv_encoding)
