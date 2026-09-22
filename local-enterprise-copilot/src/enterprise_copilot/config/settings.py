"""
Central typed configuration.

Every tunable in the system is declared here, validated by pydantic, and loaded
from the environment or `.env`. Nothing reads `os.environ` directly.

Secret handling
---------------
Anything credential-shaped is a `SecretStr`, so it renders as `**********` when
a settings object is logged, printed, dumped to JSON, or shown in the Streamlit
debug page. The only unwrapping happens inside the connection-string builders,
whose output is never logged. Use `safe_odbc_connection_string()` for display.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .profiles import ModelProfile, ProfileName, get_profile

# src/enterprise_copilot/config/settings.py -> project root is 4 levels up
PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = PROJECT_ROOT / ".env"

KEYRING_SERVICE = "enterprise-copilot"


def resolve_path(value: object) -> object:
    """Resolve a relative path against the project root, not the CWD.

    `.env` ships values like `QDRANT_PATH=./data/qdrant`, which are relative.
    Left alone, they resolve against whatever directory the process happened to
    start in — so running a script from elsewhere silently *creates a fresh,
    empty* Qdrant store instead of opening the real one, and retrieval returns
    nothing with no error. Found when executing the notebook via nbconvert.
    """
    if value is None or isinstance(value, Path) and value.is_absolute():
        return value
    path = Path(value) if not isinstance(value, Path) else value
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


class DatabaseSettings(BaseSettings):
    """SQL Server connection and access policy."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_prefix="MSSQL_", extra="ignore",
        env_file_encoding="utf-8", case_sensitive=False,
    )

    server: str = r".\SQLEXPRESS"
    database: str = "EnterpriseCopilot"
    driver: str = "ODBC Driver 18 for SQL Server"

    auth_mode: Literal["windows", "sql"] = "windows"
    username: str | None = None
    password: SecretStr | None = None

    encrypt: bool = True
    trust_server_certificate: bool = True
    connect_timeout: int = 30

    # Read-only policy, enforced in security/sql_guard.py as well as by the
    # database principal itself.
    query_timeout_seconds: int = 30
    max_result_rows: int = 5000

    @field_validator("username", "password", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        """Treat an empty .env value as absent.

        `MSSQL_PASSWORD=` is the documented way to say "the password lives in
        the Windows Credential Manager". Without this, pydantic produces
        SecretStr('') rather than None, the keyring fallback never fires, and
        an empty password reaches the driver.
        """
        if isinstance(v, SecretStr):
            v = v.get_secret_value()
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="after")
    def _resolve_password(self) -> "DatabaseSettings":
        if self.auth_mode == "sql" and self.password is None and self.username:
            fetched = _password_from_keyring(self.username)
            if fetched:
                object.__setattr__(self, "password", SecretStr(fetched))
        if self.auth_mode == "sql" and (not self.username or self.password is None):
            raise ValueError(
                "MSSQL_AUTH_MODE=sql needs MSSQL_USERNAME and a password. "
                "Set MSSQL_PASSWORD, or store it with: python scripts/set_secret.py"
            )
        return self

    def odbc_connection_string(self, *, database: str | None = None) -> str:
        """Build the ODBC string. Contains the password - never log this."""
        parts = [
            f"DRIVER={{{self.driver}}}",
            f"SERVER={self.server}",
            f"DATABASE={database or self.database}",
            f"Encrypt={'yes' if self.encrypt else 'no'}",
            f"TrustServerCertificate={'yes' if self.trust_server_certificate else 'no'}",
            f"Connection Timeout={self.connect_timeout}",
        ]
        if self.auth_mode == "windows":
            parts.append("Trusted_Connection=yes")
        else:
            parts.append(f"UID={self.username}")
            parts.append(f"PWD={self.password.get_secret_value()}")  # type: ignore[union-attr]
        return ";".join(parts) + ";"

    def safe_odbc_connection_string(self, *, database: str | None = None) -> str:
        """The same string with the password masked. Safe to log or display."""
        raw = self.odbc_connection_string(database=database)
        if self.auth_mode == "sql" and self.password is not None:
            raw = raw.replace(self.password.get_secret_value(), "********")
        return raw

    def sqlalchemy_url(self, *, database: str | None = None) -> str:
        from urllib.parse import quote_plus

        return "mssql+pyodbc:///?odbc_connect=" + quote_plus(
            self.odbc_connection_string(database=database)
        )


