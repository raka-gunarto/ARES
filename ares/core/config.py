from __future__ import annotations

import getpass
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ares.core.secrets import BaseSecretStore, SecretNotFound


class ConfigError(Exception):
    """Raised when source configuration is invalid."""

    pass


class ProdTripwire(Exception):
    """Raised at startup when ARES_ENV=prod but the security separation the
    whole model depends on is absent (§14.4). Fatal by design: a misconfigured
    VM that silently runs everything as one user must fail loudly, not boot."""

    pass


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


class TraceConfig(BaseModel):
    """Live activity-trace configuration (rotating JSONL of agent activity)."""

    enabled: bool = True
    path: str = "/var/lib/ares/trace/trace.jsonl"
    max_mb: int = 100
    backups: int = 3


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
    trace: TraceConfig = Field(default_factory=TraceConfig)
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


def enforce_prod_tripwires(config: Config, *, secret_file: Path | None = None) -> None:
    """Fail fast in prod when the user/secret separation is missing (§14.4).

    No-op in dev. In prod, raises :class:`ProdTripwire` (fatal at startup) if
    the secret file is readable by the daemon user (secrets must arrive via
    ``os.environ``, the ``.env`` being ``0600 root:root``), or if a
    privilege-dropping plugin (``shell``/``selfedit``) is enabled without a
    distinct ``sandbox_user`` to drop to. These are the exact silent-collapse
    conditions §14.4 says must become a loud error rather than a running VM.
    """
    if os.environ.get("ARES_ENV", "dev") != "prod":
        return

    problems: list[str] = []
    if secret_file is not None and os.access(secret_file, os.R_OK):
        problems.append(
            f"secret file '{secret_file}' is readable by the daemon user "
            "(prod requires 0600 root:root; secrets come from the environment)"
        )

    # Only the shell tool drops to a sandbox user in prod; self-edit is fully
    # API-driven (no local exec), so it needs no sandbox_user (PATCH-3).
    me = getpass.getuser()
    shell = config.plugins.get("shell", {})
    if shell.get("enabled"):
        sandbox_user = shell.get("sandbox_user", "")
        if not sandbox_user or sandbox_user == me:
            problems.append(
                f"shell: sandbox_user missing or equals the daemon user "
                f"'{me}' — no privilege separation"
            )

    if problems:
        raise ProdTripwire(
            "refusing to start in prod:\n  - " + "\n  - ".join(problems)
        )
