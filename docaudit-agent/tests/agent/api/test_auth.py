"""Tests for auth middleware helpers."""

import importlib
import os

import pytest

from courtier.agent.api.middleware.auth import hash_password, verify_password


class TestHashPassword:
    def test_hash_password_uses_bcrypt_rounds_setting(self, monkeypatch):
        """hash_password() honors the BCRYPT_ROUNDS setting."""
        original = os.environ.get("BCRYPT_ROUNDS")
        monkeypatch.setenv("BCRYPT_ROUNDS", "4")
        # The settings singleton is created at import time; reload the config
        # module so the new env var is picked up by get_settings().
        import courtier.config as config_module

        importlib.reload(config_module)
        try:
            hashed = hash_password("secret")
            assert verify_password("secret", hashed) is True
            assert verify_password("wrong", hashed) is False
            # bcrypt encodes the cost in the hash prefix.
            assert hashed.startswith("$2b$04$")
        finally:
            if original is None:
                monkeypatch.delenv("BCRYPT_ROUNDS", raising=False)
            else:
                monkeypatch.setenv("BCRYPT_ROUNDS", original)
            importlib.reload(config_module)

    def test_hash_password_uses_explicit_rounds(self):
        """Explicit rounds parameter overrides the setting."""
        hashed = hash_password("secret", rounds=4)
        assert hashed.startswith("$2b$04$")
        assert verify_password("secret", hashed) is True
