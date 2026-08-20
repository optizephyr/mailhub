from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Settings:
    # data_dir 由 loader 注入；YAML 不控制。
    data_dir: Path
    qq_email: str = ""
    qq_auth_code: str = ""
    apple_calendar_name: str = "日历"
    lookback_days: int = 14
    mail_limit: int = 80
    reminder_minutes: int = 30
    llm_api_base: str = ""
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    calendar_scan_days: int = 90
    source_id: str = "qq.default"
    apple_reminders_list: str = "提醒事项"
    bark_enabled: bool = False
    bark_key: str = ""
    bark_server_url: str = ""

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.llm_api_base)

    @property
    def lifecycle_log_path(self) -> Path:
        return self.data_dir / "logs" / "mail_lifecycle.jsonl"

    @property
    def llm_io_log_path(self) -> Path:
        return self.data_dir / "logs" / "llm_io.jsonl"


_INT_FIELDS = frozenset(
    {"lookback_days", "mail_limit", "reminder_minutes", "calendar_scan_days"}
)
_BOOL_FIELDS = frozenset({"bark_enabled"})

# 空字符串回退到默认值的字符串字段
_STRING_FALLBACKS = {
    "apple_calendar_name": "日历",
    "llm_model": "gpt-4o-mini",
    "source_id": "qq.default",
    "apple_reminders_list": "提醒事项",
}


def _coerce(key: str, value: Any) -> Any:
    """校验类型并规范化字符串值。"""
    if key in _INT_FIELDS:
        # bool 是 int 的子类，先排除
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"{key} 必须是整数（如 14），当前得到 {value!r}（{type(value).__name__}）"
            )
        return value

    if key in _BOOL_FIELDS:
        if not isinstance(value, bool):
            raise ValueError(
                f"{key} 必须是布尔值（true 或 false），"
                f"当前得到 {value!r}（{type(value).__name__}）"
            )
        return value

    if not isinstance(value, str):
        raise ValueError(
            f"{key} 必须是字符串，当前得到 {value!r}（{type(value).__name__}）"
        )

    cleaned = value.strip()
    if key in {"llm_api_base", "bark_server_url"}:
        # URL 末尾斜杠归一，避免与子路径拼接出现 //
        return cleaned.rstrip("/")
    if key in _STRING_FALLBACKS:
        return cleaned or _STRING_FALLBACKS[key]
    return cleaned


def load_settings(config_path: Path | str | None = None) -> Settings:
    """从 YAML 文件加载配置；缺文件即报错并提示复制示例。"""
    root = Path(__file__).resolve().parents[2]
    config_file = Path(config_path) if config_path else root / "config.yaml"

    if not config_file.exists():
        raise FileNotFoundError(
            f"找不到配置文件 {config_file}\n"
            f"请先复制示例：cp {root / 'config.example.yaml'} {config_file}\n"
            f"然后填入 QQ_EMAIL 和 QQ_AUTH_CODE"
        )

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

    # YAML 只能填 Settings 认识的键；未知键直接报错，避免拼写错误静默用默认值。
    allowed = {f.name for f in fields(Settings)} - {"data_dir"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(
            f"{config_file} 包含未知配置项 {unknown}；允许的项：{sorted(allowed)}"
        )

    clean: dict[str, Any] = {"data_dir": data_dir}
    for key, value in raw.items():
        clean[key] = _coerce(key, value)

    return Settings(**clean)


def require_mail_credentials(settings: Settings) -> None:
    if not settings.qq_email or not settings.qq_auth_code:
        raise SystemExit(
            "请先在 config.yaml 填写 qq_email 和 qq_auth_code"
            "（QQ邮箱授权码，不是登录密码）"
        )


def require_bark_config(settings: Settings) -> None:
    if not settings.bark_enabled:
        return

    missing = []
    if not settings.bark_key:
        missing.append("密钥")
    if not settings.bark_server_url:
        missing.append("服务器地址")
    if missing:
        raise SystemExit(f"Bark 已启用，但缺少 Bark {'和'.join(missing)}")