"""交易日历工具: 跳过周末与中国法定节假日。

数据来源: 新浪交易日历 (akshare tool_trade_date_hist_sina), 已内置法定节假日安排。
"""
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

_calendar: Optional[set] = None
_calendar_year: Optional[int] = None


def _load_calendar() -> set:
    """加载 (并缓存) 全部交易日集合, 格式 YYYY-MM-DD。"""
    global _calendar, _calendar_year
    year = date.today().year
    if _calendar is None or _calendar_year != year:
        try:
            import akshare as ak
            df = ak.tool_trade_date_hist_sina()
            _calendar = set(str(d) for d in df["trade_date"].tolist())
            _calendar_year = year
        except Exception:
            _calendar = None
    return _calendar or set()


def is_trade_date(day) -> bool:
    """判断某日期是否为交易日 (非周末且非法定节假日)。"""
    s = pd.Timestamp(day).strftime("%Y-%m-%d")
    return s in _load_calendar()


def get_trade_dates(start_date, end_date) -> list:
    """返回 [start_date, end_date] 区间内所有交易日 (含端点)。"""
    s = pd.Timestamp(start_date)
    e = pd.Timestamp(end_date)
    if s > e:
        return []
    cal = _load_calendar()
    dates = []
    d = s
    while d <= e:
        if d.strftime("%Y-%m-%d") in cal:
            dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return dates


def latest_trade_date() -> Optional[str]:
    """最近一个交易日 (<= 今天)。"""
    today = date.today().strftime("%Y-%m-%d")
    cal = _load_calendar()
    candidates = [d for d in cal if d <= today]
    return max(candidates) if candidates else None
