import argparse
import json
import re
from pathlib import Path

parser = argparse.ArgumentParser(description="Generate the vacation agent Grafana dashboard.")
parser.add_argument("--log-analytics-workspace-id", required=True, help="Log Analytics workspace ARM resource ID.")
args = parser.parse_args()
workspace = args.log_analytics_workspace_id.rstrip("/")
if not re.fullmatch(
    r"/subscriptions/[0-9a-f-]{36}/resourceGroups/[^/]+/providers/"
    r"Microsoft\.OperationalInsights/workspaces/[^/]+",
    workspace,
    flags=re.IGNORECASE,
):
    parser.error("--log-analytics-workspace-id must be a Log Analytics ARM resource ID.")

root = Path(__file__).resolve().parents[1]
prom = {"type": "prometheus", "uid": "vacation-prometheus"}
logs = {"type": "grafana-azure-monitor-datasource", "uid": "vacation-azure-monitor"}
panels = []


def metric(title, expression, panel_type, x, y, width, height, unit="none"):
    panels.append({
        "id": len(panels) + 1, "title": title, "type": panel_type,
        "datasource": prom, "gridPos": {"x": x, "y": y, "w": width, "h": height},
        "targets": [{"refId": "A", "expr": expression, "datasource": prom,
                     "instant": panel_type == "stat", "range": panel_type != "stat",
                     "legendFormat": "{{outcome}}", "editorMode": "code"}],
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
    })


metric("Requests in selected window", "sum(sum_over_time(vacation_requests[$__range]))", "stat", 0, 0, 6, 4)
metric("Errors in selected window", 'sum(sum_over_time(vacation_requests{outcome="error"}[$__range]))', "stat", 6, 0, 6, 4)
metric("Model tokens in selected window", "sum(sum_over_time(vacation_tokens[$__range]))", "stat", 12, 0, 6, 4)
metric("Requests in flight", "sum(vacation_in_flight)", "stat", 18, 0, 6, 4)
metric("Request rate by outcome (1-minute window)", "sum by (outcome) (sum_over_time(vacation_requests[1m])) / 60",
       "timeseries", 0, 4, 12, 8, "reqps")
metric("Model request latency (p95)", "histogram_quantile(0.95, sum(sum_over_time(vacation_duration[1m])))",
       "timeseries", 12, 4, 12, 8, "s")
metric("Model tokens by direction (1-minute window)", "sum by (token_type) (sum_over_time(vacation_tokens[1m]))",
       "timeseries", 0, 12, 12, 7)
panels[-1]["targets"][0]["legendFormat"] = "{{token_type}}"
metric("Average model request latency",
       "histogram_avg(sum(sum_over_time(vacation_duration[1m])))",
       "timeseries", 12, 12, 12, 7, "s")
queries = [
    ("Request/response messages and logs",
     'OTelLogs | where $__timeFilter(TimeGenerated) | where ServiceName == "vacation-planner" '
     '| project TimeGenerated, Request=tostring(Attributes.request_text), '
     'Response=tostring(Attributes.response_text), Outcome=tostring(Attributes.outcome), '
     'ResponseComplete=tobool(Attributes.response_complete), TraceId, SeverityText, Body, '
     'DurationSeconds=todouble(Attributes.duration_seconds) '
     '| order by TimeGenerated desc | take 100'),
    ("Model spans and token usage",
     'OTelSpans | where $__timeFilter(TimeGenerated) | where ServiceName == "vacation-planner" '
     '| project TimeGenerated, Name, DurationMs, StatusCode, '
     'InputTokens=tolong(Attributes["gen_ai.usage.input_tokens"]), '
     'OutputTokens=tolong(Attributes["gen_ai.usage.output_tokens"]), TraceId '
     '| order by TimeGenerated desc | take 100'),
]
for index, (title, query) in enumerate(queries):
    panels.append({
        "id": len(panels) + 1, "title": title, "type": "table", "datasource": logs,
        "gridPos": {"x": index * 12, "y": 19, "w": 12, "h": 10},
        "targets": [{
            "refId": "A", "datasource": logs, "queryType": "Azure Log Analytics",
            "azureLogAnalytics": {"query": query, "resources": [workspace], "resultFormat": "table"},
        }],
        "fieldConfig": {"defaults": {}, "overrides": []},
    })
    if index == 0:
        panels[-1]["fieldConfig"]["overrides"] = [
            {"matcher": {"id": "byName", "options": column},
             "properties": [{"id": "custom.inspect", "value": True},
                            {"id": "custom.width", "value": 260}]}
            for column in ("Request", "Response")
        ]
dashboard = {
    "uid": "vacation-agent-observability", "title": "Vacation agent - OpenTelemetry demo",
    "description": "Vacation-planner OTLP metrics in Azure Managed Prometheus, logs and spans in Log Analytics. Message capture is opt-in. Do not submit secrets or real personal data.",
    "schemaVersion": 39, "version": 1, "timezone": "browser", "refresh": "10s",
    "time": {"from": "now-3h", "to": "now"},
    "tags": ["vacation-demo", "opentelemetry", "foundry"],
    "panels": panels, "annotations": {"list": []},
}
(root / "infra" / "dashboard.json").write_text(
    json.dumps({"dashboard": dashboard, "overwrite": True}, indent=2), encoding="utf-8"
)
print("Dashboard definition generated with 10 panels.")
