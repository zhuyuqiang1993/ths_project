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


def save_stock_list_to_db(df: pd.DataFrame):
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


def save_stock_daily_to_db(df: pd.DataFrame, code: str):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        inserted = 0
        for _, row in df.iterrows():
            sql = """INSERT INTO stock_daily
                     (code, date, open, close, high, low, volume, amount,
                      amplitude, pct_chg, price_change, turnover_rate)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                     ON DUPLICATE KEY UPDATE
                     open=VALUES(open), close=VALUES(close), high=VALUES(high),
                     low=VALUES(low), volume=VALUES(volume), amount=VALUES(amount),
                     amplitude=VALUES(amplitude), pct_chg=VALUES(pct_chg),
                     price_change=VALUES(price_change), turnover_rate=VALUES(turnover_rate)"""
            vals = (
                code,
                str(row["date"]),
                _n(row.get("open")),
                _n(row.get("close")),
                _n(row.get("high")),
                _n(row.get("low")),
                _n(row.get("volume")),
                _n(row.get("amount")),
                _n(row.get("amplitude")),
                _n(row.get("pct_chg")),
                _n(row.get("price_change")),
                _n(row.get("turnover_rate")),
            )
            cursor.execute(sql, vals)
            if cursor.rowcount == 1:
                inserted += 1
        conn.commit()
        logger.info(f"  -> {code} MySQL 写入完成 (新增 {inserted}, 更新 {len(df) - inserted})")
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
