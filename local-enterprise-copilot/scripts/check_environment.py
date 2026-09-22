r"""
Environment health check.

Reproduces the Phase 0 inspection and verifies every runtime dependency. Run it
before anything else, and again whenever something stops working.

    .venv\Scripts\python scripts\check_environment.py
    .venv\Scripts\python scripts\check_environment.py --json

Exit code 0 means every required check passed. Warnings do not fail the run;
they mark things that are acceptable locally but wrong for a real deployment.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

Status = Literal["ok", "warn", "fail", "skip"]

SYMBOL: dict[Status, str] = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]", "skip": "[SKIP]"}


@dataclass
class Check:
    name: str
    status: Status
    detail: str = ""
    remedy: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class Report:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        line = f"{SYMBOL[check.status]} {check.name}"
        if check.detail:
            line += f"  -  {check.detail}"
        print(line)
        if check.remedy and check.status in ("fail", "warn"):
            print(f"        -> {check.remedy}")
        return check

    @property
    def failures(self) -> int:
        return sum(1 for c in self.checks if c.status == "fail")

    @property
    def warnings(self) -> int:
        return sum(1 for c in self.checks if c.status == "warn")


# ---------------------------------------------------------------------------
# Hardware and platform
# ---------------------------------------------------------------------------
def check_platform(report: Report) -> None:
    report.add(Check("OS", "ok", f"{platform.system()} {platform.release()} ({platform.machine()})"))

    v = sys.version_info
    detail = f"{v.major}.{v.minor}.{v.micro}"
    if (3, 11) <= (v.major, v.minor) < (3, 14):
        report.add(Check("Python", "ok", detail))
    elif (v.major, v.minor) >= (3, 14):
        report.add(Check(
            "Python", "fail", detail,
            "Python 3.14+ lacks wheels for parts of this stack. Recreate the venv "
            "with 3.13:  py -3.13 -m venv .venv",
        ))
    else:
        report.add(Check("Python", "fail", detail, "This project needs Python >= 3.11."))


def _detect_hardware() -> tuple[float, float, str]:
    """Return (total_ram_gb, vram_gb, gpu_name). Zeros when undetectable."""
    ram_gb = 0.0
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if out.stdout.strip():
            ram_gb = int(out.stdout.strip()) / (1024 ** 3)
    except Exception:
        pass

    vram_gb, gpu_name = 0.0, ""
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            first = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
            if "," in first:
                gpu_name, mib = (p.strip() for p in first.split(",", 1))
                vram_gb = float(mib) / 1024
        except Exception:
            pass
    return ram_gb, vram_gb, gpu_name


def check_hardware(report: Report) -> tuple[float, float]:
    ram_gb, vram_gb, gpu_name = _detect_hardware()

    report.add(Check("CPU", "ok", f"{platform.processor() or 'unknown'}"))

    if ram_gb >= 16:
        report.add(Check("RAM", "ok", f"{ram_gb:.1f} GB", data={"ram_gb": ram_gb}))
    elif ram_gb > 0:
        report.add(Check("RAM", "warn", f"{ram_gb:.1f} GB",
                         "Under 16 GB: use COPILOT_PROFILE=lite.", {"ram_gb": ram_gb}))
    else:
        report.add(Check("RAM", "skip", "could not detect"))

    if vram_gb > 0:
        report.add(Check("GPU", "ok", f"{gpu_name}, {vram_gb:.1f} GB VRAM",
                         data={"gpu": gpu_name, "vram_gb": vram_gb}))
    else:
        report.add(Check("GPU", "warn", "no NVIDIA GPU detected",
                         "Inference will run on CPU. Use COPILOT_PROFILE=lite."))

    free_gb = shutil.disk_usage(Path(__file__).resolve().parent.parent).free / (1024 ** 3)
    if free_gb >= 15:
        report.add(Check("Disk free", "ok", f"{free_gb:.0f} GB"))
    else:
        report.add(Check("Disk free", "warn", f"{free_gb:.0f} GB",
                         "Models and indexes need room; keep at least 15 GB free."))

    report.add(Check("Docker", "ok" if shutil.which("docker") else "skip",
                     "available" if shutil.which("docker")
                     else "not installed - Qdrant runs embedded (expected)"))
    return ram_gb, vram_gb


# ---------------------------------------------------------------------------
# Python packages
# ---------------------------------------------------------------------------
REQUIRED_PACKAGES = [
    "pydantic", "pydantic-settings", "pyodbc", "SQLAlchemy", "sqlglot",
    "qdrant-client", "rank-bm25", "ollama", "pandas", "numpy", "plotly",
    "streamlit", "Faker", "structlog", "opentelemetry-sdk", "pypdf", "python-docx",
]
OPTIONAL_PACKAGES = {
    "sentence-transformers": 'reranking - install with: pip install -e ".[rerank]"',
    "vanna": 'Vanna Text-to-SQL - install with: pip install -e ".[vanna]"',
    "arize-phoenix": 'trace UI - install with: pip install -e ".[phoenix]"',
}


def check_packages(report: Report) -> None:
    import importlib.metadata as md

    missing = []
    for pkg in REQUIRED_PACKAGES:
        try:
            md.version(pkg)
        except md.PackageNotFoundError:
            missing.append(pkg)
    if missing:
        report.add(Check("Required packages", "fail", f"missing: {', '.join(missing)}",
                         'pip install -e ".[dev]"'))
    else:
        report.add(Check("Required packages", "ok", f"all {len(REQUIRED_PACKAGES)} present"))

    for pkg, why in OPTIONAL_PACKAGES.items():
        try:
            report.add(Check(f"Optional: {pkg}", "ok", md.version(pkg)))
        except md.PackageNotFoundError:
            report.add(Check(f"Optional: {pkg}", "skip", "not installed", why))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def check_configuration(report: Report, ram_gb: float, vram_gb: float) -> Any:
    try:
        from enterprise_copilot.config import get_settings, recommend_profile
    except Exception as exc:
        report.add(Check("Configuration", "fail", f"{type(exc).__name__}: {exc}",
                         "Is the project installed?  pip install -e \".[dev]\""))
        return None

    try:
        settings = get_settings()
    except Exception as exc:
        report.add(Check("Configuration", "fail", f"{type(exc).__name__}: {exc}",
                         "Check .env against .env.example"))
        return None

    report.add(Check("Configuration", "ok", "loaded and validated"))
    for key, value in settings.describe().items():
        print(f"         {key:16} {value}")

    if ram_gb and vram_gb is not None:
        suggested = recommend_profile(ram_gb, vram_gb)
        if suggested.value != settings.profile_name.value:
            report.add(Check(
                "Profile fit", "warn",
                f"using '{settings.profile_name.value}', hardware suggests '{suggested.value}'",
                f"Set COPILOT_PROFILE={suggested.value} in .env if generation is slow.",
            ))
        else:
            report.add(Check("Profile fit", "ok", f"'{settings.profile_name.value}' matches hardware"))
    return settings


# ---------------------------------------------------------------------------
# External services
# ---------------------------------------------------------------------------
def check_odbc(report: Report, settings: Any) -> None:
    try:
        import pyodbc
    except ImportError:
        report.add(Check("ODBC", "fail", "pyodbc not installed"))
        return
    drivers = pyodbc.drivers()
    if settings.database.driver in drivers:
        report.add(Check("ODBC driver", "ok", settings.database.driver))
    else:
        report.add(Check("ODBC driver", "fail",
                         f"{settings.database.driver!r} not installed. Found: {drivers}",
                         "Install 'Microsoft ODBC Driver 18 for SQL Server'."))


def check_sql_server(report: Report, settings: Any) -> None:
    try:
        import pyodbc
    except ImportError:
        return

    # Connect to master first: the project database may not exist yet.
    try:
        conn = pyodbc.connect(
            settings.database.odbc_connection_string(database="master"), timeout=10
        )
    except Exception as exc:
        report.add(Check("SQL Server", "fail", f"{type(exc).__name__}: {str(exc)[:160]}",
                         "Is the SQL Server service running? Check MSSQL_SERVER in .env."))
        return

    with conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT CAST(SERVERPROPERTY('Edition') AS nvarchar(100)), "
            "CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(50)), "
            "CAST(SERVERPROPERTY('IsIntegratedSecurityOnly') AS int)"
        )
        edition, version, integrated_only = cur.fetchone()
        report.add(Check("SQL Server", "ok", f"{edition} {version}"))

        cur.execute("SELECT SUSER_SNAME(), IS_SRVROLEMEMBER('sysadmin')")
        login, is_sysadmin = cur.fetchone()

        if integrated_only == 1:
            report.add(Check(
                "Auth mode", "warn", "Windows Authentication only (Mixed Mode disabled)",
                "A read-only SQL login cannot be created until Mixed Mode is enabled. "
                "See ADR-003 and docs/security.md.",
            ))
        else:
            report.add(Check("Auth mode", "ok", "Mixed Mode - SQL logins available"))

        if is_sysadmin:
            report.add(Check(
                "DB principal", "warn", f"{login} is sysadmin - it can WRITE",
                "Defence in depth is incomplete: only the application SQL guard is "
                "protecting the data. Run sql/006_create_security.sql and switch to "
                "the copilot_reader login. See ADR-003.",
            ))
        else:
            report.add(Check("DB principal", "ok", f"{login} (not sysadmin)"))

        cur.execute("SELECT DB_ID(?)", settings.database.database)
        if cur.fetchone()[0] is None:
            report.add(Check("Project database", "warn",
                             f"'{settings.database.database}' does not exist yet",
                             "Run: .venv\\Scripts\\python scripts\\setup_database.py"))
        else:
            cur.execute(
                "SELECT COUNT(*) FROM sys.tables t JOIN sys.schemas s "
                "ON s.schema_id = t.schema_id",
            )
            report.add(Check("Project database", "ok",
                             f"'{settings.database.database}' exists"))


def check_ollama(report: Report, settings: Any) -> None:
    try:
        import ollama
    except ImportError:
        report.add(Check("Ollama", "fail", "python client not installed"))
        return

    try:
        client = ollama.Client(settings.ollama.host)
        installed = {m["model"] for m in client.list().get("models", [])}
    except Exception as exc:
        report.add(Check("Ollama service", "fail", f"{type(exc).__name__}: {str(exc)[:120]}",
                         "Start Ollama, then retry. Check OLLAMA_HOST in .env."))
        return

    report.add(Check("Ollama service", "ok", f"{settings.ollama.host} ({len(installed)} models)"))

    for label, name in (("chat", settings.chat_model), ("embedding", settings.embedding_model)):
        if name in installed or f"{name}:latest" in installed:
            report.add(Check(f"Model ({label})", "ok", name))
        else:
            report.add(Check(f"Model ({label})", "fail", f"{name} not pulled",
                             f"ollama pull {name}"))
            return

    # Prove the embedding model works AND detect its dimension. The dimension is
    # never assumed anywhere in this project: a changed model must change the
    # index, and detection is how that is caught.
    try:
        vec = client.embed(model=settings.embedding_model, input=["dimension probe"])
        dim = len(vec["embeddings"][0])
        report.add(Check("Embedding dimension", "ok", f"{dim} (detected, not assumed)",
                         data={"dimension": dim}))
    except Exception as exc:
        report.add(Check("Embedding dimension", "fail", f"{type(exc).__name__}: {str(exc)[:120]}"))

    try:
        reply = client.chat(
            model=settings.chat_model,
            messages=[{"role": "user", "content": "Reply with only the word: READY"}],
            options={"num_predict": 8},
        )
        report.add(Check("Chat generation", "ok", reply["message"]["content"].strip()[:40]))
    except Exception as exc:
        report.add(Check("Chat generation", "fail", f"{type(exc).__name__}: {str(exc)[:120]}"))


def check_vector_store(report: Report, settings: Any) -> None:
    try:
        from qdrant_client import QdrantClient
    except ImportError:
        report.add(Check("Qdrant", "fail", "qdrant-client not installed"))
        return

    vs = settings.vector_store
    try:
        if vs.mode == "embedded":
            client = QdrantClient(path=str(vs.path))
            names = [c.name for c in client.get_collections().collections]
            client.close()
            report.add(Check("Qdrant (embedded)", "ok",
                             f"{vs.path} | collections: {names or 'none yet'}"))
        else:
            client = QdrantClient(url=vs.url, api_key=(
                vs.api_key.get_secret_value() if vs.api_key else None))
            names = [c.name for c in client.get_collections().collections]
            report.add(Check("Qdrant (server)", "ok", f"{vs.url} | collections: {names or 'none'}"))
    except Exception as exc:
        report.add(Check("Qdrant", "fail", f"{type(exc).__name__}: {str(exc)[:140]}",
                         "If another process holds the embedded path, close it "
                         "(embedded Qdrant is single-process) or use QDRANT_MODE=server."))


def check_secrets_hygiene(report: Report, settings: Any) -> None:
    root = Path(__file__).resolve().parent.parent
    gitignore = root / ".gitignore"
    if gitignore.exists() and ".env" in gitignore.read_text(encoding="utf-8"):
        report.add(Check("Secrets in git", "ok", ".env is gitignored"))
    else:
        report.add(Check("Secrets in git", "fail", ".env is NOT gitignored",
                         "Add '.env' to .gitignore before committing anything."))

    if settings.database.auth_mode == "windows":
        report.add(Check("DB credentials", "ok", "Windows auth - no password stored on disk"))
    elif settings.database.password is not None:
        report.add(Check("DB credentials", "warn", "SQL auth with a stored password",
                         "Prefer the Windows Credential Manager: python scripts/set_secret.py"))

    if settings.database.encrypt:
        report.add(Check("TLS", "ok", "Encrypt=yes"))
    else:
        report.add(Check("TLS", "fail", "Encrypt=no", "Set MSSQL_ENCRYPT=true."))

    if settings.database.trust_server_certificate:
        report.add(Check("Certificate validation", "warn", "TrustServerCertificate=yes",
                         "Fine for a local instance with a self-signed cert; set false "
                         "for any remote server."))
    else:
        report.add(Check("Certificate validation", "ok", "server certificate verified"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Environment health check")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    report = Report()
    print("=" * 78)
    print("  Local Enterprise Intelligence Copilot - environment check")
    print("=" * 78)

    check_platform(report)
    ram_gb, vram_gb = check_hardware(report)
    check_packages(report)
    settings = check_configuration(report, ram_gb, vram_gb)

    if settings is not None:
        check_odbc(report, settings)
        check_sql_server(report, settings)
        check_ollama(report, settings)
        check_vector_store(report, settings)
        check_secrets_hygiene(report, settings)

    print("=" * 78)
    print(f"  {len(report.checks)} checks | {report.failures} failed | {report.warnings} warnings")
    print("=" * 78)

    if args.json:
        print(json.dumps([asdict(c) for c in report.checks], indent=2))

    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
