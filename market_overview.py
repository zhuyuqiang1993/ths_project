import os
import re
import ssl
import sys
from datetime import datetime

os.environ["HTTP_PROXY"] = "http://127.0.0.1:7892"
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7892"
os.environ["NO_PROXY"] = "127.0.0.1,localhost,.10jqka.com.cn,qt.gtimg.cn"

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

import time
import akshare as ak
import pandas as pd
import requests
from loguru import logger
from openai import OpenAI

from config import CONFIG

logger.remove()
logger.add(
    sys.stderr,
    level=CONFIG.log_level,
    format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}",
)
logger.add(
    CONFIG.log_dir / "market_overview_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
)

KEY_SECTORS = [
    "半导体", "金融", "医药生物", "新能源", "消费",
    "有色金属", "房地产", "计算机", "机械设备", "电力",
    "国防军工", "汽车", "食品饮料", "电子", "通信",
]

# 全球主要指数 (新浪财经实时行情; 恒生指数由同花顺获取)
GLOBAL_INDEX_MAP = {
    "日经225": ("int_nikkei", "日本"),
    "韩国KOSPI": ("b_KOSPI", "韩国"),
    "道琼斯": ("int_dji", "美国"),
    "纳斯达克": ("int_nasdaq", "美国"),
    "标普500": ("int_sp500", "美国"),
}


def fetch_global_indices() -> dict:
    """获取全球主要指数实时行情。

    混合数据源:
      - 恒生指数: 同花顺 hexin-v 接口 (hk_HSI), 与项目其他行情一致
      - 日经225/韩国KOSPI/美股三大指数: 新浪 hq.sinajs.cn
    返回 {name: {price, pct_chg, change, market}}。失败项自动跳过。
    """
    result = {}

    # 1) 恒生指数 (同花顺)
    try:
        from ths_client import get_v_code, _session
        v = get_v_code()
        s = _session(v)
        s.headers["Referer"] = "https://www.10jqka.com.cn"
        r = s.get("http://d.10jqka.com.cn/v2/realhead/hk_HSI/last.js",
                  cookies={"v": v}, timeout=10)
        import json
        payload = r.text[r.text.find("{"):r.text.rfind("}") + 1]
        items = json.loads(payload).get("items", {})
        price = float(items.get("10", 0) or 0)
        pct = float(items.get("199112", 0) or 0)
        change = float(items.get("264648", 0) or 0)
        if price:
            result["恒生指数"] = {
                "price": price, "pct_chg": pct, "change": change,
                "market": "中国香港",
            }
    except Exception as e:
        logger.warning(f"同花顺恒生指数获取失败: {e}")

    # 2) 其余全球指数 (新浪)
    codes = [v[0] for v in GLOBAL_INDEX_MAP.values()]
    url = "http://hq.sinajs.cn/list=" + ",".join(codes)
    try:
        import requests
        # 新浪为境内接口, 临时移除代理保证直连
        old_env = {}
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            if k in os.environ:
                old_env[k] = os.environ.pop(k)
        try:
            r = requests.get(url, headers={"Referer": "https://finance.sina.com.cn"}, timeout=10)
        finally:
            os.environ.update(old_env)
        r.encoding = "gbk"
        for line in r.text.splitlines():
            m = re.search(r'var hq_str_(\w+)="([^"]*)"', line.strip())
            if not m:
                continue
            code, payload = m.group(1), m.group(2)
            name = next((n for n, (c, _) in GLOBAL_INDEX_MAP.items() if c == code), None)
            if not name or not payload:
                continue
            fields = payload.split(",")
            try:
                price = float(fields[1])
                pct = float(fields[3])
                change = float(fields[2])
            except (IndexError, ValueError):
                continue
            result[name] = {
                "price": price, "pct_chg": pct, "change": change,
                "market": GLOBAL_INDEX_MAP[name][1],
            }
    except Exception as e:
        logger.warning(f"全球指数行情获取失败: {e}")
    return result


def fetch_indices() -> dict:
    """获取主要指数当日行情 (同花顺 v6 today.js 实时接口)。"""
    try:
        from ths_client import fetch_index_spot
        result = fetch_index_spot()
        return result
    except Exception as e:
        logger.warning(f"同花顺指数行情获取失败: {e}")
        return {}


