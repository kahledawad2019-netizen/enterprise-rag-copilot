"""
Centralised, validated, secret-safe configuration.

Precedence (highest first):
  1. Real process environment variables  (what CI / prod should use)
  2. Windows Credential Manager via `keyring`  (opt-in, for the DB password)
  3. The .env file                            (local development)

The DB password is held as a pydantic ``SecretStr``: printing the settings
object, logging it, or dumping it to JSON shows ``**********`` instead of the
value. The only place it is ever unwrapped is when the ODBC connection string
is built, and that string is never logged.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

# Service/entry names used if you choose to store the password in the
# Windows Credential Manager instead of .env (see `scripts/set_secret.py`).
KEYRING_SERVICE = "rag-system-mssql"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------- SQL Server ----------------
    mssql_server: str = Field(default=r".\SQLEXPRESS")
    mssql_database: str = Field(default="master")
    mssql_driver: str = Field(default="ODBC Driver 18 for SQL Server")
    mssql_auth_mode: Literal["windows", "sql"] = "windows"
    mssql_username: str | None = None
    mssql_password: SecretStr | None = None
    mssql_encrypt: bool = True
    mssql_trust_server_certificate: bool = True
    mssql_connect_timeout: int = 30

    # ---------------- Ollama ----------------
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:latest"
    ollama_timeout: float = 240.0
    ollama_num_ctx: int = 8192
    ollama_temperature: float = 0.1

    # ---------------- Vector store ----------------
    chroma_path: Path = PROJECT_ROOT / "data" / "chroma"
    n_results: int = 10

    # ---------------- Guardrails ----------------
    read_only: bool = True
    max_rows: int = 1000
    allow_llm_to_see_data: bool = False

    # ---------------- App ----------------
    log_level: str = "INFO"
    verbose: bool = False  # True -> dump full LLM prompts to the console

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @field_validator("mssql_username", "mssql_database", "mssql_server", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _check_auth(self) -> "Settings":
        if self.mssql_auth_mode == "sql":
            # Fall back to the Windows Credential Manager when .env has no password.
            if self.mssql_password is None:
                fetched = _password_from_keyring(self.mssql_username or "")
                if fetched:
                    object.__setattr__(self, "mssql_password", SecretStr(fetched))
            if not self.mssql_username or self.mssql_password is None:
                raise ValueError(
                    "MSSQL_AUTH_MODE=sql requires MSSQL_USERNAME and a password "
                    "(set MSSQL_PASSWORD in .env, or store it in the Windows "
                    "Credential Manager with: python scripts/set_secret.py)"
                )
        return self

    # ------------------------------------------------------------------
    # Connection strings
    # ------------------------------------------------------------------
    def odbc_connection_string(self) -> str:
        """Full ODBC string INCLUDING the password. Never log the result."""
        parts = [
            f"DRIVER={{{self.mssql_driver}}}",
            f"SERVER={self.mssql_server}",
            f"DATABASE={self.mssql_database}",
            f"Encrypt={'yes' if self.mssql_encrypt else 'no'}",
            f"TrustServerCertificate={'yes' if self.mssql_trust_server_certificate else 'no'}",
            f"Connection Timeout={self.mssql_connect_timeout}",
        ]
        if self.mssql_auth_mode == "windows":
            parts.append("Trusted_Connection=yes")
        else:
            parts.append(f"UID={self.mssql_username}")
            parts.append(f"PWD={self.mssql_password.get_secret_value()}")
        return ";".join(parts) + ";"

    def safe_connection_string(self) -> str:
        """Same string with the password masked - safe to print or log."""
        s = self.odbc_connection_string()
        if self.mssql_auth_mode == "sql" and self.mssql_password is not None:
            s = s.replace(self.mssql_password.get_secret_value(), "********")
        return s

    def describe(self) -> str:
        return (
            f"server={self.mssql_server} db={self.mssql_database} "
            f"auth={self.mssql_auth_mode} encrypt={self.mssql_encrypt} "
            f"model={self.ollama_model} read_only={self.read_only}"
        )


def _password_from_keyring(username: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, username)
    except Exception:  # keyring missing or no Windows backend available
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    logging.basicConfig(
        level=getattr(logging, s.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    s.chroma_path.mkdir(parents=True, exist_ok=True)
    return s
