from __future__ import annotations

import abc
import os
from pathlib import Path

from dotenv import dotenv_values


class SecretNotFound(Exception):
    """Raised when a secret key is not found in the store."""

    pass


class BaseSecretStore(abc.ABC):
    """Abstract base class for secret stores."""

    @abc.abstractmethod
    def get(self, key: str) -> str:
        """Retrieve a secret value by key.

        Args:
            key: The secret key to retrieve.

        Returns:
            The secret value as a string.

        Raises:
            SecretNotFound: If the key is not found in the store.
        """
        pass


class EnvSecretStore(BaseSecretStore):
    """Secret store that reads from dotenv files and environment variables.

    First checks values loaded from dotenv_path (if provided), then checks
    os.environ, then raises SecretNotFound.
    """

    def __init__(self, dotenv_path: Path | None = None) -> None:
        """Initialize the environment secret store.

        Args:
            dotenv_path: Optional path to a .env file to load (dev only). In
                prod the dotenv fallback is disabled entirely: secrets must
                come from ``os.environ`` (systemd ``EnvironmentFile=`` injects
                them as root before dropping to the ``ares`` user), and the
                ``.env`` file is ``0600 root:root`` — unreadable to the daemon
                (§14.2). Loading a dotenv in prod would defeat that isolation.
        """
        self._values: dict[str, str] = {}
        env_mode = os.environ.get("ARES_ENV", "dev")
        if dotenv_path and env_mode != "prod":
            self._values = dotenv_values(dotenv_path)

    def get(self, key: str) -> str:
        """Retrieve a secret value by key.

        Checks in order: dotenv values, os.environ, then raises SecretNotFound.

        Args:
            key: The secret key to retrieve.

        Returns:
            The secret value as a string.

        Raises:
            SecretNotFound: If the key is not found.
        """
        if key in self._values:
            return self._values[key]
        if key in os.environ:
            return os.environ[key]
        raise SecretNotFound(key)
