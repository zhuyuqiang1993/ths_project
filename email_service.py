import smtplib
import sys
import os
import re
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
FEATURE_VOLUME = "volume_price"
FEATURE_VOLRISE = "volrise_screen"
FEATURE_ETF = "etf_screen"

FEATURE_NAMES = {
    FEATURE_MARKET: "市场情绪识别",
    FEATURE_SECTOR: "候选板块",
    FEATURE_STOCK: "候选个股",
    FEATURE_VOLUME: "量价分析",
    FEATURE_VOLRISE: "量增价涨筛选",
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


def _pct(v) -> str:
    """涨幅统一保留2位小数, 空值返回空串"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return f"{float(v):.2f}%"


def _red_green(val):
    if val is None or pd.isna(val):
        return ""
    return "red" if val >= 0 else "green"


def _txt(v) -> str:
    """任意值转字符串, 空值渲染为空串"""
    n = _n(v)
    return "" if n is None else str(n)


def _num(v, digits: int = 2) -> str:
    """数值固定小数位, 空值为空串"""
    n = _n(v)
    return "" if n is None else f"{float(n):.{digits}f}"


def _pct_cell(v) -> str:
    """涨幅单元格: 红涨绿跌"""
    if _n(v) is None:
        return ""
    return f'<span style="color:{_red_green(v)}">{_pct(v)}</span>'


# ---------- 统一排版样式 ----------
_TABLE_W = 760            # 正文/表格统一宽度(px)
_TH_STYLE = ("border:1px solid #ccd4dc;background-color:#3d566e;"
             "color:#ffffff;font-weight:bold;white-space:nowrap")

# 4列候选表 (候选板块/候选个股/量增价涨) 统一列宽与对齐, 保证各板块表格上下对齐
_H4 = ["代码", "名称", "当日涨幅", "类型"]
_W4 = [16, 34, 26, 24]
_A4 = ["center", "left", "right", "center"]


def _table(headers, rows, widths, aligns, compact: bool = False) -> str:
    """生成统一样式表格.

    headers: 列头文本列表
    rows:    [[单元格HTML, ...], ...]
    widths:  各列宽度百分比 (总和=100), table-layout:fixed 下严格生效
    aligns:  各列对齐 left/center/right
    compact: 紧凑模式 (列多的表用)
    """
    fs = 12 if compact else 13
    pad = "4px 5px" if compact else "6px 8px"
    td_base = f"border:1px solid #d8dee4;padding:{pad};font-size:{fs}px"
    th_base = f"{_TH_STYLE};padding:{pad};font-size:{fs}px"
    cols = "".join(f'<col style="width:{w}%">' for w in widths)
    ths = "".join(
        f'<th style="{th_base};text-align:{a}">{h}</th>'
        for h, a in zip(headers, aligns)
    )
    trs = []
    for i, row in enumerate(rows):
        bg = "" if i % 2 == 0 else "background-color:#f4f7fa;"
        tds = "".join(
            f'<td style="{td_base};{bg}text-align:{a}">{c}</td>'
            for c, a in zip(row, aligns)
        )
        trs.append(f"<tr>{tds}</tr>")
    return (
        f'<table cellspacing="0" style="width:100%;border-collapse:collapse;'
        f'table-layout:fixed;margin:0 0 8px;'
        f'font-family:Microsoft YaHei,Arial,sans-serif">{cols}'
        f"<tr>{ths}</tr>{''.join(trs)}</table>"
    )


def _p(text: str) -> str:
    """普通说明段落"""
    return f'<p style="margin:6px 0;color:#444;font-size:13px">{text}</p>'


def _label(text: str) -> str:
    """表前小标题"""
    return f'<p style="margin:12px 0 6px;font-size:13px;color:#1f3b54">{text}</p>'


def _info(text: str) -> str:
    """汇总信息条"""
    return (f'<p style="margin:6px 0;padding:6px 10px;background-color:#f4f7fa;'
            f'border-left:3px solid #3d566e;color:#555;font-size:12.5px">{text}</p>')


def _section(feature: str, body: str) -> str:
    title = FEATURE_NAMES.get(feature, feature)
    return f"""
    <h3 style="margin:20px 0 10px;padding:6px 12px;font-size:15px;font-weight:bold;
               color:#1f3b54;background-color:#e8eef4;border-left:4px solid #3d566e">{title}</h3>
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
        from market_overview import (fetch_indices, fetch_sector_performance,
                                     fetch_global_indices, build_prompt, verify_report)
        index_data = fetch_indices()
        global_indices = fetch_global_indices()
        sector_data = fetch_sector_performance()
        prompt = build_prompt(index_data, sector_data, global_indices)
        report = _call_deepseek(prompt)
        if report:
            report = verify_report(report, index_data, global_indices)
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
    return _section(FEATURE_MARKET, _style_report(body))


def _style_report(html: str) -> str:
    """给 markdown 生成的报告 HTML 补上与全邮件一致的样式
    (markdown 输出的 th/td 自带 style, 需合并而非覆盖)"""

    def _merge(tag: str, css: str):
        def repl(m):
            attrs = m.group(1) or ""
            sm = re.search(r'style="([^"]*)"', attrs)
            if sm:
                attrs = (attrs[:sm.start(1)] + css + ";" + sm.group(1)
                         + attrs[sm.end(1):])
            else:
                attrs = f'{attrs} style="{css}"' if attrs else f' style="{css}"'
            return f"<{tag}{attrs}>"
        return repl

    th_css = f"{_TH_STYLE};padding:6px 8px;font-size:13px"
    td_css = "border:1px solid #d8dee4;padding:6px 8px;font-size:13px"
    html = re.sub(
        r"<table>",
        '<table border="1" cellspacing="0" cellpadding="6" '
        'style="border-collapse:collapse;width:100%;margin:8px 0;'
        'font-size:13px;font-family:Microsoft YaHei,Arial,sans-serif">',
        html,
    )
    html = re.sub(r"<th(\s[^>]*)?>", _merge("th", th_css), html)
    html = re.sub(r"<td(\s[^>]*)?>", _merge("td", td_css), html)
    for lvl, size in ((1, 17), (2, 16), (3, 15), (4, 14)):
        html = re.sub(
            rf"<h{lvl}(\s[^>]*)?>",
            _merge(f"h{lvl}", f"font-size:{size}px;color:#1f3b54;margin:16px 0 6px"),
            html,
        )
    html = re.sub(r"<li>", '<li style="margin:3px 0">', html)
    html = re.sub(r"<strong>", '<strong style="color:#1f3b54">', html)
    return html


def _latest_identified_at(table: str) -> str:
    df = _query(f"SELECT MAX(identified_at) AS d FROM {table}")
    if df.empty or df["d"].isna().all():
        return ""
    return str(df["d"].iloc[0])


def build_candidate_stock() -> str:
    """候选个股: 从 candidate_stock 展示最新多因子评分结果 (只留代码/名称/当日涨幅/类型)"""
    latest = _latest_identified_at("candidate_stock")
    if not latest:
        return _section(FEATURE_STOCK, "无数据")

    df = _query(
        """SELECT s.code, s.name, s.pct_chg,
                  CASE WHEN c.board_code IS NOT NULL THEN '概念'
                       ELSE '行业' END AS type_cn
           FROM candidate_stock s
           LEFT JOIN (SELECT DISTINCT board_code FROM concept_daily) c
             ON s.board_code = c.board_code
           WHERE s.identified_at = %s
           ORDER BY s.score DESC""",
        (latest,),
    )
    if df.empty:
        return _section(FEATURE_STOCK, f"识别日期 {latest} 无候选个股")

    rows = [
        [_txt(r["code"]), _txt(r["name"]), _pct_cell(r["pct_chg"]),
         _txt(r["type_cn"])]
        for _, r in df.iterrows()
    ]
    html = (
        _p(f"识别日期: <b>{latest}</b> (共 {len(df)} 条)")
        + _label(f"全部候选 ({len(df)})")
        + _table(_H4, rows, _W4, _A4)
    )
    return _section(FEATURE_STOCK, html)


def build_candidate_sector() -> str:
    """候选板块: 从 candidate_sector 展示最新识别结果 (只留代码/名称/当日涨幅/类型)"""
    latest = _latest_identified_at("candidate_sector")
    if not latest:
        return _section(FEATURE_SECTOR, "无数据")

    df = _query(
        """SELECT s.board_code, s.board_name, s.pct_chg,
                  CASE WHEN c.board_code IS NOT NULL THEN '概念'
                       ELSE '行业' END AS type_cn
           FROM candidate_sector s
           LEFT JOIN (SELECT DISTINCT board_code FROM concept_daily) c
             ON s.board_code = c.board_code
           WHERE s.identified_at = %s
           ORDER BY s.chg_5d DESC""",
        (latest,),
    )
    if df.empty:
        return _section(FEATURE_SECTOR, f"识别日期 {latest} 无候选板块")

    rows = [
        [_txt(r["board_code"]), _txt(r["board_name"]), _pct_cell(r["pct_chg"]),
         _txt(r["type_cn"])]
        for _, r in df.iterrows()
    ]
    html = (
        _p(f"识别日期: <b>{latest}</b> (共 {len(df)} 个)")
        + _table(_H4, rows, _W4, _A4)
    )
    return _section(FEATURE_SECTOR, html)


def build_volume_price() -> str:
    """量价分析: 从 volume_price_analysis 显示最新交易日分析结果"""
    latest = _latest_date("volume_price_analysis")
    if not latest:
        return _section(FEATURE_VOLUME, "无数据 (量价分析尚未运行)")

    dist = _query(
        """SELECT vol_pattern, COUNT(*) AS c FROM volume_price_analysis
           WHERE date = %s GROUP BY vol_pattern ORDER BY c DESC""",
        (latest,),
    )
    sig = _query(
        """SELECT SUM(is_volume_peak) AS peak, SUM(is_volume_trough) AS trough,
                  SUM(divergence = '顶背离') AS topdiv,
                  SUM(divergence = '底背离') AS botdiv
           FROM volume_price_analysis WHERE date = %s""",
        (latest,),
    )
    df = _query(
        """SELECT code, name, board_name, close, pct_chg, vol_ratio_5,
                  vol_trend, position_60d, vol_pattern, divergence,
                  is_volume_peak, is_volume_trough, vp_score
           FROM volume_price_analysis WHERE date = %s
           ORDER BY vp_score DESC LIMIT 20""",
        (latest,),
    )
    if df.empty:
        return _section(FEATURE_VOLUME, f"交易日 {latest} 无量价分析结果")

    total = _query(
        "SELECT COUNT(*) AS c FROM volume_price_analysis WHERE date = %s",
        (latest,),
    )["c"].iloc[0]

    dist_txt = " | ".join(
        f"{r['vol_pattern']} {int(r['c'])}" for _, r in dist.iterrows()
    )
    s = sig.iloc[0] if not sig.empty else None
    sig_txt = (f"天量 {int(s['peak'] or 0)} 只, 地量 {int(s['trough'] or 0)} 只, "
               f"顶背离{int(s['topdiv'] or 0)} 只, 底背离{int(s['botdiv'] or 0)} 只")

    rows = [
        [_txt(r["code"]), _txt(r["name"]), _txt(r["board_name"]),
         _num(r["close"], 2), _pct_cell(r["pct_chg"]),
         _num(r["vol_ratio_5"], 2), _txt(r["vol_trend"]),
         _num(r["position_60d"], 1),
         _txt(r["vol_pattern"])
         + (" 天量" if r["is_volume_peak"] else "")
         + (" 地量" if r["is_volume_trough"] else ""),
         _txt(r["divergence"]), f"<b>{_num(r['vp_score'], 2)}</b>"]
        for _, r in df.iterrows()
    ]
    html = (
        _p(f"交易日 <b>{latest}</b> (全市场 {int(total)} 只)")
        + _info(f"<b>形态分布:</b> {dist_txt}")
        + _info(f"<b>信号统计:</b> {sig_txt}")
        + _label(f"评分 TOP{len(df)} (婴儿肥5)")
        + _table(
            ["代码", "名称", "板块", "收盘", "涨幅", "量比", "量能",
             "60日位置", "形态", "背离", "得分"],
            rows,
            [9, 12, 13, 8, 8, 7, 7, 9, 12, 8, 7],
            ["center", "left", "left", "right", "right", "right", "center",
             "right", "center", "center", "right"],
            compact=True,
        )
    )
    return _section(FEATURE_VOLUME, html)


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

    def _amt(v):
        n = _n(v)
        return "" if n is None else f"{round(float(n) / 1e8, 2)}亿"

    rows = [
        [_txt(r["code"]), _txt(r["name"]), _num(r["close"], 2),
         _pct_cell(r["pct_chg"]), _pct_cell(r["chg_5d"]), _amt(r["amount"])]
        for _, r in df.iterrows()
    ]
    html = (
        _p(f"识别日期: <b>{latest}</b> (共 {len(df)} 只)")
        + _table(["代码", "名称", "收盘", "当日涨幅", "5日涨幅", "成交额(亿)"],
                 rows, [15, 24, 14, 16, 15, 16],
                 ["center", "left", "right", "right", "right", "right"])
    )
    return _section(FEATURE_ETF, html)


def build_volrise() -> str:
    """量增价涨筛选: 板块需近3日每日涨幅>0且量环比>=-20%; 个股需近3日每日涨幅>0且板块涨幅>0(不做量环比)"""
    latest_s = _latest_identified_at("candidate_sector_volrise")
    latest_t = _latest_identified_at("candidate_stock_volrise")
    if not latest_s and not latest_t:
        return _section(FEATURE_VOLRISE, "无数据 (量增价涨筛选尚未运行)")

    type_cn = {"industry": "行业", "concept": "概念"}
    parts = []
    latest = latest_s or latest_t

    if latest_s:
        sdf = _query(
            """SELECT board_code, board_name, board_type, pct_chg
                 FROM candidate_sector_volrise
                WHERE identified_at = %s
                ORDER BY pct_chg DESC LIMIT 15""",
            (latest_s,),
        )
        if not sdf.empty:
            srows = [
                [_txt(r["board_code"]), _txt(r["board_name"]),
                 _pct_cell(r["pct_chg"]),
                 type_cn.get(r["board_type"], r["board_type"])]
                for _, r in sdf.iterrows()
            ]
            parts.append(
                _label(f"候选板块 ({len(sdf)} 个)")
                + _table(_H4, srows, _W4, _A4)
            )

    if latest_t:
        tdf = _query(
            """SELECT t.code, t.name, t.pct_chg,
                      CASE WHEN c.board_code IS NOT NULL THEN '概念'
                           ELSE '行业' END AS type_cn
                 FROM candidate_stock_volrise t
                 LEFT JOIN (SELECT DISTINCT board_code FROM concept_daily) c
                   ON t.board_code = c.board_code
                WHERE t.identified_at = %s
                ORDER BY t.pct_chg DESC LIMIT 20""",
            (latest_t,),
        )
        if not tdf.empty:
            trows = [
                [_txt(r["code"]), _txt(r["name"]), _pct_cell(r["pct_chg"]),
                 _txt(r["type_cn"])]
                for _, r in tdf.iterrows()
            ]
            parts.append(
                _label(f"候选个股 ({len(tdf)} 只)")
                + _table(_H4, trows, _W4, _A4)
            )

    if not parts:
        return _section(FEATURE_VOLRISE, f"识别日期 {latest} 无量增价涨候选")

    html = (
        _p(f"识别日期: <b>{latest}</b> (板块: 近3日每天涨幅>0且量环比>=-20%; "
           "个股: 近3日每天涨幅>0且板块涨幅>0，不做量环比)")
        + "".join(parts)
    )
    return _section(FEATURE_VOLRISE, html)


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
            elif f == FEATURE_VOLUME:
                sections.append(build_volume_price())
            elif f == FEATURE_VOLRISE:
                sections.append(build_volrise())
            elif f == FEATURE_ETF:
                sections.append(build_candidate_etf())

    body = "\n".join(sections)
    html = f"""<html>
<body style="margin:0;padding:0;background-color:#ffffff">
<div style="max-width:{_TABLE_W}px;margin:0 auto;padding:16px 12px;
            font-family:Microsoft YaHei,Arial,sans-serif;font-size:14px;color:#333333">
  <h2 style="margin:0 0 6px;text-align:center;font-size:20px;color:#1f3b54">A股数据日报 {today}</h2>
  <p style="margin:0 0 2px;text-align:center;color:#888888;font-size:12px">收件人: {recipient}</p>
  <p style="margin:0 0 10px;text-align:center;color:#888888;font-size:12px">本邮件由系统自动生成，仅供参考，不构成投资建议。</p>
  <hr style="border:none;border-top:2px solid #3d566e;margin:12px 0">
  {body}
  <hr style="border:none;border-top:1px solid #dde3e9;margin:16px 0 8px">
  <p style="margin:0;color:#999999;font-size:12px;text-align:center">发送时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
</div>
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