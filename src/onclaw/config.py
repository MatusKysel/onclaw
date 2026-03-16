from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, model_validator


class OnclawConfig(BaseModel):
    slack_app_token: str | None = None
    slack_bot_token: str | None = None
    telegram_bot_token: str | None = None
    anthropic_api_key: str
    kubeconfig_path: str | None = None          # None = in-cluster / auto-detect
    memory_path: str = "onclaw_memory.db"
    claude_model: str = "claude-sonnet-4-20250514"
    claude_fast_model: str = "claude-haiku-4-5-20251001"
    claude_max_tokens: int = 4096
    max_log_lines: int = 200
    max_concurrent_investigations: int = 3

    @model_validator(mode="after")
    def check_platform_configured(self) -> OnclawConfig:
        has_slack = bool(self.slack_app_token and self.slack_bot_token)
        has_telegram = bool(self.telegram_bot_token)
        if not has_slack and not has_telegram:
            raise ValueError(
                "At least one platform must be configured: "
                "set SLACK_APP_TOKEN + SLACK_BOT_TOKEN for Slack, "
                "and/or TELEGRAM_BOT_TOKEN for Telegram"
            )
        return self


_ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)\}")


def _substitute_env_vars(obj: object) -> object:
    """Recursively substitute ${ENV_VAR} placeholders with environment variable values."""
    if isinstance(obj, str):
        def replacer(match: re.Match[str]) -> str:
            var_name = match.group(1)
            value = os.environ.get(var_name)
            if value is None:
                raise ValueError(
                    f"Environment variable '{var_name}' is not set "
                    f"(referenced as ${{{var_name}}} in config)"
                )
            return value
        return _ENV_VAR_PATTERN.sub(replacer, obj)
    elif isinstance(obj, dict):
        return {k: _substitute_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_substitute_env_vars(item) for item in obj]
    return obj


def load_config(config_path: str | Path | None = None) -> OnclawConfig:
    """Load config from file (if provided) merged with env vars.

    Priority: config file values < env var overrides.
    If no config file, builds config entirely from env vars + defaults.
    """
    file_values: dict = {}

    if config_path is not None:
        path = Path(config_path)
        if path.exists():
            with open(path) as f:
                raw = yaml.safe_load(f)
            if isinstance(raw, dict):
                file_values = _substitute_env_vars(raw)

    # Env vars override / provide values
    def _env_or(key: str, file_key: str | None = None, default: object = None) -> object:
        env_val = os.environ.get(key)
        if env_val is not None:
            return env_val
        if file_key and file_key in file_values:
            return file_values[file_key]
        if default is not None:
            return default
        return None

    kubeconfig = _env_or("KUBECONFIG", "kubeconfig_path")
    if kubeconfig is None:
        # Default to ~/.kube/config if it exists
        default_path = Path.home() / ".kube" / "config"
        if default_path.exists():
            kubeconfig = str(default_path)

    return OnclawConfig(
        slack_app_token=_env_or("SLACK_APP_TOKEN", "slack_app_token"),
        slack_bot_token=_env_or("SLACK_BOT_TOKEN", "slack_bot_token"),
        telegram_bot_token=_env_or("TELEGRAM_BOT_TOKEN", "telegram_bot_token"),
        anthropic_api_key=_env_or("ANTHROPIC_API_KEY", "anthropic_api_key"),
        kubeconfig_path=kubeconfig,
        memory_path=_env_or("ONCLAW_MEMORY_PATH", "memory_path", "onclaw_memory.db"),
        claude_model=_env_or("CLAUDE_MODEL", "claude_model", "claude-sonnet-4-20250514"),
        claude_fast_model=_env_or("CLAUDE_FAST_MODEL", "claude_fast_model", "claude-haiku-4-5-20251001"),
        claude_max_tokens=int(_env_or("CLAUDE_MAX_TOKENS", "claude_max_tokens", 4096)),
        max_log_lines=int(_env_or("MAX_LOG_LINES", "max_log_lines", 200)),
        max_concurrent_investigations=int(
            _env_or("MAX_CONCURRENT_INVESTIGATIONS", "max_concurrent_investigations", 3)
        ),
    )
