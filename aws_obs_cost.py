#!/usr/bin/env python3
"""AWS Observability Bill vs Bronto.io.

A minimal CLI that pulls observability spend from the AWS Cost Explorer
API and compares it to what Bronto.io would charge for the same ingested
volume at $0.10/GB. Cost Explorer only — no probes, no per-account API
walks, no bucket scanning.

Usage:
  python aws_obs_cost.py [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                         [--profile PROFILE] [--accounts a,b,c]
                         [--bronto-config PATH] [--out PATH]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from obscost import bronto, cost_explorer, org, report  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    today = date.today()
    default_start = (today - timedelta(days=90)).isoformat()
    default_end = today.isoformat()
    default_pricing = str(HERE / "config" / "bronto_pricing.yaml")

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", default=default_start, help="Start date YYYY-MM-DD (default: 90 days ago)")
    p.add_argument("--end", default=default_end, help="End date YYYY-MM-DD (default: today)")
    p.add_argument("--profile", default=os.environ.get("AWS_PROFILE"), help="AWS profile (defaults to AWS_PROFILE)")
    p.add_argument("--accounts", default="", help="Comma-separated account IDs to include (default: all)")
    p.add_argument("--bronto-config", default=default_pricing, help="Path to bronto_pricing.yaml")
    p.add_argument("--out", default="report.md", help="Output Markdown file")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("aws_obs_cost")

    pricing = bronto.BrontoPricing.load(args.bronto_config)
    log.info("Loaded Bronto pricing from %s (ingest=$%.2f/GB)",
             args.bronto_config, pricing.ingest_per_gb_usd)

    oc = org.OrgClient(profile=args.profile)
    log.info("Management account: %s", oc.mgmt_account_id)
    accounts = oc.list_accounts()
    log.info("Account name lookup: %d account(s) resolved", len(accounts))

    filter_ids = [a.strip() for a in args.accounts.split(",") if a.strip()] or None
    cost = cost_explorer.fetch_costs(
        session=oc.session,
        start=args.start,
        end=args.end,
        account_ids=filter_ids,
    )
    log.info("Cost Explorer returned %d usage lines across %d account(s)",
             len(cost.lines), len(cost.accounts_seen))

    projection = bronto.project(cost, pricing)
    log.info("Bronto projection: $%.2f (plan: %s) on %.1f GB ingested",
             projection.cheapest_cost, projection.cheapest_plan, projection.gb_ingested)

    md = report.render(
        report=cost,
        accounts=accounts,
        projection=projection,
        pricing=pricing,
        mgmt_account_id=oc.mgmt_account_id,
    )
    Path(args.out).write_text(md)
    print(f"Wrote report to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
