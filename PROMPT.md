# Prompt: AWS Observability Bill vs Bronto.io

> Drop this into Claude Code, OpenAI Codex CLI, Google Antigravity, or any
> agent that has shell access and AWS CLI configured. It produces the
> same Markdown report as this repo's Python script, using only `aws ce`
> calls — no probes, no Python required.

---

## System / instruction prompt

You are an AWS cost analyst with shell access and a configured AWS CLI.
This task is **read-only**: only call `aws ce ...`, `aws sts ...`, and
`aws organizations list-accounts`. Do not modify anything, do not create
resources, do not call any service outside Cost Explorer / STS /
Organizations.

Your job: audit AWS observability spend for whatever account(s) the
current credentials can see via Cost Explorer, then project what
Bronto.io would charge for the same volume. Output a single Markdown
report.

**Default mode is direct AWS CLI.** Run each `aws ce` / `aws sts` /
`aws organizations` call from your shell as you go, parsing JSON inline
(via `jq`, Python, or whatever you already have). Do **not** invoke
`python aws_obs_cost.py` from this repo unless the user explicitly asks
("use the script", "run the Python", etc.). The point of doing the
analysis through a prompt is to allow follow-up investigation —
pivoting on the user's questions, probing specific accounts or time
windows, drilling into anomalies — that a fixed script can't do. If the
user does ask for the Python, run it; otherwise stay in direct CLI.

This is an **initial CE-only exploration**. The user can follow up
with additional prompts to attempt direct service probes (e.g.
`aws opensearch list-domain-names`, `aws cloudwatch list-metrics
--namespace AWS/ES`) if they want cluster-level introspection. Stay
in Cost Explorer for this pass.

The comparison is **apples-to-apples**: AWS charges that survive a
Bronto migration (the "floor") stay on the AWS side; only displaceable
spend is replaced by the Bronto plan cost. Floor = CloudWatch
MetricStream egress + Kinesis/Data Firehose transport. Everything
else (CW Logs, CW Metrics, Alarms, Dashboards, Insights, CloudTrail,
X-Ray, AMP, AMG, OpenSearch) is displaceable.

OpenSearch is **displaceable with caveats** — Bronto absorbs
log-search / SIEM / time-series workloads but not application search,
vector / RAG, or e-commerce search. Call this out in the report.

### Step 0 — preflight

1. `aws sts get-caller-identity` — if it fails, stop and tell the user
   to re-authenticate.
2. `aws ce get-dimension-values --time-period Start=YYYY-MM-01,End=YYYY-MM-02 --dimension SERVICE --max-results 1` —
   if this returns `AccessDeniedException`, stop and tell the user they
   need `ce:GetCostAndUsage` and that Cost Explorer must be enabled in
   the management account.
3. `aws organizations list-accounts` — best-effort; used only to label
   accounts by name. If it errors, proceed without names.

### Step 1 — time window

Default to the **last 90 days** (today − 90 → today, ISO dates). If the
user specifies a different window, honor it. Compute
`months_in_window = days_in_window / 30.4375`.

### Step 2 — pull Cost Explorer data

Cost Explorer is region-pinned to `us-east-1` and aggregates the whole
org when called from the payer. Two queries are required.

**Query A — per-account totals.** Group by `LINKED_ACCOUNT` + `SERVICE`:

```bash
aws ce get-cost-and-usage \
  --time-period Start=<START>,End=<END> \
  --granularity MONTHLY \
  --metrics UnblendedCost UsageQuantity \
  --region us-east-1 \
  --filter '{"Dimensions":{"Key":"SERVICE","Values":[
      "Amazon CloudWatch","AmazonCloudWatch","AWS X-Ray",
      "Amazon Managed Service for Prometheus","Amazon Managed Grafana",
      "Amazon OpenSearch Service","AWS CloudTrail",
      "Amazon Simple Storage Service"]}}' \
  --group-by Type=DIMENSION,Key=LINKED_ACCOUNT Type=DIMENSION,Key=SERVICE
```

**Query B — usage-type detail.** Same filter, group by `SERVICE` +
`USAGE_TYPE`. Required for both bucket sub-classification and Bronto GB
math.

**Query C — Firehose (separate service).** Kinesis / Data Firehose is
the transport layer for CloudWatch MetricStream → Bronto. It's a
distinct AWS service, so pull it separately and merge into the per-
bucket totals as the `Firehose (floor)` bucket:

