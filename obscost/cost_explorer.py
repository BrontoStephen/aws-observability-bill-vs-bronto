"""Cost Explorer queries for observability spend.

Cost Explorer is region-pinned to us-east-1 and already aggregates across
the entire AWS Organization when called from the payer account. We make
two grouped queries to get both per-account and per-usage-type detail:

  Pass A: GroupBy (LINKED_ACCOUNT, SERVICE)    → per-account spend totals
  Pass B: GroupBy (SERVICE, USAGE_TYPE)        → sub-classification of
                                                  CloudWatch into Logs /
                                                  Metrics / Alarms / etc.,
                                                  plus the usage *quantity*
                                                  the Bronto projector
                                                  needs.

Pass-B lines are tagged with `account_id='*'` so the rest of the codebase
can tell the two passes apart and avoid double-counting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import boto3

log = logging.getLogger(__name__)

# Service-dimension values that count as "observability" for this audit.
# CloudTrail's billable component is data events under "AWS CloudTrail".
OBS_SERVICES = [
    "Amazon CloudWatch",
    "AmazonCloudWatch",
    "AWS X-Ray",
    "Amazon Managed Service for Prometheus",
    "Amazon Managed Grafana",
    "Amazon OpenSearch Service",
    "AWS CloudTrail",
]

# Firehose is the transport layer for CW MetricStream → Bronto. It's billed
# as a separate AWS service, so we fetch it with a dedicated CE call (see
# `fetch_firehose_costs`). Both naming variants exist in different accounts.
FIREHOSE_SERVICES = ["Amazon Kinesis Firehose", "Amazon Data Firehose"]

S3_SERVICE = "Amazon Simple Storage Service"


BUCKETS = [
    "CloudWatch Logs",
    "CloudWatch Metrics",
    "CloudWatch MetricStream (floor)",
    "CloudWatch Alarms",
    "CloudWatch Dashboards",
    "CloudWatch Insights",
    "CloudWatch Synthetics",
    "CloudWatch Other",
    "X-Ray",
    "Managed Prometheus",
    "Managed Grafana",
    "OpenSearch",
    "CloudTrail",
    "Firehose (floor)",
    "S3 (unattributed)",
]


def _classify(service: str, usage_type: str) -> str:
    """Map a (service, usage_type) pair to one of BUCKETS.

    Floor buckets ("CloudWatch MetricStream (floor)", "Firehose (floor)")
    represent AWS-side charges that *survive* a Bronto migration. They
    still contribute to gb_ingested for the Bronto projection (the bytes
    still flow into Bronto), but they are kept on the AWS side of the
    apples-to-apples comparison.
    """
    ut = usage_type or ""
    svc = service or ""

    if svc.startswith("AWS X-Ray") or "XRay" in ut:
        return "X-Ray"
    if svc == "Amazon Managed Service for Prometheus":
        return "Managed Prometheus"
    if svc == "Amazon Managed Grafana":
        return "Managed Grafana"
    if svc == "Amazon OpenSearch Service":
        return "OpenSearch"
    if svc == "AWS CloudTrail":
        return "CloudTrail"
    if svc in FIREHOSE_SERVICES:
        return "Firehose (floor)"
    if svc == S3_SERVICE:
        return "S3 (unattributed)"

    # CloudWatch sub-buckets — usage-type strings vary by region prefix.
    # Order matters:
    #   1. MetricStream (floor) must be checked before generic Metrics —
    #      it's an egress/transport charge on top of regular CW Metrics.
    #   2. Insights (DataScanned-Bytes) must be checked before Logs —
    #      the region prefix can contain the substring "log".
    u = ut.lower()
    if "metricstream" in u:
        return "CloudWatch MetricStream (floor)"
    if "datascanned" in u or "queryscanned" in u or "insight" in u:
        return "CloudWatch Insights"
    if "logs" in u or "log-" in u or "vpcflowlog" in u or "dataprocessing-bytes" in u:
        return "CloudWatch Logs"
    if "alarm" in u:
        return "CloudWatch Alarms"
    if "dashboard" in u:
        return "CloudWatch Dashboards"
    if "canary" in u or "synthetic" in u:
        return "CloudWatch Synthetics"
    if "metric" in u or "request" in u or "cw:metricmonitor" in u:
        return "CloudWatch Metrics"
    return "CloudWatch Other"


@dataclass
class UsageLine:
    account_id: str
    service: str
    usage_type: str
    bucket: str
    amount_usd: float
    quantity: float
    unit: str


@dataclass
class CostReport:
    start: str
    end: str
    lines: list[UsageLine] = field(default_factory=list)
    accounts_seen: set[str] = field(default_factory=set)

    def by_account(self) -> dict[str, float]:
        """Per-account totals — pass A only (account_id != '*')."""
        out: dict[str, float] = {}
        for ln in self.lines:
            if ln.account_id == "*":
                continue
            out[ln.account_id] = out.get(ln.account_id, 0.0) + ln.amount_usd
        return out

    def by_bucket(self) -> dict[str, float]:
        """Per-bucket totals — pass B only (sub-classifies CloudWatch)."""
        out: dict[str, float] = {}
        for ln in self.lines:
            if ln.account_id != "*":
                continue
            out[ln.bucket] = out.get(ln.bucket, 0.0) + ln.amount_usd
        return out

    def total(self, include_s3_unattributed: bool = False) -> float:
        return sum(
            ln.amount_usd
            for ln in self.lines
            if ln.account_id == "*"
            and (include_s3_unattributed or ln.bucket != "S3 (unattributed)")
        )


def fetch_costs(
    session: boto3.Session,
    start: str,
    end: str,
    account_ids: Optional[list[str]] = None,
) -> CostReport:
    """Pull observability spend from Cost Explorer.

    Two grouped queries: (LINKED_ACCOUNT, SERVICE) for per-account totals,
    then (SERVICE, USAGE_TYPE) for sub-classification + usage quantities.
    """
    ce = session.client("ce", region_name="us-east-1")

    services = OBS_SERVICES + [S3_SERVICE]
    cost_filter: dict = {"Dimensions": {"Key": "SERVICE", "Values": services}}
    if account_ids:
        cost_filter = {
            "And": [
                cost_filter,
                {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": list(account_ids)}},
            ]
        }

    report = CostReport(start=start, end=end)

    # Pass A — per-account totals.
    token: Optional[str] = None
    while True:
        kwargs = dict(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            Filter=cost_filter,
            GroupBy=[
                {"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"},
                {"Type": "DIMENSION", "Key": "SERVICE"},
            ],
        )
        if token:
            kwargs["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**kwargs)
        for period in resp.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                keys = group.get("Keys", [])
                if len(keys) < 2:
                    continue
                account_id, service = keys[0], keys[1]
                m = group.get("Metrics", {})
                amount = float(m.get("UnblendedCost", {}).get("Amount", 0.0))
                qty = float(m.get("UsageQuantity", {}).get("Amount", 0.0))
                unit = m.get("UsageQuantity", {}).get("Unit", "")
                report.accounts_seen.add(account_id)
                report.lines.append(
                    UsageLine(
                        account_id=account_id,
                        service=service,
                        usage_type="",
                        bucket=_classify(service, ""),
                        amount_usd=amount,
                        quantity=qty,
                        unit=unit,
                    )
                )
        token = resp.get("NextPageToken")
        if not token:
            break

    # Pass B — usage-type detail.
    token = None
    while True:
        kwargs = dict(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            Filter=cost_filter,
            GroupBy=[
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
            ],
        )
        if token:
            kwargs["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**kwargs)
        for period in resp.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                keys = group.get("Keys", [])
                if len(keys) < 2:
                    continue
                service, usage_type = keys[0], keys[1]
                m = group.get("Metrics", {})
                amount = float(m.get("UnblendedCost", {}).get("Amount", 0.0))
                qty = float(m.get("UsageQuantity", {}).get("Amount", 0.0))
                unit = m.get("UsageQuantity", {}).get("Unit", "")
                report.lines.append(
                    UsageLine(
                        account_id="*",
                        service=service,
                        usage_type=usage_type,
                        bucket=_classify(service, usage_type),
                        amount_usd=amount,
                        quantity=qty,
                        unit=unit,
                    )
                )
        token = resp.get("NextPageToken")
        if not token:
            break

    return report


def fetch_firehose_costs(
    session: boto3.Session,
    start: str,
    end: str,
    account_ids: Optional[list[str]] = None,
) -> list:
    """Pull Kinesis Firehose / Data Firehose spend.

    Firehose is the transport for CloudWatch MetricStream → Bronto. It's a
    distinct AWS service so we fetch it with a dedicated CE call rather than
    bundling it into the main observability filter. Returns a list of
    UsageLine objects (account_id="*", bucket="Firehose (floor)") ready to
    be merged into the main CostReport.lines.
    """
    ce = session.client("ce", region_name="us-east-1")
    cost_filter: dict = {"Dimensions": {"Key": "SERVICE", "Values": FIREHOSE_SERVICES}}
    if account_ids:
        cost_filter = {
            "And": [
                cost_filter,
                {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": list(account_ids)}},
            ]
        }

    lines: list[UsageLine] = []
    token: Optional[str] = None
    while True:
        kwargs = dict(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            Filter=cost_filter,
            GroupBy=[
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
            ],
        )
        if token:
            kwargs["NextPageToken"] = token
        try:
            resp = ce.get_cost_and_usage(**kwargs)
        except Exception as e:  # noqa: BLE001
            log.warning("Firehose CE query failed: %s", e)
            return lines
        for period in resp.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                keys = group.get("Keys", [])
                if len(keys) < 2:
                    continue
                service, usage_type = keys[0], keys[1]
                m = group.get("Metrics", {})
                amount = float(m.get("UnblendedCost", {}).get("Amount", 0.0))
                qty = float(m.get("UsageQuantity", {}).get("Amount", 0.0))
                unit = m.get("UsageQuantity", {}).get("Unit", "")
                lines.append(
                    UsageLine(
                        account_id="*",
                        service=service,
                        usage_type=usage_type,
                        bucket="Firehose (floor)",
                        amount_usd=amount,
                        quantity=qty,
                        unit=unit,
                    )
                )
        token = resp.get("NextPageToken")
        if not token:
            break
    return lines


def fetch_recent_spend_by_service(
    session: boto3.Session,
    days: int = 7,
    account_ids: Optional[list[str]] = None,
) -> dict[str, float]:
    """Trailing-N-day spend grouped by SERVICE — used for decom detection.

    A service with spend > 0 in the analysis window but $0 in the trailing
    `days` window is flagged as decommissioned and excluded from the
    forward-looking AWS baseline.

    Filtered to observability services + Firehose (so we never flag e.g.
    EC2 or S3 as decom — out of scope).
    """
    ce = session.client("ce", region_name="us-east-1")
    today = date.today()
    start = (today - timedelta(days=days)).isoformat()
    end = today.isoformat()

    services = OBS_SERVICES + FIREHOSE_SERVICES
    cost_filter: dict = {"Dimensions": {"Key": "SERVICE", "Values": services}}
    if account_ids:
        cost_filter = {
            "And": [
                cost_filter,
                {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": list(account_ids)}},
            ]
        }

    spend_by_service: dict[str, float] = {svc: 0.0 for svc in services}
    token: Optional[str] = None
    while True:
        kwargs = dict(
            TimePeriod={"Start": start, "End": end},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            Filter=cost_filter,
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        if token:
            kwargs["NextPageToken"] = token
        try:
            resp = ce.get_cost_and_usage(**kwargs)
        except Exception as e:  # noqa: BLE001
            log.warning("Trailing-%dd CE probe failed: %s", days, e)
            return spend_by_service
        for period in resp.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                keys = group.get("Keys", [])
                if not keys:
                    continue
                svc = keys[0]
                amount = float(
                    group.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", 0.0)
                )
                spend_by_service[svc] = spend_by_service.get(svc, 0.0) + amount
        token = resp.get("NextPageToken")
        if not token:
            break
    return spend_by_service
