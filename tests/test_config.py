from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from ares.core.config import Config, load_config
from ares.core.secrets import EnvSecretStore, SecretNotFound


def test_secret_resolution() -> None:
    """Test that !secret tags are resolved from dotenv file and defaults work."""
    # Load the real config.yaml with the .env.example stub secrets
    config_path = Path(__file__).resolve().parent.parent / "instance" / "config.yaml"
    env_path = Path(__file__).resolve().parent.parent / "instance" / ".env.example"

    secrets = EnvSecretStore(env_path)
    cfg = load_config(config_path, secrets)

    # Assert it's a Config object
    assert isinstance(cfg, Config)

    # Test !secret resolution: LLM_API_KEY should resolve to "ollama" from .env.example
    assert cfg.llm.api_key == "ollama"

    # Test !secret resolution: DASHBOARD_PASSWORD should resolve to "changeme"
    assert cfg.plugins["dashboard"]["password"] == "changeme"

    # Test defaults: temperature should default to 0.7
    assert cfg.llm.temperature == 0.7

    # Test defaults: max_tool_iterations should default to 10
    assert cfg.llm.max_tool_iterations == 10

    # Test typed access: users["primary"].ntfy_topic should be "ares-primary"
    assert cfg.users["primary"].ntfy_topic == "ares-primary"


def test_secret_not_found(tmp_path: Path) -> None:
    """Test that SecretNotFound is raised when a secret key is missing."""
    # Create a minimal YAML config with all 8 required top-level keys
    # but with a !secret tag referencing a key that won't exist
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("""
persona: Test persona
timezone: UTC
llm:
  base_url: http://localhost:11434/v1
  api_key: !secret ARES_TEST_MISSING_XYZ
  model: test-model
  temperature: 0.7
  max_tool_iterations: 10
session:
  history_limit: 30
  timeout_minutes: 45
memory:
  root: /tmp/memory
  retention_days: 14
tasks:
  db_path: /tmp/tasks.db
users:
  primary:
    sip_uri: null
    ntfy_topic: null
plugins:
  test:
    enabled: true
""")

    # Ensure the key is genuinely absent from os.environ
    if "ARES_TEST_MISSING_XYZ" in os.environ:
        del os.environ["ARES_TEST_MISSING_XYZ"]

    # Create an EnvSecretStore without a dotenv file
    secrets = EnvSecretStore()

    # Assert SecretNotFound is raised
    with pytest.raises(SecretNotFound):
        load_config(config_yaml, secrets)


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    """Test that an unknown top-level key raises ValidationError."""
    # Create a minimal YAML config with all 8 required keys plus an extra bogus key
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("""
persona: Test persona
timezone: UTC
llm:
  base_url: http://localhost:11434/v1
  api_key: test-key
  model: test-model
  temperature: 0.7
  max_tool_iterations: 10
session:
  history_limit: 30
  timeout_minutes: 45
memory:
  root: /tmp/memory
  retention_days: 14
tasks:
  db_path: /tmp/tasks.db
users:
  primary:
    sip_uri: null
    ntfy_topic: null
plugins:
  test:
    enabled: true
bogus: 1
""")

    # Use an empty EnvSecretStore (no secrets needed)
    secrets = EnvSecretStore()

    # Assert ValidationError is raised for the unknown top-level key
    with pytest.raises(ValidationError):
        load_config(config_yaml, secrets)
