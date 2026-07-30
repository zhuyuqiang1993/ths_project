import os
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

_SESSION = requests.Session()
_SESSION.verify = False
_SESSION.trust_env = True

CN_INDEX_TENCENT = [
    ("上证指数", "sh000001"),
    ("深证成指", "sz399001"),
    ("创业板指", "sz399006"),
    ("科创50", "sh000688"),
    ("沪深300", "sh000300"),
]

KEY_SECTORS = [
    "半导体", "金融", "医药生物", "新能源", "消费",
    "有色金属", "房地产", "计算机", "机械设备", "电力",
    "国防军工", "汽车", "食品饮料", "电子", "通信",
]


def fetch_indices() -> dict:
    codes = ",".join(c for _, c in CN_INDEX_TENCENT)
    try:
        r = _SESSION.get(f"https://qt.gtimg.cn/q={codes}", timeout=10)
        result = {}
        for line in r.text.strip().split(";"):
            parts = line.split("~")
            if len(parts) < 35:
                continue
            name = parts[1]
            price = float(parts[3]) if parts[3] else 0
            pct = float(parts[32]) if parts[32] else 0
            chg = float(parts[31]) if parts[31] else 0
            result[name] = {"price": price, "pct_chg": pct, "change": chg}
        return result
    except Exception as e:
        logger.warning(f"腾讯行情获取失败: {e}")
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


def build_prompt(index_data: dict, sector_data: dict) -> str:
    today = datetime.now().strftime("%Y-%m-%d")

    idx_lines = []
    for name, d in index_data.items():
        pct = d.get("pct_chg", 0)
        emoji = "🔴" if pct < 0 else "🟢"
        idx_lines.append(
            f"  {emoji} {name}: {d['price']:.2f}  ({pct:+.2f}%)"
        )

    sector_lines = []
    if sector_data.get("leading"):
        sector_lines.append("\n**A股主要行业板块表现:**")
        all_sectors = sector_data["leading"][::-1] + sector_data["lagging"][::-1]
        for r in all_sectors:
            emoji = "🔴" if r["pct_chg"] < 0 else "🟢"
            sector_lines.append(f"  {emoji} {r['name']}: {r['pct_chg']:+.2f}%")

    prompt = f"""你是资深全球股市分析师。今天是 {today}，请基于以下市场数据，并**主动使用联网搜索**补充各市场最新消息、外围事件及期货走势，生成中日韩美四国股市综合概览。

## 实时市场数据

### 主要指数
{chr(10).join(idx_lines) if idx_lines else "（数据暂缺，请联网获取）"}

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

确保每条结论都有数据支撑，不空泛。"""
    return prompt


def generate_report(prompt: str) -> str | None:
    if not CONFIG.deepseek_api_key:
        logger.error("请在 config.py 中配置 deepseek_api_key")
        return None

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
            extra_body={"enable_search": True},
        )
        content = resp.choices[0].message.content
        logger.info("报告生成完成")
        return content
    except Exception as e:
        logger.error(f"API 调用失败: {e}")
        return None


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

    sector_data = fetch_sector_performance()
    logger.info(f"板块数据: 领涨 {len(sector_data.get('leading',[]))} 领跌 {len(sector_data.get('lagging',[]))}")

    prompt = build_prompt(index_data, sector_data)
    report = generate_report(prompt)

    if report:
        save_report(report)
        print("\n" + "=" * 60)
        print(report)
        print("=" * 60)
    else:
        logger.error("报告生成失败，请检查 API Key 和网络连接")

    logger.info("===== 全球股市概览结束 =====")


if __name__ == "__main__":
    run()
