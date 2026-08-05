from typing import Optional

import mysql.connector
import pandas as pd
from loguru import logger

from config import CONFIG


def get_connection():
    return mysql.connector.connect(
        host=CONFIG.mysql_host,
        port=CONFIG.mysql_port,
        user=CONFIG.mysql_user,
        password=CONFIG.mysql_password,
        database=CONFIG.mysql_database,
    )


DDL_STOCK_LIST = """
CREATE TABLE IF NOT EXISTS stock_list (
    code varchar(10) NOT NULL,
    name varchar(32) NOT NULL DEFAULT '',
    latest_price decimal(12,2) DEFAULT NULL,
    pct_chg decimal(6,2) DEFAULT NULL,
    price_change decimal(12,2) DEFAULT NULL,
    volume bigint DEFAULT NULL,
    amount decimal(16,2) DEFAULT NULL,
    amplitude decimal(6,2) DEFAULT NULL,
    high decimal(12,2) DEFAULT NULL,
    low decimal(12,2) DEFAULT NULL,
    open decimal(12,2) DEFAULT NULL,
    prev_close decimal(12,2) DEFAULT NULL,
    volume_ratio decimal(6,2) DEFAULT NULL,
    turnover_rate decimal(10,4) DEFAULT NULL,
    pe_dynamic decimal(12,2) DEFAULT NULL,
    pb decimal(12,2) DEFAULT NULL,
    total_mv decimal(20,2) DEFAULT NULL,
    float_mv decimal(20,2) DEFAULT NULL,
    pct_chg_60d decimal(6,2) DEFAULT NULL,
    created_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


DDL_STOCK_DAILY = """
CREATE TABLE IF NOT EXISTS stock_daily (
    code varchar(10) NOT NULL,
    date date NOT NULL,
    name varchar(32) DEFAULT '',
    board_code varchar(10) DEFAULT '',
    board_name varchar(32) DEFAULT '',
    prev_close decimal(12,2) DEFAULT NULL,
    open decimal(12,2) DEFAULT NULL,
    high decimal(12,2) DEFAULT NULL,
    low decimal(12,2) DEFAULT NULL,
    close decimal(12,2) DEFAULT NULL,
    pct_chg decimal(6,2) DEFAULT NULL,
    volume bigint DEFAULT NULL,
    amount decimal(16,2) DEFAULT NULL,
    macd decimal(10,4) DEFAULT NULL,
    macd_signal decimal(10,4) DEFAULT NULL,
    macd_hist decimal(10,4) DEFAULT NULL,
    created_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (code, date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

DDL_SECTOR_DAILY = """
CREATE TABLE IF NOT EXISTS sector_daily (
    board_code varchar(10) NOT NULL,
    board_name varchar(32) DEFAULT '',
    date date NOT NULL,
    low decimal(12,2) DEFAULT NULL,
    high decimal(12,2) DEFAULT NULL,
    close decimal(12,2) DEFAULT NULL,
    prev_close decimal(12,2) DEFAULT NULL,
    volume bigint DEFAULT NULL,
    amount decimal(20,2) DEFAULT NULL,
    pct_chg decimal(8,4) DEFAULT NULL,
    `change` decimal(12,2) DEFAULT NULL,
    advance int DEFAULT NULL,
    decline int DEFAULT NULL,
    net_inflow decimal(20,2) DEFAULT NULL,
    created_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (board_code, date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

DDL_ETF_DAILY = """
CREATE TABLE IF NOT EXISTS etf_daily (
    code varchar(10) NOT NULL,
    name varchar(32) DEFAULT '',
    date date NOT NULL,
    prev_close decimal(12,2) DEFAULT NULL,
    open decimal(12,2) DEFAULT NULL,
    high decimal(12,2) DEFAULT NULL,
    low decimal(12,2) DEFAULT NULL,
    close decimal(12,2) DEFAULT NULL,
    volume bigint DEFAULT NULL,
    amount decimal(20,2) DEFAULT NULL,
    pct_chg decimal(8,4) DEFAULT NULL,
    `change` decimal(12,2) DEFAULT NULL,
    created_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (code, date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


DDL_EMAIL_SUBSCRIPTION = """
CREATE TABLE IF NOT EXISTS email_subscription (
    id bigint NOT NULL AUTO_INCREMENT,
    email varchar(64) NOT NULL,
    start_date date NOT NULL,
    duration int NOT NULL COMMENT '订阅时长(天)',
    features varchar(255) NOT NULL COMMENT '订阅功能, 逗号分隔',
    created_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

DEFAULT_EMAIL_SUBSCRIPTION = (
    "zhuyuqiang2015@outlook.com",
    "2026-01-01",
    9999999,
    "market_sentiment,sector_screen,stock_screen,etf_screen",
)


DDL_CANDIDATE_SECTOR = """
CREATE TABLE IF NOT EXISTS candidate_sector (
    board_code varchar(10) NOT NULL,
    board_name varchar(32) DEFAULT '',
    date date NOT NULL COMMENT '交易日',
    identified_at date NOT NULL COMMENT '识别为候选板块的日期',
    close decimal(12,2) DEFAULT NULL,
    score decimal(6,2) DEFAULT NULL COMMENT '综合评分(满分5)',
    ret_20d decimal(8,4) DEFAULT NULL COMMENT '20日收益率%',
    vol_ratio decimal(6,2) DEFAULT NULL COMMENT '量能扩张比(5日/20日)',
    low decimal(12,2) DEFAULT NULL,
    high decimal(12,2) DEFAULT NULL,
    prev_close decimal(12,2) DEFAULT NULL,
    volume bigint DEFAULT NULL,
    amount decimal(20,2) DEFAULT NULL,
    pct_chg decimal(8,4) DEFAULT NULL,
    `change` decimal(12,2) DEFAULT NULL,
    advance int DEFAULT NULL,
    decline int DEFAULT NULL,
    net_inflow decimal(20,2) DEFAULT NULL,
    chg_5d decimal(8,4) DEFAULT NULL COMMENT '近5日累计涨幅%',
    created_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (board_code, identified_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


DDL_CANDIDATE_STOCK = """
CREATE TABLE IF NOT EXISTS candidate_stock (
    code varchar(10) NOT NULL,
    name varchar(32) DEFAULT '',
    board_code varchar(10) NOT NULL,
    board_name varchar(32) DEFAULT '',
    date date NOT NULL COMMENT '交易日',
    identified_at date NOT NULL COMMENT '识别为候选股票的日期',
    open decimal(12,2) DEFAULT NULL COMMENT '开盘价',
    close decimal(12,2) DEFAULT NULL,
    pct_chg decimal(6,2) DEFAULT NULL,
    macd decimal(10,4) DEFAULT NULL,
    chg_5d decimal(8,4) DEFAULT NULL COMMENT '近5日涨幅%',
    avg_rel decimal(8,4) DEFAULT NULL COMMENT '平均每日跑赢板块幅度(百分点)',
    tag varchar(16) NOT NULL DEFAULT '' COMMENT 'stock=股票筛选',
    score decimal(6,2) DEFAULT NULL COMMENT '综合评分(满分5)',
    rps_20 decimal(6,2) DEFAULT NULL COMMENT '20日RPS(相对强度百分位)',
    rps_60 decimal(6,2) DEFAULT NULL COMMENT '60日RPS(相对强度百分位)',
    trend decimal(4,2) DEFAULT NULL COMMENT '趋势得分(0-1)',
    momentum decimal(4,2) DEFAULT NULL COMMENT '动量得分(0-1)',
    volume_score decimal(4,2) DEFAULT NULL COMMENT '量价得分(0-1)',
    macd_score decimal(4,2) DEFAULT NULL COMMENT 'MACD得分(0-1)',
    sector_score decimal(4,2) DEFAULT NULL COMMENT '板块得分(0-1)',
    created_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (code, identified_at, tag)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


DDL_CANDIDATE_ETF = """
CREATE TABLE IF NOT EXISTS candidate_etf (
    code varchar(10) NOT NULL,
    name varchar(32) DEFAULT '',
    date date NOT NULL COMMENT '交易日',
    identified_at date NOT NULL COMMENT '识别为候选ETF的日期',
    close decimal(12,2) DEFAULT NULL,
    score decimal(6,2) DEFAULT NULL COMMENT '综合评分(满分5)',
    ret_20d decimal(8,4) DEFAULT NULL COMMENT '20日收益率%',
    vol_ratio decimal(6,2) DEFAULT NULL COMMENT '量能扩张比(5日/20日)',
    prev_close decimal(12,2) DEFAULT NULL,
    pct_chg decimal(8,4) DEFAULT NULL,
    volume bigint DEFAULT NULL,
    amount decimal(20,2) DEFAULT NULL,
    chg_5d decimal(8,4) DEFAULT NULL COMMENT '近5日累计涨幅%',
    created_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (code, identified_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


TABLE_DDL = {
    "stock_list": DDL_STOCK_LIST,
    "stock_daily": DDL_STOCK_DAILY,
    "sector_daily": DDL_SECTOR_DAILY,
    "etf_daily": DDL_ETF_DAILY,
    "email_subscription": DDL_EMAIL_SUBSCRIPTION,
    "candidate_sector": DDL_CANDIDATE_SECTOR,
    "candidate_stock": DDL_CANDIDATE_STOCK,
    "candidate_etf": DDL_CANDIDATE_ETF,
}


def ensure_table(table: str):
    """若表不存在则按 DDL 自动创建, 确保写入前表结构就绪。"""
    ddl = TABLE_DDL.get(table)
    if not ddl:
        return
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """SELECT COUNT(*) FROM information_schema.TABLES
               WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s""",
            (CONFIG.mysql_database, table),
        )
        if cursor.fetchone()[0] == 0:
            cursor.execute(ddl)
            conn.commit()
            logger.info(f"表 {table} 不存在, 已自动创建")
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cursor.close()
        conn.close()


def create_tables():
    """创建/重建表结构。若旧 stock_daily 表存在（id 主键），先删除再按新结构重建。"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """SELECT COUNT(*) FROM information_schema.TABLES
               WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s""",
            (CONFIG.mysql_database, "stock_daily"),
        )
        exists = cursor.fetchone()[0] > 0
        if exists:
            cursor.execute("SHOW INDEX FROM stock_daily")
            keys = cursor.fetchall()
            pk_col = next((k[4] for k in keys if k[2] == "PRIMARY"), None)
            if pk_col != "code":
                cursor.execute("DROP TABLE stock_daily")
                logger.info("旧版 stock_daily (id 主键) 已删除，重建新结构")
        cursor.execute(DDL_STOCK_LIST)
        cursor.execute(DDL_STOCK_DAILY)
        # 兼容旧表: 补 high/low/close 列 (旧 stock_daily 缺当日最高/最低/收盘)
        cursor.execute(
            """SELECT COLUMN_NAME FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA=%s AND TABLE_NAME='stock_daily'""",
            (CONFIG.mysql_database,),
        )
        existing_cols = {r[0] for r in cursor.fetchall()}
        for col, ddl in (("high", "ADD COLUMN high decimal(12,2) DEFAULT NULL AFTER open"),
                         ("low", "ADD COLUMN low decimal(12,2) DEFAULT NULL AFTER high"),
                         ("close", "ADD COLUMN close decimal(12,2) DEFAULT NULL AFTER low")):
            if col not in existing_cols:
                cursor.execute("ALTER TABLE stock_daily " + ddl)
                logger.info(f"stock_daily 表已补 {col} 列")
        cursor.execute(DDL_SECTOR_DAILY)
        cursor.execute(DDL_ETF_DAILY)
        cursor.execute(DDL_EMAIL_SUBSCRIPTION)
        cursor.execute(DDL_CANDIDATE_SECTOR)
        cursor.execute(DDL_CANDIDATE_STOCK)
        cursor.execute(DDL_CANDIDATE_ETF)
        # 兼容旧表: 若缺 tag 列则补列, 并升级主键 (code, identified_at) -> (code, identified_at, tag)
        cursor.execute(
            """SELECT COUNT(*) FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME='tag'""",
            (CONFIG.mysql_database, "candidate_stock"),
        )
        if cursor.fetchone()[0] == 0:
            cursor.execute("ALTER TABLE candidate_stock ADD COLUMN tag varchar(16) NOT NULL DEFAULT '' COMMENT 'strict=严格筛选 strong=强于板块 vol_price=量价股票'")
            logger.info("candidate_stock 表已补 tag 列")
        cursor.execute("SHOW INDEX FROM candidate_stock")
        pk_cols = sorted(k[4] for k in cursor.fetchall() if k[2] == "PRIMARY")
        if pk_cols != ["code", "identified_at", "tag"]:
            cursor.execute("ALTER TABLE candidate_stock DROP PRIMARY KEY, ADD PRIMARY KEY (code, identified_at, tag)")
            logger.info(f"candidate_stock 主键已升级: {pk_cols} -> code+identified_at+tag")
        # 兼容旧表: 若缺 open 列则补列
        cursor.execute(
            """SELECT COUNT(*) FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA=%s AND TABLE_NAME='candidate_stock' AND COLUMN_NAME='open'""",
            (CONFIG.mysql_database,),
        )
        if cursor.fetchone()[0] == 0:
            cursor.execute("ALTER TABLE candidate_stock ADD COLUMN open decimal(12,2) DEFAULT NULL COMMENT '开盘价' AFTER identified_at")
            logger.info("candidate_stock 表已补 open 列")
        # 兼容旧表: 补 v2 筛选字段
        for tbl, new_cols in [
            ("candidate_sector", [
                ("score", "ADD COLUMN score decimal(6,2) DEFAULT NULL COMMENT '综合评分' AFTER close"),
                ("ret_20d", "ADD COLUMN ret_20d decimal(8,4) DEFAULT NULL COMMENT '20日收益率%' AFTER score"),
                ("vol_ratio", "ADD COLUMN vol_ratio decimal(6,2) DEFAULT NULL COMMENT '量能扩张比' AFTER ret_20d"),
            ]),
            ("candidate_stock", [
                ("score", "ADD COLUMN score decimal(6,2) DEFAULT NULL COMMENT '综合评分' AFTER tag"),
                ("rps_20", "ADD COLUMN rps_20 decimal(6,2) DEFAULT NULL COMMENT '20日RPS' AFTER score"),
                ("rps_60", "ADD COLUMN rps_60 decimal(6,2) DEFAULT NULL COMMENT '60日RPS' AFTER rps_20"),
                ("trend", "ADD COLUMN trend decimal(4,2) DEFAULT NULL COMMENT '趋势得分' AFTER rps_60"),
                ("momentum", "ADD COLUMN momentum decimal(4,2) DEFAULT NULL COMMENT '动量得分' AFTER trend"),
                ("volume_score", "ADD COLUMN volume_score decimal(4,2) DEFAULT NULL COMMENT '量价得分' AFTER momentum"),
                ("macd_score", "ADD COLUMN macd_score decimal(4,2) DEFAULT NULL COMMENT 'MACD得分' AFTER volume_score"),
                ("sector_score", "ADD COLUMN sector_score decimal(4,2) DEFAULT NULL COMMENT '板块得分' AFTER macd_score"),
            ]),
            ("candidate_etf", [
                ("score", "ADD COLUMN score decimal(6,2) DEFAULT NULL COMMENT '综合评分' AFTER close"),
                ("ret_20d", "ADD COLUMN ret_20d decimal(8,4) DEFAULT NULL COMMENT '20日收益率%' AFTER score"),
                ("vol_ratio", "ADD COLUMN vol_ratio decimal(6,2) DEFAULT NULL COMMENT '量能扩张比' AFTER ret_20d"),
            ]),
        ]:
            cursor.execute(
                f"SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME='{tbl}'",
                (CONFIG.mysql_database,),
            )
            existing = {r[0] for r in cursor.fetchall()}
            for col_name, ddl in new_cols:
                if col_name not in existing:
                    cursor.execute(f"ALTER TABLE {tbl} {ddl}")
                    logger.info(f"{tbl} 表已补 {col_name} 列")
        conn.commit()
        logger.info("表结构创建完成: stock_list / stock_daily / sector_daily / etf_daily / email_subscription / candidate_sector / candidate_stock / candidate_etf")
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cursor.close()
        conn.close()


def init_email_subscription():
    """创建订阅表并写入默认账号"""
    create_tables()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """INSERT INTO email_subscription (email, start_date, duration, features)
               VALUES (%s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE
               start_date=VALUES(start_date), duration=VALUES(duration),
               features=VALUES(features)""",
            DEFAULT_EMAIL_SUBSCRIPTION,
        )
        conn.commit()
        logger.info(f"邮箱订阅默认账号已就绪: {DEFAULT_EMAIL_SUBSCRIPTION[0]}")
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cursor.close()
        conn.close()


def has_data_in_range(table: str, start_date: str, end_date: str,
                      date_col: str = "date") -> bool:
    """检查指定表在日期范围内是否每天都有数据, 用于避免冗余API调用。

    Args:
        table: 表名 (stock_daily / sector_daily / etf_daily)
        start_date: 起始日期, 格式 YYYY-MM-DD 或 YYYYMMDD
        end_date: 结束日期, 格式 YYYY-MM-DD 或 YYYYMMDD
        date_col: 日期列名, 默认 "date"

    Returns:
        True = 区间内每天都有数据, 可跳过拉取; False = 有缺失, 需要拉取
    """
    s = start_date.replace("-", "")
    e = end_date.replace("-", "")
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # 获取区间内的交易日数量
        from trade_calendar import get_trade_dates
        trade_dates = get_trade_dates(start_date, end_date)
        if not trade_dates:
            return True  # 无交易日, 无需拉取
        expected = len(trade_dates)

        cursor.execute(
            f"""SELECT COUNT(DISTINCT `{date_col}`) FROM `{table}`
                WHERE `{date_col}` BETWEEN %s AND %s""",
            (s, e),
        )
        actual = cursor.fetchone()[0]
        if actual >= expected:
            logger.info(f"[{table}] 区间内 {expected} 个交易日数据完整, 可跳过拉取")
            return True
        else:
            logger.info(f"[{table}] 区间内缺失 {expected - actual}/{expected} 个交易日数据, 需要拉取")
            return False
    except Exception as e:
        logger.warning(f"检查 {table} 日期范围数据失败: {e}")
        return False
    finally:
        cursor.close()
        conn.close()


def save_stock_list_to_db(df: pd.DataFrame):
    ensure_table("stock_list")
    conn = get_connection()
    cursor = conn.cursor()
    try:
        for _, row in df.iterrows():
            sql = """INSERT INTO stock_list
                     (code, name, latest_price, pct_chg, price_change, volume, amount,
                      amplitude, high, low, open, prev_close, volume_ratio,
                      turnover_rate, pe_dynamic, pb, total_mv, float_mv, pct_chg_60d)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                     ON DUPLICATE KEY UPDATE
                     name=VALUES(name), latest_price=VALUES(latest_price),
                     pct_chg=VALUES(pct_chg), price_change=VALUES(price_change),
                     volume=VALUES(volume), amount=VALUES(amount),
                     amplitude=VALUES(amplitude), high=VALUES(high), low=VALUES(low),
                     open=VALUES(open), prev_close=VALUES(prev_close),
                     volume_ratio=VALUES(volume_ratio),
                     turnover_rate=VALUES(turnover_rate),
                     pe_dynamic=VALUES(pe_dynamic), pb=VALUES(pb),
                     total_mv=VALUES(total_mv), float_mv=VALUES(float_mv),
                     pct_chg_60d=VALUES(pct_chg_60d)"""
            vals = (
                str(row.get("code", "")),
                str(row.get("name", "")),
                _n(row.get("latest_price")),
                _n(row.get("pct_chg")),
                _n(row.get("price_change")),
                _n(row.get("volume")),
                _n(row.get("amount")),
                _n(row.get("amplitude")),
                _n(row.get("high")),
                _n(row.get("low")),
                _n(row.get("open")),
                _n(row.get("prev_close")),
                _n(row.get("volume_ratio")),
                _n(row.get("turnover_rate")),
                _n(row.get("pe_dynamic")),
                _n(row.get("pb")),
                _n(row.get("total_mv")),
                _n(row.get("float_mv")),
                _n(row.get("pct_chg_60d")),
            )
            cursor.execute(sql, vals)
        conn.commit()
        logger.info(f"股票列表已写入 MySQL ({len(df)} 条)")
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cursor.close()
        conn.close()


def save_stock_daily_to_db(df: pd.DataFrame):
    """写入个股日线 (date/code/name/board_code/board_name/prev_close/open/
    high/low/close/pct_chg/volume/amount/macd/macd_signal/macd_hist)"""
    cols = ["code", "date", "name", "board_code", "board_name",
            "prev_close", "open", "high", "low", "close", "pct_chg",
            "volume", "amount", "macd", "macd_signal", "macd_hist"]
    sql = """INSERT INTO stock_daily
             (code, date, name, board_code, board_name,
              prev_close, open, high, low, close, pct_chg,
              volume, amount, macd, macd_signal, macd_hist)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
             ON DUPLICATE KEY UPDATE
             name=VALUES(name), board_code=VALUES(board_code),
             board_name=VALUES(board_name), prev_close=VALUES(prev_close),
             open=VALUES(open), high=VALUES(high), low=VALUES(low),
             close=VALUES(close), pct_chg=VALUES(pct_chg), volume=VALUES(volume),
             amount=VALUES(amount), macd=VALUES(macd),
             macd_signal=VALUES(macd_signal), macd_hist=VALUES(macd_hist)"""
    _write_df(df, sql, cols, "stock_daily")


def save_sector_daily_to_db(df: pd.DataFrame):
    """写入板块日线 (board_code/board_name/date/low/high/close/prev_close/
    volume/amount/pct_chg/change/advance/decline/net_inflow)"""
    cols = ["board_code", "board_name", "date", "low", "high", "close",
            "prev_close", "volume", "amount", "pct_chg", "change",
            "advance", "decline", "net_inflow"]
    sql = """INSERT INTO sector_daily
             (board_code, board_name, date, low, high, close,
              prev_close, volume, amount, pct_chg, `change`,
              advance, decline, net_inflow)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
             ON DUPLICATE KEY UPDATE
             board_name=VALUES(board_name), low=VALUES(low), high=VALUES(high),
             close=VALUES(close), prev_close=VALUES(prev_close),
             volume=VALUES(volume), amount=VALUES(amount), pct_chg=VALUES(pct_chg),
             `change`=VALUES(`change`), advance=VALUES(advance), decline=VALUES(decline),
             net_inflow=VALUES(net_inflow)"""
    _write_df(df, sql, cols, "sector_daily")


def save_etf_daily_to_db(df: pd.DataFrame):
    """写入ETF日线 (code/name/date/prev_close/open/high/low/close/volume/amount/
    pct_chg/change)"""
    cols = ["code", "name", "date", "prev_close", "open", "high", "low",
            "close", "volume", "amount", "pct_chg", "change"]
    sql = """INSERT INTO etf_daily
             (code, name, date, prev_close, open, high, low,
              close, volume, amount, pct_chg, `change`)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
             ON DUPLICATE KEY UPDATE
             name=VALUES(name), prev_close=VALUES(prev_close), open=VALUES(open),
             high=VALUES(high), low=VALUES(low), close=VALUES(close),
             volume=VALUES(volume), amount=VALUES(amount), pct_chg=VALUES(pct_chg),
             `change`=VALUES(`change`)"""
    _write_df(df, sql, cols, "etf_daily")


def save_candidate_sector_to_db(df: pd.DataFrame):
    """写入候选板块 (每次清空全表, 只保留最新一批候选)"""
    cols = ["board_code", "board_name", "date", "identified_at",
            "close", "score", "ret_20d", "vol_ratio",
            "low", "high", "prev_close", "volume", "amount",
            "pct_chg", "change", "advance", "decline", "net_inflow", "chg_5d"]
    sql = """INSERT INTO candidate_sector
             (board_code, board_name, date, identified_at,
              close, score, ret_20d, vol_ratio,
              low, high, prev_close, volume, amount,
              pct_chg, `change`, advance, decline, net_inflow, chg_5d)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
             ON DUPLICATE KEY UPDATE
             board_name=VALUES(board_name), date=VALUES(date),
             close=VALUES(close), score=VALUES(score), ret_20d=VALUES(ret_20d),
             vol_ratio=VALUES(vol_ratio),
             low=VALUES(low), high=VALUES(high), prev_close=VALUES(prev_close),
             volume=VALUES(volume), amount=VALUES(amount), pct_chg=VALUES(pct_chg),
             `change`=VALUES(`change`), advance=VALUES(advance),
             decline=VALUES(decline), net_inflow=VALUES(net_inflow),
             chg_5d=VALUES(chg_5d)"""
    _write_df(df, sql, cols, "candidate_sector", clear_before=True)


def save_candidate_stock_to_db(df: pd.DataFrame, replace_date: bool = False):
    """写入候选股票 (每次清空全表, 只保留最新一批候选)"""
    cols = ["code", "name", "board_code", "board_name", "date", "identified_at",
            "open", "close", "pct_chg", "macd", "chg_5d", "avg_rel", "tag",
            "score", "rps_20", "rps_60", "trend", "momentum",
            "volume_score", "macd_score", "sector_score"]
    sql = """INSERT INTO candidate_stock
             (code, name, board_code, board_name, date, identified_at,
              open, close, pct_chg, macd, chg_5d, avg_rel, tag,
              score, rps_20, rps_60, trend, momentum,
              volume_score, macd_score, sector_score)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
             ON DUPLICATE KEY UPDATE
             name=VALUES(name), board_code=VALUES(board_code),
             board_name=VALUES(board_name), date=VALUES(date),
             open=VALUES(open), close=VALUES(close), pct_chg=VALUES(pct_chg),
             macd=VALUES(macd), chg_5d=VALUES(chg_5d), avg_rel=VALUES(avg_rel),
             tag=VALUES(tag), score=VALUES(score), rps_20=VALUES(rps_20),
             rps_60=VALUES(rps_60), trend=VALUES(trend), momentum=VALUES(momentum),
             volume_score=VALUES(volume_score), macd_score=VALUES(macd_score),
             sector_score=VALUES(sector_score)"""
    _write_df(df, sql, cols, "candidate_stock", clear_before=True)


def save_candidate_etf_to_db(df: pd.DataFrame, replace_date: bool = False):
    """写入候选ETF (每次清空全表, 只保留最新一批候选)"""
    cols = ["code", "name", "date", "identified_at",
            "close", "score", "ret_20d", "vol_ratio",
            "prev_close", "pct_chg", "volume", "amount", "chg_5d"]
    sql = """INSERT INTO candidate_etf
             (code, name, date, identified_at,
              close, score, ret_20d, vol_ratio,
              prev_close, pct_chg, volume, amount, chg_5d)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
             ON DUPLICATE KEY UPDATE
             name=VALUES(name), date=VALUES(date),
             close=VALUES(close), score=VALUES(score), ret_20d=VALUES(ret_20d),
             vol_ratio=VALUES(vol_ratio),
             prev_close=VALUES(prev_close), pct_chg=VALUES(pct_chg),
             volume=VALUES(volume), amount=VALUES(amount), chg_5d=VALUES(chg_5d)"""
    _write_df(df, sql, cols, "candidate_etf", clear_before=True)


def _write_df(df: pd.DataFrame, sql: str, cols: list, table: str,
              clear_before: bool = False):
    if df is None or df.empty:
        return
    ensure_table(table)
    conn = get_connection()
    cursor = conn.cursor()
    try:
        if clear_before:
            cursor.execute(f"DELETE FROM {table}")
            logger.info(f"{table} 已清空历史数据, 仅保留最新候选")
        total = len(df)
        chunk = 5000
        for i in range(0, total, chunk):
            rows = []
            for _, row in df.iloc[i:i + chunk].iterrows():
                rows.append(tuple(_n(row.get(c)) for c in cols))
            cursor.executemany(sql, rows)
        conn.commit()
        logger.info(f"{table} 已写入 MySQL ({total} 条)")
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cursor.close()
        conn.close()


def _n(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    return val
