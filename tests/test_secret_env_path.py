from __future__ import annotations

from pathlib import Path

import pytest

from ares.core.config import enforce_prod_tripwires
from instance.main import resolve_secret_env_path


@pytest.fixture
def release_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checked-out release: instance/.env.example present (committed), no .env.

    Mirrors what `git worktree add` produces — the exact shape that crash-looped
    the daemon in prod when main() read the template as a secret file.
    """
    inst = tmp_path / "instance"
    inst.mkdir()
    (inst / ".env.example").write_text("LLM_API_KEY=example\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_prod_ignores_committed_env_example(
    release_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In prod the daemon must NOT read instance/.env.example (secrets = environ)."""
    monkeypatch.setenv("ARES_ENV", "prod")
    env_path = resolve_secret_env_path()
    assert env_path == Path("instance/.env")  # not the readable template

    # And the resulting tripwire check passes (nonexistent instance/.env),
    # even though a readable instance/.env.example sits in the tree.
    class _Cfg:
        plugins: dict = {}

    enforce_prod_tripwires(_Cfg(), secret_file=env_path)  # must not raise


def test_dev_falls_back_to_env_example(
    release_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dev keeps the convenience fallback to the committed template."""
    monkeypatch.setenv("ARES_ENV", "dev")
    assert resolve_secret_env_path() == Path("instance/.env.example")


def test_real_env_used_when_present(
    release_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real instance/.env (if provisioned) is used in either mode."""
    (release_tree / "instance" / ".env").write_text("LLM_API_KEY=real\n")
    monkeypatch.setenv("ARES_ENV", "prod")
    assert resolve_secret_env_path() == Path("instance/.env")
