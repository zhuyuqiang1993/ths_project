import smtplib
import sys
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
FEATURE_STOCK = "stock_screen"
FEATURE_ETF = "etf_screen"

FEATURE_NAMES = {
    FEATURE_MARKET: "市场情绪识别",
    FEATURE_STOCK: "股票筛选结果",
    FEATURE_ETF: "ETF筛选结果",
}

_ALL_FEATURES = list(FEATURE_NAMES.keys())


def get_valid_recipients(feature: str = "") -> list:
    """获取有效收件方列表 [(email, features), ...]。

    有效条件: 订阅功能匹配 且 订阅未过期
        (订阅时间 + 订阅时长) >= 当前日期
    注: 需求描述"订阅时间+订阅时长小于当前日期为有效"应为笔误，
        按业务语义实现为"未过期即有效"，否则默认账号(时长达9999999天)
        将永远收不到邮件。
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


# ================= 内容生成 =================

def build_market_sentiment() -> str:
    """市场情绪识别"""
    latest = _latest_date("stock_daily")
    if not latest:
        return _section(FEATURE_MARKET, "无数据")

    df = _query(
        """SELECT COUNT(*) AS total,
                  SUM(pct_chg > 0) AS up,
                  SUM(pct_chg < 0) AS down,
                  ROUND(AVG(pct_chg), 2) AS avg_chg,
                  ROUND(SUM(amount), 0) AS total_amount
           FROM stock_daily WHERE date = %s""",
        (latest,),
    )
    row = df.iloc[0]
    total = int(_n(row["total"]) or 0)
    up = int(_n(row["up"]) or 0)
    down = int(_n(row["down"]) or 0)
    avg_chg = _n(row["avg_chg"])
    total_amount = _n(row["total_amount"])

    advance_ratio = up / total if total else 0
    if advance_ratio >= 0.6:
        sentiment = "偏多（市场普涨）"
    elif advance_ratio <= 0.4:
        sentiment = "偏空（市场普跌）"
    else:
        sentiment = "中性（分化震荡）"

    # 板块表现
    sector_df = _query(
        """SELECT board_name, close, pct_chg FROM sector_daily
           WHERE date = (SELECT MAX(date) FROM sector_daily)
           ORDER BY pct_chg DESC LIMIT 5"""
    )
    sector_rows = ""
    for _, r in sector_df.iterrows():
        sector_rows += f"<tr><td>{_n(r['board_name']) or ''}</td>" \
                       f"<td>{_n(r['close']) or ''}</td>" \
                       f"<td style='color:{_red_green(r['pct_chg'])}'>{_n(r['pct_chg']) or ''}%</td></tr>"

    html = f"""
    <p>交易日期: <b>{latest}</b></p>
    <p>市场情绪判断: <b>{sentiment}</b></p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse">
      <tr bgcolor="#f0f0f0"><td>上涨家数</td><td>下跌家数</td><td>平均涨幅</td><td>总成交额(亿)</td></tr>
      <tr><td style="color:red">{up}</td><td style="color:green">{down}</td>
          <td>{avg_chg}%</td><td>{round(total_amount / 1e8, 1) if total_amount else '-'}</td></tr>
    </table>
    <p><b>板块领涨 TOP5</b></p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse">
      <tr bgcolor="#f0f0f0"><td>板块</td><td>收盘指数</td><td>涨幅</td></tr>
      {sector_rows}
    </table>
    """
    return _section(FEATURE_MARKET, html)


def build_stock_screen() -> str:
    """股票筛选结果"""
    latest = _latest_date("stock_daily")
    if not latest:
        return _section(FEATURE_STOCK, "无数据")

    # 涨幅榜
    top_df = _query(
        """SELECT code, name, pct_chg, volume, amount FROM stock_daily
           WHERE date = %s ORDER BY pct_chg DESC LIMIT 10""",
        (latest,),
    )
    # 涨停/大涨 (创业板/科创板 20cm)
    limit_df = _query(
        """SELECT code, name, pct_chg FROM stock_daily
           WHERE date = %s AND pct_chg >= 9.9 ORDER BY pct_chg DESC""",
        (latest,),
    )
    # MACD金叉 (当日 hist>0, 前一日 <=0)
    cross_df = _query(
        """SELECT a.code, a.name, a.pct_chg
           FROM stock_daily a
           JOIN stock_daily b ON a.code = b.code
           WHERE a.date = %s AND b.date = (
                 SELECT MAX(date) FROM stock_daily WHERE date < %s)
             AND a.macd_hist > 0 AND b.macd_hist <= 0
           ORDER BY a.pct_chg DESC LIMIT 10""",
        (latest, latest),
    )

    top_rows = "".join(
        f"<tr><td>{_n(r['code'])}</td><td>{_n(r['name'])}</td>"
        f"<td style='color:{_red_green(r['pct_chg'])}'>{_n(r['pct_chg']) or ''}%</td>"
        f"<td>{_n(r['amount']) and round(r['amount'] / 1e8, 2)}亿</td></tr>"
        for _, r in top_df.iterrows()
    )
    cross_rows = "".join(
        f"<tr><td>{_n(r['code'])}</td><td>{_n(r['name'])}</td>"
        f"<td style='color:{_red_green(r['pct_chg'])}'>{_n(r['pct_chg']) or ''}%</td></tr>"
        for _, r in cross_df.iterrows()
    )

    html = f"""
    <p><b>当日涨幅榜 TOP10</b></p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse">
      <tr bgcolor="#f0f0f0"><td>代码</td><td>名称</td><td>涨幅</td><td>成交额(亿)</td></tr>
      {top_rows}
    </table>
    <p>当日大涨(≥9.9%)家数: <b style="color:red">{len(limit_df)}</b></p>
    <p><b>MACD金叉 TOP10</b></p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse">
      <tr bgcolor="#f0f0f0"><td>代码</td><td>名称</td><td>涨幅</td></tr>
      {cross_rows}
    </table>
    """
    return _section(FEATURE_STOCK, html)


def build_etf_screen() -> str:
    """ETF筛选结果"""
    latest = _latest_date("etf_daily")
    if not latest:
        return _section(FEATURE_ETF, "无数据")

    gain_df = _query(
        """SELECT code, name, close, pct_chg, amount FROM etf_daily
           WHERE date = %s ORDER BY pct_chg DESC LIMIT 10""",
        (latest,),
    )
    amt_df = _query(
        """SELECT code, name, close, pct_chg, amount FROM etf_daily
           WHERE date = %s ORDER BY amount DESC LIMIT 10""",
        (latest,),
    )

    gain_rows = "".join(
        f"<tr><td>{_n(r['code'])}</td><td>{_n(r['name'])}</td>"
        f"<td>{_n(r['close']) or ''}</td>"
        f"<td style='color:{_red_green(r['pct_chg'])}'>{_n(r['pct_chg']) or ''}%</td></tr>"
        for _, r in gain_df.iterrows()
    )
    amt_rows = "".join(
        f"<tr><td>{_n(r['code'])}</td><td>{_n(r['name'])}</td>"
        f"<td>{_n(r['close']) or ''}</td>"
        f"<td style='color:{_red_green(r['pct_chg'])}'>{_n(r['pct_chg']) or ''}%</td>"
        f"<td>{_n(r['amount']) and round(r['amount'] / 1e8, 2)}亿</td></tr>"
        for _, r in amt_df.iterrows()
    )

    html = f"""
    <p><b>ETF涨幅榜 TOP10</b></p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse">
      <tr bgcolor="#f0f0f0"><td>代码</td><td>名称</td><td>收盘</td><td>涨幅</td></tr>
      {gain_rows}
    </table>
    <p><b>ETF成交额 TOP10</b></p>
    <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse">
      <tr bgcolor="#f0f0f0"><td>代码</td><td>名称</td><td>收盘</td><td>涨幅</td><td>成交额(亿)</td></tr>
      {amt_rows}
    </table>
    """
    return _section(FEATURE_ETF, html)


def _red_green(val):
    if val is None or pd.isna(val):
        return ""
    return "red" if val >= 0 else "green"


def _section(feature: str, body: str) -> str:
    return f"""
    <h3 style="border-bottom:2px solid #333;padding-bottom:4px">{FEATURE_NAMES[feature]}</h3>
    {body}
    """


# ================= 邮件发送 =================

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


def build_email(recipient: str, features: list) -> str:
    today = date.today().strftime("%Y-%m-%d")
    sections = []
    for f in _ALL_FEATURES:
        if f in features:
            if f == FEATURE_MARKET:
                sections.append(build_market_sentiment())
            elif f == FEATURE_STOCK:
                sections.append(build_stock_screen())
            elif f == FEATURE_ETF:
                sections.append(build_etf_screen())

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
