from __future__ import annotations

import getpass
import os
import types

import pytest

from ares.core.config import ProdTripwire, enforce_prod_tripwires
from ares.core.secrets import EnvSecretStore, SecretNotFound


# ============================================================================
# Test 1: Dev mode never raises
# ============================================================================


def test_dev_never_trips(monkeypatch, tmp_path):
    """Dev mode (default) never raises ProdTripwire."""
    # Unset ARES_ENV (defaults to dev)
    monkeypatch.delenv("ARES_ENV", raising=False)

    config = types.SimpleNamespace(
        plugins={"shell": {"enabled": True, "sandbox_user": ""}}
    )
    secret_file = tmp_path / ".env"
    secret_file.write_text("FOO=bar")
    secret_file.chmod(0o644)

    # Should NOT raise
    enforce_prod_tripwires(config, secret_file=secret_file)


def test_dev_with_explicit_env_never_trips(monkeypatch, tmp_path):
    """Dev mode with explicit ARES_ENV=dev never raises ProdTripwire."""
    monkeypatch.setenv("ARES_ENV", "dev")

    config = types.SimpleNamespace(
        plugins={"shell": {"enabled": True, "sandbox_user": ""}}
    )
    secret_file = tmp_path / ".env"
    secret_file.write_text("FOO=bar")
    secret_file.chmod(0o644)

    # Should NOT raise
    enforce_prod_tripwires(config, secret_file=secret_file)


# ============================================================================
# Test 2: Prod mode trips on readable secret file
# ============================================================================


def test_prod_trips_on_readable_secret(monkeypatch, tmp_path):
    """Prod mode raises ProdTripwire if secret file is readable."""
    monkeypatch.setenv("ARES_ENV", "prod")

    secret_file = tmp_path / ".env"
    secret_file.write_text("API_KEY=secret123")
    secret_file.chmod(0o644)  # Readable by daemon user

    config = types.SimpleNamespace(plugins={})

    with pytest.raises(ProdTripwire):
        enforce_prod_tripwires(config, secret_file=secret_file)


# ============================================================================
# Test 3: Prod mode trips on missing sandbox_user
# ============================================================================


def test_prod_trips_on_missing_sandbox_user(monkeypatch):
    """Prod mode raises ProdTripwire if sandbox_user is missing or empty."""
    monkeypatch.setenv("ARES_ENV", "prod")

    config = types.SimpleNamespace(
        plugins={"shell": {"enabled": True, "sandbox_user": ""}}
    )

    with pytest.raises(ProdTripwire) as exc_info:
        enforce_prod_tripwires(config, secret_file=None)

    assert "sandbox_user" in str(exc_info.value)


# ============================================================================
# Test 4: Prod mode trips on sandbox_user == daemon user
# ============================================================================


def test_prod_trips_on_sandbox_user_equals_daemon(monkeypatch):
    """Prod mode raises ProdTripwire if the shell sandbox_user equals the daemon user."""
    monkeypatch.setenv("ARES_ENV", "prod")

    current_user = getpass.getuser()
    config = types.SimpleNamespace(
        plugins={"shell": {"enabled": True, "sandbox_user": current_user}}
    )

    with pytest.raises(ProdTripwire):
        enforce_prod_tripwires(config, secret_file=None)


def test_prod_selfedit_needs_no_sandbox_user(monkeypatch):
    """PATCH-3: self-edit is API-driven, so enabling it in prod without a
    sandbox_user must NOT trip (unlike shell)."""
    monkeypatch.setenv("ARES_ENV", "prod")
    config = types.SimpleNamespace(
        plugins={"selfedit": {"enabled": True}}  # no sandbox_user, and that's fine now
    )
    enforce_prod_tripwires(config, secret_file=None)  # must not raise


# ============================================================================
# Test 5: Prod mode OK when properly separated
# ============================================================================


def test_prod_ok_when_properly_separated(monkeypatch, tmp_path):
    """Prod mode OK when sandbox_user differs from daemon user."""
    monkeypatch.setenv("ARES_ENV", "prod")

    config = types.SimpleNamespace(
        plugins={
            "shell": {"enabled": True, "sandbox_user": "ares-sbx"},
            "selfedit": {"enabled": False},
        }
    )
    secret_file = tmp_path / "nope.env"  # Does not exist

    # Should NOT raise
    enforce_prod_tripwires(config, secret_file=secret_file)


# ============================================================================
# Test 6: Prod mode ignores disabled plugins
# ============================================================================


def test_prod_disabled_plugins_ignored(monkeypatch):
    """Prod mode ignores disabled plugins."""
    monkeypatch.setenv("ARES_ENV", "prod")

    config = types.SimpleNamespace(
        plugins={"shell": {"enabled": False, "sandbox_user": ""}}
    )

    # Should NOT raise (disabled plugin ignored)
    enforce_prod_tripwires(config, secret_file=None)


# ============================================================================
# Test 7: EnvSecretStore respects ARES_ENV
# ============================================================================


def test_secretstore_prod_ignores_dotenv(monkeypatch, tmp_path):
    """EnvSecretStore in prod mode ignores .env file."""
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("FOO=bar")

    monkeypatch.setenv("ARES_ENV", "prod")
    store = EnvSecretStore(dotenv_file)

    # FOO is not in os.environ, only in .env file
    # In prod mode, dotenv is never loaded, so SecretNotFound is raised
    with pytest.raises(SecretNotFound):
        store.get("FOO")


def test_secretstore_dev_loads_dotenv(monkeypatch, tmp_path):
    """EnvSecretStore in dev mode loads .env file."""
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("FOO=bar")

    monkeypatch.delenv("ARES_ENV", raising=False)  # Default to dev
    store = EnvSecretStore(dotenv_file)

    # In dev mode, dotenv is loaded
    assert store.get("FOO") == "bar"
