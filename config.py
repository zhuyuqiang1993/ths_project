import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import List


@dataclass
class Config:
    stock_list_api: str = "ths_stock_spot"
    stock_daily_api: str = "ths_kline"

    start_date: str = "20100101"
    end_date: str = ""

    data_dir: Path = Path("./data")
    log_dir: Path = Path("./logs")
    log_level: str = "INFO"

    retry_times: int = 3
    retry_delay: float = 2.0
    request_interval: float = 0.5

    daily_run_time: str = "21:35"

    csv_encoding: str = "utf-8-sig"

    exclude_stock_codes: List[str] = field(default_factory=lambda: [])

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = "ths_project_2024"
    mysql_database: str = "stock_db"

    deepseek_api_key: str = os.environ.get("DS_APP_KEY", "")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    mail_smtp_host: str = "smtp.qq.com"
    mail_smtp_port: int = 465
    mail_sender: str = os.environ.get("MAIL_SENDER", "")
    mail_auth_code: str = os.environ.get("MAIL_AUTH_CODE", "")


CONFIG = Config()
