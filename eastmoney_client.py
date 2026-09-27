"""东方财富免费公开接口客户端。

统一封装节流与退避重试: 东财对高频请求会临时断连 (RemoteDisconnected),
全局节流 + 失败指数退避可规避。仅用公开行情接口, 无需 token。
"""
import threading
import time

import pandas as pd
import requests
from loguru import logger

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

_LOCK = threading.Lock()
_LAST_CALL = [0.0]
MIN_INTERVAL = 0.15          # 全局最小请求间隔(秒)
COOLDOWN_ON_CONN_ERROR = 20  # 疑似限流时首次退避秒数

# 主要指数 secid (东财: 1=沪, 0=深)
INDEX_MAP = {
    "000001": ("1.000001", "上证指数"),
    "399001": ("0.399001", "深证成指"),
    "399006": ("0.399006", "创业板指"),
    "000300": ("1.000300", "沪深300"),
    "000905": ("1.000905", "中证500"),
    "000688": ("1.000688", "科创50"),
}


def _throttle():
    with _LOCK:
        gap = time.time() - _LAST_CALL[0]
        if gap < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - gap)
        _LAST_CALL[0] = time.time()


def em_get(url: str, params: dict, tries: int = 4, timeout: int = 10):
    """带全局节流与指数退避的 GET, 返回解析后的 JSON 或 None。"""
    err = ""
    for attempt in range(tries):
        _throttle()
        try:
            r = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            err = f"HTTP {r.status_code}"
            delay = 3 * (attempt + 1)
        except (requests.ConnectionError, requests.exceptions.ProxyError):
            # 连接被切断: 大概率触发东财限流, 退避要够久
            err = "ConnectionError"
            delay = COOLDOWN_ON_CONN_ERROR * (attempt + 1)
        except requests.Timeout:
            err = "Timeout"
            delay = 5 * (attempt + 1)
        except Exception as e:
            err = type(e).__name__
            delay = 5 * (attempt + 1)
        if attempt < tries - 1:
            logger.warning(f"东财请求失败({err}), {delay}s 后重试: {url}")
            time.sleep(delay)
    logger.error(f"东财请求最终失败({err}): {url} params={params}")
    return None


def _em_num(v):
    """东财返回 '-'/'--' 表示无数据, 统一转 None; 数值原样返回。"""
    if v is None or v == "" or v == "-" or v == "--":
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def secid_of(code: str) -> str | None:
    """A股代码 -> 东财 secid (仅沪深, 北交所无资金流数据返回 None)。"""
    if code.startswith(("60", "68", "90")):
        return f"1.{code}"
    if code.startswith(("00", "30", "001", "002", "003", "301")):
        return f"0.{code}"
    return None


