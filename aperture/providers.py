"""Multi-provider registry, routing and capabilities for Aperture / Lens.

Enables Codex CLI and third-party tools to configure heterogeneous model providers,
each with independent base_url, wire_api, auth, headers, query parameters,
and capability policies.
"""

from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from typing import Any


DEFAULT_CAPABILITIES = {
    "responses",
    "chat",
    "anthropic",
    "reasoning_summary",
    "structured_output",
    "strict_tools",
}


@dataclass
class ProviderConfig:
    """Configuration for an upstream model provider."""
    id: str
    base_url: str
    api_key: str = ""
    wire_api: str = "auto"  # "responses", "chat", "anthropic", "auto"
    headers: dict[str, str] = field(default_factory=dict)
    query_params: dict[str, str] = field(default_factory=dict)
    timeout_ms: int = 120000
    request_max_retries: int = 2
    stream_max_retries: int = 1
    capabilities: set[str] = field(default_factory=set)
    model_routes: dict[str, str] = field(default_factory=dict)  # client_model -> upstream_model

    def has_capability(self, cap: str) -> bool:
        """Check if provider declares support for a specific feature."""
        return cap in self.capabilities

    def resolve_model(self, model: str) -> str:
        """Map client model to upstream provider model name."""
        return self.model_routes.get(model, model)


class ProviderRegistry:
    """Registry managing model providers and routing rules."""

    def __init__(self):
        self._providers: dict[str, ProviderConfig] = {}
        self._default_provider_id: str = "default"
        self._model_to_provider: dict[str, str] = {}
        self._initialize_from_env()

    def _initialize_from_env(self):
        """Bootstrap registry from environment variables."""
        # 1. Custom JSON config via PROVIDERS_CONFIG if present
        raw_json = os.environ.get("PROVIDERS_CONFIG", "").strip()
        if raw_json:
            try:
                data = json.loads(raw_json)
                if isinstance(data, dict):
                    for pid, pdata in data.items():
                        if isinstance(pdata, dict):
                            self.register_provider(ProviderConfig(
                                id=pid,
                                base_url=pdata.get("base_url", ""),
                                api_key=pdata.get("api_key", ""),
                                wire_api=pdata.get("wire_api", "auto"),
                                headers=pdata.get("headers", {}),
                                query_params=pdata.get("query_params", {}),
                                timeout_ms=int(pdata.get("timeout_ms", 120000)),
                                request_max_retries=int(pdata.get("request_max_retries", 2)),
                                stream_max_retries=int(pdata.get("stream_max_retries", 1)),
                                capabilities=set(pdata.get("capabilities", DEFAULT_CAPABILITIES)),
                                model_routes=pdata.get("models", {}),
                            ))
                            for m in pdata.get("models", {}):
                                self._model_to_provider[m] = pid
            except Exception:
                pass

        # 2. Always ensure default primary provider is registered from existing env
        primary_url = os.environ.get("UPSTREAM_BASE_URL", "https://opencode.ai/zen/go/v1")
        primary_key = os.environ.get("API_KEY", "")
        passthrough = os.environ.get("PASSTHROUGH_MODE", "false").lower() in ("true", "1", "yes")

        default_caps = set(DEFAULT_CAPABILITIES)
        if not passthrough:
            default_caps.discard("responses")  # In translation mode, primary doesn't natively speak responses

        if "default" not in self._providers:
            self._providers["default"] = ProviderConfig(
                id="default",
                base_url=primary_url,
                api_key=primary_key,
                wire_api="responses" if passthrough else "chat",
                capabilities=default_caps,
            )

        # 3. Backup provider if present
        backup_url = os.environ.get("BACKUP_UPSTREAM_BASE_URL", "")
        backup_key = os.environ.get("BACKUP_API_KEY", "")
        if backup_url and "backup" not in self._providers:
            self._providers["backup"] = ProviderConfig(
                id="backup",
                base_url=backup_url,
                api_key=backup_key,
                wire_api="chat",
                capabilities=set(DEFAULT_CAPABILITIES),
            )

    def register_provider(self, config: ProviderConfig) -> None:
        """Register or override a provider configuration."""
        self._providers[config.id] = config
        for client_model in config.model_routes:
            self._model_to_provider[client_model] = config.id

    def get_provider(self, provider_id: str | None = None) -> ProviderConfig:
        """Get provider by id, or return default provider."""
        pid = provider_id or self._default_provider_id
        return self._providers.get(pid, self._providers[self._default_provider_id])

    def resolve_provider_for_model(self, model_name: str) -> ProviderConfig:
        """Resolve which provider should handle a given model name."""
        if not model_name:
            return self.get_provider()
        pid = self._model_to_provider.get(model_name)
        if pid and pid in self._providers:
            return self._providers[pid]
        return self.get_provider()

    def list_providers(self) -> list[ProviderConfig]:
        """Return list of all registered providers."""
        return list(self._providers.values())


# Singleton instance
_registry_instance: ProviderRegistry | None = None


def get_provider_registry() -> ProviderRegistry:
    """Get global ProviderRegistry singleton."""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = ProviderRegistry()
    return _registry_instance


def reset_provider_registry() -> None:
    """Reset singleton (for testing)."""
    global _registry_instance
    _registry_instance = None
