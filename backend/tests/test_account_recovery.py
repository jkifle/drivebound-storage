import asyncio
import getpass
import importlib.util
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models.auth import AuditEvent
from app.models.account_deletion import AccountDeletionJob
from app.models.user import User


def load_recovery_tool():
    path = Path(__file__).resolve().parents[2] / "tools" / "account_recovery.py"
    spec = importlib.util.spec_from_file_location("drivebound_account_recovery_cli", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecoverySession:
    def __init__(self, user: User, deletion_id: uuid.UUID | None = None):
        self.user = user
        self.deletion_id = deletion_id
        self.statements = []
        self.added = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def scalar(self, statement):
        if any(item.get("entity") is AccountDeletionJob for item in statement.column_descriptions):
            return self.deletion_id
        return self.user

    async def execute(self, statement):
        self.statements.append(statement)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True


def test_trusted_host_recovery_revokes_every_authenticator(monkeypatch, capsys) -> None:
    tool = load_recovery_tool()
    user = User(
        id=uuid.uuid4(),
        email="person@example.com",
        password_hash="old",
        password_enabled=False,
        disabled_at=datetime.now(timezone.utc),
        totp_secret_encrypted="encrypted",
        totp_enabled_at=datetime.now(timezone.utc),
        pending_totp_secret_encrypted="pending",
        pending_totp_expires_at=datetime.now(timezone.utc),
        pending_totp_session_id=uuid.uuid4(),
    )
    session = RecoverySession(user)
    monkeypatch.setattr(tool, "async_session_factory", lambda: session)
    monkeypatch.setattr(tool, "hash_password", lambda value: f"hashed:{value}")

    asyncio.run(tool.recover("PERSON@example.com", "replacement-password"))

    assert user.password_hash == "hashed:replacement-password"
    assert user.password_enabled is True
    assert user.disabled_at is None
    assert user.totp_secret_encrypted is None
    assert user.totp_enabled_at is None
    assert user.pending_totp_secret_encrypted is None
    assert session.committed is True
    affected_tables = {statement.table.name for statement in session.statements}
    assert affected_tables == {
        "auth_sessions",
        "mfa_recovery_codes",
        "account_tokens",
        "webauthn_challenges",
        "passkey_credentials",
        "devices",
        "node_pairing_codes",
        "paired_nodes",
        "external_identities",
    }
    audit = next(value for value in session.added if isinstance(value, AuditEvent))
    assert audit.event_type == "administrator_recovery"
    assert audit.detail["all_credentials_revoked"] is True
    assert "replacement-password" not in capsys.readouterr().out


def test_password_file_is_bounded_and_strips_only_trailing_line_endings(tmp_path: Path) -> None:
    tool = load_recovery_tool()
    password_file = tmp_path / "password.txt"
    password_file.write_text("safe replacement password\r\n", encoding="utf-8")
    assert tool.read_password(password_file) == "safe replacement password"

    password_file.write_text("x" * 1025, encoding="utf-8")
    with pytest.raises(SystemExit, match="at most 1024"):
        tool.read_password(password_file)

    password_file.write_text("safe password\nsecond line", encoding="utf-8")
    with pytest.raises(SystemExit, match="line break"):
        tool.read_password(password_file)


def test_interactive_recovery_never_falls_back_to_echoed_input(monkeypatch) -> None:
    tool = load_recovery_tool()

    def unsafe_fallback(_prompt):
        warnings = __import__("warnings")
        warnings.warn("cannot control echo", getpass.GetPassWarning)
        return "would-have-been-echoed"

    monkeypatch.setattr(tool.getpass, "getpass", unsafe_fallback)
    with pytest.raises(SystemExit, match="No private terminal"):
        tool.read_password(None)


def test_trusted_host_recovery_refuses_irreversible_deletion(monkeypatch) -> None:
    tool = load_recovery_tool()
    user = User(id=uuid.uuid4(), email="deleted@example.com", password_hash="old", password_enabled=False)
    session = RecoverySession(user, deletion_id=uuid.uuid4())
    monkeypatch.setattr(tool, "async_session_factory", lambda: session)
    monkeypatch.setattr(tool, "hash_password", lambda value: f"hashed:{value}")

    with pytest.raises(SystemExit, match="irreversible"):
        asyncio.run(tool.recover(user.email, "replacement-password"))

    assert user.password_hash == "old"
    assert session.committed is False


def test_trusted_host_recovery_refuses_ledger_tombstone_without_database_job(monkeypatch) -> None:
    tool = load_recovery_tool()
    user = User(id=uuid.uuid4(), email="restored@example.com", password_hash="old", password_enabled=False)
    session = RecoverySession(user, deletion_id=None)
    monkeypatch.setattr(tool, "async_session_factory", lambda: session)
    monkeypatch.setattr(tool, "hash_password", lambda value: f"hashed:{value}")
    monkeypatch.setattr(tool, "suppression_subject_is_denied", lambda subject: subject == user.id)

    with pytest.raises(SystemExit, match="irreversible"):
        asyncio.run(tool.recover(user.email, "replacement-password"))

    assert user.password_hash == "old"
    assert session.committed is False
