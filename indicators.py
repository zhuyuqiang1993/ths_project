"""技术指标计算工具

提供板块/股票/ETF 筛选所需的基础指标函数:
- 移动平均 (MA)
- 平均真实波幅 (ATR)
- 相对强度百分位 (RPS)
- 量能趋势
- MACD 方向
"""

import pandas as pd
import numpy as np


def calc_ma(series: pd.Series, period: int) -> pd.Series:
    """简单移动平均"""
    return series.rolling(window=period, min_periods=period).mean()


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    """指数移动平均"""
    return series.ewm(span=period, adjust=False).mean()


def calc_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """平均真实波幅 (ATR)"""
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=period).mean()


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI 相对强弱指标"""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_rps_global(all_close: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """计算全局 RPS (相对强度百分位)

    对每个时间点, 计算各品种 N 日收益率在全部品种中的百分位排名。

    Args:
        all_close: index=date, columns=codes, values=close
        window: 回看天数

    Returns:
        同结构 DataFrame, 值域 0-100
    """
    if len(all_close) < window + 1:
        return pd.DataFrame(50.0, index=all_close.index, columns=all_close.columns)

    returns = all_close.pct_change(window)
    rps = returns.rank(axis=1, pct=True) * 100
    return rps


def volume_expansion_ratio(volume: pd.Series, short: int = 5, long: int = 20) -> pd.Series:
    """量能扩张比: 短期均量 / 长期均量

    >1.2 放量, 0.8-1.2 平量, <0.8 缩量
    """
    short_avg = calc_ma(volume, short)
    long_avg = calc_ma(volume, long)
    return short_avg / long_avg


def price_trend_score(close: pd.Series, ma20: pd.Series, ma60: pd.Series) -> pd.Series:
    """价格趋势打分 (0-3)

    1分: close > ma20
    1分: ma20 > ma60
    1分: close > ma60 (多头排列)
    """
    score = pd.Series(0, index=close.index, dtype=float)
    score = score + (close > ma20).astype(float)
    score = score + (ma20 > ma60).astype(float)
    score = score + (close > ma60).astype(float)
    return score


def macd_direction_score(macd: pd.Series, signal: pd.Series, hist: pd.Series) -> pd.Series:
    """MACD 方向打分 (0-3)

    1分: macd > signal (金叉状态)
    1分: hist > 0 (动能为正)
    1分: hist > hist.shift(1) (动能增强)
    """
    score = pd.Series(0, index=macd.index, dtype=float)
    score = score + (macd > signal).astype(float)
    score = score + (hist > 0).astype(float)
    score = score + (hist > hist.shift(1)).astype(float)
    return score


def momentum_score_20d(close: pd.Series) -> pd.Series:
    """20日动量分: 近期收益率 (正值=上涨趋势)"""
    if len(close) < 21:
        return pd.Series(0, index=close.index)
    ret = close / close.shift(20) - 1
    return ret * 100


def breadth_score(advance: pd.Series, decline: pd.Series) -> pd.Series:
    """涨跌家数比得分 (0-1)

    advance > decline → 1, 否则 0
    """
    total = advance + decline
    ratio = advance / total
    return (ratio > 0.5).astype(float)