class OllamaSettings(BaseSettings):
    """Local inference runtime. No cloud provider is ever contacted."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_prefix="OLLAMA_", extra="ignore",
        env_file_encoding="utf-8", case_sensitive=False,
    )

    host: str = "http://localhost:11434"
    timeout_seconds: float = 300.0
    keep_alive: str = "5m"

    # Optional overrides. When unset, the active profile decides.
    chat_model: str | None = None
    embedding_model: str | None = None


class VectorStoreSettings(BaseSettings):
    """Qdrant configuration.

    Two modes are supported:
      embedded - Qdrant runs in-process against a local path. No Docker needed.
      server   - Qdrant runs as a service at `url`.

    Embedded is the default because the reference machine has no Docker.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_prefix="QDRANT_", extra="ignore",
        env_file_encoding="utf-8", case_sensitive=False,
    )

    mode: Literal["embedded", "server"] = "embedded"
    path: Path = PROJECT_ROOT / "data" / "qdrant"
    url: str = "http://localhost:6333"
    api_key: SecretStr | None = None
    collection: str = "enterprise_documents"

    # Bumped whenever chunking or the embedding model changes, so that an index
    # built by an older configuration is never silently queried by a newer one.
    index_version: str = "v1"

    _resolve = field_validator("path", mode="before")(resolve_path)


class RetrievalSettings(BaseSettings):
    """Chunking and retrieval knobs, all tuned against evals/ rather than guessed."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_prefix="RETRIEVAL_", extra="ignore",
        env_file_encoding="utf-8", case_sensitive=False,
    )

    chunk_target_tokens: int = 400
    chunk_overlap_tokens: int = 60
    chunk_min_tokens: int = 50

    # Reciprocal Rank Fusion constant. 60 is the value from the original RRF
    # paper and is a reasonable default, not a tuned optimum.
    rrf_k: int = 60

    enable_query_rewriting: bool = True
    enable_reranking: bool = True
    enable_parent_expansion: bool = True
    mmr_lambda: float = 0.7

    # always | adaptive | never. Adaptive skips reranking when the ranking is
    # already confident -- chiefly when dense and sparse independently agree on
    # the top result. The evaluation showed 78 of 96 queries were unchanged by
    # reranking, so most of that 2.4 s per query buys nothing.
    rerank_policy: str = "adaptive"
    # Secondary check only. Calibrated to the observed RRF score distribution
    # (median top-1 margin 0.048, max 0.112); the original 0.35 was unreachable
    # because RRF encodes rank, not magnitude. See reranking/policy.py.
    rerank_margin_threshold: float = 0.10


class ObservabilitySettings(BaseSettings):
    """Tracing and logging. The app must run fine when Phoenix is absent."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_prefix="OBS_", extra="ignore",
        env_file_encoding="utf-8", case_sensitive=False,
    )

    enable_tracing: bool = True
    enable_phoenix: bool = False
    phoenix_endpoint: str = "http://localhost:6006/v1/traces"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "console"
    log_dir: Path = PROJECT_ROOT / "data" / "logs"
    trace_dir: Path = PROJECT_ROOT / "data" / "traces"

    _resolve = field_validator("log_dir", "trace_dir", mode="before")(resolve_path)