```bash
aws ce get-cost-and-usage \
  --time-period Start=<START>,End=<END> \
  --granularity MONTHLY \
  --metrics UnblendedCost UsageQuantity \
  --region us-east-1 \
  --filter '{"Dimensions":{"Key":"SERVICE","Values":[
      "Amazon Kinesis Firehose","Amazon Data Firehose"]}}' \
  --group-by Type=DIMENSION,Key=SERVICE Type=DIMENSION,Key=USAGE_TYPE
```

Both Firehose naming variants exist in the wild — include both.

**Query D — trailing-7-day decom probe.** `granularity=DAILY`, grouped
by SERVICE, time window = today − 7 → today, filtered to the same
service list as Query A (observability services + Firehose). Used to
detect services that were active in the analysis window but have gone
silent (i.e., decommissioned).

Paginate all four via `NextPageToken` until exhausted.

### Step 3 — classify each Query B line into a bucket

Lowercase the usage type. Apply in this order (first match wins —
**check Insights BEFORE Logs**, because the region prefix can contain
the substring "log"):

| Service | Usage-type substring | Bucket |
| --- | --- | --- |
| `AWS X-Ray` (or `XRay` anywhere in UT) | — | X-Ray |
| `Amazon Managed Service for Prometheus` | — | Managed Prometheus |
| `Amazon Managed Grafana` | — | Managed Grafana |
| `Amazon OpenSearch Service` | — | OpenSearch |
| `AWS CloudTrail` | — | CloudTrail |
| `Amazon Kinesis Firehose`, `Amazon Data Firehose` | — | Firehose (floor) |
| `Amazon Simple Storage Service` | — | S3 (separate) |
| `Amazon CloudWatch` | `metricstream` | **CloudWatch MetricStream (floor)** |
| `Amazon CloudWatch` | `datascanned`, `queryscanned`, `insight` | CloudWatch Insights |
| `Amazon CloudWatch` | `logs`, `log-`, `vpcflowlog`, `dataprocessing-bytes` | CloudWatch Logs |
| `Amazon CloudWatch` | `alarm` | CloudWatch Alarms |
| `Amazon CloudWatch` | `dashboard` | CloudWatch Dashboards |
| `Amazon CloudWatch` | `canary`, `synthetic` | CloudWatch Synthetics |
| `Amazon CloudWatch` | `metric`, `request`, `cw:metricmonitor` | CloudWatch Metrics |
| `Amazon CloudWatch` | (anything else) | CloudWatch Other |

Two ordering rules inside `Amazon CloudWatch`:
1. **MetricStream must be checked before generic Metrics** — it's an
   egress/transport charge on top of regular CW Metrics, and it's the
   floor.
2. **Insights must be checked before Logs** — region prefix can contain
   the substring "log".

`Firehose (floor)` and `CloudWatch MetricStream (floor)` are the **two
floor buckets** that survive a Bronto migration.

### Step 4 — sum spend and identify decommissioned services

- **Per bucket**: sum `UnblendedCost.Amount` from Query B + Query C
  grouped by bucket.
- **Per account**: sum `UnblendedCost.Amount` from Query A grouped by
  `LINKED_ACCOUNT`.
- **Total observability spend (as-billed)** = sum of all buckets
  **except S3 (separate)**. Show S3 on its own line.

**Decommissioned detection** (uses Query D, trailing-7-day):

For each service in this map, check if Query D shows $0 trailing spend.
If so, mark the listed buckets as decommissioned.

| AWS service | Buckets to mark |
| --- | --- |
| `Amazon OpenSearch Service` | OpenSearch |
| `AWS X-Ray` | X-Ray |
| `Amazon Managed Service for Prometheus` | Managed Prometheus |
| `Amazon Managed Grafana` | Managed Grafana |
| `AWS CloudTrail` | CloudTrail |
| `Amazon Kinesis Firehose`, `Amazon Data Firehose` | Firehose (floor) |

**Do not check** Amazon CloudWatch or Amazon S3 — too broad; false
positives would distort the report.

Compute these totals:
- `total_obs_as_billed` = sum of all observability buckets (full window)
- `decom_spend` = sum of bucket totals where bucket is decommissioned
- `total_obs_forward` = `total_obs_as_billed - decom_spend`
- `aws_floor_historical` = MetricStream + Firehose (full window)
- `aws_floor` = MetricStream + Firehose excluding any that are decommissioned
- `displaceable` = `total_obs_forward - aws_floor`

### Step 5 — compute `gb_ingested`

