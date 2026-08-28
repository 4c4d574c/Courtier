"""SettingsStore — encrypted persistence, masking, audit (sqlite-backed)."""


import pytest
from cryptography.fernet import Fernet

from courtier.db.db_manager import AsyncDatabase
from courtier.settings_store import FernetCodec, SettingsKeyMissing, SettingsStore


@pytest.fixture
async def db(tmp_path):
    database = AsyncDatabase(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    await database.create_all()
    yield database
    await database.drop_all(testing=True)
    await database.engine.dispose()


class TestFernetCodec:
    def test_accepts_raw_fernet_key(self):
        raw = Fernet.generate_key().decode()
        codec = FernetCodec(raw)
        assert codec.decrypt(codec.encrypt("秘密")) == "秘密"

    def test_accepts_hex_material(self):
        codec = FernetCodec("ab" * 32)
        assert codec.decrypt(codec.encrypt("秘密")) == "秘密"

    def test_stretches_short_passphrase(self):
        codec = FernetCodec("short-passphrase")
        assert codec.decrypt(codec.encrypt("秘密")) == "秘密"

    def test_wrong_key_returns_none(self):
        encrypted = FernetCodec("aa" * 32).encrypt("秘密")
        assert FernetCodec("bb" * 32).decrypt(encrypted) is None

    def test_from_env_unset_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv("COURTIER_SETTINGS_KEY", raising=False)
        # Isolate from the developer's real .env (which now carries the key).
        env_file = tmp_path / ".env"
        env_file.write_text("LLM_IP=http://x\n", encoding="utf-8")
        monkeypatch.setattr("courtier.config._ENV_FILE", str(env_file))
        assert FernetCodec.from_env() is None

    def test_from_env_builds_codec(self, monkeypatch):
        monkeypatch.setenv("COURTIER_SETTINGS_KEY", "cd" * 32)
        assert FernetCodec.from_env() is not None

    def test_from_env_falls_back_to_env_file(self, monkeypatch, tmp_path):
        """Dev runs don't export .env into os.environ — the key must also
        be readable from the .env file (Tier 0 is not a Settings field)."""
        from courtier.config import _ENV_FILE, get_settings_encryption_key

        env_file = tmp_path / ".env"
        env_file.write_text("LLM_IP=http://x\nCOURTIER_SETTINGS_KEY=efef\n", encoding="utf-8")
        monkeypatch.delenv("COURTIER_SETTINGS_KEY", raising=False)
        monkeypatch.setattr("courtier.config._ENV_FILE", str(env_file))

        assert get_settings_encryption_key() == "efef"
        assert FernetCodec.from_env() is not None

        # Process env wins over the file.
        monkeypatch.setenv("COURTIER_SETTINGS_KEY", "ab" * 32)
        assert get_settings_encryption_key() == "ab" * 32

        # Missing everywhere → empty string (secrets stay fail-closed).
        monkeypatch.delenv("COURTIER_SETTINGS_KEY", raising=False)
        env_file.write_text("LLM_IP=http://x\n", encoding="utf-8")
        assert get_settings_encryption_key() == ""
        assert FernetCodec.from_env() is None
        assert _ENV_FILE  # original constant untouched (patched via monkeypatch)


class TestSettingsStore:
    async def test_plain_roundtrip(self, db):
        store = SettingsStore(db)
        await store.save({"llm_model": "qwen3.6-27b"}, actor="admin")

        overrides, unreadable = await store.load_overrides()
        assert overrides == {"llm_model": "qwen3.6-27b"}
        assert unreadable == []

    async def test_secret_encrypted_at_rest(self, db):
        codec = FernetCodec("ab" * 32)
        store = SettingsStore(db, codec)
        await store.save({"llm_api_key": "sk-plain"}, actor="admin")

        rows = await store.load_rows()
        stored = rows[0].value
        assert "__enc__" in stored and "sk-plain" not in str(stored)
        assert rows[0].is_secret is True
        assert rows[0].category == "model"

        overrides, _ = await store.load_overrides()
        assert overrides["llm_api_key"] == "sk-plain"

    async def test_secret_without_key_fails_closed(self, db):
        store = SettingsStore(db, codec=None)
        with pytest.raises(SettingsKeyMissing, match="COURTIER_SETTINGS_KEY"):
            await store.save({"llm_api_key": "sk-plain"}, actor="admin")
        # Nothing was persisted, not even plaintext.
        assert await store.load_rows() == []

    async def test_unknown_field_rejected(self, db):
        store = SettingsStore(db)
        with pytest.raises(KeyError, match="non-editable"):
            await store.save({"mysql_url": "mysql://x"}, actor="admin")

    async def test_tier0_fields_rejected(self, db):
        store = SettingsStore(db)
        with pytest.raises(KeyError):
            await store.save({"upload_dir": "/elsewhere"}, actor="admin")

    async def test_wrong_key_secret_reported_unreadable(self, db):
        store = SettingsStore(db, FernetCodec("ab" * 32))
        await store.save({"llm_api_key": "sk-plain"}, actor="admin")

        other_store = SettingsStore(db, FernetCodec("cd" * 32))
        overrides, unreadable = await other_store.load_overrides()
        assert "llm_api_key" not in overrides
        assert unreadable == ["llm_api_key"]

    async def test_audit_rows_written_with_hashes(self, db):
        codec = FernetCodec("ab" * 32)
        store = SettingsStore(db, codec)
        await store.save({"llm_model": "model-a"}, actor="alice")
        await store.save({"llm_model": "model-b"}, actor="bob")

        audit = await store.load_audit()
        assert len(audit) == 2
        latest = audit[0]
        assert latest["key"] == "llm_model"
        assert latest["actor"] == "bob"
        assert latest["old_hash"] == audit[1]["new_hash"]
        assert latest["old_hash"] != latest["new_hash"]
        # Plaintext never lands in the audit trail.
        assert "model-a" not in str(audit) and "model-b" not in str(audit)

    async def test_secret_audit_hashes_differ_from_plaintext(self, db):
        store = SettingsStore(db, FernetCodec("ab" * 32))
        await store.save({"llm_api_key": "sk-plain"}, actor="admin")
        audit = await store.load_audit()
        assert "sk-plain" not in str(audit)
