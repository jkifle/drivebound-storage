"""add passkey credentials, WebAuthn challenges, and recent auth state

Revision ID: 0016
Revises: 0015
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing sessions deliberately have no recent-auth grant and must prove
    # identity again before performing a high-risk action.
    op.add_column("auth_sessions", sa.Column("reauthenticated_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("password_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("users", sa.Column("password_set_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("pending_totp_secret_encrypted", sa.Text()))
    op.add_column("users", sa.Column("pending_totp_expires_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("pending_totp_session_id", postgresql.UUID(as_uuid=True)))
    # Discard only abandoned pre-confirmation secrets from the legacy flow.
    # An enabled factor with a missing secret is a security-critical corruption:
    # abort instead of silently downgrading the account's MFA policy.
    op.execute(
        "UPDATE users SET totp_secret_encrypted = NULL, totp_last_used_step = NULL "
        "WHERE totp_secret_encrypted IS NOT NULL AND totp_enabled_at IS NULL"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM users WHERE totp_enabled_at IS NOT NULL "
        "AND totp_secret_encrypted IS NULL) THEN "
        "RAISE EXCEPTION 'Cannot migrate: an enabled MFA account is missing its encrypted secret'; "
        "END IF; END $$"
    )
    op.create_check_constraint(
        "ck_users_totp_complete",
        "users",
        "(totp_secret_encrypted IS NULL AND totp_enabled_at IS NULL) OR "
        "(totp_secret_encrypted IS NOT NULL AND totp_enabled_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_users_pending_totp_complete",
        "users",
        "(pending_totp_secret_encrypted IS NULL AND pending_totp_expires_at IS NULL "
        "AND pending_totp_session_id IS NULL) OR "
        "(pending_totp_secret_encrypted IS NOT NULL AND pending_totp_expires_at IS NOT NULL "
        "AND pending_totp_session_id IS NOT NULL)",
    )
    op.execute("UPDATE users SET password_set_at = created_at")
    # Historical Google-created accounts cannot be distinguished perfectly
    # from local accounts that later linked Google. Treat every linked account
    # conservatively until it proves its password or sets a fresh one.
    op.execute(
        "UPDATE users SET password_enabled = false, password_set_at = NULL "
        "WHERE EXISTS (SELECT 1 FROM external_identities "
        "WHERE external_identities.user_id = users.id AND external_identities.provider = 'google')"
    )
    op.add_column(
        "account_tokens",
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("auth_sessions.id", ondelete="CASCADE"),
        ),
    )
    op.create_index("ix_account_tokens_session_id", "account_tokens", ["session_id"])
    op.create_table(
        "passkey_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("credential_id", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("transports", sa.JSON(), nullable=False),
        sa.Column("aaguid", sa.String(36)),
        sa.Column("device_type", sa.String(32), nullable=False),
        sa.Column("backed_up", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("sign_count >= 0", name="ck_passkey_credentials_sign_count"),
    )
    op.create_index("ix_passkey_credentials_user_id", "passkey_credentials", ["user_id"])
    op.create_table(
        "webauthn_challenges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("auth_sessions.id", ondelete="CASCADE"),
        ),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("challenge", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column("discoverable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "purpose IN ('passkey_registration', 'passkey_authentication', 'passkey_reauthentication')",
            name="ck_webauthn_challenges_purpose",
        ),
        sa.CheckConstraint(
            "(purpose = 'passkey_authentication' AND session_id IS NULL) OR "
            "(purpose IN ('passkey_registration', 'passkey_reauthentication') "
            "AND session_id IS NOT NULL AND user_id IS NOT NULL)",
            name="ck_webauthn_challenges_binding",
        ),
    )
    op.create_index("ix_webauthn_challenges_user_id", "webauthn_challenges", ["user_id"])
    op.create_index("ix_webauthn_challenges_session_id", "webauthn_challenges", ["session_id"])
    op.create_index("ix_webauthn_challenges_purpose", "webauthn_challenges", ["purpose"])
    op.create_index("ix_webauthn_challenges_expires_at", "webauthn_challenges", ["expires_at"])


def downgrade() -> None:
    op.drop_table("webauthn_challenges")
    op.drop_table("passkey_credentials")
    op.drop_index("ix_account_tokens_session_id", table_name="account_tokens")
    op.drop_column("account_tokens", "session_id")
    op.drop_constraint("ck_users_totp_complete", "users", type_="check")
    op.drop_constraint("ck_users_pending_totp_complete", "users", type_="check")
    op.drop_column("users", "pending_totp_session_id")
    op.drop_column("users", "pending_totp_expires_at")
    op.drop_column("users", "pending_totp_secret_encrypted")
    op.drop_column("users", "password_set_at")
    op.drop_column("users", "password_enabled")
    op.drop_column("auth_sessions", "reauthenticated_at")
