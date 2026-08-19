from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    qq_email: str
    qq_auth_code: str
    apple_calendar_name: str
    lookback_days: int
    mail_limit: int
    reminder_minutes: int
    llm_api_base: str
    llm_api_key: str
    llm_model: str
    data_dir: Path
    calendar_scan_days: int = 90
    source_id: str = "qq.default"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.llm_api_base)

    @property
    def lifecycle_log_path(self) -> Path:
        return self.data_dir / "logs" / "mail_lifecycle.jsonl"

    @property
    def llm_io_log_path(self) -> Path:
        return self.data_dir / "logs" / "llm_io.jsonl"


def load_settings(env_file: str | None = None) -> Settings:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(env_file or root / ".env")

    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        qq_email=os.getenv("QQ_EMAIL", "").strip(),
        qq_auth_code=os.getenv("QQ_AUTH_CODE", "").strip(),
        apple_calendar_name=os.getenv("APPLE_CALENDAR_NAME", "日历").strip() or "日历",
        lookback_days=int(os.getenv("LOOKBACK_DAYS", "14")),
        mail_limit=int(os.getenv("MAIL_LIMIT", "80")),
        reminder_minutes=int(os.getenv("REMINDER_MINUTES", "30")),
        llm_api_base=os.getenv("LLM_API_BASE", "").strip().rstrip("/"),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini").strip(),
        data_dir=data_dir,
        calendar_scan_days=int(os.getenv("CALENDAR_SCAN_DAYS", "90")),
        source_id=os.getenv("MAIL_SOURCE_ID", "qq.default").strip() or "qq.default",
    )


def require_mail_credentials(settings: Settings) -> None:
    if not settings.qq_email or not settings.qq_auth_code:
        raise SystemExit(
            "请先在 .env 填写 QQ_EMAIL 和 QQ_AUTH_CODE（QQ邮箱授权码，不是登录密码）"
        )
