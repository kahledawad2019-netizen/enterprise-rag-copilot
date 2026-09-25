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

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODEL = "qwen/qwen3.8-27b"
# Each Groq model has its own free-tier rate budget; these take over, in
# order, when the main model answers 429. Both cite as 【D1】, which
# Answerer.finish normalises to [D1].
DEFAULT_GROQ_FALLBACK_MODELS = "openai/gpt-oss-20b,openai/gpt-oss-120b"


def resolve_path(value: object) -> object:
    """Resolve a relative path against the project root, not the CWD.

    `.env` ships values like `QDRANT_PATH=./data/qdrant`, which are relative.
    Left alone, they resolve against whatever directory the process happened to
    start in — so running a script from elsewhere silently *creates a fresh,
    empty* Qdrant store instead of opening the real one, and retrieval returns
    nothing with no error. Found when executing the notebook via nbconvert.
    """
    if value is None or (isinstance(value, Path) and value.is_absolute()):
        return value
    if not isinstance(value, (str, Path)):
        return value
    path = Path(value) if not isinstance(value, Path) else value
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


class DatabaseSettings(BaseSettings):
    """SQL Server connection and access policy."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_prefix="MSSQL_",
        extra="ignore",
        env_file_encoding="utf-8",
        case_sensitive=False,
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
    def _resolve_password(self) -> DatabaseSettings:
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


class PostgresSettings(BaseSettings):
    """PostgreSQL/Neon connection settings.

    The DSN is secret because it normally embeds the database password. It is
    deliberately separate from the SQL Server settings so local SSMS users do
    not have to change their existing configuration.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_prefix="POSTGRES_",
        extra="ignore",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    dsn: SecretStr | None = None
    connect_timeout: int = 30

    @field_validator("dsn", mode="before")
    @classmethod
    def _blank_dsn_is_none(cls, value: object) -> object:
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        return None if isinstance(value, str) and not value.strip() else value

    def connection_dsn(self) -> str:
        if self.dsn is None:
            raise ValueError("POSTGRES_DSN is required when DATABASE_BACKEND=postgresql")
        return self.dsn.get_secret_value()

    def sqlalchemy_url(self) -> str:
        dsn = self.connection_dsn()
        if dsn.startswith("postgres://"):
            dsn = "postgresql://" + dsn.removeprefix("postgres://")
        if dsn.startswith("postgresql://"):
            return "postgresql+psycopg://" + dsn.removeprefix("postgresql://")
        return dsn


class DuckDBSettings(BaseSettings):
    """Embedded analytics database (DATABASE_BACKEND=duckdb).

    A file, not a server: built from sql/postgres + the synthetic generator
    by scripts/build_duckdb.py (or on first start), then opened read-only.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_prefix="DUCKDB_",
        extra="ignore",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    path: Path = PROJECT_ROOT / "data" / "warehouse" / "northwind.duckdb"
    memory_limit: str = "512MB"
    threads: int = 2

    _resolve = field_validator("path", mode="before")(resolve_path)


class OllamaSettings(BaseSettings):
    """Local inference runtime. No cloud provider is ever contacted."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_prefix="OLLAMA_",
        extra="ignore",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    host: str = "http://localhost:11434"
    timeout_seconds: float = 300.0
    keep_alive: str = "5m"

    # Optional overrides. When unset, the active profile decides.
    chat_model: str | None = None
    embedding_model: str | None = None


