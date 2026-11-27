import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

parser = argparse.ArgumentParser(description="Verify Grafana queries against demo telemetry.")
parser.add_argument("--subscription-id", required=True)
parser.add_argument("--grafana-url", required=True, help="Azure Managed Grafana HTTPS endpoint.")
args = parser.parse_args()
subscription = args.subscription_id
azure_cli = shutil.which("az") or shutil.which("az.cmd")
if not azure_cli:
    parser.error("Azure CLI is required to authenticate to Grafana.")
endpoint = urlsplit(args.grafana_url)
if (endpoint.scheme != "https" or not endpoint.hostname
        or not endpoint.hostname.endswith(".grafana.azure.com")
        or endpoint.username or endpoint.password or endpoint.path not in ("", "/")
        or endpoint.query or endpoint.fragment):
    parser.error("--grafana-url must be an Azure Managed Grafana HTTPS endpoint.")
grafana = args.grafana_url.rstrip("/")


def token(resource):
    result = subprocess.run(
        [azure_cli, "account", "get-access-token", "--subscription", subscription,
         "--resource", resource, "--query", "accessToken", "-o", "tsv"],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


headers = {"Authorization": f"Bearer {token('ce34e7e5-485f-4d76-964f-b3d2b16d1e4f')}"}
end = int(time.time() * 1000)
root = Path(__file__).resolve().parents[1]
dashboard = json.loads((root / "infra" / "dashboard.json").read_text(encoding="utf-8"))["dashboard"]
queries = []
for panel in dashboard["panels"]:
    target = dict(panel["targets"][0])
    target.update(refId=str(panel["id"]), intervalMs=10000, maxDataPoints=1000)
    if "expr" in target:
        target["expr"] = target["expr"].replace("$__range", "3h")
    else:
        target["azureLogAnalytics"] = dict(target["azureLogAnalytics"])
        target["azureLogAnalytics"]["query"] = target["azureLogAnalytics"]["query"].replace(
            "$__timeFilter(TimeGenerated)", "TimeGenerated > ago(3h)"
        )
    queries.append(target)
response = requests.post(
    f"{grafana}/api/ds/query", headers=headers,
    json={"from": str(end - 3 * 3600000), "to": str(end), "queries": queries},
    timeout=60, allow_redirects=False,
)
if not response.headers.get("Content-Type", "").startswith("application/json"):
    raise RuntimeError(f"Grafana returned HTTP {response.status_code}, not JSON.")
payload = response.json()
summary = []
for ref, result in payload.get("results", {}).items():
    count = sum(
        len(frame.get("data", {}).get("values", [[]])[0])
        for frame in result.get("frames", [])
        if frame.get("data", {}).get("values")
    )
    item = {"panel": ref, "error": result.get("error"), "dataRows": count}
    summary.append(item)
    print(json.dumps(item))
response.raise_for_status()
for ref, result in payload["results"].items():
    if result.get("error"):
        raise RuntimeError(f"{ref}: {result['error']}")
    if not any(
        frame.get("data", {}).get("values") and frame["data"]["values"][0]
        for frame in result.get("frames", [])
    ):
        raise RuntimeError(f"{ref}: No ingested data returned.")
(root / "dashboard-verification.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
print("All 10 dashboard panels returned ingested data.")