def fetch_sector_performance() -> dict:
    try:
        today = datetime.now().strftime("%Y%m%d")
        rows = []
        for name in KEY_SECTORS:
            try:
                df = ak.stock_board_industry_index_ths(
                    symbol=name, start_date=today, end_date=today
                )
                if df is not None and not df.empty:
                    row = df.iloc[-1]
                    pct = float(row.get("pct_chg", row.get("涨跌幅", 0)))
                    rows.append({"name": name, "pct_chg": pct})
                else:
                    # 回退到最近5日获取最新一条
                    df2 = ak.stock_board_industry_index_ths(
                        symbol=name, start_date="20260725", end_date=today
                    )
                    if df2 is not None and not df2.empty:
                        row = df2.iloc[-1]
                        pct = float(row.get("pct_chg", row.get("涨跌幅", 0)))
                        rows.append({"name": name, "pct_chg": pct})
            except Exception:
                pass
            time.sleep(0.3)
        rows.sort(key=lambda x: x["pct_chg"], reverse=True)
        return {
            "leading": [r for r in rows[:5]],
            "lagging": [r for r in rows[-5:]],
        }
    except Exception as e:
        logger.warning(f"板块数据获取失败: {e}")
        return {}


def build_prompt(index_data: dict, sector_data: dict, global_indices: dict | None = None) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    global_indices = global_indices or {}

    idx_lines = []
    for name, d in index_data.items():
        pct = d.get("pct_chg", 0)
        emoji = "🔴" if pct < 0 else "🟢"
        idx_lines.append(
            f"  {emoji} {name}: {d['price']:.2f}  ({pct:+.2f}%)"
        )

    global_lines = []
    for name, d in global_indices.items():
        pct = d.get("pct_chg", 0)
        emoji = "🔴" if pct < 0 else "🟢"
        global_lines.append(
            f"  {emoji} {name}: {d['price']:.2f}  ({pct:+.2f}%)"
        )

    sector_lines = []
    if sector_data.get("leading"):
        sector_lines.append("\n**A股主要行业板块表现:**")
        all_sectors = sector_data["leading"][::-1] + sector_data.get("lagging", [])[::-1]
        for r in all_sectors:
            emoji = "🔴" if r["pct_chg"] < 0 else "🟢"
            sector_lines.append(f"  {emoji} {r['name']}: {r['pct_chg']:+.2f}%")

    global_block = ""
    if global_lines:
        global_block = f"""\n### 全球主要指数 (新浪实时行情, 必须照搬以下数值, 禁止编造/推测)
{chr(10).join(global_lines)}"""

    prompt = f"""你是资深全球股市分析师。今天是 {today}，请基于以下市场数据，并**主动使用联网搜索**补充各市场最新消息、外围事件及期货走势，生成中日韩美四国股市综合概览。

## 实时市场数据

### 主要指数 (A股)
{chr(10).join(idx_lines) if idx_lines else "（数据暂缺，请联网获取）"}
{global_block}
### A股板块
{chr(10).join(sector_lines) if sector_lines else ""}

## 输出格式要求

---
## 📊 全球股市概览 ({today})

### 一、市场情绪
- **中国 A 股**: [整体情绪：偏多/偏空/震荡 + 简要理由]
- **日本股市**: [同上]
- **韩国股市**: [同上]
- **美国股市**: [同上]

### 二、大类板块表现
#### 中国 A 股
- [板块] ±x% — 简析
#### 日本股市
- [主要行业] ±x% — 简析（联网获取）
#### 韩国股市
- [主要行业] ±x% — 简析（联网获取）
#### 美国股市
- [板块] ±x% — 简析（联网获取）

### 三、风险提示
- [风险点]
- [风险点]
- [风险点]
（含宏观、地缘、政策、汇率等维度）

### 四、下一个交易日预期
- **中国 A 股**: [方向判断 / 关键位 / 逻辑]
- **日本股市**: [同上]
- **韩国股市**: [同上]
- **美国股市**: [同上]
---

**重要约束**：
1. 中日韩美各国指数点位与涨跌幅，**必须以上方提供的实时数据为准**，不得使用联网搜索的数值，不得编造。
2. 若某市场指数数据未提供，须明确标注"数据暂缺"，不得虚构点位。
3. 每个市场情绪判断需与给出的指数涨跌幅保持一致（如指数大涨不能判为偏空）。
4. 确保每条结论都有数据支撑，不空泛。"""
    return prompt


