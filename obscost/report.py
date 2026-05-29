"""Markdown report renderer for the CE-only auditor.

Layout (top-down):

  1. Header + savings callout (forward-looking, apples-to-apples)
  2. Executive Summary
  3. Spend by Service (with Status column: floor / displaceable / decommissioned)
  4. AWS-side Floor detail
  5. OpenSearch Displacement Analysis (if footprint detected)
  6. Spend by Account
  7. Bronto Projection Detail (per-source GB, plan comparison incl. floor)
  8. Caveats
"""

from __future__ import annotations

from .bronto import (
    FLOOR_BUCKETS,
    BrontoPricing,
    BrontoProjection,
    TB_TO_GB,
    signal_type,
)
from .cost_explorer import CostReport
from .org import Account


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def _pct_of(x: float, total: float) -> str:
    if total <= 0:
        return "0.0%"
    return f"{(x / total) * 100:.1f}%"


def _pct(x: float) -> str:
    return f"{x:.1f}%"


def _gb(x: float) -> str:
    return f"{x:,.1f} GB"


def _bucket_status(bucket: str, decommissioned: set[str]) -> str:
    if bucket in decommissioned:
        return "_decommissioned_"
    if bucket in FLOOR_BUCKETS:
        return "**floor (survives)**"
    return "displaceable"


