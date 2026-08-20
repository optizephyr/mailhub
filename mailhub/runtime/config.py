from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import os

import yaml
from dotenv import load_dotenv


# YAML 只允许这些旋钮；部署项只来自环境变量。
_YAML_KEYS = frozenset(
    {
        "source_id",
        "calendar_name",
        "reminders_list",
        "lookback_days",
        "mail_limit",
        "reminder_minutes",
        "calendar_scan_days",
        "llm_model",
    }
)

_ENV_KEYS = {
    "QQ_EMAIL": "qq_email",
    "QQ_AUTH_CODE": "qq_auth_code",
    "CALDAV_URL": "caldav_url",
    "CALDAV_USERNAME": "caldav_username",
    "CALDAV_PASSWORD": "caldav_password",
    "LLM_API_BASE": "llm_api_base",
    "LLM_API_KEY": "llm_api_key",
    "BARK_SERVER_URL": "bark_server_url",
    "BARK_KEY": "bark_key",
}

_INT_FIELDS = frozenset(
    {"lookback_days", "mail_limit", "reminder_minutes", "calendar_scan_days"}
)

_STRING_FALLBACKS = {
    "llm_model": "gpt-4o-mini",
    "source_id": "qq.default",
}


@dataclass(frozen=True)
class Settings:
    # data_dir 由 loader 注入；YAML 不控制。
    data_dir: Path
    qq_email: str = ""
    qq_auth_code: str = ""
    caldav_url: str = ""
    caldav_username: str = ""
    caldav_password: str = ""
    calendar_name: str = ""
    reminders_list: str = ""
    lookback_days: int = 14
    mail_limit: int = 80
    reminder_minutes: int = 30
    llm_api_base: str = ""
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    calendar_scan_days: int = 90
    source_id: str = "qq.default"
    bark_key: str = ""
    bark_server_url: str = ""

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.llm_api_base)

    @property
    def bark_enabled(self) -> bool:
        return bool(self.bark_key and self.bark_server_url)

    @property
    def lifecycle_log_path(self) -> Path:
        return self.data_dir / "logs" / "mail_lifecycle.jsonl"

    @property
    def llm_io_log_path(self) -> Path:
        return self.data_dir / "logs" / "llm_io.jsonl"


def _coerce(key: str, value: Any) -> Any:
    """校验类型并规范化字符串值。"""
    if key in _INT_FIELDS:
        # bool 是 int 的子类，先排除
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"{key} 必须是整数（如 14），当前得到 {value!r}（{type(value).__name__}）"
            )
        return value

    if not isinstance(value, str):
        raise ValueError(
            f"{key} 必须是字符串，当前得到 {value!r}（{type(value).__name__}）"
        )

    cleaned = value.strip()
    if key in {"llm_api_base", "bark_server_url", "caldav_url"}:
        # URL 末尾斜杠归一，避免与子路径拼接出现 //
        return cleaned.rstrip("/")
    if key in _STRING_FALLBACKS:
        return cleaned or _STRING_FALLBACKS[key]
    return cleaned


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_settings(config_path: Path | str | None = None) -> Settings:
    """从 YAML 旋钮与环境变量部署项加载配置。"""
    root = _project_root()
    config_file = Path(config_path) if config_path else root / "config.yaml"

    if not config_file.exists():
        raise FileNotFoundError(
            f"找不到配置文件 {config_file}\n"
            f"仓库应包含 config.yaml（行为旋钮）。部署项见 .env.example。"
        )

    dotenv_file = config_file.parent / ".env"
    if dotenv_file.is_file():
        load_dotenv(dotenv_file, override=False)

    with config_file.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{config_file} 顶层必须是 key: value 映射，当前是 {type(raw).__name__}"
        )

    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    unknown = sorted(set(raw) - _YAML_KEYS)
    if unknown:
        raise ValueError(
            f"{config_file} 包含未知配置项 {unknown}；"
            f"YAML 允许的项：{sorted(_YAML_KEYS)}。"
            f"邮箱、口令、服务入口请写在 .env（见 .env.example）"
        )

    clean: dict[str, Any] = {"data_dir": data_dir}
    for key, value in raw.items():
        clean[key] = _coerce(key, value)

    for env_name, field in _ENV_KEYS.items():
        value = os.environ.get(env_name)
        if value is None:
            continue
        clean[field] = _coerce(field, value)

    return Settings(**clean)


def require_mail_credentials(settings: Settings) -> None:
    if not settings.qq_email or not settings.qq_auth_code:
        raise SystemExit(
            "请先在环境变量或 .env 填写 QQ_EMAIL 和 QQ_AUTH_CODE"
            "（QQ邮箱授权码，不是登录密码）"
        )


def require_bark_config(settings: Settings) -> None:
    has_key = bool(settings.bark_key)
    has_url = bool(settings.bark_server_url)
    if has_key and has_url:
        return
    if not has_key and not has_url:
        return

    missing = []
    if not has_key:
        missing.append("密钥")
    if not has_url:
        missing.append("服务器地址")
    raise SystemExit(f"缺少 Bark {'和'.join(missing)}")


def require_caldav_config(settings: Settings) -> None:
    if not settings.calendar_name and not settings.reminders_list:
        return
    require_caldav_account(settings)


def require_caldav_account(settings: Settings) -> None:
    missing = []
    if not settings.caldav_url:
        missing.append("服务器地址")
    if not settings.caldav_username:
        missing.append("用户名")
    if not settings.caldav_password:
        missing.append("密码")
    if missing:
        raise SystemExit(f"缺少 CalDAV {'、'.join(missing)}")
