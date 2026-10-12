"""Configuration helpers."""

import os

# Allowlist for DSML content regex to prevent ReDoS
DSML_CONTENT_MAX: int = 100_000

# Backend model — all incoming model names are overridden to this.
BACKEND_MODEL: str = os.environ.get("BACKEND_MODEL", "deepseek-v4-flash")

# Backup backend model — used when falling back to backup upstream.
BACKUP_BACKEND_MODEL: str = os.environ.get("BACKUP_BACKEND_MODEL", BACKEND_MODEL)

# Passthrough mode — when upstream natively supports the protocol
PASSTHROUGH_MODE: bool = os.environ.get("PASSTHROUGH_MODE", "false").lower() in ("true", "1", "yes")

# Display model names — returned to the client, never leaks the backend model.
ANTHROPIC_DISPLAY_MODEL: str = os.environ.get("ANTHROPIC_DISPLAY_MODEL", "claude-fable-5")
RESPONSES_DISPLAY_MODEL: str = os.environ.get("RESPONSES_DISPLAY_MODEL", "gpt-5.6-sol")
CHAT_DISPLAY_MODEL: str = os.environ.get("CHAT_DISPLAY_MODEL", "gpt-5.6-sol")


def get_env_str(key: str, default: str = "") -> str:
    """Read a string from the environment."""
    return os.environ.get(key, default)


def get_env_int(key: str, default: int) -> int:
    """Read an integer from the environment."""
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def map_model_name(_model: str | None, _env: dict | None = None) -> str:
    """Always return BACKEND_MODEL. Every incoming model is replaced."""
    return BACKEND_MODEL


resolve_default_model = map_model_name
