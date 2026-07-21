from pathlib import Path

import pytest

from scripts.ensure_operator_token import TOKEN_NAME, ensure_operator_token


def test_ensure_operator_token_creates_secret_without_returning_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    path.write_text("EXISTING=value\n", encoding="utf-8")

    assert ensure_operator_token(path) is True

    text = path.read_text(encoding="utf-8")
    token = next(
        line.split("=", 1)[1]
        for line in text.splitlines()
        if line.startswith(f"{TOKEN_NAME}=")
    )
    assert len(token) >= 32
    assert token.isascii()
    assert not any(character.isspace() for character in token)


def test_ensure_operator_token_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        f"{TOKEN_NAME}=existing-secret-value-0123456789\n",
        encoding="utf-8",
    )

    assert ensure_operator_token(path) is False
    assert path.read_text(encoding="utf-8") == (
        f"{TOKEN_NAME}=existing-secret-value-0123456789\n"
    )


def test_ensure_operator_token_replaces_one_blank_value(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(f"{TOKEN_NAME}=\n", encoding="utf-8")

    assert ensure_operator_token(path) is True
    assert path.read_text(encoding="utf-8").startswith(f"{TOKEN_NAME}=")
    assert path.read_text(encoding="utf-8") != f"{TOKEN_NAME}=\n"


def test_ensure_operator_token_rejects_duplicates(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        f"{TOKEN_NAME}=first\n{TOKEN_NAME}=second\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match=TOKEN_NAME):
        ensure_operator_token(path)
