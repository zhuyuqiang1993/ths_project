"""同花顺 (10jqka) 数据接口封装: 全量A股列表/实时行情、个股/指数/板块 K线。

统一通过 hexin-v 签名访问 d.10jqka.com.cn 与 data.10jqka.com.cn。
"""
import os
import re
import time
from io import StringIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import pandas as pd
import requests

from config import CONFIG

_JS_PATH_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ths.js"),
    os.path.join(os.path.dirname(__import__("akshare").__file__), "stock_feature", "ths.js"),
]


def get_v_code() -> str:
    """生成同花顺 hexin-v 签名 (单次有效)。"""
    from py_mini_racer import MiniRacer
    js_path = next((p for p in _JS_PATH_CANDIDATES if os.path.exists(p)), None)
    if js_path is None:
        raise FileNotFoundError("找不到 ths.js, 请放置到项目目录或安装 akshare")
    ctx = MiniRacer()
    with open(js_path, "r", encoding="utf-8") as f:
        ctx.eval(f.read())
    return ctx.call("v")


def _session(v_code: str) -> requests.Session:
    s = requests.Session()
    s.verify = False
    s.headers.update({
        "Accept": "text/html, */*; q=0.01",
        "Accept-Encoding": "gzip, deflate",
        "hexin-v": v_code,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
    })
    return s


# ================= 全量A股列表 + 实时行情 =================

def fetch_ths_stock_spot(pages: Optional[int] = None) -> pd.DataFrame:
    """通过 data.10jqka.com.cn 个股资金流排行页获取全量A股列表及实时行情。

    返回字段: code/name/latest_price/pct_chg/turnover_rate/amount (净流入字段忽略)。
    默认抓取全部页 (约 104 页 * 100 只)。
    """
    v = get_v_code()
    s = _session(v)
    s.headers["Referer"] = "http://data.10jqka.com.cn/funds/ggzjl/"

    base = "http://data.10jqka.com.cn/funds/ggzjl/field/zdf/order/desc/page/{}/ajax/1/free/1/"
    all_rows = []
    page = 1
    consecutive_errors = 0
    while True:
        try:
            r = s.get(base.format(page), timeout=15)
            r.encoding = "gbk"
            tables = pd.read_html(StringIO(r.text))
            if not tables:
                break
            df = tables[0]
            if df.empty or "股票代码" not in df.columns:
                break
            df = df[["股票代码", "股票简称", "最新价", "涨跌幅", "换手率", "成交额(元)"]].copy()
            df.columns = ["code", "name", "latest_price", "pct_chg", "turnover_rate", "amount"]
            all_rows.append(df)
            consecutive_errors = 0
        except Exception:
            # hexin-v 签名在约 17 次请求后失效, 刷新后重试当前页
            consecutive_errors += 1
            if consecutive_errors > 3:
                break
            v = get_v_code()
            s.headers["hexin-v"] = v
            time.sleep(0.5)
            continue
        if pages and page >= pages:
            break
        # 末页行数不足即结束 (每页满 50 行)
        if len(df) < 50:
            break
        page += 1
        time.sleep(0.3)

    if not all_rows:
        return pd.DataFrame()
    result = pd.concat(all_rows, ignore_index=True)
    result["code"] = result["code"].astype(str).str.zfill(6)
    for col in ["latest_price"]:
        result[col] = pd.to_numeric(result[col], errors="coerce")
    for col in ["pct_chg", "turnover_rate"]:
        result[col] = result[col].astype(str).str.replace("%", "", regex=False)
        result[col] = pd.to_numeric(result[col], errors="coerce")
    result["amount"] = result["amount"].apply(_parse_zh_amount)
    result = result.drop_duplicates(subset="code").reset_index(drop=True)
    return result


def _parse_zh_amount(value) -> float:
    """将 1.23亿 / 456.7万 等中文金额换算为元。"""
    if value is None:
        return 0.0
    s = str(value).strip().replace(",", "")
    m = re.match(r"^([-\d.]+)\s*(亿|万)?$", s)
    if not m:
        return 0.0
    num = float(m.group(1))
    unit = m.group(2)
    if unit == "亿":
        return num * 100_000_000
    if unit == "万":
        return num * 10_000
    return num