def generate_report(prompt: str) -> str | None:
    if not CONFIG.deepseek_api_key:
        logger.error("请在 config.py 中配置 deepseek_api_key")
        return None

    # DeepSeek 为境外接口, 需清理模块顶层设置的代理保证直连
    old_env = {}
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if k in os.environ:
            old_env[k] = os.environ.pop(k)
    try:
        client = OpenAI(api_key=CONFIG.deepseek_api_key, base_url=CONFIG.deepseek_base_url)
        logger.info("正在调用 DeepSeek (联网搜索模式)...")
        try:
            resp = client.chat.completions.create(
                model=CONFIG.deepseek_model,
                messages=[
                    {"role": "system", "content": "你是一位专业、严谨的全球股市分析师，输出结构化中文报告，结论需有数据支撑。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                timeout=120,
                extra_body={"enable_search": True},
            )
            content = resp.choices[0].message.content
            logger.info("报告生成完成")
            return content
        except Exception as e:
            logger.error(f"API 调用失败: {e}")
            return None
    finally:
        os.environ.update(old_env)


def _anchor_lines(index_data: dict, global_indices: dict) -> list:
    """汇总所有真实指数数据为文本行 (用于后验校验的锚定数据)。"""
    lines = []
    for name, d in {**index_data, **global_indices}.items():
        pct = d.get("pct_chg", 0)
        lines.append(f"  {name}: {d['price']:.2f} ({pct:+.2f}%)")
    return lines


def verify_report(report: str, index_data: dict, global_indices: dict | None = None) -> str:
    """后验校验: 用真实指数数据二次核对报告, 修正/删除编造的指数点位与涨跌幅。

    若校验失败则原样返回初稿, 不影响主流程。
    """
    if not report:
        return report
    global_indices = global_indices or {}
    anchor = "\n".join(_anchor_lines(index_data, global_indices))
    if not anchor.strip():
        return report

    verify_prompt = f"""下面是由数据接口抓取的【真实指数行情】（截至当前时间，权威可信，作为唯一事实依据）：

{anchor}

下面是一篇 AI 生成的《全球股市概览》初稿：

---
{report}
---

请对初稿做【后验校验】，仅依据上面提供的真实指数行情逐项核对，并输出修正后的完整报告。要求：

1. 初稿中凡是涉及上述指数【点位】或【涨跌幅】的表述，必须与真实行情完全一致；不一致的直接改正为真实数值。
2. 初稿中出现但上面【没有】的指数点位/涨跌幅，属于编造，必须删除或替换为"数据暂缺"，不得保留虚构数值。
3. 市场情绪判断（偏多/偏空/震荡）需与修正后的指数涨跌幅自洽（例如指数上涨则不应判为偏空）。
4. 保持原有章节结构与文风，仅修正数字与自洽性问题，不要重写整篇报告。
5. 直接输出修正后的完整报告正文，不要输出任何解释、前后缀或代码块标记。

注意：除上述指数点位与涨跌幅外，其他行业涨跌幅、风险提示等文字若无事实冲突则保留原文。"""

    try:
        # DeepSeek 需直连, 临时清理代理
        old_env = {}
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            if k in os.environ:
                old_env[k] = os.environ.pop(k)
        try:
            client = OpenAI(api_key=CONFIG.deepseek_api_key, base_url=CONFIG.deepseek_base_url)
            logger.info("正在对报告进行后验校验...")
            resp = client.chat.completions.create(
                model=CONFIG.deepseek_model,
                messages=[
                    {"role": "system", "content": "你是严谨的数据校验员，只依据给定的真实行情修正报告，禁止编造数据。"},
                    {"role": "user", "content": verify_prompt},
                ],
                temperature=0.0,
                timeout=120,
            )
            content = resp.choices[0].message.content
            if content and content.strip():
                logger.info("后验校验完成")
                return content
        finally:
            os.environ.update(old_env)
    except Exception as e:
        logger.error(f"后验校验失败，保留初稿: {e}")
    return report


def save_report(report: str):
    date_str = datetime.now().strftime("%Y%m%d")
    path = CONFIG.data_dir / f"market_report_{date_str}.md"
    path.write_text(report, encoding="utf-8")
    logger.info(f"报告已保存: {path}")
    return path


def run():
    CONFIG.data_dir.mkdir(parents=True, exist_ok=True)
    CONFIG.log_dir.mkdir(parents=True, exist_ok=True)

    logger.info("===== 全球股市概览开始 =====")

    index_data = fetch_indices()
    logger.info(f"指数数据: {len(index_data)} 条")

    global_indices = fetch_global_indices()
    logger.info(f"全球指数数据: {len(global_indices)} 条")

    sector_data = fetch_sector_performance()
    logger.info(f"板块数据: 领涨 {len(sector_data.get('leading',[]))} 领跌 {len(sector_data.get('lagging',[]))}")

    prompt = build_prompt(index_data, sector_data, global_indices)
    report = generate_report(prompt)

    if report:
        report = verify_report(report, index_data, global_indices)
        save_report(report)
        print("\n" + "=" * 60)
        print(report)
        print("=" * 60)
    else:
        logger.error("报告生成失败，请检查 API Key 和网络连接")

    logger.info("===== 全球股市概览结束 =====")


if __name__ == "__main__":
    run()