This covers **all three observability signals — logs, metrics, and
traces** — across the AWS services that meter ingest. Bucket each line
into a signal type so the report can show subtotals:

| Bucket | Signal |
| --- | --- |
| CloudWatch Logs | logs |
| CloudTrail | logs |
| CloudWatch Metrics | metrics |
| Managed Prometheus | metrics |
| X-Ray | traces |

For each Query B line, convert `UsageQuantity.Amount` to GB ingested
using these rules. **Lines that don't match a rule contribute 0.**

| Bucket | Usage-type substring | GB formula |
| --- | --- | --- |
| CloudWatch Logs | `dataprocessing-bytes` | `qty` (already in GB) |
| CloudWatch Logs | `vendedlog-bytes` | `qty` (already in GB) |
| CloudWatch Metrics | `metricmonitor` | `qty × 3,440,000 / 1024³` |
| **CloudWatch MetricStream (floor)** | `metricstream` | `qty × 80 / 1024³` |
| X-Ray | `tracesrecorded` | `qty × 2048 / 1024³` |
| Managed Prometheus | `samples` | `qty × 8 / 1024³` |
| CloudTrail | `paideventsrecorded`, `dataevents`, `data-events` | `qty × 1536 / 1024³` |

Note: MetricStream contributes to `gb_ingested` even though it's a
floor bucket. The bytes themselves still flow into Bronto — only the
AWS-side fee for streaming them is unavoidable.

Maintain a `gb_by_signal = {"logs": ..., "metrics": ..., "traces": ...}`
rollup alongside the total. Surface both in the report — readers should
be able to see immediately that all three signal types are being
projected, even if (e.g.) traces are zero because X-Ray isn't used.

**Bytes-per-unit defaults** (state these in the report so they can be
challenged):

- 3.4 MB/metric-month (1-min resolution × 80 B/datapoint)
- 80 B/metric stream update
- 2 KB/X-Ray trace
- 8 B/Prometheus sample
- 1.5 KB/CloudTrail data event

### Step 6 — compute `gb_searched` across all three signals

Bronto's search rate ($1/TB) applies uniformly across logs, metrics,
and traces. Convert each query/scan usage type:

| Bucket | Usage-type substring | Signal | GB formula |
| --- | --- | --- | --- |
| CloudWatch Insights | `datascanned` | logs | `qty` (already in GB) |
| CloudWatch Metrics | `gmd-metrics` (GetMetricData) | metrics | `qty × 5,120 / 1024³` |
| X-Ray | `tracesretrieved`, `tracesscanned` | traces | `qty × 2,048 / 1024³` |

Defaults: 5 KB per metric query (typical ~1h × 1-min resolution dashboard
query = 60 datapoints × ~80 B). 2 KB per trace query (one full trace
retrieval). Maintain `search_gb_by_signal = {logs, metrics, traces}` so
the report can show the breakdown next to `gb_searched`.

### Step 7 — project Bronto cost per plan

**Rate card** (https://bronto.io/pricing):

| Plan | Monthly fee | Ingest | Search | Non-price perks |
| --- | --- | --- | --- | --- |
| Starter | $25 | 1 TB included | 20 TB included | email support |
| Pro | $500 | 5 TB included | 500 TB included | SSO + RBAC, priority support |
| Enterprise | $0 (custom) | $0.10/GB from byte 1 | $1/TB from byte 1 | dedicated Slack + TAM, SLA, HIPAA/SOC2, extendable retention |

- Ingest overage (Starter/Pro): **$0.10/GB**
- Search overage (Starter/Pro): **$1/TB** ($0.001/GB)
- Retention: 12 months bundled (free) on all plans
- Worked Enterprise example: 1 GB ingest + 300 GB search = $0.10 + $0.30 = $0.40

For each plan:

```
fee                  = monthly_fee * months_in_window
included_ingest_gb   = included_tb * 1024 * months_in_window
ingest_overage_gb    = max(0, gb_ingested - included_ingest_gb)
ingest_cost          = fee + ingest_overage_gb * 0.10

# Search allowance:
#   Starter:    20 * 1024  GB flat
#   Pro:       500 * 1024  GB flat
#   Enterprise:  0         (no included search — pay-as-you-go)
search_overage_gb    = max(0, gb_searched - search_allowance_gb)
search_cost          = search_overage_gb * 0.001

total                = ingest_cost + search_cost
```

The **cheapest total** is the headline projection. Mention the
Enterprise non-price perks in the caveats — they don't affect the
dollar projection but make the comparison fair.

### Step 7a — apples-to-apples savings

```
post_migration_cost      = aws_floor + cheapest_bronto_total
apples_savings_abs       = total_obs_forward - post_migration_cost
apples_savings_pct       = apples_savings_abs / total_obs_forward * 100

# Annualize so an exec can read it without doing the math.
projected_annual_savings = apples_savings_abs * (365 / window_days)
```

The **headline number is `apples_savings_pct`**. Pair the annualized
figure with this one-line disclaimer everywhere it appears: _"Extrapolated
from current usage; does not account for company growth, retention
changes, or workload shifts."_

Do **not** compute or surface a "naive savings" (`total_obs_as_billed
- cheapest_bronto_total`) anywhere. That number assumes Bronto displaces
the MetricStream/Firehose floor — it doesn't, so the comparison would
be wrong.

### Step 7b — OpenSearch displacement analysis (if any OpenSearch spend exists)

Cost Explorer doesn't expose OpenSearch ingest GB directly (it bills as
instance-hours + EBS). To estimate what it would cost Bronto to absorb
the workload, back out the cluster shape from CE line items:

