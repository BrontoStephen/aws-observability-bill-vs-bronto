"""Markdown report renderer for the CE-only auditor."""

from __future__ import annotations

from .bronto import BrontoPricing, BrontoProjection
from .cost_explorer import CostReport
from .org import Account


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def _pct(x: float, total: float) -> str:
    if total <= 0:
        return "0.0%"
    return f"{(x / total) * 100:.1f}%"


def render(
    *,
    report: CostReport,
    accounts: list[Account],
    projection: BrontoProjection,
    pricing: BrontoPricing,
    mgmt_account_id: str,
) -> str:
    obs_total = report.total(include_s3_unattributed=False)
    s3_unattr = report.by_bucket().get("S3 (unattributed)", 0.0)
    bronto_total = projection.cheapest_cost
    savings = obs_total - bronto_total
    savings_pct = _pct(savings, obs_total) if obs_total > 0 else "n/a"
    window_days = max(int(round(projection.months_in_window * 30.4375)), 1)

    lines: list[str] = []
    lines.append("# AWS Observability Bill vs Bronto.io")
    lines.append("")
    lines.append(f"_Window: {report.start} → {report.end} ({window_days} days)_")
    lines.append(
        f"_Management account: {mgmt_account_id} — "
        f"accounts in scope: {len(report.accounts_seen)}_"
    )
    lines.append("")

    lines.append("## Executive Summary")
    lines.append("")
    lines.append(f"- **AWS observability spend ({window_days}d):** {_usd(obs_total)}")
    lines.append(
        f"- **Projected Bronto spend ({projection.cheapest_plan} plan):** "
        f"{_usd(bronto_total)}"
    )
    if obs_total > 0:
        lines.append(f"- **Projected savings:** {_usd(savings)} ({savings_pct})")
    if s3_unattr > 0:
        lines.append(
            f"- _S3 spend across the same accounts: {_usd(s3_unattr)} — shown "
            "separately because the bulk of S3 spend is typically product/data, "
            "not log sinks. See caveats._"
        )
    lines.append("")

    lines.append("## Spend by Service")
    lines.append("")
    lines.append("| Service / Bucket | Spend | % of obs total |")
    lines.append("| --- | ---: | ---: |")
    by_bucket = report.by_bucket()
    for bucket, amt in sorted(by_bucket.items(), key=lambda kv: kv[1], reverse=True):
        if bucket == "S3 (unattributed)":
            continue
        lines.append(f"| {bucket} | {_usd(amt)} | {_pct(amt, obs_total)} |")
    if s3_unattr > 0:
        lines.append(f"| _S3 (separate)_ | _{_usd(s3_unattr)}_ | _(excluded)_ |")
    lines.append(f"| **Total** | **{_usd(obs_total)}** | **100.0%** |")
    lines.append("")

    lines.append("## Spend by Account")
    lines.append("")
    lines.append("| Account ID | Name | Spend |")
    lines.append("| --- | --- | ---: |")
    by_account = report.by_account()
    name_by_id = {a.id: a.name for a in accounts}
    for acct_id, amt in sorted(by_account.items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"| {acct_id} | {name_by_id.get(acct_id, '?')} | {_usd(amt)} |")
    lines.append("")

    lines.append("## Bronto Projection Detail")
    lines.append("")
    lines.append(
        f"_Total ingested volume (from Cost Explorer usage meters): "
        f"**{projection.gb_ingested:,.1f} GB** over {window_days} days._"
    )
    lines.append("")
    if projection.per_source_gb:
        lines.append("| Source | GB ingested |")
        lines.append("| --- | ---: |")
        for src, gb in sorted(
            projection.per_source_gb.items(), key=lambda kv: kv[1], reverse=True
        ):
            lines.append(f"| {src} | {gb:,.1f} |")
        lines.append("")
    lines.append("| Plan | Monthly fee | Included | Overage rate | Projected total |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for plan in pricing.plans:
        name = plan["name"]
        fee = float(plan["monthly_fee_usd"])
        inc = float(plan["included_tb"])
        cost = projection.plan_costs.get(name, 0.0)
        cheapest = " ←" if name == projection.cheapest_plan else ""
        lines.append(
            f"| {name}{cheapest} | {_usd(fee)} | {inc} TB/mo | "
            f"${pricing.ingest_per_gb_usd:.2f}/GB | {_usd(cost)} |"
        )
    lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- This tool uses **Cost Explorer only** — no probes, no per-account "
        "API walks, no bucket scanning. Numbers come straight from the AWS bill."
    )
    lines.append(
        "- The `S3` line is the total S3 spend across the same accounts and is "
        "**not** rolled into observability totals. Most S3 spend is usually "
        "product data, not log sinks; treat that figure as an upper bound on "
        "what could be log-related."
    )
    lines.append(
        "- Bronto's per-GB projection only counts data Bronto would actually "
        "ingest (log bytes, metric data points, traces, CloudTrail events). "
        "AWS charges Bronto does **not** levy — alarm-monitor hours, dashboard "
        "fees, GetMetricData API tiers, retention beyond 12 months, OpenSearch "
        "EBS — show up as AWS spend with no Bronto counterpart, which is why "
        "projected savings can look large."
    )
    lines.append("- Bronto search pricing is excluded per spec.")
    if projection.extended_retention_note:
        lines.append(f"- {projection.extended_retention_note}")
    lines.append(
        "- CloudWatch custom metrics convert at `bytes_per_metric_month` "
        "(default 3.4 MB/metric-month, 1-min resolution). Tune in "
        "`config/bronto_pricing.yaml` if your resolution differs."
    )
    lines.append(
        "- Trace / CloudTrail event / Prometheus sample volumes use "
        "configurable bytes-per-unit assumptions in `config/bronto_pricing.yaml`."
    )
    lines.append(
        "- OpenSearch ingestion cannot be derived from Cost Explorer alone "
        "(billed as instance hours and EBS). It contributes to AWS spend but "
        "has no GB counterpart in the Bronto projection."
    )
    lines.append("")
    return "\n".join(lines)
