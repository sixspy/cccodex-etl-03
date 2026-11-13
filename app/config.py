import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Config:
    SECRET = os.environ.get("APP_SECRET", "cc-etl-lab-dev-secret")
    ADMIN_USER = "admin"
    ADMIN_PASSWORD = "123456"
    LAB_NAME = "抽取转换加载台"

    @staticmethod
    def database() -> str:
        return os.environ.get("DATABASE_PATH", str(ROOT / "instance" / "etl_lab.db"))

    @staticmethod
    def settle_day() -> str:
        """配额按后台日历日结算；ETL_SETTLE_DAY 可把结算日钉死（测试用）。"""
        pinned = os.environ.get("ETL_SETTLE_DAY", "").strip()
        return pinned or date.today().isoformat()