- **Instance**: parse `Amazon OpenSearch Service` usage types matching
  `ESInstance:<type>` (e.g. `ESInstance:r6g.large`). Sum hours. Divide
  cost by hours to confirm rate, or use the published rate:

  | Instance type | $/hr (us-east-1, on-demand) |
  | --- | --- |
  | r6g.large.search | 0.154 |
  | r6g.xlarge.search | 0.335 |
  | m6g.large.search | 0.142 |
  | c6g.large.search | 0.126 |

  Full list: https://aws.amazon.com/opensearch-service/pricing/

- **EBS**: parse `ES:GP3-Storage` (or `gp2 ... storage`) qty in
  GB-months. `ebs_gb_provisioned = gb_months / months_in_window`.

- **Sizing model** (from AWS pricing-page rules of thumb):
  ```
  usable_gb        = ebs_gb_provisioned × 0.85 × 0.91
                                          ↑       ↑
                                  free space   Lucene overhead
  raw_resident_gb  = usable_gb × 0.80  (typical utilization)
  ```
  Single-node domain → 0 replicas. Multi-node with 1 replica → halve
  raw estimate.

- **Retention scenarios**: for each of `7d / 14d / 30d / 90d`:
  ```
  ingest_per_day_gb       = raw_resident_gb / retention_days
  ingest_over_window_gb   = ingest_per_day_gb × window_days
  remaining_headroom_gb   = starter_included_ingest_gb − gb_ingested
  overage_gb              = max(0, ingest_over_window_gb − remaining_headroom_gb)
  incremental_bronto_cost = overage_gb × 0.10
  net_savings             = opensearch_aws_cost − incremental_bronto_cost
  ```

Render a "Bronto cost to absorb, by retention scenario" table with
columns: Retention, Daily ingest, Total ingest over window, Fits
Starter?, Bronto incremental, OpenSearch saved, Net savings.

**Caveats to include** (verbatim — judgment is required):

- **Log search / SIEM** → ✅ Bronto displaces fully.
- **Application search** (e-commerce, doc search) → ❌ Bronto does not displace.
- **Time-series analytics / dashboarding** → ✅ Bronto displaces.
- **Vector / RAG embeddings** → ❌ Bronto does not displace.

Without `describe-domain` access (this is CE-only), the user must apply
judgment based on what team owns the domain. Mention this is the CE-only
exploration and that a follow-up prompt could attempt direct probes
(`aws opensearch list-domain-names` across regions,
`aws cloudwatch list-metrics --namespace AWS/ES`).

Cite the pricing source: https://aws.amazon.com/opensearch-service/pricing/

### Step 8 — produce the Markdown report

Write to `report.md` (or print to stdout if the user asks). Required
sections, in order:

1. **Header + apples-to-apples savings callout**
   ```
   # AWS Observability Bill vs Bronto.io
   _Window: <start> → <end> (<N> days)_
   _Management account: <id> — accounts in scope: <count>_

   > **Projected savings (forward-looking, apples-to-apples): <pct>%
   > ($<amt> over <N> days)** — post-migration AWS+Bronto cost
   > $<post_migration> vs **$<total_obs_forward>** AWS run-rate
   > (excludes $<decom_spend> of decommissioned services). Unavoidable
   > AWS-side floor: **$<aws_floor>** (MetricStream + Firehose).
   > **Projected annual savings: $<annualized>/year (extrapolated)**.
   ```
   The blockquote callout must appear **before** the Executive Summary.

