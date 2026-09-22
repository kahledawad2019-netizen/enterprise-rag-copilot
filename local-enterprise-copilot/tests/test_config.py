"""
Configuration and secret-handling tests.

The important assertion here is that a password cannot leak through the usual
accidental routes: repr, str, model_dump, JSON, or a log line.
"""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from enterprise_copilot.config import PROFILES, ProfileName, get_profile, recommend_profile
from enterprise_copilot.config.settings import DatabaseSettings, Settings


class TestProfiles:
    def test_three_profiles_exist(self) -> None:
        assert set(PROFILES) == {ProfileName.LITE, ProfileName.STANDARD, ProfileName.HIGH}

    def test_profiles_are_ordered_by_cost(self) -> None:
        assert PROFILES[ProfileName.LITE].approx_vram_gb < PROFILES[ProfileName.STANDARD].approx_vram_gb
        assert PROFILES[ProfileName.STANDARD].approx_vram_gb < PROFILES[ProfileName.HIGH].approx_vram_gb

    def test_lite_profile_has_no_reranker(self) -> None:
        """Lite targets CPU-only machines, where a cross-encoder is too slow."""
        assert PROFILES[ProfileName.LITE].reranker_model is None
        assert PROFILES[ProfileName.LITE].cpu_only_viable is True

    def test_no_profile_declares_an_embedding_dimension(self) -> None:
        """Dimension is detected at runtime; assuming it corrupts the index."""
        for profile in PROFILES.values():
            assert not hasattr(profile, "embedding_dimension")

    def test_unknown_profile_raises_a_clear_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown model profile"):
            get_profile("enormous")

    @pytest.mark.parametrize(
        ("ram_gb", "vram_gb", "expected"),
        [
            (8, 0, ProfileName.LITE),
            (16, 0, ProfileName.LITE),
            (32, 8, ProfileName.STANDARD),
            (32, 16, ProfileName.HIGH),
            (64, 24, ProfileName.HIGH),
        ],
    )
    def test_hardware_recommendation(self, ram_gb: float, vram_gb: float, expected) -> None:
        assert recommend_profile(ram_gb, vram_gb) == expected


class TestSecretHandling:
    def _sql_settings(self) -> DatabaseSettings:
        return DatabaseSettings(
            auth_mode="sql", username="copilot_reader",
            password=SecretStr("SuperSecret123!"), server="testhost",
            database="TestDb",
        )

    def test_password_is_not_in_repr(self) -> None:
        assert "SuperSecret123!" not in repr(self._sql_settings())

    def test_password_is_not_in_str(self) -> None:
        assert "SuperSecret123!" not in str(self._sql_settings())

    def test_password_is_not_in_model_dump_json(self) -> None:
        assert "SuperSecret123!" not in self._sql_settings().model_dump_json()

    def test_password_is_present_in_the_real_connection_string(self) -> None:
        """It must actually be there, or the connection would fail."""
        assert "PWD=SuperSecret123!" in self._sql_settings().odbc_connection_string()

    def test_safe_connection_string_masks_the_password(self) -> None:
        safe = self._sql_settings().safe_odbc_connection_string()
        assert "SuperSecret123!" not in safe
        assert "********" in safe
        assert "testhost" in safe  # still useful for diagnostics

    def test_sql_auth_without_credentials_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="MSSQL_AUTH_MODE=sql"):
            DatabaseSettings(auth_mode="sql", username=None, password=None)

    def test_windows_auth_stores_no_password(self) -> None:
        settings = DatabaseSettings(auth_mode="windows")
        assert settings.password is None
        assert "Trusted_Connection=yes" in settings.odbc_connection_string()
        assert "PWD=" not in settings.odbc_connection_string()


class TestSettingsShape:
    def test_describe_never_exposes_a_secret(self, settings: Settings) -> None:
        rendered = json.dumps(settings.describe())
        for banned in ("PWD=", "password", "Trusted_Connection"):
            assert banned not in rendered

    def test_env_override_beats_the_profile(self, settings: Settings) -> None:
        overridden = settings.model_copy(
            update={"ollama": settings.ollama.model_copy(update={"chat_model": "custom:7b"})}
        )
        assert overridden.chat_model == "custom:7b"

    def test_profile_supplies_the_model_when_unset(self, settings: Settings) -> None:
        assert settings.chat_model == settings.profile.chat_model

    def test_encryption_is_on_by_default(self) -> None:
        assert DatabaseSettings().encrypt is True

    def test_security_blocklist_covers_credential_shaped_columns(self, settings: Settings) -> None:
        blocked = {c.lower() for c in settings.security.blocked_columns}
        assert {"password_hash", "api_key", "secret", "token"} <= blocked

    def test_ai_and_security_schemas_are_not_queryable(self, settings: Settings) -> None:
        """The AI must not read its own audit trail or the permission tables."""
        allowed = {s.lower() for s in settings.security.allowed_schemas}
        assert "security" not in allowed
        assert "ai" not in allowed