class SecuritySettings(BaseSettings):
    """Guardrails applied to generated SQL and retrieved evidence."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_prefix="SECURITY_", extra="ignore",
        env_file_encoding="utf-8", case_sensitive=False,
    )

    # Schemas the generated SQL may read. Everything else is refused.
    allowed_schemas: tuple[str, ...] = ("analytics", "core", "billing", "support")
    # Columns that must never appear in a result set, matched case-insensitively.
    blocked_columns: tuple[str, ...] = (
        "password_hash", "api_key", "secret", "token", "ssn", "tax_id",
    )
    require_sql_approval: bool = False
    enforce_tenant_isolation: bool = True
    max_sql_joins: int = 8

    # Objects that genuinely have no tenant_id column. Demanding a tenant
    # predicate on these produces SQL that fails with "Invalid column name
    # 'tenant_id'", so they are exempt from the isolation check. They are
    # exempt because they hold no customer data, not as a convenience.
    tenant_exempt_objects: tuple[str, ...] = (
        "analytics.vw_month_spine",
        "core.products",
        "core.plans",
        "support.sla_policies",
    )

    # --- semantic intent screening -----------------------------------------
    # An LLM pass that runs AFTER the deterministic rules and can only ever ADD
    # a refusal, never remove one. The rules catch the phrasings someone
    # anticipated; measured against 15 evasive rewordings they missed 12, all
    # of which a model recognises immediately. See security/intent_classifier.py
    # for why the rules are nonetheless kept as the floor.
    enable_llm_intent_screening: bool = True
    # A request is refused when the model's confidence reaches this. Set high
    # because a false refusal is a visible product failure, while a false
    # clearance still meets the SQL guard.
    intent_confidence_threshold: float = 0.7
    # The screen is advisory on timeout: a slow model must not take the product
    # down, and the deterministic layers are still in force underneath.
    intent_timeout_seconds: float = 12.0


class Settings(BaseSettings):
    """Root settings object. Build it with `get_settings()`."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE, extra="ignore",
        env_file_encoding="utf-8", case_sensitive=False,
    )

    app_name: str = "Local Enterprise Intelligence Copilot"
    # Which Text-to-SQL implementation runs. "vanna" is the specified default;
    # "native" is the in-repo reference implementation. Previously this was a
    # hardcoded default inside build_provider(), which meant the evaluation
    # harness and the application could silently disagree about what was being
    # measured -- and for a while they did.
    text_to_sql_provider: str = Field(default="vanna", alias="TEXT_TO_SQL_PROVIDER")
    profile_name: ProfileName = Field(default=ProfileName.STANDARD, alias="COPILOT_PROFILE")
    demo_mode: bool = Field(default=False, alias="COPILOT_DEMO_MODE")
    random_seed: int = Field(default=20240601, alias="COPILOT_SEED")

    project_root: Path = PROJECT_ROOT
    documents_dir: Path = PROJECT_ROOT / "data" / "documents"
    generated_dir: Path = PROJECT_ROOT / "data" / "generated"
    manifests_dir: Path = PROJECT_ROOT / "data" / "manifests"

    _resolve = field_validator(
        "project_root", "documents_dir", "generated_dir", "manifests_dir", mode="before",
    )(resolve_path)

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    vector_store: VectorStoreSettings = Field(default_factory=VectorStoreSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def profile(self) -> ModelProfile:
        return get_profile(self.profile_name)

    @property
    def chat_model(self) -> str:
        """Explicit env override wins over the profile default."""
        return self.ollama.chat_model or self.profile.chat_model

    @property
    def embedding_model(self) -> str:
        return self.ollama.embedding_model or self.profile.embedding_model

    def ensure_directories(self) -> None:
        for path in (
            self.documents_dir, self.generated_dir, self.manifests_dir,
            self.observability.log_dir, self.observability.trace_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        if self.vector_store.mode == "embedded":
            self.vector_store.path.mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict[str, str]:
        """A redacted summary, safe for logs and the UI config page."""
        return {
            "profile": self.profile_name.value,
            "chat_model": self.chat_model,
            "embedding_model": self.embedding_model,
            "reranker": self.profile.reranker_model or "(disabled)",
            "ollama_host": self.ollama.host,
            "sql_server": self.database.server,
            "database": self.database.database,
            "sql_auth": self.database.auth_mode,
            "vector_store": f"qdrant:{self.vector_store.mode}",
            "index_version": self.vector_store.index_version,
            "demo_mode": str(self.demo_mode),
        }


def _password_from_keyring(username: str) -> str | None:
    """Read the DB password from the Windows Credential Manager, if present."""
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, username)
    except Exception:
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    settings = Settings()
    settings.ensure_directories()
    return settings


def reset_settings_cache() -> None:
    """Drop the cached settings. Used by tests and by the UI profile switcher."""
    get_settings.cache_clear()


__all__ = [
    "Settings", "DatabaseSettings", "OllamaSettings", "VectorStoreSettings",
    "RetrievalSettings", "ObservabilitySettings", "SecuritySettings",
    "get_settings", "reset_settings_cache", "PROJECT_ROOT", "KEYRING_SERVICE",
]