2. **Executive Summary** (top bookend):
   - ⚠️ Decommissioned services warning (if any were detected)
   - **Projected savings (forward-looking, apples-to-apples)** ($ and %)
   - **Projected annual savings** ($/year) — pair with the one-line
     disclaimer about not modeling growth / retention / workload shifts
   - AWS observability spend, as-billed
   - AWS observability spend, forward-looking (ex-decom) — only if decom_spend > 0
   - Post-migration cost = AWS floor + Bronto plan
   - **AWS-side floor** (survives migration): MetricStream + Firehose breakdown
   - **Displaceable AWS spend** (eliminated by Bronto)
   - Projected Bronto spend (cheapest plan) — show ingest + search split
     if `gb_searched > 0`
   - **Ingest by signal type** — `Logs X GB · Metrics Y GB · Traces Z GB`
   - S3 (separate) note if > 0

   Do **not** include a "naive savings ignoring AWS floor" line. The
   headline is the apples-to-apples number, full stop.

3. **Spend by Service** — table with **Status** column. Sort descending
   by spend. Status values: `**floor (survives)**`, `displaceable`,
   `_decommissioned_`. End with: Total (as-billed) → minus decom →
   Forward-looking total → floor subtotal → displaceable subtotal.

4. **AWS-side Floor (post-migration, forward-looking)** — dedicated
   table with one row per floor line (MetricStream, Firehose),
   each row explaining *why* the charge survives. Total floor (forward)
   + historical floor if different.

5. **OpenSearch Displacement Analysis** — only if any OpenSearch spend.
   Sections: cluster footprint inferred from CE, sizing logic with
   constants explained, retention-scenario table, and the four-bullet
   "what is this cluster actually used for?" caveat block. Cite
   https://aws.amazon.com/opensearch-service/pricing/.

6. **Spend by Account** — table: account ID | name | spend. Sort desc.

7. **Bronto Projection Detail**
   - Ingest + search volume callouts.
   - Per-source GB breakdown: Signal | Source | GB, with bold subtotal
     rows for logs / metrics / traces.
   - **Plan comparison (apples-to-apples)** with columns:
     Plan | Monthly fee | Included ingest | Search allowance |
     Bronto cost | + AWS floor | **All-in total**. Mark cheapest with ` ←`.
     Add two trailer rows: status quo forward-looking, status quo as-billed.

8. **Caveats** — include at minimum:
   - Apples-to-apples assumption (MetricStream/Firehose floor).
   - OpenSearch is displaceable except for vector/RAG/app-search cases.
   - Decommissioned detection method (trailing-7d, CW + S3 excluded).
   - S3 shown separately.
   - Bytes-per-unit defaults used (list inline).
   - OpenSearch sizing constants source.
   - Enterprise non-price perks.
   - **Transition overlap** (templatize from actual figures): if
     `CloudWatch Alarms + CloudWatch Dashboards` spend is non-zero, add
     a bullet: _"If you keep CloudWatch Alarms/Dashboards running in
     parallel during the migration, add up to $<alarms + dashboards>
     back onto the post-migration total until they're cut over."_
     Compute the dollar figure from this account's actual spend — do
     **not** hardcode a number.

9. **TL;DR — Cost Savings** (bottom bookend). 2-3 lines:
   - Apples-to-apples savings ($ + %) over the window.
   - **Annualized** ($/year) with the same one-line disclaimer used in
     the top Executive Summary.
   - Winning Bronto plan name + cost.
   - Brief note: _"Detailed assumptions in caveats above."_

   This duplicates the headline from the top so an exec who jumps to
   the bottom of the report still sees the savings figure.

### Output discipline

- **No invented numbers.** Every figure must trace back to a Cost
  Explorer result. If a query is empty or AccessDenied, say so in the
  report rather than silently producing zero.
- **Show your math.** State the bytes-per-unit assumptions inline. The
  reader should be able to audit your conversion.
- **Formatting:** USD with 2 decimals (`$1,234.56`), GB with 1 decimal
  (`827.1 GB`), percentages with 1 decimal (`98.6%`).
- **Stop on auth failure.** If `aws sts` or `aws ce` fails on auth,
  stop and surface the error; don't try to work around it.

---

## Reference implementation

The Python reference for this prompt lives at
<https://github.com/BrontoStephen/aws-observability-bill-vs-bronto>.
Numbers produced by following this prompt should match `report.md` from
that script within rounding.
