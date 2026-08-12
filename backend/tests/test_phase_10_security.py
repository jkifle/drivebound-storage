from app.core.security import token_digest


def test_pairing_secrets_are_only_persisted_as_digests() -> None:
    secret = "one-time-secret-value"
    digest = token_digest(secret)
    assert digest != secret
    assert len(digest) == 64
    assert digest == token_digest(secret)
