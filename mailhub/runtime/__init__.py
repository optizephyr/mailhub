from .config import Settings, load_settings, require_mail_credentials
from .context import RunContext, RunResult

__all__ = [
    "RunContext",
    "RunResult",
    "Settings",
    "load_settings",
    "require_mail_credentials",
    "run_once",
]


def __getattr__(name: str):
    if name == "run_once":
        from .engine import run_once as _run_once

        return _run_once
    raise AttributeError(name)
