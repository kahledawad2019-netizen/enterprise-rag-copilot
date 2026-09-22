"""
Tracing and structured logging.

Every request gets a trace id and a span per stage. Spans are always written to
a local JSONL file; OpenTelemetry export and the Phoenix UI are optional
additions on top.

## The system must run when the collector is down

Observability is the part of a system most likely to be misconfigured, and a
request that fails because a trace could not be exported has turned a debugging
aid into an outage. Every export path here is wrapped: a collector that is
unreachable produces one warning and the request continues.

## Everything is redacted on the way out

Spans carry prompts, SQL and result previews. `redaction.py` strips credentials
and masks personal data before anything is written, because a trace file
outlives the request and is read by people who were never granted access to the
underlying records.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from .redaction import redact_mapping, safe_exception

log = logging.getLogger(__name__)


@dataclass
class Span:
    """One stage of one request."""

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    started_at: float = field(default_factory=time.perf_counter)
    duration_ms: float = 0.0
    status: str = "ok"  # ok | error
    attributes: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def set(self, **attributes: Any) -> Span:
        self.attributes.update(attributes)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "duration_ms": round(self.duration_ms, 2),
            "status": self.status,
            "error": self.error,
            "attributes": redact_mapping(self.attributes),
        }


class Tracer:
    """Collects spans for one process and writes them to disk.

    Deliberately simple: a list of spans and a JSONL file. The OpenTelemetry
    exporter is attached only if configured, so the core path has no dependency
    on a collector being present.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.enabled = self.settings.observability.enable_tracing
        self.spans: list[Span] = []
        self._current_trace_id: str | None = None
        self._span_stack: list[str] = []
        self._otel_tracer = None
        self._otel_failed = False

        self.trace_dir = self.settings.observability.trace_dir
        self.trace_dir.mkdir(parents=True, exist_ok=True)

    # -- trace lifecycle ---------------------------------------------------
    def new_trace(self, trace_id: str | None = None) -> str:
        self._current_trace_id = trace_id or uuid.uuid4().hex[:16]
        self._span_stack = []
        return self._current_trace_id

    @property
    def trace_id(self) -> str:
        if self._current_trace_id is None:
            self.new_trace()
        return self._current_trace_id  # type: ignore[return-value]

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        """Time a stage and record it, even when it raises."""
        if not self.enabled:
            yield Span(name=name, trace_id="disabled", span_id="disabled")
            return

        span = Span(
            name=name,
            trace_id=self.trace_id,
            span_id=uuid.uuid4().hex[:12],
            parent_span_id=self._span_stack[-1] if self._span_stack else None,
            attributes=dict(attributes),
        )
        self._span_stack.append(span.span_id)
        started = time.perf_counter()
        try:
            yield span
        except Exception as exc:
            span.status = "error"
            span.error = safe_exception(exc)
            raise
        finally:
            span.duration_ms = (time.perf_counter() - started) * 1000
            self._span_stack.pop()
            self.spans.append(span)
            self._export(span)

    # -- export ------------------------------------------------------------
    def _export(self, span: Span) -> None:
        """Send a span to OpenTelemetry, if configured. Never raises."""
        if self._otel_failed or not self.settings.observability.enable_phoenix:
            return
        try:
            tracer = self._ensure_otel()
            if tracer is None:
                return
            otel_span = tracer.start_span(span.name)
            for key, value in redact_mapping(span.attributes).items():
                otel_span.set_attribute(key, str(value)[:1000])
            otel_span.set_attribute("trace.id", span.trace_id)
            otel_span.set_attribute("duration_ms", span.duration_ms)
            if span.error:
                otel_span.set_attribute("error", span.error)
            otel_span.end()
        except Exception as exc:
            self._otel_failed = True
            log.warning(
                "OpenTelemetry export disabled for this process (%s). "
                "Tracing continues to local files.",
                exc,
            )

    def _ensure_otel(self):
        if self._otel_tracer is not None:
            return self._otel_tracer
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider(
                resource=Resource.create({"service.name": "enterprise-copilot"})
            )
            provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(endpoint=self.settings.observability.phoenix_endpoint)
                )
            )
            trace.set_tracer_provider(provider)
            self._otel_tracer = trace.get_tracer("enterprise_copilot")
            log.info(
                "OpenTelemetry export enabled -> %s", self.settings.observability.phoenix_endpoint
            )
            return self._otel_tracer
        except Exception as exc:
            self._otel_failed = True
            log.warning("OpenTelemetry unavailable (%s); local tracing only", exc)
            return None

    # -- persistence -------------------------------------------------------
    def flush(self, extra: dict[str, Any] | None = None) -> Path | None:
        """Write this trace's spans to a JSONL file, newest last."""
        if not self.enabled or not self.spans:
            return None

        day = datetime.now(UTC).strftime("%Y%m%d")
        path = self.trace_dir / f"traces_{day}.jsonl"
        record = {
            "trace_id": self.trace_id,
            "recorded_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "total_ms": round(sum(s.duration_ms for s in self.spans), 2),
            "span_count": len(self.spans),
            "spans": [s.to_dict() for s in self.spans],
            **(redact_mapping(extra) if extra else {}),
        }
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:
            log.warning("Could not write the trace file: %s", exc)
            return None
        return path

    def reset(self) -> None:
        self.spans = []
        self._current_trace_id = None
        self._span_stack = []

    # -- summary -----------------------------------------------------------
    def stage_breakdown(self) -> dict[str, float]:
        breakdown: dict[str, float] = {}
        for span in self.spans:
            breakdown[span.name] = breakdown.get(span.name, 0.0) + span.duration_ms
        return dict(sorted(breakdown.items(), key=lambda kv: kv[1], reverse=True))

    def summary(self) -> str:
        if not self.spans:
            return "(no spans recorded)"
        total = sum(s.duration_ms for s in self.spans)
        parts = [f"{name}={ms:.0f}ms" for name, ms in self.stage_breakdown().items()]
        return f"trace {self.trace_id}: {total:.0f}ms total | " + " ".join(parts)


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    """One JSON object per line, with credentials stripped."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1] is not None:
            payload["error"] = safe_exception(record.exc_info[1])
        for key, value in getattr(record, "__dict__", {}).items():
            if key.startswith("copilot_"):
                payload[key[8:]] = value
        return json.dumps(redact_mapping(payload), default=str)


def configure_logging(settings: Settings | None = None) -> None:
    """Set up console and file logging once, at process start."""
    settings = settings or get_settings()
    observability = settings.observability

    root = logging.getLogger()
    root.setLevel(getattr(logging, observability.log_level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    if observability.log_format == "json":
        console.setFormatter(JsonFormatter())
    else:
        console.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-7s | %(name)-38s | %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    root.addHandler(console)

    # Structured file log is always JSON, whatever the console shows: it is
    # meant to be parsed, not read.
    try:
        observability.log_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(UTC).strftime("%Y%m%d")
        file_handler = logging.FileHandler(
            observability.log_dir / f"copilot_{day}.jsonl", encoding="utf-8"
        )
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)
    except Exception as exc:
        log.warning("File logging disabled: %s", exc)

    # These libraries log every HTTP call at INFO, which drowns everything else.
    for noisy in ("httpx", "httpcore", "urllib3", "chromadb", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_TRACER: Tracer | None = None


def get_tracer(settings: Settings | None = None) -> Tracer:
    """Process-wide tracer."""
    global _TRACER
    if _TRACER is None:
        _TRACER = Tracer(settings)
    return _TRACER


__all__ = ["JsonFormatter", "Span", "Tracer", "configure_logging", "get_tracer"]