class LLMSettings(BaseSettings):
    """Where chat generation happens: routing, SQL, and answers.

    The project was built against a local Ollama and that is still the
    default. `provider="openai"` points it at any OpenAI-compatible endpoint -
    Groq, OpenRouter, Together, DeepSeek, vLLM, Azure OpenAI - which is what
    makes a container without a GPU possible at all.

    Switching provider does NOT change any safety property. Generated SQL
    still goes through SQLGuard and ReadOnlyRunner; a hosted model is exactly
    as untrusted as a local one.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_prefix="LLM_",
        extra="ignore",
        env_file_encoding="utf-8",
        case_sensitive=False,
        protected_namespaces=(),
    )

    # "groq" is "openai" with Groq's endpoint and GROQ_* variables filled in.
    provider: Literal["ollama", "openai", "groq"] = "ollama"

    # OpenAI-compatible endpoint. Groq is https://api.groq.com/openai/v1
    base_url: str = GROQ_BASE_URL
    api_key: SecretStr | None = None

    # Groq's own variable names, accepted so a deployment can be configured
    # with GROQ_API_KEY / GROQ_MODEL as Groq documents them. LLM_* wins.
    groq_api_key: SecretStr | None = Field(default=None, validation_alias="GROQ_API_KEY")
    groq_model: str = Field(default=DEFAULT_GROQ_MODEL, validation_alias="GROQ_MODEL")

    # Blank means "use the profile's model", which is right for Ollama and
    # wrong for a hosted API, where the name is provider-specific.
    model: str = ""

    timeout_seconds: float = 120.0

    # Comma-separated models to try, in order, when the main one is rate
    # limited (HTTP 429). Blank on Groq means DEFAULT_GROQ_FALLBACK_MODELS;
    # "none" disables fallback.
    fallback_models: str = ""
    groq_fallback_models: str = Field(default="", validation_alias="GROQ_FALLBACK_MODELS")

    @property
    def fallback_model_list(self) -> list[str]:
        raw = self.fallback_models or self.groq_fallback_models
        if raw.strip().lower() == "none":
            return []
        if not raw.strip() and self.provider == "groq":
            raw = DEFAULT_GROQ_FALLBACK_MODELS
        return [m.strip() for m in raw.split(",") if m.strip()]

    @field_validator("api_key", "groq_api_key", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _apply_groq_defaults(self) -> LLMSettings:
        if self.provider == "groq":
            object.__setattr__(self, "base_url", GROQ_BASE_URL)
            if self.api_key is None and self.groq_api_key is not None:
                object.__setattr__(self, "api_key", self.groq_api_key)
            if not self.model:
                object.__setattr__(self, "model", self.groq_model or DEFAULT_GROQ_MODEL)
        return self

    @property
    def is_hosted(self) -> bool:
        return self.provider != "ollama"


class EmbeddingSettings(BaseSettings):
    """Where text becomes vectors.

    Kept separate from LLMSettings because the two genuinely come apart:
    Groq has no embeddings endpoint, and Anthropic has none either. The
    common deployment is therefore a hosted chat model plus embeddings from
    somewhere else entirely.

    **Changing `provider` or `model` invalidates the index.** A different
    model produces different vectors for the same text, so the existing
    collection becomes meaningless rather than merely stale - and it fails
    silently, by returning confident nonsense. Bump QDRANT_INDEX_VERSION and
    re-run scripts/build_index.py --rebuild whenever this section changes.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_prefix="EMBEDDING_",
        extra="ignore",
        env_file_encoding="utf-8",
        case_sensitive=False,
        protected_namespaces=(),
    )

    provider: Literal["ollama", "openai", "cloudflare", "fastembed"] = "ollama"

    # fastembed only: where ONNX model files are cached.
    cache_dir: str = ""

    # --- OpenAI-compatible ---
    base_url: str = "https://api.openai.com/v1"
    api_key: SecretStr | None = None

    # --- Cloudflare Workers AI ---
    # bge-m3 is 1024-dimensional, the same as the local qwen3-embedding the
    # index was built with, so the collection's vector size does not change.
    # The vectors still do, so a rebuild is still required.
    cloudflare_account_id: str = ""
    cloudflare_api_token: SecretStr | None = None

    # Blank means the profile's model (Ollama). Set explicitly otherwise:
    #   cloudflare  @cf/baai/bge-m3
    #   openai      text-embedding-3-small
    model: str = ""

    timeout_seconds: float = 120.0

    @field_validator("api_key", "cloudflare_api_token", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def is_hosted(self) -> bool:
        return self.provider != "ollama"


class VannaCloudSettings(BaseSettings):
    """Vanna Cloud (ask.vanna.ai) configuration.

    **This is the one component in the system that sends data off the
    machine.** Read `docs/vanna_cloud.md` before enabling it. In summary:

    * ``mode="hybrid"`` keeps generation local. Vanna Cloud holds the training
      corpus - DDL, the business glossary and the approved SQL examples - and
      answers retrieval requests. Questions are sent, because retrieval needs
      the question. Query *results* never are.
    * ``mode="cloud"`` additionally sends the assembled prompt to Vanna's
      hosted model. Nothing about the database rows leaves either way, but the
      schema and the question both do.

    Unset ``api_key`` disables the provider entirely, which is the default.
    The project's local Vanna and native providers are unaffected.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_prefix="VANNA_",
        extra="ignore",
        env_file_encoding="utf-8",
        case_sensitive=False,
        protected_namespaces=(),
    )

    api_key: SecretStr | None = None

    # The model name as it appears in the Vanna dashboard. Vanna calls a
    # training corpus a "model", which is confusing next to an LLM: this names
    # the corpus, not the language model.
    model: str = "enterprise-copilot"

    endpoint: str = "https://ask.vanna.ai/rpc"
    mode: Literal["hybrid", "cloud"] = "hybrid"

    # Vanna can be told to feed query results back into the prompt to refine a
    # follow-up. That would send actual customer rows to a third party, so it
    # is off, and `docs/security.md` states that it is off.
    allow_llm_to_see_data: bool = False

    @field_validator("api_key", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        """`VANNA_API_KEY=` must mean absent, not an empty key.

        The same mistake was found and fixed for MSSQL_PASSWORD in phase 1: an
        empty SecretStr is truthy enough to pass an `is not None` check and
        then fails much later, somewhere unhelpful.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def is_configured(self) -> bool:
        return self.api_key is not None


class VectorStoreSettings(BaseSettings):
    """Qdrant configuration.

    Two modes are supported:
      embedded - Qdrant runs in-process against a local path. No Docker needed.
      server   - Qdrant runs as a service at `url`.

    Embedded is the default because the reference machine has no Docker.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_prefix="QDRANT_",
        extra="ignore",
        env_file_encoding="utf-8",
        case_sensitive=False,
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
        env_file=ENV_FILE,
        env_prefix="RETRIEVAL_",
        extra="ignore",
        env_file_encoding="utf-8",
        case_sensitive=False,
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
        env_file=ENV_FILE,
        env_prefix="OBS_",
        extra="ignore",
        env_file_encoding="utf-8",
        case_sensitive=False,
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
        env_file=ENV_FILE,
        env_prefix="SECURITY_",
        extra="ignore",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Schemas the generated SQL may read. Everything else is refused.
    allowed_schemas: tuple[str, ...] = ("analytics", "core", "billing", "support")
    # Columns that must never appear in a result set, matched case-insensitively.
    blocked_columns: tuple[str, ...] = (
        "password_hash",
        "api_key",
        "secret",
        "token",
        "ssn",
        "tax_id",
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
        env_file=ENV_FILE,
        extra="ignore",
        env_file_encoding="utf-8",
        case_sensitive=False,
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
    # "none" runs documents-only: no database is contacted, Text-to-SQL is
    # disabled, and data questions are answered from documents with an
    # explicit note that live figures are unavailable. The cloud demo and a
    # fresh local install use this.
    # Whether the router consults the chat model (semantic intent screen and
    # route classification) after its deterministic rules.
    #   auto    only when it earns its cost: SQL is enabled (the route decides
    #           whether a query runs) or the model is hosted and fast. In a
    #           documents-only deployment on a local CPU model the two calls
    #           cost ~50 s per question while every route ends in document
    #           search anyway, and there is no database to protect.
    #   always / never   force it.
    # The destructive-intent rules run in every mode.
    router_llm: Literal["auto", "always", "never"] = Field(default="auto", alias="ROUTER_LLM")
    # Self-correction after the database rejects a guard-approved query: the
    # error is fed back to the model and the new query is validated again.
    # A guard refusal is never retried.
    sql_execution_retries: int = Field(default=2, ge=0, le=5, alias="SQL_EXECUTION_RETRIES")

    database_backend: Literal["sqlserver", "postgresql", "duckdb", "none"] = Field(
        default="sqlserver", alias="DATABASE_BACKEND"
    )

    project_root: Path = PROJECT_ROOT
    documents_dir: Path = PROJECT_ROOT / "data" / "documents"
    generated_dir: Path = PROJECT_ROOT / "data" / "generated"
    manifests_dir: Path = PROJECT_ROOT / "data" / "manifests"

    _resolve = field_validator(
        "project_root",
        "documents_dir",
        "generated_dir",
        "manifests_dir",
        mode="before",
    )(resolve_path)

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    duckdb: DuckDBSettings = Field(default_factory=DuckDBSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    embeddings: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    vanna_cloud: VannaCloudSettings = Field(default_factory=VannaCloudSettings)
    vector_store: VectorStoreSettings = Field(default_factory=VectorStoreSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def profile(self) -> ModelProfile:
        return get_profile(self.profile_name)

    @model_validator(mode="after")
    def _require_selected_database_configuration(self) -> Settings:
        if self.database_backend == "postgresql" and self.postgres.dsn is None:
            raise ValueError("DATABASE_BACKEND=postgresql requires POSTGRES_DSN")
        return self

    @property
    def sql_enabled(self) -> bool:
        return self.database_backend != "none"

    @property
    def router_uses_llm(self) -> bool:
        if self.router_llm != "auto":
            return self.router_llm == "always"
        return self.sql_enabled or self.llm.is_hosted

    @property
    def sql_dialect(self) -> Literal["tsql", "postgres"]:
        """The dialect the model writes and the guard validates.

        DuckDB deployments use PostgreSQL here on purpose: the prompts,
        few-shot examples and guard rules are the tested PostgreSQL ones, and
        the approved statement is transpiled only at execution
        (`execution_dialect`).
        """
        return "postgres" if self.database_backend in ("postgresql", "duckdb") else "tsql"

    @property
    def execution_dialect(self) -> Literal["tsql", "postgres", "duckdb"]:
        return "duckdb" if self.database_backend == "duckdb" else self.sql_dialect

    @property
    def chat_model(self) -> str:
        """The model name to send, whichever provider is in use.

        Precedence: the LLM section's explicit model, then the Ollama
        override, then the profile. A hosted provider must name its own model
        - the profile's `llama3.1:8b` is an Ollama tag and means nothing to
        Groq - so an unset model on a hosted provider is a configuration
        error, raised by `build_chat_client` rather than guessed at here.
        """
        if self.llm.model:
            return self.llm.model
        return self.ollama.chat_model or self.profile.chat_model

    @property
    def embedding_model(self) -> str:
        if self.embeddings.model:
            return self.embeddings.model
        if self.embeddings.provider == "fastembed":
            return "nomic-ai/nomic-embed-text-v1.5-Q"
        return self.ollama.embedding_model or self.profile.embedding_model

    def ensure_directories(self) -> None:
        for path in (
            self.documents_dir,
            self.generated_dir,
            self.manifests_dir,
            self.observability.log_dir,
            self.observability.trace_dir,
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
            "database_backend": self.database_backend,
            "database_endpoint": {
                "postgresql": "PostgreSQL (secret DSN)",
                "duckdb": f"DuckDB file {self.duckdb.path.name} (read-only)",
                "none": "(disabled)",
            }.get(self.database_backend, self.database.server),
            "database": {
                "postgresql": "PostgreSQL",
                "duckdb": "DuckDB",
                "none": "(disabled)",
            }.get(self.database_backend, self.database.database),
            "sql_auth": {
                "postgresql": "dsn",
                "duckdb": "read-only file",
                "none": "-",
            }.get(self.database_backend, self.database.auth_mode),
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
    "KEYRING_SERVICE",
    "PROJECT_ROOT",
    "DatabaseSettings",
    "ObservabilitySettings",
    "OllamaSettings",
    "PostgresSettings",
    "RetrievalSettings",
    "SecuritySettings",
    "Settings",
    "VannaCloudSettings",
    "VectorStoreSettings",
    "get_settings",
    "reset_settings_cache",
]
