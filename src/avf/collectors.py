from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Callable
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, HttpUrl, field_validator


class CollectorMode(StrEnum):
    URL_REFRESH = "URL_REFRESH"
    SEARCH_DISCOVERY = "SEARCH_DISCOVERY"


class SourcePolicyMetadata(BaseModel):
    """Source-specific operating metadata for collector deployment.

    This registry records what has been reviewed; it does not itself grant permission to
    crawl. ``live_fetch_allowed`` stays false until source terms/robots/auth/rate-limit
    requirements are explicitly reviewed for the deployment context.
    """

    policy_id: str
    host_patterns: list[str] = Field(min_length=1)
    auth_mode: str = "PUBLIC_READ_ONLY"
    terms_status: str = "REVIEW_REQUIRED"
    robots_status: str = "NOT_VERIFIED"
    rate_limit_policy: str = "LOW_FREQUENCY_ONLY"
    live_fetch_allowed: bool = False
    reviewed_at: str | None = None
    notes: str | None = None

    @field_validator("host_patterns")
    @classmethod
    def normalize_host_patterns(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for raw in value:
            host = raw.strip().lower().rstrip(".")
            if not host or "://" in host or "/" in host:
                raise ValueError("host_patterns must contain hostnames, not URLs")
            out.append(host)
        return sorted(set(out))

    def allows_target_host(self, target: str) -> bool:
        host = (urlsplit(target).hostname or "").lower().rstrip(".")
        for pattern in self.host_patterns:
            if pattern.startswith("*."):
                suffix = pattern[2:]
                if host == suffix or host.endswith("." + suffix):
                    return True
            elif host == pattern:
                return True
        return False


class CollectorSpec(BaseModel):
    collector_id: str
    platform: str
    mode: CollectorMode
    target: HttpUrl | str
    source_kind: str
    candidate_keys: list[str] = Field(default_factory=list)
    cadence_hours: int = Field(gt=0)
    enabled: bool = True
    notes: str | None = None
    source_policy_id: str | None = None


class FetchPayload(BaseModel):
    status_code: int
    body: str
    content_type: str = "text/plain"


class CollectorRunRecord(BaseModel):
    collector_id: str
    target: str
    attempted_at: datetime
    success: bool
    status_code: int | None = None
    content_hash: str | None = None
    body_length: int = 0
    error: str | None = None


class CollectorRunner:
    """Minimal collector runtime with an injected transport.

    The core intentionally does not hard-code Internet access. A deployment adapter must
    inject a fetcher that enforces source-specific ToS, authentication, rate limits and
    robots policy. Unit tests use a fake transport, so passing tests prove orchestration
    semantics rather than live-network certification.
    """

    def due(
        self,
        specs: list[CollectorSpec],
        last_success: dict[str, datetime],
        *,
        now: datetime | None = None,
    ) -> list[CollectorSpec]:
        now = now or datetime.now(timezone.utc)
        due: list[CollectorSpec] = []
        for spec in specs:
            if not spec.enabled:
                continue
            last = last_success.get(spec.collector_id)
            if last is None or now - last >= timedelta(hours=spec.cadence_hours):
                due.append(spec)
        return due

    def run_one(
        self,
        spec: CollectorSpec,
        fetcher: Callable[[str], FetchPayload],
        *,
        now: datetime | None = None,
        source_policies: dict[str, SourcePolicyMetadata] | None = None,
    ) -> CollectorRunRecord:
        from hashlib import sha256

        attempted = now or datetime.now(timezone.utc)
        target = str(spec.target)
        try:
            # Runtime fail-closed enforcement for deployment-linked collectors.
            # Legacy/unit-test specs without source_policy_id remain injectable fixtures.
            if spec.source_policy_id:
                if not source_policies:
                    raise RuntimeError("source policy registry required for policy-linked collector")
                policy = source_policies.get(spec.source_policy_id)
                if policy is None:
                    raise RuntimeError(f"unknown source policy: {spec.source_policy_id}")
                if not policy.allows_target_host(target):
                    raise RuntimeError("collector target is outside source policy host patterns")
                if not policy.live_fetch_allowed:
                    raise RuntimeError("source policy is fail-closed for live fetch")
            payload = fetcher(target)
            ok = 200 <= payload.status_code < 300 and bool(payload.body.strip())
            return CollectorRunRecord(
                collector_id=spec.collector_id,
                target=target,
                attempted_at=attempted,
                success=ok,
                status_code=payload.status_code,
                content_hash=sha256(payload.body.encode("utf-8")).hexdigest() if payload.body else None,
                body_length=len(payload.body),
                error=None if ok else "non-success status or empty body",
            )
        except Exception as exc:  # transport boundary: preserve failure, do not invent data
            return CollectorRunRecord(
                collector_id=spec.collector_id,
                target=target,
                attempted_at=attempted,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )
