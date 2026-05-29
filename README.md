# AWS Observability Bill vs Bronto.io

A tiny Python CLI that pulls observability spend from the AWS **Cost
Explorer API** and compares it to what [Bronto.io](https://bronto.io/pricing)
would charge for the same ingested volume at `$0.10/GB`.

No probes. No regional walks. No bucket scanning. **Just the bill.**

The comparison is **apples-to-apples**: AWS charges that survive a
Bronto migration (CloudWatch MetricStream egress + Kinesis Firehose
transport — the "floor") stay on the AWS side. Only displaceable
spend gets replaced by the Bronto plan cost. Services that have gone
silent in the trailing 7 days are flagged as decommissioned and
excluded from the forward-looking baseline.

OpenSearch sits in the displaceable bucket — Bronto absorbs log-search /
SIEM / time-series workloads. Vector / RAG / application search are the
exceptions, called out in caveats. The OpenSearch displacement section
estimates Bronto incremental cost across retention scenarios using AWS's
[published pricing examples](https://aws.amazon.com/opensearch-service/pricing/)
to size the cluster from CE line items.

> **Want this without the Python?** See [PROMPT.md](PROMPT.md) — a
> portable, self-contained prompt you can paste into Claude Code,
> OpenAI Codex CLI, Google Antigravity, or any coding agent that has
> AWS CLI access. It produces the same Markdown report using only
> `aws ce` calls.

> Looking for a richer version that also walks every region and attributes
> S3 log-sink buckets to their producing service? See the sibling repo
> [bronto-aws-savings-report](https://github.com/BrontoStephen/bronto-aws-savings-report).

## What it counts

| Source | Where the cost shows up | How GB is derived |
| --- | --- | --- |
| CloudWatch Logs — customer | `Amazon CloudWatch` / `DataProcessing-Bytes` | Direct (GB) |
| CloudWatch Logs — vended | `Amazon CloudWatch` / `VendedLog-Bytes` | Direct (GB) — ALB, CloudFront, Route 53, etc. |
| CloudWatch Logs Insights (search) | `Amazon CloudWatch` / `DataScanned-Bytes` | Direct (GB) — counted toward Bronto search |
| CloudWatch custom metrics | `Amazon CloudWatch` / `MetricMonitorUsage` | metric-months × bytes/metric-month |
| CloudWatch Metric Streams | `Amazon CloudWatch` / `MetricStreamUsage` | updates × bytes/update (the Bronto-equivalent for metrics forwarding) |
| X-Ray | `AWS X-Ray` / `TracesRecorded` | traces × bytes/trace |
| Managed Prometheus | `Amazon Managed Service for Prometheus` / samples | samples × bytes/sample |
| CloudTrail data events | `AWS CloudTrail` / `PaidEventsRecorded` | events × bytes/event |

The bytes-per-unit assumptions are all configurable in
[config/bronto_pricing.yaml](config/bronto_pricing.yaml).

### Bronto pricing model

- **Ingest:** $0.10/GB, uniform across logs/metrics/traces.
- **Retention:** 12 months included on all plans.
- **Search:** included on every plan, with overage at $1/TB:

  | Plan | Monthly fee | Ingest | Search | Notes |
  | --- | --- | --- | --- | --- |
  | Starter | $25 | 1 TB included | 20 TB included | email support, no SSO |
  | Pro | $500 | 5 TB included | 500 TB included | SSO + RBAC, priority support |
  | Enterprise | custom | $0.10/GB pay-as-you-go | $1/TB pay-as-you-go | dedicated Slack + TAM, SLA, HIPAA/SOC2, extendable retention |

  Worked Enterprise example from Bronto: 1 GB ingest + 300 GB search =
  $0.10 + $0.30 = **$0.40**.

  The projector picks the cheapest plan total (ingest + search) for the
  headline projection and shows all three side by side.

OpenSearch, AMG, alarms, dashboards, retention storage, and API request
charges appear in the AWS total but have no Bronto counterpart — see the
caveats in the generated report for why.

## Install

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run

Default (last 90 days, all org accounts visible to your CE permissions):

```sh
python aws_obs_cost.py
```

Custom window / specific account:

```sh
python aws_obs_cost.py --start 2026-04-01 --end 2026-05-01 --accounts 123456789012
```

Non-default profile:

```sh
python aws_obs_cost.py --profile mycompany-payer
```

Report lands at `report.md` (`--out` to override).

## IAM permissions

Run with credentials that have:

```
ce:GetCostAndUsage           # required
organizations:ListAccounts   # optional — used only to label accounts by name
```

Cost Explorer must be enabled in the management account (one-time toggle
in the Billing console). When called from the payer, CE already
aggregates data across the whole organization — you do **not** need
cross-account roles.

## Configuring the rate card

Edit [config/bronto_pricing.yaml](config/bronto_pricing.yaml):

```yaml
ingest_per_gb_usd: 0.10           # Bronto's published rate
included_retention_months: 12     # bundled with all plans
bytes_per_metric_month: 3_440_000 # 1-min resolution
bytes_per_xray_trace: 2048
bytes_per_prometheus_sample: 8
bytes_per_cloudtrail_event: 1536
```

Plans (Starter $25 / 1 TB, Pro $500 / 5 TB, Enterprise per-GB) are
defined in the same file. The projector picks the cheapest one for your
volume and shows all three side-by-side in the report.

## Output

A single Markdown report with:

- **Executive summary** — AWS total, Bronto projection, savings %
- **Spend by service** — CloudWatch Logs / Metrics / Alarms / Dashboards /
  Insights, X-Ray, AMP, AMG, OpenSearch, CloudTrail
- **Spend by account**
- **Bronto projection detail** across all three plans
- **Caveats** explaining why the savings number is what it is

## Caveats up front

1. **The S3 line is shown separately, not in observability totals.** Most
   S3 spend in any account is product data, not log sinks. This tool
   intentionally does not try to attribute S3 to log sources — that's
   what the sibling repo does.
2. **Bronto charges per ingested GB only.** AWS charges alarm-monitor
   hours, dashboard fees, API request tiers, retention storage, and
   OpenSearch EBS — none of which Bronto bills. Those show up as AWS
   spend with no Bronto counterpart, which is why projected savings can
   look large.
3. **OpenSearch contributes to AWS spend but not Bronto GB.** Cost
   Explorer doesn't expose OpenSearch ingest bytes; only instance hours
   and EBS storage. To get an OpenSearch GB estimate, use the sibling
   repo with `--probe`.
