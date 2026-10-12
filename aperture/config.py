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

# Upstream capabilities: comma-separated list or "auto" (e.g. "anthropic,responses,chat")
UPSTREAM_CAPABILITIES: str = os.environ.get("UPSTREAM_CAPABILITIES", "auto").lower()

# Upstream protocol flavor: "standard" (Anthropic/OpenAI official) or "mimo" (Xiaomi MiMo)
UPSTREAM_FLAVOR: str = os.environ.get("UPSTREAM_FLAVOR", "standard").lower()

# Model override mode: "force" (legacy default), "mapped", or "passthrough"
MODEL_OVERRIDE_MODE: str = os.environ.get("MODEL_OVERRIDE_MODE", "force").lower()

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


def get_model_map() -> dict[str, str]:
    """Parse MODEL_MAP environment variable as a JSON dict."""
    raw = os.environ.get("MODEL_MAP", "").strip()
    if not raw:
        return {}
    try:
        import json
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def get_upstream_capabilities() -> set[str]:
    """Return set of protocols supported natively by the upstream."""
    raw = os.environ.get("UPSTREAM_CAPABILITIES", "auto").lower()
    if raw and raw != "auto":
        return {c.strip() for c in raw.split(",") if c.strip()}
    if os.environ.get("PASSTHROUGH_MODE", "false").lower() in ("true", "1", "yes"):
        return {"anthropic", "responses", "chat"}
    return set()


def map_model_name(model: str | None, _env: dict | None = None) -> str:
    """Resolve model name based on MODEL_OVERRIDE_MODE and MODEL_MAP."""
    mode = os.environ.get("MODEL_OVERRIDE_MODE", "force").lower()
    if mode == "passthrough":
        return model or BACKEND_MODEL
    elif mode == "mapped":
        mmap = get_model_map()
        if model and model in mmap:
            return mmap[model]
        return model or BACKEND_MODEL
    # default "force": legacy behavior, map everything to BACKEND_MODEL
    return BACKEND_MODEL


resolve_default_model = map_model_name
