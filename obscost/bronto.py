"""Bronto.io cost projection from Cost Explorer usage meters.

Pricing source: https://bronto.io/pricing
  - Ingest: $0.10/GB, uniform across logs, metrics, and traces.
  - Retention: 12 months included on all plans.
  - Search: excluded from this projection per spec.

The projector reads Cost Explorer's per-USAGE_TYPE quantities and converts
them into 'GB ingested'. Sources covered:

  * CloudWatch Logs ingestion (DataProcessing-Bytes, already in GB)
  * CloudWatch custom metrics (MetricMonitor metric-months → bytes)
  * X-Ray traces (count → bytes)
  * Managed Prometheus samples (count → bytes)
  * CloudTrail data events (count → bytes)

OpenSearch is NOT in this projection: Cost Explorer reports it as instance
hours and EBS, neither of which translates to ingested bytes without
querying the cluster. AMP/Grafana/Synthetics/Alarms/Dashboards/Insights
likewise show up as AWS spend with no Bronto counterpart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from .cost_explorer import CostReport, UsageLine

log = logging.getLogger(__name__)

GB = 1024 ** 3
TB_TO_GB = 1024


@dataclass
class BrontoPricing:
    ingest_per_gb_usd: float
    included_retention_months: int
    extended_retention_per_gb_month_usd: float | None
    search_per_gb_usd: float
    plans: list[dict]
    bytes_per_xray_trace: int
    bytes_per_prometheus_sample: int
    bytes_per_cloudtrail_event: int
    bytes_per_metric_month: int
    bytes_per_metric_stream_update: int

    @classmethod
    def load(cls, path: str | Path) -> "BrontoPricing":
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        return cls(
            ingest_per_gb_usd=float(data["ingest_per_gb_usd"]),
            included_retention_months=int(data["included_retention_months"]),
            extended_retention_per_gb_month_usd=(
                None
                if data.get("extended_retention_per_gb_month_usd") is None
                else float(data["extended_retention_per_gb_month_usd"])
            ),
            search_per_gb_usd=float(data.get("search_per_gb_usd", 0.0)),
            plans=list(data.get("plans", [])),
            bytes_per_xray_trace=int(data.get("bytes_per_xray_trace", 2048)),
            bytes_per_prometheus_sample=int(data.get("bytes_per_prometheus_sample", 8)),
            bytes_per_cloudtrail_event=int(data.get("bytes_per_cloudtrail_event", 1536)),
            bytes_per_metric_month=int(data.get("bytes_per_metric_month", 3_440_000)),
            bytes_per_metric_stream_update=int(data.get("bytes_per_metric_stream_update", 80)),
        )


SIGNAL_TYPE = {
    "CloudWatch Logs": "logs",
    "CloudTrail": "logs",
    "CloudWatch Metrics": "metrics",
    "Managed Prometheus": "metrics",
    "X-Ray": "traces",
    # OpenSearch can be any of the three — bucket it under "logs" since
    # that's the most common use case, but flag in caveats.
    "OpenSearch": "logs",
}


def signal_type(bucket: str) -> str:
    return SIGNAL_TYPE.get(bucket, "other")


@dataclass
class BrontoProjection:
    gb_ingested: float = 0.0
    gb_searched: float = 0.0
    per_source_gb: dict[str, float] = field(default_factory=dict)
    per_signal_gb: dict[str, float] = field(default_factory=dict)
    plan_costs: dict[str, float] = field(default_factory=dict)  # ingest + search per plan
    plan_ingest_costs: dict[str, float] = field(default_factory=dict)
    plan_search_costs: dict[str, float] = field(default_factory=dict)
    plan_search_allowance_gb: dict[str, float] = field(default_factory=dict)
    cheapest_plan: str = ""
    cheapest_cost: float = 0.0
    months_in_window: float = 0.0
    extended_retention_note: str | None = None


def _months_between(start: str, end: str) -> float:
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    return max((e - s).days / 30.4375, 0.0)


def _line_to_gb(line: UsageLine, pricing: BrontoPricing) -> float:
    """Convert a Cost Explorer usage line into estimated GB ingested.

    Returns 0 for lines that don't represent ingestion (retention storage,
    API requests, dashboards, alarms, instance hours, search/scan, etc.).
    """
    ut = (line.usage_type or "").lower()
    qty = line.quantity

    if line.bucket == "CloudWatch Logs":
        # Both customer-published logs and vended logs (ALB/CloudFront/etc.)
        # are reported in GB and both represent ingest.
        if "dataprocessing-bytes" in ut or "vendedlog-bytes" in ut:
            return qty
    if line.bucket == "CloudWatch Metrics":
        # Custom metrics published (metric-months).
        if "metricmonitor" in ut:
            return (qty * pricing.bytes_per_metric_month) / GB
        # Metric Streams forwarding (per metric update — Bronto-equivalent
        # since this is what you'd send if you routed metrics to Bronto
        # directly instead of via Streams + Firehose).
        if "metricstream" in ut:
            return (qty * pricing.bytes_per_metric_stream_update) / GB
    if line.bucket == "X-Ray" and "tracesrecorded" in ut:
        return (qty * pricing.bytes_per_xray_trace) / GB
    if line.bucket == "Managed Prometheus" and "samples" in ut:
        return (qty * pricing.bytes_per_prometheus_sample) / GB
    if line.bucket == "CloudTrail" and (
        "dataevents" in ut or "data-events" in ut or "paideventsrecorded" in ut
    ):
        return (qty * pricing.bytes_per_cloudtrail_event) / GB
    return 0.0


def _line_to_search_gb(line: UsageLine) -> float:
    """Return GB scanned by CloudWatch Logs Insights queries.

    AWS reports this as `*-DataScanned-Bytes` (quantity already in GB).
    """
    ut = (line.usage_type or "").lower()
    if line.bucket == "CloudWatch Insights" and "datascanned" in ut:
        return line.quantity
    return 0.0


def _search_allowance_gb(plan: dict, gb_ingested: float) -> float:
    """Search inclusion is per-plan: either a flat `search_included_tb`
    or a `search_multiplier_of_ingest` (e.g. Enterprise = 100× ingested)."""
    if "search_included_tb" in plan:
        return float(plan["search_included_tb"]) * TB_TO_GB
    if "search_multiplier_of_ingest" in plan:
        return float(plan["search_multiplier_of_ingest"]) * gb_ingested
    return 0.0


def project(report: CostReport, pricing: BrontoPricing) -> BrontoProjection:
    """Convert observability usage into a Bronto cost projection.

    Each plan has its own search inclusion model — flat TB allowance or
    a multiplier on ingested volume. Search overage is uniformly
    `search_per_gb_usd` ($1/TB by default). Cheapest plan wins.
    """
    proj = BrontoProjection(months_in_window=_months_between(report.start, report.end))

    per_source: dict[str, float] = {}
    for line in report.lines:
        if line.account_id != "*":
            continue
        gb = _line_to_gb(line, pricing)
        if gb > 0:
            per_source[line.bucket] = per_source.get(line.bucket, 0.0) + gb
            proj.gb_ingested += gb
        search_gb = _line_to_search_gb(line)
        if search_gb > 0:
            proj.gb_searched += search_gb
    proj.per_source_gb = per_source

    # Roll up per-source into the three signal types (logs/metrics/traces).
    # Sources that don't map (e.g. OpenSearch with no probe data) land in
    # whichever signal SIGNAL_TYPE assigns them; default is "other".
    per_signal: dict[str, float] = {"logs": 0.0, "metrics": 0.0, "traces": 0.0}
    for src, gb in per_source.items():
        sig = signal_type(src)
        per_signal[sig] = per_signal.get(sig, 0.0) + gb
    proj.per_signal_gb = per_signal

    months = max(proj.months_in_window, 1e-9)
    for plan in pricing.plans:
        name = plan["name"]
        fee = float(plan["monthly_fee_usd"]) * months
        included_ingest_gb = float(plan["included_tb"]) * TB_TO_GB * months
        ingest_overage_gb = max(proj.gb_ingested - included_ingest_gb, 0.0)
        ingest_cost = fee + ingest_overage_gb * pricing.ingest_per_gb_usd

        included_search_gb = _search_allowance_gb(plan, proj.gb_ingested)
        search_overage_gb = max(proj.gb_searched - included_search_gb, 0.0)
        search_cost = search_overage_gb * pricing.search_per_gb_usd

        proj.plan_ingest_costs[name] = ingest_cost
        proj.plan_search_costs[name] = search_cost
        proj.plan_search_allowance_gb[name] = included_search_gb
        proj.plan_costs[name] = ingest_cost + search_cost

    if proj.plan_costs:
        proj.cheapest_plan, proj.cheapest_cost = min(
            proj.plan_costs.items(), key=lambda kv: kv[1]
        )

    if pricing.extended_retention_per_gb_month_usd is None:
        proj.extended_retention_note = (
            "Extended retention (>12 months) priced via 'contact sales' — "
            "not included in projection."
        )
    return proj
