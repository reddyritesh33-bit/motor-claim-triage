"""OpenTelemetry tracing (exported over OTLP/HTTP to Jaeger) and trace-context
propagation across the Redis queue, so one run is a single trace end to end."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .config import get_settings

_initialised = False


def init_tracing(service_name: str) -> None:
    global _initialised
    if _initialised:
        return
    settings = get_settings()
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if settings.otel_enabled and settings.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception:  # instrumentation is best-effort
        pass
    _initialised = True


def instrument_fastapi(app) -> None:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
    except Exception:
        pass


def tracer(name: str = "claimtriage"):
    return trace.get_tracer(name)


def inject_carrier() -> dict[str, str]:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


@contextmanager
def extracted_context(carrier: dict[str, str] | None) -> Iterator[None]:
    token = otel_context.attach(propagate.extract(carrier or {}))
    try:
        yield
    finally:
        otel_context.detach(token)


def current_trace_id() -> str:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.trace_id else ""


def set_attrs(span, **attrs: Any) -> None:
    for k, v in attrs.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            v = [str(x) for x in v]
        elif not isinstance(v, (str, bool, int, float)):
            v = str(v)
        span.set_attribute(k, v)
