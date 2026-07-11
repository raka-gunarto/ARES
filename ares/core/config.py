from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from ares.core.secrets import BaseSecretStore, SecretNotFound


class LLMConfig(BaseModel):
    """LLM service configuration."""

    base_url: str
    api_key: str
    model: str
    temperature: float = 0.7
    max_tool_iterations: int = 10


class SessionConfig(BaseModel):
    """Session management configuration."""

    history_limit: int = 30
    timeout_minutes: int = 45


class MemoryConfig(BaseModel):
    """Memory storage configuration."""

    root: str
    retention_days: int = 14


class TasksConfig(BaseModel):
    """Tasks storage configuration."""

    db_path: str


class UserConfig(BaseModel):
    """User-specific configuration."""

    sip_uri: str | None = None
    ntfy_topic: str | None = None


class Config(BaseModel):
    """Top-level application configuration.

    Rejects unknown top-level keys per spec §4.7.
    """

    model_config = ConfigDict(extra="forbid")

    persona: str
    timezone: str
    llm: LLMConfig
    session: SessionConfig
    memory: MemoryConfig
    tasks: TasksConfig
    users: dict[str, UserConfig]
    plugins: dict[str, dict]


def load_config(path: Path, secrets: BaseSecretStore) -> Config:
    """Load and parse application configuration from a YAML file.

    The YAML file may contain !secret tags that reference secret keys.
    Secrets are resolved at load time using the provided secret store.

    Args:
        path: Path to the YAML configuration file.
        secrets: Secret store for resolving !secret tags.

    Returns:
        Parsed Config object.

    Raises:
        SecretNotFound: If a !secret tag references a missing key.
        ValidationError: If the configuration does not match the Config schema.
    """

    class _SecretLoader(yaml.SafeLoader):
        """YAML loader with support for !secret tag."""

        pass

    def _construct_secret(loader: yaml.Loader, node: yaml.Node) -> str:
        """Construct a secret value from a YAML node.

        Args:
            loader: The YAML loader instance.
            node: The scalar node containing the secret key.

        Returns:
            The secret value resolved from the secret store.

        Raises:
            SecretNotFound: If the key is not found in the store.
        """
        key = loader.construct_scalar(node)
        return secrets.get(key)

    _SecretLoader.add_constructor("!secret", _construct_secret)

    with open(path) as f:
        raw = yaml.load(f, Loader=_SecretLoader)

    return Config(**raw)
