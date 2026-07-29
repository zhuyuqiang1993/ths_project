CREATE DATABASE IF NOT EXISTS stock_db
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE stock_db;

DROP TABLE IF EXISTS stock_daily;
CREATE TABLE stock_daily (
  id            BIGINT       AUTO_INCREMENT PRIMARY KEY,
  code          VARCHAR(10)  NOT NULL COMMENT '股票代码',
  `date`        DATE         NOT NULL COMMENT '日期',
  `open`        DECIMAL(12,2)   DEFAULT NULL COMMENT '开盘价',
  `close`       DECIMAL(12,2)   DEFAULT NULL COMMENT '收盘价',
  `high`        DECIMAL(12,2)   DEFAULT NULL COMMENT '最高价',
  `low`         DECIMAL(12,2)   DEFAULT NULL COMMENT '最低价',
  volume        BIGINT          DEFAULT NULL COMMENT '成交量',
  amount        DECIMAL(16,2)   DEFAULT NULL COMMENT '成交额',
  amplitude     DECIMAL(6,2)    DEFAULT NULL COMMENT '振幅(%)',
  pct_chg       DECIMAL(6,2)    DEFAULT NULL COMMENT '涨跌幅(%)',
  price_change  DECIMAL(12,2)   DEFAULT NULL COMMENT '涨跌额',
  turnover_rate DECIMAL(10,4)   DEFAULT NULL COMMENT '换手率(%)',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_code_date (code, `date`),
  KEY idx_code (code),
  KEY idx_date (`date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='A股日线数据';

DROP TABLE IF EXISTS stock_list;
CREATE TABLE stock_list (
  code         VARCHAR(10)  NOT NULL PRIMARY KEY COMMENT '股票代码',
  name         VARCHAR(32)  NOT NULL COMMENT '股票名称',
  latest_price DECIMAL(12,2) DEFAULT NULL,
  pct_chg      DECIMAL(6,2)  DEFAULT NULL,
  price_change DECIMAL(12,2) DEFAULT NULL,
  volume       BIGINT        DEFAULT NULL,
  amount       DECIMAL(16,2) DEFAULT NULL,
  amplitude    DECIMAL(6,2)  DEFAULT NULL,
  `high`       DECIMAL(12,2) DEFAULT NULL,
  `low`        DECIMAL(12,2) DEFAULT NULL,
  `open`       DECIMAL(12,2) DEFAULT NULL,
  prev_close   DECIMAL(12,2) DEFAULT NULL,
  volume_ratio DECIMAL(6,2)  DEFAULT NULL,
  turnover_rate DECIMAL(10,4) DEFAULT NULL,
  pe_dynamic   DECIMAL(12,2) DEFAULT NULL,
  pb           DECIMAL(12,2) DEFAULT NULL,
  total_mv     DECIMAL(20,2) DEFAULT NULL,
  float_mv     DECIMAL(20,2) DEFAULT NULL,
  pct_chg_60d  DECIMAL(6,2)  DEFAULT NULL,
  created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='A股列表信息';
