from pathlib import Path
from dataclasses import dataclass, field
from typing import List


@dataclass
class Config:
    stock_list_api: str = "stock_zh_a_spot_em"
    stock_daily_api: str = "stock_zh_a_hist"

    start_date: str = "20100101"
    end_date: str = ""

    data_dir: Path = Path("./data")
    log_dir: Path = Path("./logs")
    log_level: str = "INFO"

    retry_times: int = 3
    retry_delay: float = 2.0
    request_interval: float = 0.5

    daily_run_time: str = "18:00"

    csv_encoding: str = "utf-8-sig"

    exclude_stock_codes: List[str] = field(default_factory=lambda: [])

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = "ths_project_2024"
    mysql_database: str = "stock_db"

    @property
    def stock_list_path(self) -> Path:
        return self.data_dir / "stock_list.csv"


CONFIG = Config()