def render(
    *,
    report: CostReport,
    accounts: list[Account],
    projection: BrontoProjection,
    pricing: BrontoPricing,
    mgmt_account_id: str,
) -> str:
    obs_total = projection.obs_total_as_billed
    obs_total_forward = projection.obs_total_forward
    s3_unattr = report.by_bucket().get("S3 (unattributed)", 0.0)
    window_days = max(int(round(projection.months_in_window * 30.4375)), 1)
    bronto_total = projection.cheapest_cost

    lines: list[str] = []
    lines.append("# AWS Observability Bill vs Bronto.io")
    lines.append("")
    lines.append(f"_Window: {report.start} → {report.end} ({window_days} days)_  ")
    lines.append(
        f"_Management account: {mgmt_account_id} — "
        f"accounts in scope: {len(report.accounts_seen)}_"
    )
    lines.append("")

    # Lead with forward-looking apples-to-apples savings.
    if obs_total_forward > 0:
        callout = (
            f"> **Projected savings (forward-looking, apples-to-apples): "
            f"{_pct(projection.apples_savings_pct)} "
            f"({_usd(projection.apples_savings_abs)} over {window_days} days)** — "
            f"post-migration AWS+Bronto cost {_usd(projection.post_migration_cost)} "
            f"vs **{_usd(obs_total_forward)}** AWS run-rate"
        )
        if projection.decom_spend > 0:
            callout += f" (excludes {_usd(projection.decom_spend)} of decommissioned services)"
        callout += f". Unavoidable AWS-side floor: **{_usd(projection.aws_floor)}** (MetricStream + Firehose)."
        lines.append(callout)
        lines.append("")

    # Executive Summary
    lines.append("## Executive Summary")
    lines.append("")
    if projection.decommissioned:
        decom_list = ", ".join(sorted(projection.decommissioned))
        lines.append(
            f"⚠️  **Decommissioned services detected**: {decom_list} had spend in the "
            f"{window_days}-day window but $0 in the trailing 7 days. Excluded from "
            f"forward-looking projection. Historical spend: {_usd(projection.decom_spend)}."
        )
        lines.append("")
    if obs_total > 0:
        lines.append(
            f"- **Projected savings (forward-looking, apples-to-apples)**: "
            f"{_usd(projection.apples_savings_abs)} ({_pct(projection.apples_savings_pct)})"
        )
    lines.append(
        f"- AWS observability spend, as-billed over {window_days} days: "
        f"**{_usd(obs_total)}**"
    )
    if projection.decom_spend > 0:
        lines.append(
            f"- AWS observability spend, forward-looking (ex-decommissioned): "
            f"**{_usd(obs_total_forward)}**"
        )
    lines.append(
        f"- Post-migration cost: **{_usd(projection.post_migration_cost)}** "
        f"= AWS floor {_usd(projection.aws_floor)} + Bronto {projection.cheapest_plan} "
        f"{_usd(bronto_total)}"
    )
    floor_parts: list[str] = []
    cw_ms = report.by_bucket().get("CloudWatch MetricStream (floor)", 0.0)
    fh = report.by_bucket().get("Firehose (floor)", 0.0)
    if cw_ms > 0:
        floor_parts.append(f"MetricStream {_usd(cw_ms)}")
    if fh > 0:
        floor_parts.append(f"Firehose {_usd(fh)}")
    if floor_parts:
        lines.append(
            f"- **AWS-side floor** ({_usd(projection.aws_floor)}, survives migration): "
            + " + ".join(floor_parts)
        )
    lines.append(
        f"- **Displaceable AWS spend** (eliminated by Bronto): "
        f"{_usd(projection.displaceable)}"
    )
    bronto_line = (
        f"- Projected Bronto spend (cheapest = **{projection.cheapest_plan}**): "
        f"**{_usd(bronto_total)}**"
    )
    if projection.gb_searched > 0:
        ingest_cost = projection.plan_ingest_costs.get(projection.cheapest_plan, 0.0)
        search_cost = projection.plan_search_costs.get(projection.cheapest_plan, 0.0)
        bronto_line += f" — ingest {_usd(ingest_cost)} + search {_usd(search_cost)}"
    lines.append(bronto_line)
    if projection.per_signal_gb:
        sig = projection.per_signal_gb
        lines.append(
            f"- Ingest by signal type: Logs {_gb(sig.get('logs', 0))} · "
            f"Metrics {_gb(sig.get('metrics', 0))} · "
            f"Traces {_gb(sig.get('traces', 0))} (total **{_gb(projection.gb_ingested)}**)"
        )
    if obs_total > 0:
        lines.append(
            f"- _For reference, naive savings ignoring AWS floor: "
            f"{_usd(projection.naive_savings_abs)} ({_pct(projection.naive_savings_pct)}) — "
            "overstated because it assumes Bronto displaces MetricStream/Firehose._"
        )
    if s3_unattr > 0:
        lines.append(
            f"- _S3 spend across the same accounts: {_usd(s3_unattr)} — "
            "shown separately (mostly product data, not log sinks)._"
        )
    lines.append("")

    # Spend by Service with Status column
    lines.append("## Spend by Service")
    lines.append("")
    lines.append(
        "Status legend: **floor** = survives migration (AWS egress charges); "
        "**displaceable** = eliminated by Bronto; **decommissioned** = had spend in "
        "window but $0 in trailing 7d, excluded from forward-looking projection."
    )
    lines.append("")
    lines.append("| Bucket | Spend | % of obs total | Status |")
    lines.append("| --- | ---: | ---: | --- |")
    by_bucket = report.by_bucket()
    for bucket, amt in sorted(by_bucket.items(), key=lambda kv: kv[1], reverse=True):
        if bucket == "S3 (unattributed)":
            continue
        status = _bucket_status(bucket, projection.decommissioned)
        lines.append(
            f"| {bucket} | {_usd(amt)} | {_pct_of(amt, obs_total)} | {status} |"
        )
    if s3_unattr > 0:
        lines.append(f"| _S3 (separate)_ | _{_usd(s3_unattr)}_ | — | excluded from comparison |")
    lines.append(
        f"| **Total (observability, as-billed)** | **{_usd(obs_total)}** | 100.0% | |"
    )
    if projection.decom_spend > 0:
        lines.append(f"| ↳ minus decommissioned | −{_usd(projection.decom_spend)} | | |")
        lines.append(
            f"| **= Forward-looking total** | **{_usd(obs_total_forward)}** | | |"
        )
    if obs_total_forward > 0:
        lines.append(
            f"| ↳ floor subtotal (forward) | {_usd(projection.aws_floor)} | "
            f"{_pct_of(projection.aws_floor, obs_total_forward)} | survives |"
        )
        lines.append(
            f"| ↳ displaceable subtotal | {_usd(projection.displaceable)} | "
            f"{_pct_of(projection.displaceable, obs_total_forward)} | eliminated |"
        )
    lines.append("")

    # AWS-side Floor detail
    lines.append("## AWS-side Floor (post-migration, forward-looking)")
    lines.append("")
    lines.append(
        "These AWS charges remain after switching log / metric / trace storage and "
        "querying to Bronto. They are kept on the AWS side of the comparison."
    )
    lines.append("")
    lines.append("| Line | Spend | Status | Why it survives |")
    lines.append("| --- | ---: | --- | --- |")
    floor_rows = [
        (
            "CloudWatch MetricStream",
            cw_ms,
            (
                "$0.003 per 1K metric updates streamed out of CW Metrics — same fee "
                "regardless of destination (Bronto, Datadog, S3). Avoidable only by "
                "re-sourcing AWS-platform metrics away from CW."
            ),
        ),
        (
            "Kinesis Firehose",
            fh,
            "Transport layer for MetricStream → Bronto. Billed per GB delivered "
            "+ cross-region transfer.",
        ),
    ]
    for name, amt, why in floor_rows:
        if amt <= 0:
            continue
        lines.append(f"| {name} | {_usd(amt)} | active | {why} |")
    lines.append(f"| **Total floor (forward-looking)** | **{_usd(projection.aws_floor)}** | | |")
    if projection.aws_floor_historical > projection.aws_floor:
        lines.append(
            f"| _Historical floor incl. decommissioned_ | "
            f"_{_usd(projection.aws_floor_historical)}_ | | |"
        )
    lines.append("")

    # OpenSearch Displacement Analysis
    os_aws_cost = by_bucket.get("OpenSearch", 0.0)
    if projection.os_displacement is not None and os_aws_cost > 0:
        os_decom = "OpenSearch" in projection.decommissioned
        title = "## OpenSearch: Decommissioned" if os_decom else "## OpenSearch Displacement Analysis"
        lines.append(title)
        lines.append("")
        if os_decom:
            lines.append(
                f"**The OpenSearch domain went silent in the trailing 7 days** "
                f"(was active during the analysis window). The {_usd(os_aws_cost)} "
                "in the historical window is excluded from the forward-looking "
                "projection above. The displacement scenario below is retained "
                "as reference for what the workload *would have* cost on Bronto."
            )
        else:
            lines.append(
                "OpenSearch is treated as **displaceable** — Bronto can absorb "
                "log-search / SIEM / time-series-analytics workloads (with caveats "
                "for vector / RAG / e-commerce search; see end of section). This "
                "section estimates what it would cost Bronto to absorb the workload, "
                "using the cluster shape backed out of Cost Explorer line items + "
                "[AWS's published OpenSearch pricing](https://aws.amazon.com/opensearch-service/pricing/) "
                "sizing rules."
            )
        lines.append("")

        fp = projection.os_displacement.footprint
        lines.append("### Cluster footprint (inferred from Cost Explorer)")
        lines.append("")
        if fp.instance_type:
            lines.append(
                f"- **Instance**: {fp.instance_hours:.0f} hours of "
                f"`{fp.instance_type}.search` @ ${fp.instance_rate:.4f}/hr "
                f"({fp.node_days:.1f} node-days over {window_days}-day window)"
            )
        if fp.ebs_type:
            lines.append(
                f"- **Storage**: {fp.ebs_gb_months:.1f} GB-months of "
                f"{fp.ebs_type.upper()} @ ${fp.ebs_rate:.3f}/GB-mo → "
                f"**~{fp.ebs_gb_provisioned:.0f} GB provisioned**"
            )
        lines.append(
            "- **Direct probe not attempted** — this is the CE-only tool. Cluster "
            "introspection (`aws opensearch list-domain-names`, `aws cloudwatch "
            "list-metrics --namespace AWS/ES`) would require either running with "
            "permissions in the OpenSearch account, or assuming a cross-account role. "
            "The sibling repo (bronto-aws-savings-report) attempts these probes."
        )
        lines.append("")

        disp = projection.os_displacement
        lines.append("### Sizing logic (from AWS OpenSearch pricing examples)")
        lines.append("")
        lines.append(f"- Provisioned EBS: **{fp.ebs_gb_provisioned:.0f} GB**")
        lines.append("- × 0.85 free-space headroom (OS reserves space to avoid disk-full)")
        lines.append("- × 0.91 Lucene/segment overhead (indexing produces ~10% overhead vs raw)")
        lines.append(f"- = **{disp.usable_gb:.0f} GB usable** for actual data")
        lines.append("- × 0.80 typical utilization (clusters don't run at 100% full)")
        lines.append(
            f"- = **{disp.raw_resident_gb:.0f} GB raw data resident** at steady state"
        )
        lines.append("")
        lines.append(
            "Single-node domain ⇒ 0 replicas (can't replicate to self). For "
            "multi-node clusters with 1 replica, halve the raw data estimate."
        )
        lines.append("")

        starter_incl_gb = float(
            next((p["included_tb"] for p in pricing.plans if p["name"].lower() == "starter"), 0)
        ) * TB_TO_GB * max(projection.months_in_window, 1e-9)
        headroom = max(starter_incl_gb - projection.gb_ingested, 0.0)
        lines.append("### Bronto cost to absorb, by retention scenario")
        lines.append("")
        lines.append(
            f"Resident data is the same regardless of retention — the difference is "
            f"*flow*. Shorter retention means higher daily ingest rate to maintain "
            f"the same {disp.raw_resident_gb:.0f} GB resident."
        )
        lines.append("")
        lines.append(
            f"Current observability ingest projection: **{_gb(projection.gb_ingested)}** "
            f"out of Starter's **{_gb(starter_incl_gb)}** included → headroom of "
            f"**{_gb(headroom)}** before any overage."
        )
        lines.append("")
        lines.append(
            "| Retention | Daily ingest | Total ingest over window | Fits Starter? | "
            "Bronto incremental | OpenSearch saved | Net savings |"
        )
        lines.append("| --- | ---: | ---: | :---: | ---: | ---: | ---: |")
        for sc in disp.scenarios:
            fits = "✓" if sc.fits_in_starter else "overage"
            net = os_aws_cost - sc.incremental_bronto_cost
            lines.append(
                f"| {sc.retention_days}d | {_gb(sc.ingest_per_day_gb)}/day | "
                f"{_gb(sc.ingest_over_window_gb)} | {fits} | "
                f"{_usd(sc.incremental_bronto_cost)} | {_usd(os_aws_cost)} | "
                f"**{_usd(net)}** |"
            )
        lines.append("")
        lines.append(
            "**Caveat — does Bronto actually replace this OpenSearch workload?** "
            f"A `{fp.instance_type or 'small'}` + ~{fp.ebs_gb_provisioned:.0f} GB "
            "cluster could be any of:"
        )
        lines.append("")
        lines.append("- **Log search / SIEM** → ✅ Bronto displaces fully.")
        lines.append(
            "- **Application search** (e-commerce search box, doc search) → "
            "❌ Bronto does not displace; OpenSearch stays."
        )
        lines.append(
            "- **Time-series analytics / dashboarding** → ✅ Bronto displaces "
            "(it's effectively logs + metrics)."
        )
        lines.append("- **Vector / RAG embeddings** → ❌ Bronto does not displace.")
        lines.append("")
        lines.append(
            "Without describe-domain access we can't tell which. **Apply judgment "
            "based on what team owns this domain.**"
        )
        lines.append("")

    # Spend by Account
    lines.append("## Spend by Account")
    lines.append("")
    lines.append("| Account ID | Name | Spend |")
    lines.append("| --- | --- | ---: |")
    by_account = report.by_account()
    name_by_id = {a.id: a.name for a in accounts}
    for acct_id, amt in sorted(by_account.items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"| {acct_id} | {name_by_id.get(acct_id, '?')} | {_usd(amt)} |")
    lines.append("")

    # Bronto Projection Detail
    lines.append("## Bronto Projection Detail")
    lines.append("")
    lines.append(
        f"_Ingest volume: **{_gb(projection.gb_ingested)}** over {window_days} days_"
    )
    if projection.gb_searched > 0:
        sbs = projection.search_gb_by_signal
        lines.append(
            f"_Search/scan volume: **{_gb(projection.gb_searched)}** "
            f"(Logs {_gb(sbs.get('logs', 0))} · "
            f"Metrics {_gb(sbs.get('metrics', 0))} · "
            f"Traces {_gb(sbs.get('traces', 0))})_"
        )
    lines.append("")
    if projection.per_source_gb:
        lines.append("| Signal | Source | GB ingested |")
        lines.append("| --- | --- | ---: |")
        for src, gb in sorted(
            projection.per_source_gb.items(), key=lambda kv: kv[1], reverse=True
        ):
            lines.append(f"| {signal_type(src)} | {src} | {_gb(gb)} |")
        sig = projection.per_signal_gb
        for s in ("logs", "metrics", "traces"):
            lines.append(f"| **{s}** | **subtotal** | **{_gb(sig.get(s, 0))}** |")
        lines.append("")

    lines.append("### Plan comparison (apples-to-apples — Bronto + AWS floor vs current AWS)")
    lines.append("")
    lines.append(
        f"AWS floor surviving migration: **{_usd(projection.aws_floor)}** "
        "(added to each plan's total below)."
    )
    lines.append("")
    lines.append(
        "| Plan | Monthly fee | Included ingest | Search allowance | "
        "Bronto cost | + AWS floor | All-in total |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for plan in pricing.plans:
        name = plan["name"]
        fee = float(plan["monthly_fee_usd"])
        inc_tb = float(plan["included_tb"])
        ingest_cost = projection.plan_ingest_costs.get(name, 0.0)
        search_cost = projection.plan_search_costs.get(name, 0.0)
        bronto_cost = projection.plan_costs.get(name, 0.0)
        allowance_gb = projection.plan_search_allowance_gb.get(name, 0.0)
        if "search_multiplier_of_ingest" in plan:
            search_label = (
                f"{plan['search_multiplier_of_ingest']}× ingest "
                f"({allowance_gb / TB_TO_GB:,.1f} TB)"
            )
        elif allowance_gb > 0:
            search_label = f"{allowance_gb / TB_TO_GB:,.0f} TB"
        else:
            search_label = "$1/TB from byte 1"
        if inc_tb > 0:
            incl_label = f"{inc_tb} TB/mo"
        else:
            incl_label = "$0.10/GB from byte 1"
        all_in = bronto_cost + projection.aws_floor
        cheapest = " ←" if name == projection.cheapest_plan else ""
        lines.append(
            f"| {name}{cheapest} | {_usd(fee)} | {incl_label} | {search_label} | "
            f"{_usd(bronto_cost)} | {_usd(projection.aws_floor)} | **{_usd(all_in)}** |"
        )
    lines.append(
        f"| _Status quo (forward-looking, ex-decom)_ | — | — | — | — | — | "
        f"**{_usd(obs_total_forward)}** |"
    )
    if projection.decom_spend > 0:
        lines.append(
            f"| _Status quo (as-billed, incl. decom)_ | — | — | — | — | — | "
            f"_{_usd(obs_total)}_ |"
        )
    lines.append("")

    # Caveats
    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- **Apples-to-apples comparison** assumes you keep AWS-platform metrics "
        "flowing into Bronto via CloudWatch MetricStream → Firehose. That path is "
        "the cost floor you cannot avoid without re-sourcing metrics away from "
        "CloudWatch entirely (e.g., OTel Collector scraping services directly)."
    )
    lines.append(
        "- **OpenSearch is in the displaceable bucket** — Bronto absorbs log-search, "
        "SIEM, and time-series workloads. **It does not displace** application "
        "search (e-commerce, doc search), vector / RAG embeddings, or other search "
        "use cases. Use the displacement section above + your knowledge of the "
        "domain's purpose to decide whether the saving is real."
    )
    lines.append(
        "- **Decommissioned services** are detected via a trailing-7-day Cost Explorer "
        "probe (`granularity=DAILY`, grouped by SERVICE). Any observability service "
        "with spend in the analysis window but $0 in the trailing 7 days is flagged "
        "and excluded from the forward-looking baseline. CloudWatch and S3 are "
        "deliberately excluded from this check (too broad — false positives would "
        "distort the report)."
    )
    lines.append(
        "- **S3 shown separately** ($%s). Most S3 spend is product data, not log "
        "sinks; do not assume Bronto displaces it." % f"{s3_unattr:,.2f}"
    )
    lines.append(
        "- **Bronto charges per ingested GB + search overage only.** Remaining AWS "
        "billables outside the floor (alarm hours, dashboards, API requests, "
        "retention beyond 12 mo, OpenSearch EBS) only displace if you actually "
        "cut over those workflows."
    )
    lines.append("- **Bytes-per-unit defaults used**:")
    lines.append(
        f"  - CloudWatch custom metrics: {pricing.bytes_per_metric_month/1_000_000:.1f} MB / metric-month"
    )
    lines.append(f"  - CloudWatch MetricStream updates: {pricing.bytes_per_metric_stream_update} B each")
    lines.append(f"  - X-Ray traces: {pricing.bytes_per_xray_trace} B each")
    lines.append(f"  - Prometheus samples: {pricing.bytes_per_prometheus_sample} B each")
    lines.append(f"  - CloudTrail data events: {pricing.bytes_per_cloudtrail_event} B each")
    lines.append(f"  - GetMetricData queries: {pricing.bytes_per_metric_query} B each (≈1h × 1-min res)")
    lines.append(f"  - X-Ray trace retrieval: {pricing.bytes_per_trace_query} B each")
    if projection.os_displacement:
        lines.append(
            "- **OpenSearch sizing constants** (0.85 / 0.91 / 0.80): from "
            "[AWS OpenSearch pricing examples](https://aws.amazon.com/opensearch-service/pricing/) "
            "plus general operational guidance. Cluster ingest cannot be derived "
            "from Cost Explorer alone — instance hours and EBS are billed, not bytes."
        )
    lines.append(
        "- **Enterprise plan** has no included allowance ($0.10/GB ingest, $1/TB "
        "search from byte 1) but bundles dedicated Slack + TAM, SLA, HIPAA/SOC2, "
        "and extendable retention — perks not reflected in the dollar projection."
    )
    if projection.extended_retention_note:
        lines.append(f"- {projection.extended_retention_note}")
    lines.append("")
    return "\n".join(lines)
