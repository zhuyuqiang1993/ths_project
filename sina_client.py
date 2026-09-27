"""新浪财经客户端: 历史资金流兜底 (东财 push2 被封时使用)。

接口: MoneyFlow.ssl_qsfx_lscjfb?daima=sh600519&num=260&page=N
- 分页按时间升序, num=6000 可拿全历史 (2010-03 至今); page 参数定位末页附近
- 字段: opendate/netamount/ratioamount/r0_net..r3_net
  r0=超大单 r1=大单 r2=中单 r3=小单 (净额 r0_net..r3_net, 单位元)
  主力 = r0_net + r1_net (与东财 f62 = f66+f72 口径一致)
  amount = netamount / ratioamount (与本地 stock_daily.amount 校验一致)
"""
import threading
import time

import pandas as pd
import requests
from loguru import logger

SINA_MF_URL = ("https://money.finance.sina.com.cn/quotes_service/api/"
               "json_v2.php/MoneyFlow.ssl_qsfx_lscjfb")
_HEADERS = {"User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/"}
_MIN_INTERVAL = 0.05
_lock = threading.Lock()
_last_ts = [0.0]


def _throttle():
    with _lock:
        gap = time.time() - _last_ts[0]
        if gap < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - gap)
        _last_ts[0] = time.time()


def sina_symbol(code: str) -> str | None:
    """A股代码 -> 新浪资金流符号。"""
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    if code.startswith(("4", "8", "9")):
        return f"bj{code}"
    return None


def _page_guess(code: str) -> int:
    """按代码前缀估计末页页码 (num=260, 新浪数据始于 2010-03, 上限约4020行=16页)。"""
    if code.startswith("603"):
        return 12
    if code.startswith(("605", "688", "689")):
        return 7
    if code.startswith("301"):
        return 6
    if code.startswith("001"):
        return 10
    if code.startswith("003"):
        return 4
    if code.startswith(("4", "8", "9")):
        return 5
    return 16


def _fetch_page(symbol: str, page: int, num: int = 260):
    _throttle()
    try:
        r = requests.get(SINA_MF_URL,
                         params={"daima": symbol, "num": num, "page": page},
                         headers=_HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        return data if isinstance(data, list) else None
    except Exception:
        return None


def _to_row(code: str, d: dict) -> dict | None:
    try:
        netamount = float(d.get("netamount") or 0)
        ratio = float(d.get("ratioamount") or 0)
        r0 = float(d.get("r0_net") or 0)
        r1 = float(d.get("r1_net") or 0)
        r2 = float(d.get("r2_net") or 0)
        r3 = float(d.get("r3_net") or 0)
    except (TypeError, ValueError):
        return None
    main = r0 + r1
    pct = None
    if ratio:
        amount = netamount / ratio
        if amount:
            pct = round(main / amount * 100, 4)
    return {
        "code": code,
        "date": d.get("opendate"),
        "main_net_inflow": round(main, 2),
        "main_net_pct": pct,
        "super_net_inflow": round(r0, 2),
        "big_net_inflow": round(r1, 2),
        "mid_net_inflow": round(r2, 2),
        "small_net_inflow": round(r3, 2),
    }


def fetch_moneyflow_history(code: str, start: str, end: str,
                            expected: int = 0, max_probe: int = 16) -> pd.DataFrame:
    """单只个股历史资金流 (新浪), 覆盖 [start, end] (YYYY-MM-DD)。

    expected: 本地 stock_daily 在该区间的行数, 用于判断分页是否覆盖完整。
    跨迭代累积命中行, 页码估计偏差时逐步修正。
    """
    symbol = sina_symbol(code)
    if not symbol:
        return pd.DataFrame()
    page = _page_guess(code)
    best = {}

    for _ in range(max_probe):
        if page < 1:
            break
        d1 = _fetch_page(symbol, page)
        if d1 is None:
            d1 = _fetch_page(symbol, page)
        d2 = _fetch_page(symbol, page + 1)
        if d2 is None:
            d2 = _fetch_page(symbol, page + 1)

        if not d1 and not d2:
            page = max(1, page - 3)
            if page == 1 and best:
                break
            continue

        merged = {}
        for d in (d1 or []) + (d2 or []):
            od = d.get("opendate")
            if od:
                merged[od] = d
        if not merged:
            page += 1
            continue

        dates = sorted(merged)
        if dates[-1] < start:
            page += 1
            continue
        if dates[0] > end:
            page -= 1
            continue

        win = {k: v for k, v in merged.items() if start <= k <= end}
        if not win:
            page += 1
            continue

        best.update(win)
        need = expected if expected else len(best)
        if len(best) >= max(need - 3, int(need * 0.95)):
            break
        if min(dates) > start:
            page -= 1
        else:
            page += 1

    if not best:
        return pd.DataFrame()
    rows = [r for r in (_to_row(code, d) for d in best.values()) if r]
    return pd.DataFrame(rows) if rows else pd.DataFrame()
