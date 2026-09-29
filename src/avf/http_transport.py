from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from email.message import Message
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, Field, field_validator

from .collectors import FetchPayload


class HttpTransportPolicy(BaseModel):
    """Network boundary for collector URL refreshes.

    Defaults are intentionally restrictive: HTTPS only, explicit host allowlist,
    bounded responses, bounded retries, and no credentials embedded in URLs.
    """

    allowed_hosts: list[str] = Field(min_length=1)
    timeout_seconds: float = Field(default=10.0, ge=0.1, le=60.0)
    max_response_bytes: int = Field(default=2_000_000, ge=1_024, le=20_000_000)
    max_attempts: int = Field(default=2, ge=1, le=5)
    backoff_seconds: float = Field(default=0.25, ge=0.0, le=5.0)
    user_agent: str = "AutonomousVentureFactory/0.13 (+evidence-collector)"
    allow_http: bool = False
    retry_statuses: set[int] = Field(default_factory=lambda: {429, 500, 502, 503, 504})

    @field_validator("allowed_hosts")
    @classmethod
    def normalize_hosts(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for raw in value:
            host = raw.strip().lower().rstrip(".")
            if not host or "://" in host or "/" in host:
                raise ValueError("allowed_hosts must contain hostnames, not URLs")
            if host.startswith("*."):
                host = "*." + host[2:].encode("idna").decode("ascii")
            else:
                host = host.encode("idna").decode("ascii")
            out.append(host)
        return sorted(set(out))


@dataclass(frozen=True)
class ResolvedTarget:
    url: str
    hostname: str
    addresses: tuple[str, ...]


class HttpTransportError(RuntimeError):
    pass


class _PolicyRedirectHandler(HTTPRedirectHandler):
    def __init__(self, validator: Callable[[str], ResolvedTarget]):
        super().__init__()
        self._validator = validator

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        self._validator(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SafeHttpFetcher:
    """Callable HTTP transport compatible with CollectorRunner.

    Unit tests may inject ``open_func`` and ``resolver``. A successful local test
    proves policy/orchestration semantics only; it is not a live-network certification.
    """

    def __init__(
        self,
        policy: HttpTransportPolicy,
        *,
        resolver: Callable[[str, int], Iterable[str]] | None = None,
        open_func: Callable[[Request, float], object] | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.policy = policy
        self._extra_headers = dict(extra_headers or {})
        self._resolver = resolver or self._default_resolver
        self._sleep = sleep_func
        if open_func is None:
            opener = build_opener(_PolicyRedirectHandler(self._validate_url))
            self._open = lambda req, timeout: opener.open(req, timeout=timeout)
        else:
            self._open = open_func

    @staticmethod
    def _default_resolver(hostname: str, port: int) -> Iterable[str]:
        rows = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        return sorted({row[4][0] for row in rows})

    def _host_allowed(self, hostname: str) -> bool:
        host = hostname.lower().rstrip(".")
        for allowed in self.policy.allowed_hosts:
            if allowed.startswith("*."):
                suffix = allowed[1:]  # '.example.com'
                if host.endswith(suffix) and host != suffix[1:]:
                    return True
            elif host == allowed:
                return True
        return False

    def _validate_url(self, url: str) -> ResolvedTarget:
        parsed = urlsplit(url)
        if parsed.scheme not in ({"https", "http"} if self.policy.allow_http else {"https"}):
            raise HttpTransportError("URL scheme is not allowed by policy")
        if parsed.username or parsed.password:
            raise HttpTransportError("Credentials embedded in URLs are not allowed")
        if not parsed.hostname:
            raise HttpTransportError("URL hostname is required")
        hostname = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if not self._host_allowed(hostname):
            raise HttpTransportError(f"Host not allowlisted: {hostname}")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.scheme == "https" and port != 443:
            raise HttpTransportError("Non-default HTTPS ports are not allowed")
        if parsed.scheme == "http" and port != 80:
            raise HttpTransportError("Non-default HTTP ports are not allowed")
        try:
            addresses = tuple(self._resolver(hostname, port))
        except Exception as exc:
            raise HttpTransportError(f"DNS resolution failed for {hostname}: {exc}") from exc
        if not addresses:
            raise HttpTransportError(f"DNS resolution returned no addresses for {hostname}")
        for raw in addresses:
            try:
                ip = ipaddress.ip_address(raw)
            except ValueError as exc:
                raise HttpTransportError(f"Resolver returned invalid IP: {raw}") from exc
            if not ip.is_global:
                raise HttpTransportError(f"Non-public address rejected for {hostname}: {raw}")
        return ResolvedTarget(url=url, hostname=hostname, addresses=addresses)

    @staticmethod
    def _headers_content_type(headers: object) -> tuple[str, str]:
        if isinstance(headers, Message):
            content_type = headers.get_content_type() or "application/octet-stream"
            charset = headers.get_content_charset() or "utf-8"
            return content_type, charset
        get = getattr(headers, "get", None)
        raw = get("Content-Type", "application/octet-stream") if callable(get) else "application/octet-stream"
        parts = [part.strip() for part in str(raw).split(";")]
        content_type = parts[0] or "application/octet-stream"
        charset = "utf-8"
        for part in parts[1:]:
            if part.lower().startswith("charset="):
                charset = part.split("=", 1)[1].strip('"\'') or "utf-8"
        return content_type, charset

    def _read_response(self, response: object, status_code: int | None = None) -> FetchPayload:
        status = status_code or int(getattr(response, "status", getattr(response, "code", 200)))
        headers = getattr(response, "headers", {})
        raw = response.read(self.policy.max_response_bytes + 1)
        if len(raw) > self.policy.max_response_bytes:
            raise HttpTransportError("Response exceeded max_response_bytes")
        content_type, charset = self._headers_content_type(headers)
        try:
            body = raw.decode(charset, errors="replace")
        except LookupError:
            body = raw.decode("utf-8", errors="replace")
        return FetchPayload(status_code=status, body=body, content_type=content_type)

    def __call__(self, url: str) -> FetchPayload:
        self._validate_url(url)
        last_error: Exception | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            req = Request(url, headers={"User-Agent": self.policy.user_agent, "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.1"})
            try:
                response = self._open(req, self.policy.timeout_seconds)
                payload = self._read_response(response)
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                if payload.status_code in self.policy.retry_statuses and attempt < self.policy.max_attempts:
                    self._sleep(self.policy.backoff_seconds * attempt)
                    continue
                return payload
            except HTTPError as exc:
                last_error = exc
                if exc.code in self.policy.retry_statuses and attempt < self.policy.max_attempts:
                    self._sleep(self.policy.backoff_seconds * attempt)
                    continue
                try:
                    return self._read_response(exc, status_code=exc.code)
                finally:
                    exc.close()
            except (URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < self.policy.max_attempts:
                    self._sleep(self.policy.backoff_seconds * attempt)
                    continue
                break
            except HttpTransportError:
                raise
        raise HttpTransportError(f"HTTP fetch failed after {self.policy.max_attempts} attempts: {last_error}")