def get_stock_list() -> pd.DataFrame:
    """A股代码/名称列表 (同花顺), 仅保留沪深主板/创业板/科创板 (0/3/6 开头)。"""
    spot = fetch_ths_stock_spot()
    if spot.empty:
        return pd.DataFrame()
    df = spot[["code", "name"]].copy()
    df = df[df["code"].str.match(r"^(0|3|6)\d{5}$")].reset_index(drop=True)
    return df


# ================= K线 (个股/指数/板块) =================

def fetch_ths_kline(
    code: str,
    start_year: int = 2020,
    end_year: Optional[int] = None,
    prefix: str = "hs_",
) -> Optional[pd.DataFrame]:
    """抓取同花顺 v4 K线, 返回 date/open/high/low/close/volume/amount。"""
    end_year = end_year or int(time.strftime("%Y"))
    v = get_v_code()
    s = _session(v)
    s.headers["Referer"] = "https://www.10jqka.com.cn"

    all_rows = []
    for year in range(start_year, end_year + 1):
        url = f"https://d.10jqka.com.cn/v4/line/{prefix}{code}/01/{year}.js"
        try:
            r = s.get(url, cookies={"v": v}, timeout=10)
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
    df = pd.DataFrame(all_rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    return df


def fetch_ths_kline_batch(codes: list, start_year: int = 2020) -> dict:
    """并发抓取多只 K线, 返回 {code: DataFrame}。"""
    results = {}
    v = get_v_code()
    s = _session(v)
    s.headers["Referer"] = "https://www.10jqka.com.cn"

    def _one(code: str):
        all_rows = []
        end_year = int(time.strftime("%Y"))
        for year in range(start_year, end_year + 1):
            url = f"https://d.10jqka.com.cn/v4/line/hs_{code}/01/{year}.js"
            try:
                r = s.get(url, cookies={"v": v}, timeout=10)
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
        if not all_rows:
            return code, None
        df = pd.DataFrame(all_rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
        return code, df

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(_one, c) for c in codes]
        for f in as_completed(futures):
            code, df = f.result()
            if df is not None:
                results[code] = df
    return results


# ================= 指数 =================

# 同花顺指数代码映射 (v4/v6 line 前缀)
INDEX_CODE_MAP = {
    "上证指数": "hs_1A0001",
    "深证成指": "hs_399001",
    "创业板指": "hs_399006",
    "科创50": "hs_1B0688",
    "沪深300": "hs_399300",
}


def fetch_index_spot(names: Optional[list] = None) -> dict:
    """获取同花顺指数当日行情 (最新/开盘/最高/最低/昨收/成交量/成交额/涨跌幅)。

    返回 {name: {price, pct_chg, change, open, high, low, prev_close, ...}}。
    基于 v6 today.js 实时接口, 字段: 7=开盘 8=最高 9=最低 11=最新/收盘
    13=成交量 19=成交额 1968584=涨跌幅(%)。
    """
    import json
    names = names or list(INDEX_CODE_MAP.keys())
    v = get_v_code()
    s = _session(v)
    s.headers["Referer"] = "https://www.10jqka.com.cn"

    result = {}
    for name in names:
        code = INDEX_CODE_MAP.get(name)
        if not code:
            continue
        try:
            r = s.get(f"https://d.10jqka.com.cn/v6/line/{code}/01/today.js",
                      cookies={"v": v}, timeout=10)
            payload = r.text[r.text.find("{"):]
            payload = payload[:payload.rfind("}") + 1]
            data = json.loads(payload)
            d = next(iter(data.values()))
            price = float(d.get("11", 0) or 0)
            open_ = float(d.get("7", 0) or 0)
            high = float(d.get("8", 0) or 0)
            low = float(d.get("9", 0) or 0)
            volume = float(d.get("13", 0) or 0)
            amount = float(d.get("19", 0) or 0)
            pct = float(d.get("1968584", 0) or 0)
            prev_close = round(price / (1 + pct / 100), 2) if pct != -100 and price else 0
            change = round(price - prev_close, 2) if price else 0
            result[name] = {
                "price": price, "pct_chg": pct, "change": change,
                "open": open_, "high": high, "low": low, "prev_close": prev_close,
                "date": d.get("1", ""),
            }
        except Exception:
            continue
        time.sleep(0.2)
    return result
