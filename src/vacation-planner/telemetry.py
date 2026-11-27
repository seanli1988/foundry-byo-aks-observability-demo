import atexit
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from azure.identity import DefaultAzureCredential
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode, use_span


@dataclass
class RequestRecord:
    usage: dict[str, int] = field(default_factory=dict)
    request_text: str | None = None
    response_text: str | None = None
    model_response_id: str | None = None


class AzureOtlpSession(requests.Session):
    def __init__(self, credential, endpoint):
        super().__init__()
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname.endswith(".monitor.azure.com"):
            raise ValueError("OTLP export requires an HTTPS Azure Monitor endpoint.")
        self.credential = credential
        self.host = parsed.hostname

    def send(self, request, **kwargs):
        if urlsplit(request.url).hostname != self.host:
            raise ValueError("Refusing to send telemetry credentials to another host.")
        request.headers["Authorization"] = (
            f"Bearer {self.credential.get_token('https://monitor.azure.com/.default').token}"
        )
        kwargs["allow_redirects"] = False
        return super().send(request, **kwargs)


class VacationTelemetry:
    def __init__(self, model):
        self.model = model
        capture = os.getenv("DEMO_CAPTURE_MESSAGES", "false").lower()
        if capture not in ("true", "false"):
            raise ValueError("DEMO_CAPTURE_MESSAGES must be true or false.")
        self.capture_messages = capture == "true"
        self.credential = DefaultAzureCredential()
        resource = Resource.create({
            "service.name": "vacation-planner",
            "service.instance.id": str(uuid4()),
            "service.namespace": "vacation-demo",
            "deployment.environment.name": "demo",
            "gen_ai.agent.name": "vacation-planner",
        })
        endpoints = {
            signal: os.environ[f"DEMO_OTLP_{signal.upper()}_ENDPOINT"]
            for signal in ("metrics", "logs", "traces")
        }
        self.sessions = [
            AzureOtlpSession(self.credential, endpoints[signal])
            for signal in ("metrics", "logs", "traces")
        ]
        metric_exporter = OTLPMetricExporter(
            endpoint=endpoints["metrics"], session=self.sessions[0], timeout=15
        )
        self.meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(metric_exporter, export_interval_millis=10000)
            ],
        )
        self.log_provider = LoggerProvider(resource=resource)
        self.log_provider.add_log_record_processor(BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=endpoints["logs"], session=self.sessions[1], timeout=15),
            schedule_delay_millis=2000,
        ))
        self.trace_provider = TracerProvider(resource=resource)
        self.trace_provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoints["traces"], session=self.sessions[2], timeout=15),
            schedule_delay_millis=2000,
        ))
        self.tracer = self.trace_provider.get_tracer("vacation.demo")
        self.logger = logging.getLogger("vacation.demo")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.logger.addHandler(LoggingHandler(logger_provider=self.log_provider))
        self.logger.addHandler(logging.StreamHandler())
        meter = self.meter_provider.get_meter("vacation.demo")
        self.requests = meter.create_counter("vacation_requests", unit="1")
        self.duration = meter.create_histogram("vacation_duration", unit="s")
        self.tokens = meter.create_counter("vacation_tokens", unit="1")
        self.active = meter.create_up_down_counter("vacation_in_flight", unit="1")
        self.requests.add(0, {"outcome": "error", "model": model})
        atexit.register(self.shutdown)

    @contextmanager
    def request(self, request_text=None):
        started = time.perf_counter()
        labels = {"model": self.model}
        record = RequestRecord(
            request_text=request_text if self.capture_messages else None,
            response_text="" if self.capture_messages else None,
        )
        self.active.add(1, labels)
        span = self.tracer.start_span(
            "chat vacation-planner",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "azure.ai.openai",
                "gen_ai.request.model": self.model,
                "gen_ai.agent.name": "vacation-planner",
            },
        )
        outcome = "success"
        with use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
            try:
                yield record
                span.set_status(Status(StatusCode.OK))
            except BaseException as error:
                outcome = "cancelled" if not isinstance(error, Exception) else "error"
                span.set_attribute("error.type", type(error).__name__)
                span.set_status(Status(StatusCode.ERROR, type(error).__name__))
                raise
            finally:
                elapsed = time.perf_counter() - started
                attributes = {**labels, "outcome": outcome}
                self.requests.add(1, attributes)
                self.duration.record(elapsed, attributes)
                self.active.add(-1, labels)
                for token_type in ("input", "output"):
                    count = record.usage.get(f"{token_type}_token_count")
                    if count is not None:
                        self.tokens.add(count, {**labels, "token_type": token_type})
                        span.set_attribute(f"gen_ai.usage.{token_type}_tokens", count)
                message_fields = {}
                if self.capture_messages:
                    message_fields = {
                        "request_text": record.request_text or "",
                        "response_text": record.response_text or "",
                        "response_complete": outcome == "success",
                    }
                    if record.model_response_id:
                        message_fields["model_response_id"] = record.model_response_id
                self.logger.log(
                    logging.INFO if outcome == "success" else logging.ERROR,
                    "Vacation planning request %s", outcome,
                    extra={"outcome": outcome, "duration_seconds": elapsed,
                           "model": self.model, **record.usage, **message_fields},
                )
                span.end()

    def shutdown(self):
        for provider in (self.meter_provider, self.log_provider, self.trace_provider):
            provider.shutdown()
        for session in self.sessions:
            session.close()
        self.credential.close()
