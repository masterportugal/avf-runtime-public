from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, Field

from .http_transport import HttpTransportPolicy, SafeHttpFetcher


class AtlassianZeroSearchSignal(BaseModel):
    search_keyword: str
    count: int = Field(default=0, ge=0)
    product_filter: str | None = None
    hosting_filter: str | None = None
    category_filter: str | None = None
    other_filter: str | None = None

    @property
    def signal_key(self) -> str:
        material = "|".join([
            self.search_keyword.strip().lower(),
            self.product_filter or "",
            self.hosting_filter or "",
            self.category_filter or "",
            self.other_filter or "",
        ])
        return "atlassian-zero-search:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json")
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class AtlassianZeroSearchResult(BaseModel):
    status: str
    fetched: bool = False
    signal_count: int = 0
    signals: list[AtlassianZeroSearchSignal] = Field(default_factory=list)
    next_eligible_at: str | None = None


class AtlassianMarketplaceZeroSearchCollector:
    HOST = "api.atlassian.com"
    CADENCE = timedelta(hours=24)

    @staticmethod
    def _credentials(env: dict[str, str] | None = None) -> tuple[str, str, str]:
        source = env if env is not None else os.environ
        return tuple((source.get(name) or "").strip() for name in (
            "AVF_ATLASSIAN_EMAIL",
            "AVF_ATLASSIAN_API_TOKEN",
            "AVF_ATLASSIAN_DEVELOPER_ID",
        ))  # type: ignore[return-value]

    @classmethod
    def credential_status(cls, env: dict[str, str] | None = None) -> str:
        values = cls._credentials(env)
        present = sum(bool(x) for x in values)
        if present == 0:
            return "DISABLED_NO_CREDENTIALS"
        if present != 3:
            return "BLOCKED_PARTIAL_CREDENTIALS"
        return "READY"

    @staticmethod
    def _load_last_fetch(state_path: Path) -> datetime | None:
        if not state_path.exists():
            return None
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            value = str(raw.get("last_fetch_at") or "")
            if not value:
                return None
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    @staticmethod
    def _parse(payload: object) -> list[AtlassianZeroSearchSignal]:
        if isinstance(payload, dict):
            rows = payload.get("details") or payload.get("data") or payload.get("values") or []
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []
        out=[]
        for row in rows:
            if not isinstance(row, dict):
                continue
            keyword=str(row.get("searchKeyword") or row.get("keyword") or "").strip()
            if not keyword:
                continue
            out.append(AtlassianZeroSearchSignal(
                search_keyword=keyword,
                count=max(0, int(row.get("count") or 0)),
                product_filter=(str(row.get("productFilter")).strip() if row.get("productFilter") is not None else None),
                hosting_filter=(str(row.get("hostingFilter")).strip() if row.get("hostingFilter") is not None else None),
                category_filter=(str(row.get("categoryFilter")).strip() if row.get("categoryFilter") is not None else None),
                other_filter=(str(row.get("otherFilter")).strip() if row.get("otherFilter") is not None else None),
            ))
        return sorted(out, key=lambda x: (-x.count, x.search_keyword.lower()))

    def collect(self, *, state_path: Path, now: datetime | None = None, env: dict[str, str] | None = None, fetcher=None) -> AtlassianZeroSearchResult:
        now = now or datetime.now(timezone.utc)
        status = self.credential_status(env)
        if status != "READY":
            return AtlassianZeroSearchResult(status=status)
        last = self._load_last_fetch(state_path)
        if last is not None and now < last + self.CADENCE:
            return AtlassianZeroSearchResult(
                status="SKIPPED_NOT_DUE",
                fetched=False,
                next_eligible_at=(last + self.CADENCE).isoformat(),
            )
        email, token, developer_id = self._credentials(env)
        endpoint=(
            "https://api.atlassian.com/marketplace/rest/3/reporting/developer-space/"
            + quote(developer_id, safe="")
            + "/zero-search-results-keywords/source/marketplace"
        )
        if fetcher is None:
            auth = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
            fetcher = SafeHttpFetcher(
                HttpTransportPolicy(
                    allowed_hosts=[self.HOST], timeout_seconds=15,
                    max_response_bytes=2_000_000, max_attempts=2, backoff_seconds=0.25,
                ),
                extra_headers={
                    "Authorization": f"Basic {auth}",
                    "Accept": "application/json",
                },
            )
        response=fetcher(endpoint)
        if int(response.status_code) != 200:
            return AtlassianZeroSearchResult(status=f"HTTP_{int(response.status_code)}", fetched=True)
        try:
            payload=json.loads(response.body)
        except json.JSONDecodeError:
            return AtlassianZeroSearchResult(status="INVALID_JSON", fetched=True)
        signals=self._parse(payload)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "last_fetch_at": now.isoformat(),
            "status": "PASS",
            "signal_count": len(signals),
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return AtlassianZeroSearchResult(
            status="PASS", fetched=True, signal_count=len(signals), signals=signals,
            next_eligible_at=(now + self.CADENCE).isoformat(),
        )


def append_atlassian_signals(signal_path: Path, result: AtlassianZeroSearchResult, *, observed_at: datetime | None = None) -> int:
    if result.status != "PASS" or not result.signals:
        return 0
    observed_at = observed_at or datetime.now(timezone.utc)
    existing=set()
    if signal_path.exists():
        for line in signal_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row=json.loads(line)
                existing.add((row.get("signal_key"), row.get("fingerprint")))
            except Exception:
                continue
    rows=[]
    for signal in result.signals:
        key=(signal.signal_key, signal.fingerprint)
        if key in existing:
            continue
        rows.append({
            "spec_id":"atlassian-marketplace-zero-search",
            "platform":"Atlassian Marketplace",
            "friction_family":"marketplace_unserved_search",
            "signal_key":signal.signal_key,
            "fingerprint":signal.fingerprint,
            "title":f"Zero-result marketplace search: {signal.search_keyword}",
            "url":"https://marketplace.atlassian.com/",
            "updated_at":observed_at.isoformat(),
            "observed_at":observed_at.isoformat(),
            "marketplace_search_count":signal.count,
            "product_filter":signal.product_filter,
            "hosting_filter":signal.hosting_filter,
            "category_filter":signal.category_filter,
            "other_filter":signal.other_filter,
        })
    if rows:
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        with signal_path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(rows)