def fetch_index_kline(code: str, beg: str, end: str, tries: int = 4) -> pd.DataFrame:
    """指数日K线 (东财 push2his)。

    Returns: date/open/high/low/close/volume/amount/pct_chg/change/turnover
    """
    secid, name = INDEX_MAP[code]
    j = em_get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        {"secid": secid, "klt": "101", "fqt": "1", "beg": beg, "end": end,
         "fields1": "f1,f2,f3,f4,f5,f6",
         "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"},
        tries=tries,
    )
    klines = ((j or {}).get("data") or {}).get("klines") or []
    rows = []
    for line in klines:
        p = line.split(",")
        if len(p) < 11:
            continue
        # f51..f61: 日期,开盘,收盘,最高,最低,成交量,成交额,振幅,涨跌幅,涨跌额,换手
        rows.append({
            "date": p[0], "open": _em_num(p[1]), "close": _em_num(p[2]),
            "high": _em_num(p[3]), "low": _em_num(p[4]),
            "volume": int(float(p[5])) if p[5] else None,
            "amount": _em_num(p[6]), "pct_chg": _em_num(p[8]),
            "change": _em_num(p[9]), "turnover": _em_num(p[10]),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["code"] = code
    df["name"] = name
    df = df[["code", "name", "date", "open", "high", "low", "close",
             "volume", "amount", "pct_chg", "change", "turnover"]]
    return df


def _fetch_clist_all(fields: str, fs: str, fid: str = "f12",
                      po: str = "0", page_size: int = 5000,
                      tries: int = 4) -> list:
    """分页拉取 clist 全量列表, 返回 diff 行 dict 列表。"""
    out = []
    pn = 1
    while True:
        j = em_get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            {"po": po, "pz": str(page_size), "pn": str(pn), "np": "1",
             "fltt": "2", "invt": "2", "fid": fid, "fs": fs, "fields": fields},
            tries=tries,
        )
        data = (j or {}).get("data") or {}
        diff = data.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        out.extend(diff)
        total = data.get("total") or 0
        if not diff or len(out) >= total or len(diff) < page_size:
            break
        pn += 1
    return out


def fetch_stock_moneyflow_rank() -> pd.DataFrame:
    """全市场个股当日主力资金流排行 (东财 clist, 分页2次左右)。

    Returns: code/main_net_inflow/main_net_pct/super/big/mid/small
    """
    lines = _fetch_clist_all(
        fields="f12,f62,f184,f66,f72,f78,f84",
        fs="m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        fid="f62", po="1",
    )
    rows = [{
        "code": str(d.get("f12", "")),
        "main_net_inflow": _em_num(d.get("f62")),
        "main_net_pct": _em_num(d.get("f184")),
        "super_net_inflow": _em_num(d.get("f66")),
        "big_net_inflow": _em_num(d.get("f72")),
        "mid_net_inflow": _em_num(d.get("f78")),
        "small_net_inflow": _em_num(d.get("f84")),
    } for d in lines]
    return pd.DataFrame(rows)


def fetch_stock_valuation() -> pd.DataFrame:
    """全市场个股估值快照: 最新价/涨跌/成交/换手/量比/PE/PB/市值/60日涨幅。

    东财 clist 不可用 (IP被封) 时自动切换腾讯行情兜底 (tries=1 快速失败)。

    Returns: code/name/latest_price/pct_chg/price_change/volume/amount/
             high/low/open/prev_close/turnover_rate/volume_ratio/
             pe_dynamic/pb/total_mv/float_mv/pct_chg_60d
    """
    lines = _fetch_clist_all(
        fields="f12,f14,f2,f3,f4,f5,f6,f8,f9,f10,f15,f16,f17,f18,f20,f21,f23,f164",
        fs="m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        fid="f12", po="0", tries=1,
    )
    if not lines:
        logger.warning("东财估值接口不可用, 切换腾讯行情兜底")
        return fetch_stock_valuation_tencent()
    rows = []
    for d in lines:
        high = _em_num(d.get("f15"))
        low = _em_num(d.get("f16"))
        prev_close = _em_num(d.get("f18"))
        amplitude = None
        if high is not None and low is not None and prev_close:
            amplitude = round((high - low) / prev_close * 100, 2)
        rows.append({
            "code": str(d.get("f12", "")),
            "name": str(d.get("f14", "")),
            "latest_price": _em_num(d.get("f2")),
            "pct_chg": _em_num(d.get("f3")),
            "price_change": _em_num(d.get("f4")),
            "volume": int(_em_num(d.get("f5"))) if _em_num(d.get("f5")) is not None else None,
            "amount": _em_num(d.get("f6")),
            "amplitude": amplitude,
            "high": high, "low": low, "open": _em_num(d.get("f17")),
            "prev_close": prev_close,
            "volume_ratio": _em_num(d.get("f10")),
            "turnover_rate": _em_num(d.get("f8")),
            "pe_dynamic": _em_num(d.get("f9")),
            "pb": _em_num(d.get("f23")),
            "total_mv": _em_num(d.get("f20")),
            "float_mv": _em_num(d.get("f21")),
            "pct_chg_60d": _em_num(d.get("f164")),
        })
    return pd.DataFrame(rows)


def _tencent_symbol(code: str) -> str | None:
    """A股代码 -> 腾讯行情符号 (sh/sz/bj 前缀)。"""
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    if code.startswith(("4", "8", "9")):
        return f"bj{code}"
    return None


def _pct60_from_db() -> dict:
    """近60交易日涨幅 (由 stock_daily 计算, 腾讯行情无此字段)。"""
    try:
        from db_handler import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT code, MAX(CASE WHEN rn=1 THEN close END),
                   MAX(CASE WHEN rn=61 THEN close END)
            FROM (SELECT code, close, ROW_NUMBER() OVER
                      (PARTITION BY code ORDER BY `date` DESC) rn
                  FROM stock_daily WHERE `date` >= '2025-08-01') t
            GROUP BY code""")
        out = {}
        for code, c1, c0 in cur.fetchall():
            if c1 and c0:
                out[str(code)] = round((float(c1) / float(c0) - 1) * 100, 2)
        cur.close()
        conn.close()
        return out
    except Exception as e:
        logger.warning(f"pct_chg_60d 计算失败: {e}")
        return {}


def fetch_stock_valuation_tencent() -> pd.DataFrame:
    """腾讯行情兜底: 估值快照 PE/PB/市值/换手等 (东财被封时使用)。

    字段映射: 3=最新价 32=涨跌% 31=涨跌额 36=量(手) 37=额(万元) 43=振幅
              33/34/5=高/低/开 4=昨收 49=量比 38=换手 39=PE 46=PB
              45/44=总/流通市值(亿)
    """
    from db_handler import get_connection
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""SELECT code FROM (SELECT code, MAX(`date`) m FROM stock_daily
                   GROUP BY code) t ORDER BY code""")
    codes = [str(r[0]) for r in cur.fetchall()]
    cur.close()
    conn.close()

    rows = []
    batch = 60
    for i in range(0, len(codes), batch):
        cmap = {}
        syms = []
        for c in codes[i:i + batch]:
            s = _tencent_symbol(c)
            if s:
                cmap[s] = c
                syms.append(s)
        if not syms:
            continue
        try:
            r = requests.get(
                "https://qt.gtimg.cn/q=" + ",".join(syms),
                headers={"User-Agent": "Mozilla/5.0",
                         "Referer": "https://gu.qq.com/"},
                timeout=10)
            txt = r.content.decode("gbk", "ignore")
        except Exception as e:
            logger.warning(f"腾讯行情批次失败: {e}")
            continue

        def num(f, idx):
            if idx >= len(f):
                return None
            return _em_num(f[idx])

        for piece in txt.split(";"):
            piece = piece.strip()
            if "=" not in piece or "~" not in piece:
                continue
            sym = piece.split("=", 1)[0].split("_", 1)[-1]
            code = cmap.get(sym)
            if not code:
                continue
            f = piece.split("~")
            if len(f) < 50:
                continue
            vol = num(f, 36)
            amt = num(f, 37)
            price = num(f, 3)
            rows.append({
                "code": code,
                "name": f[1] if len(f) > 1 else "",
                "latest_price": price if price else None,
                "pct_chg": num(f, 32),
                "price_change": num(f, 31),
                "volume": int(vol) if vol is not None else None,
                "amount": round(amt * 10000, 2) if amt is not None else None,
                "amplitude": num(f, 43),
                "high": num(f, 33), "low": num(f, 34), "open": num(f, 5),
                "prev_close": num(f, 4),
                "volume_ratio": num(f, 49),
                "turnover_rate": num(f, 38),
                "pe_dynamic": num(f, 39),
                "pb": num(f, 46),
                "total_mv": num(f, 45),
                "float_mv": num(f, 44),
                "pct_chg_60d": None,
            })
        time.sleep(0.06)

    df = pd.DataFrame(rows)
    if not df.empty:
        df["pct_chg_60d"] = df["code"].map(_pct60_from_db())
        df.loc[df["latest_price"] == 0, "latest_price"] = None
    logger.info(f"腾讯估值兜底: {len(df)} 只")
    return df


