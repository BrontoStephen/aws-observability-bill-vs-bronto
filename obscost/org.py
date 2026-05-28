"""AWS Organization discovery (for account names only).

We do NOT assume-role into member accounts — all spend data comes from a
single Cost Explorer call at the payer level. This module exists purely
so the report can show friendly account names alongside spend.

If `organizations:ListAccounts` is denied (e.g., not running as the payer),
we fall back gracefully and the account-name column shows '?'.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Account:
    id: str
    name: str


class OrgClient:
    def __init__(self, profile: Optional[str]):
        self.session = boto3.Session(profile_name=profile)
        self._sts = self.session.client("sts")
        self._mgmt_account_id: Optional[str] = None

    @property
    def mgmt_account_id(self) -> str:
        if self._mgmt_account_id is None:
            self._mgmt_account_id = self._sts.get_caller_identity()["Account"]
        return self._mgmt_account_id

    def list_accounts(self) -> list[Account]:
        """List active org accounts. Returns [(current account)] on AccessDenied."""
        org = self.session.client("organizations")
        try:
            accounts: list[Account] = []
            paginator = org.get_paginator("list_accounts")
            for page in paginator.paginate():
                for a in page["Accounts"]:
                    if a.get("Status") != "ACTIVE":
                        continue
                    accounts.append(Account(id=a["Id"], name=a.get("Name", "")))
            return accounts
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in ("AWSOrganizationsNotInUseException", "AccessDeniedException"):
                log.warning(
                    "Organizations API unavailable (%s). Account names will show as '?'.",
                    code,
                )
                return [Account(id=self.mgmt_account_id, name="(current)")]
            raise
