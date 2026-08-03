import smtplib
import sys
import os
import warnings
from datetime import date, datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText

import pandas as pd
from loguru import logger

from config import CONFIG
from db_handler import get_connection, init_email_subscription

warnings.filterwarnings("ignore", message=".*only supports SQLAlchemy.*")

logger.remove()
logger.add(
    sys.stderr,
    level=CONFIG.log_level,
    format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}",
)
logger.add(
    CONFIG.log_dir / "email_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
)

FEATURE_MARKET = "market_sentiment"
FEATURE_SECTOR = "sector_screen"
FEATURE_STOCK = "stock_screen"
FEATURE_ETF = "etf_screen"

FEATURE_NAMES = {
    FEATURE_MARKET: "市场情绪识别",
    FEATURE_SECTOR: "候选板块",
    FEATURE_STOCK: "候选个股",
    FEATURE_ETF: "候选ETF",
}

_ALL_FEATURES = list(FEATURE_NAMES.keys())

TAG_NAMES = {
    "strict": "严格筛选",
    "strong": "强于板块",
    "vol_price": "量价股票",
}

# 默认展示候选数量（0 = 全量）
TOP_N = 0


def get_valid_recipients(feature: str = "") -> list:
    """获取有效收件方列表 [(email, features), ...]。

    有效条件: 订阅功能匹配 且 订阅未过期
        (订阅时间 + 订阅时长) >= 当前日期
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """SELECT email, features FROM email_subscription
               WHERE DATEDIFF(CURDATE(), start_date) <= duration"""
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    result = []
    for email, features in rows:
        feats = [f.strip() for f in features.split(",") if f.strip()]
        if not feats:
            continue
        if feature and feature not in feats:
            continue
        result.append((email, feats))
    return result


def _query(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = get_connection()
    try:
        df = pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()
    return df


def _latest_date(table: str) -> str:
    df = _query(f"SELECT MAX(date) AS d FROM {table}")
    if df.empty or df["d"].isna().all():
        return ""
    return str(df["d"].iloc[0])


def _n(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return v


def _red_green(val):
    if val is None or pd.isna(val):
        return ""
    return "red" if val >= 0 else "green"


def _section(feature: str, body: str) -> str:
    title = FEATURE_NAMES.get(feature, feature)
    return f"""
    <h3 style="border-bottom:2px solid #333;padding-bottom:4px">{title}</h3>
    {body}
    """


# ================= 内容生成 =================

def build_market_sentiment() -> str:
    """市场情绪识别: 完全复用 market_overview 模块, 输出市场情绪/大类板块表现/
    风险提示/下一个交易日预测 (markdown 转 HTML)"""
    # market_overview 模块导入时会设置代理, 清理以支持直连
    for k in ("HTTP_PROXY", "HTTPS_PROXY"):
        os.environ.pop(k, None)
    try:
        from market_overview import fetch_indices, fetch_sector_performance, build_prompt
        index_data = fetch_indices()
        sector_data = fetch_sector_performance()
        prompt = build_prompt(index_data, sector_data)
        report = _call_deepseek(prompt)
    except Exception as e:
        logger.error(f"市场情绪报告生成失败: {e}")
        report = None

    if not report:
        return _section(FEATURE_MARKET, "市场情绪报告生成失败")

    import markdown as md
    body = md.markdown(
        report,
        extensions=["tables", "fenced_code", "nl2br"],
        output_format="html",
    )
    return _section(FEATURE_MARKET, body)


def _latest_identified_at(table: str) -> str:
    df = _query(f"SELECT MAX(identified_at) AS d FROM {table}")
    if df.empty or df["d"].isna().all():
        return ""
    return str(df["d"].iloc[0])


def build_candidate_stock() -> str:
    """候选个股: 从 candidate_stock 按标签分组展示最新识别结果"""
    latest = _latest_identified_at("candidate_stock")
    if not latest:
        return _section(FEATURE_STOCK, "无数据")

    df = _query(
        """SELECT code, name, board_name, tag, open, close, pct_chg, macd, chg_5d, avg_rel
           FROM candidate_stock WHERE identified_at = %s
           ORDER BY FIELD(tag,'strict','strong','vol_price'), avg_rel DESC""",
        (latest,),
    )
    if df.empty:
        return _section(FEATURE_STOCK, f"识别日期 {latest} 无候选个股")

    blocks = []
    for tag in ["strict", "strong", "vol_price"]:
        sub = df[df["tag"] == tag]
        if sub.empty:
            continue
        rows = "".join(
            f"<tr><td>{_n(r['code'])}</td><td>{_n(r['name'])}</td>"
            f"<td>{_n(r['board_name']) or ''}</td>"
            f"<td>{_n(r['open']) or ''}</td>"
            f"<td>{_n(r['close']) or ''}</td>"
            f"<td style='color:{_red_green(r['pct_chg'])}'>{_n(r['pct_chg']) or ''}%</td>"
            f"<td>{_n(r['chg_5d']) or ''}%</td>"
            f"<td>{_n(r['macd']) or ''}</td></tr>"
            for _, r in sub.iterrows()
        )
        blocks.append(f"""
        <p><b>{TAG_NAMES[tag]} ({len(sub)})</b></p>
        <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse">
          <tr bgcolor="#f0f0f0"><td>代码</td><td>名称</td><td>板块</td>
              <td>开盘</td><td>收盘</td><td>当日涨幅</td><td>5日涨幅</td><td>MACD</td></tr>
          {rows}
        </table>
        """)

    html = f"<p>识别日期: <b>{latest}</b> (共 {len(df)} 条)</p>" + "".join(blocks)
    return _section(FEATURE_STOCK, html)


def build_candidate_sector() -> str:
    """候选板块: 从 candidate_sector 展示最新识别结果"""
    latest = _latest_identified_at("candidate_sector")
    if not latest:
        return _section(FEATURE_SECTOR, "无数据")

    df = _query(
        """SELECT board_code, board_name, close, pct_chg, chg_5d,
                  amount, advance, decline, net_inflow
           FROM candidate_sector WHERE identified_at = %s
           ORDER BY chg_5d DESC""",
        (latest,),
    )
    if df.empty:
        return _section(FEATURE_SECTOR, f"识别日期 {latest} 无候选板块")

    rows = "".join(
        f"<tr><td>{_n(r['board_code'])}</td><td>{_n(r['board_name'])}</td>"
        f"<td>{_n(r['close']) or ''}</td>"
        f"<td style='color:{_red_green(r['pct_chg'])}'>{_n(r['pct_chg']) or ''}%</td>"
        f"<td>{_n(r['chg_5d']) or ''}%</td>"
        f"<td>{_n(r['amount']) and round(r['amount'] / 1e8, 2)}亿</td></tr>"
        for _, r in df.iterrows()
    )
    html = f"""
    <p>识别日期: <b>{latest}</b> (共 {len(df)} 个)</p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse">
      <tr bgcolor="#f0f0f0"><td>代码</td><td>名称</td><td>收盘</td>
          <td>当日涨幅</td><td>5日涨幅</td><td>成交额(亿)</td></tr>
      {rows}
    </table>
    """
    return _section(FEATURE_SECTOR, html)


def build_candidate_etf() -> str:
    """候选ETF: 从 candidate_etf 展示最新识别结果"""
    latest = _latest_identified_at("candidate_etf")
    if not latest:
        return _section(FEATURE_ETF, "无数据")

    df = _query(
        """SELECT code, name, close, pct_chg, chg_5d, volume, amount
           FROM candidate_etf WHERE identified_at = %s
           ORDER BY chg_5d DESC""",
        (latest,),
    )
    if df.empty:
        return _section(FEATURE_ETF, f"识别日期 {latest} 无候选ETF")

    rows = "".join(
        f"<tr><td>{_n(r['code'])}</td><td>{_n(r['name'])}</td>"
        f"<td>{_n(r['close']) or ''}</td>"
        f"<td style='color:{_red_green(r['pct_chg'])}'>{_n(r['pct_chg']) or ''}%</td>"
        f"<td>{_n(r['chg_5d']) or ''}%</td>"
        f"<td>{_n(r['amount']) and round(r['amount'] / 1e8, 2)}亿</td></tr>"
        for _, r in df.iterrows()
    )
    html = f"""
    <p>识别日期: <b>{latest}</b> (共 {len(df)} 只)</p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse">
      <tr bgcolor="#f0f0f0"><td>代码</td><td>名称</td><td>收盘</td>
          <td>当日涨幅</td><td>5日涨幅</td><td>成交额(亿)</td></tr>
      {rows}
    </table>
    """
    return _section(FEATURE_ETF, html)


def _call_deepseek(prompt: str) -> str | None:
    if not CONFIG.deepseek_api_key:
        logger.error("未配置 deepseek_api_key (DS_APP_KEY)")
        return None
    # 清理 market_overview 模块导入时设置的代理, 保证直连 DeepSeek
    for k in ("HTTP_PROXY", "HTTPS_PROXY"):
        os.environ.pop(k, None)
    try:
        from openai import OpenAI
        client = OpenAI(api_key=CONFIG.deepseek_api_key, base_url=CONFIG.deepseek_base_url)
        resp = client.chat.completions.create(
            model=CONFIG.deepseek_model,
            messages=[
                {"role": "system", "content": "你是一位专业、严谨的A股分析师，输出结构化中文HTML结论，结论需有数据支撑。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            extra_body={"enable_search": True},
        )
        content = resp.choices[0].message.content
        logger.info("预测结论生成完成")
        return content
    except Exception as e:
        logger.error(f"DeepSeek 调用失败: {e}")
        return None


# ================= 邮件组装 =================

def build_email(recipient: str, features: list) -> str:
    today = date.today().strftime("%Y-%m-%d")
    sections = []
    for f in _ALL_FEATURES:
        if f in features:
            if f == FEATURE_MARKET:
                sections.append(build_market_sentiment())
            elif f == FEATURE_SECTOR:
                sections.append(build_candidate_sector())
            elif f == FEATURE_STOCK:
                sections.append(build_candidate_stock())
            elif f == FEATURE_ETF:
                sections.append(build_candidate_etf())

    body = "\n".join(sections)
    html = f"""
    <html><body style="font-family:Microsoft YaHei,Arial;font-size:14px">
    <h2 style="text-align:center">A股数据日报 {today}</h2>
    <p>收件人: {recipient}</p>
    <p>本邮件由系统自动生成，仅供参考，不构成投资建议。</p>
    <hr>
    {body}
    <hr>
    <p style="color:#888;font-size:12px">发送时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    </body></html>
    """
    return html


def send_email(recipient: str, subject: str, html_body: str) -> bool:
    if not CONFIG.mail_sender or not CONFIG.mail_auth_code:
        logger.error("未配置QQ邮箱发件账号/授权码 (MAIL_SENDER / MAIL_AUTH_CODE)")
        return False

    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = CONFIG.mail_sender
    msg["To"] = recipient

    try:
        server = smtplib.SMTP_SSL(CONFIG.mail_smtp_host, CONFIG.mail_smtp_port, timeout=30)
        server.login(CONFIG.mail_sender, CONFIG.mail_auth_code)
        server.sendmail(CONFIG.mail_sender, [recipient], msg.as_string())
        server.quit()
        logger.info(f"邮件已发送 -> {recipient}")
        return True
    except Exception as e:
        logger.error(f"邮件发送失败 -> {recipient}: {e}")
        return False


def run():
    init_email_subscription()
    recipients = get_valid_recipients()
    if not recipients:
        logger.warning("无有效收件方")
        return

    logger.info(f"共 {len(recipients)} 个有效收件方")
    today = date.today().strftime("%Y-%m-%d")
    for email, feats in recipients:
        try:
            html = build_email(email, feats)
            feats_cn = "、".join(FEATURE_NAMES[f] for f in feats if f in FEATURE_NAMES)
            subject = f"A股数据日报 {today} ({feats_cn})"
            send_email(email, subject, html)
        except Exception as e:
            logger.error(f"生成/发送 {email} 失败: {e}")


if __name__ == "__main__":
    run()