def _flow_line_to_row(line: str, code: str) -> dict | None:
    """个股资金流历史单行 -> dict。

    f51..: 日期,主力净流入,小单净流入,中单净流入,大单净流入,超大单净流入,
           主力净占比%,小单占比,中单占比,大单占比,超大单占比,收盘,涨跌幅,...
    """
    p = line.split(",")
    if len(p) < 7:
        return None
    return {
        "code": code,
        "date": p[0],
        "main_net_inflow": _em_num(p[1]),
        "main_net_pct": _em_num(p[6]),
        "super_net_inflow": _em_num(p[5]),
        "big_net_inflow": _em_num(p[4]),
        "mid_net_inflow": _em_num(p[3]),
        "small_net_inflow": _em_num(p[2]),
    }


def fetch_stock_moneyflow_history(code: str, lmt: int = 30) -> pd.DataFrame:
    """单只个股资金流历史日线 (东财 push2 fflow/kline)。"""
    secid = secid_of(code)
    if not secid:
        return pd.DataFrame()
    j = em_get(
        "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
        {"secid": secid, "fields1": "f1,f2,f3,f7",
         "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
         "klt": "101", "lmt": str(lmt)},
    )
    klines = ((j or {}).get("data") or {}).get("klines") or []
    rows = [r for r in (_flow_line_to_row(l, code) for l in klines) if r]
    return pd.DataFrame(rows)


def fetch_sector_moneyflow_history(bk_code: str, lmt: int = 30) -> pd.DataFrame:
    """东财行业板块资金流历史日线 (secid=90.BKxxxx, 用于板块净流入)。"""
    j = em_get(
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
        {"secid": f"90.{bk_code}", "fields1": "f1,f2,f3,f7",
         "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
         "klt": "101", "lmt": str(lmt)},
    )
    klines = ((j or {}).get("data") or {}).get("klines") or []
    rows = []
    for line in klines:
        p = line.split(",")
        if len(p) < 7:
            continue
        rows.append({"date": p[0], "main_net_inflow": _em_num(p[1])})
    df = pd.DataFrame(rows)
    if not df.empty:
        df["bk_code"] = bk_code
    return df


def parallel_fetch(fetch_fn, items: list, workers: int = 4,
                   desc: str = "fetch") -> list:
    """多线程抓取, 全局节流在 em_get 内生效; 返回 [(item, result), ...]。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    total = len(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_fn, it): it for it in items}
        done = 0
        for f in as_completed(futures):
            it = futures[f]
            try:
                results.append((it, f.result()))
            except Exception as e:
                logger.warning(f"{desc} 失败 {it}: {e}")
                results.append((it, None))
            done += 1
            if done % 200 == 0:
                logger.info(f"  {desc}: {done}/{total}")
    return results
