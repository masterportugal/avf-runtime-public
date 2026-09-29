from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote_plus
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class GitHubIssueDiscoverySpec(BaseModel):
    spec_id: str
    repository: str
    query: str
    platform: str
    cadence_hours: int = Field(default=24, gt=0)
    cadence_minutes: int | None = Field(default=None, gt=0)

    @property
    def effective_cadence_minutes(self) -> int:
        return self.cadence_minutes if self.cadence_minutes is not None else self.cadence_hours * 60
    max_results: int = Field(default=25, ge=1, le=100)
    enabled: bool = True
    source_policy_id: str = "host-api-github-com"

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        if value.count("/") != 1 or any(not part.strip() for part in value.split("/")):
            raise ValueError("repository must be owner/name")
        return value.strip()

    @property
    def api_url(self) -> str:
        q = f"repo:{self.repository} is:issue {self.query}".strip()
        return f"https://api.github.com/search/issues?q={quote_plus(q)}&per_page={self.max_results}&sort=updated&order=desc"


class GitHubIssueSignal(BaseModel):
    repository: str
    issue_number: int = Field(gt=0)
    title: str
    url: str
    state: str | None = None
    updated_at: str | None = None
    comments: int = Field(default=0, ge=0)
    labels: list[str] = Field(default_factory=list)
    body_excerpt: str = ""

    @property
    def signal_key(self) -> str:
        return f"{self.repository}#{self.issue_number}"

    @property
    def fingerprint(self) -> str:
        payload = {
            "signal_key": self.signal_key,
            "title": self.title.strip(),
            "state": self.state,
            "updated_at": self.updated_at,
            "comments": self.comments,
            "labels": sorted(self.labels),
            "body_excerpt": self.body_excerpt.strip(),
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class GitHubDiscoveryState(BaseModel):
    spec_id: str
    last_run_at: str | None = None
    seen_signal_keys: list[str] = Field(default_factory=list)
    last_fingerprints: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class GitHubDiscoveryRun:
    spec_id: str
    run_at: str
    observed: tuple[GitHubIssueSignal, ...]
    new_or_changed: tuple[GitHubIssueSignal, ...]
    state: GitHubDiscoveryState


class GitHubIssueDiscoveryEngine:
    """Normalize GitHub REST issue-search payloads into reproducible, deduplicated signals."""

    def normalize(self, spec: GitHubIssueDiscoverySpec, payload: dict) -> list[GitHubIssueSignal]:
        out: list[GitHubIssueSignal] = []
        for item in payload.get("items", []):
            number = int(item["number"])
            labels = []
            for label in item.get("labels") or []:
                labels.append(str(label.get("name")) if isinstance(label, dict) else str(label))
            body = (item.get("body") or "").strip().replace("\x00", "")
            out.append(
                GitHubIssueSignal(
                    repository=spec.repository,
                    issue_number=number,
                    title=(item.get("title") or "").strip(),
                    url=item.get("html_url") or f"https://github.com/{spec.repository}/issues/{number}",
                    state=item.get("state"),
                    updated_at=item.get("updated_at"),
                    comments=int(item.get("comments") or 0),
                    labels=labels,
                    body_excerpt=body[:1200],
                )
            )
        by_key: dict[str, GitHubIssueSignal] = {}
        for signal in out:
            by_key[signal.signal_key] = signal
        return sorted(by_key.values(), key=lambda s: s.signal_key)

    def run(self, spec: GitHubIssueDiscoverySpec, payload: dict, previous: GitHubDiscoveryState | None = None, *, run_at: datetime | None = None) -> GitHubDiscoveryRun:
        observed = self.normalize(spec, payload)
        previous = previous or GitHubDiscoveryState(spec_id=spec.spec_id)
        if previous.spec_id != spec.spec_id:
            raise ValueError("state/spec mismatch")
        new_or_changed = tuple(s for s in observed if previous.last_fingerprints.get(s.signal_key) != s.fingerprint)
        fingerprints = dict(previous.last_fingerprints)
        fingerprints.update({s.signal_key: s.fingerprint for s in observed})
        seen = sorted(set(previous.seen_signal_keys) | {s.signal_key for s in observed})
        ts = (run_at or datetime.now(timezone.utc)).isoformat()
        state = GitHubDiscoveryState(spec_id=spec.spec_id,last_run_at=ts,seen_signal_keys=seen,last_fingerprints=fingerprints)
        return GitHubDiscoveryRun(spec.spec_id, ts, tuple(observed), new_or_changed, state)

class GitHubDiscoveryStateStore:
    FORMAT_VERSION = 1
    def __init__(self, path: str | Path):
        self.path = Path(path)
    def load_all(self) -> dict[str, GitHubDiscoveryState]:
        if not self.path.exists(): return {}
        try: raw=json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError,json.JSONDecodeError) as exc: raise ValueError(f"invalid persistent GitHub discovery state: {exc}") from exc
        if raw.get("format_version") != self.FORMAT_VERSION: raise ValueError("unsupported persistent GitHub discovery state format")
        rows=raw.get("states")
        if not isinstance(rows,list): raise ValueError("persistent GitHub discovery state must contain a states list")
        out={}
        for row in rows:
            state=GitHubDiscoveryState.model_validate(row)
            if state.spec_id in out: raise ValueError(f"duplicate persistent GitHub discovery state: {state.spec_id}")
            out[state.spec_id]=state
        return out
    def get(self, spec_id: str) -> GitHubDiscoveryState | None: return self.load_all().get(spec_id)
    def save_all(self, states: dict[str, GitHubDiscoveryState]) -> None:
        import tempfile
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload={"format_version":self.FORMAT_VERSION,"states":[states[k].model_dump(mode="json") for k in sorted(states)]}
        text=json.dumps(payload,ensure_ascii=False,indent=2,sort_keys=True)+"\n"
        fd,tmp_name=tempfile.mkstemp(prefix=f".{self.path.name}.",suffix=".tmp",dir=self.path.parent)
        try:
            with os.fdopen(fd,"w",encoding="utf-8") as fh:
                fh.write(text); fh.flush(); os.fsync(fh.fileno())
            os.replace(tmp_name,self.path)
        except Exception:
            try: os.unlink(tmp_name)
            except FileNotFoundError: pass
            raise
    def put(self,state:GitHubDiscoveryState)->None:
        states=self.load_all(); states[state.spec_id]=state; self.save_all(states)

class PersistentGitHubIssueDiscoveryRunner:
    def __init__(self, store: GitHubDiscoveryStateStore, engine: GitHubIssueDiscoveryEngine | None = None):
        self.store=store; self.engine=engine or GitHubIssueDiscoveryEngine()
    def run(self,spec:GitHubIssueDiscoverySpec,payload:dict,*,run_at:datetime|None=None)->GitHubDiscoveryRun:
        previous=self.store.get(spec.spec_id); result=self.engine.run(spec,payload,previous,run_at=run_at); self.store.put(result.state); return result

@dataclass(frozen=True)
class GitHubDiscoveryRuntimeConfig:
    state_path: Path
    @classmethod
    def from_env(cls, env: dict[str,str]|None=None)->"GitHubDiscoveryRuntimeConfig":
        env=os.environ if env is None else env
        direct=(env.get("AVF_GITHUB_DISCOVERY_STATE_PATH") or "").strip()
        runtime_dir=(env.get("AVF_RUNTIME_DATA_DIR") or "").strip()
        if direct: path=Path(direct).expanduser()
        elif runtime_dir: path=Path(runtime_dir).expanduser()/"github_discovery_state.json"
        else: raise ValueError("durable GitHub discovery state path is not configured")
        if not path.is_absolute(): raise ValueError("durable GitHub discovery state path must be absolute")
        return cls(state_path=path)
    def build_runner(self)->PersistentGitHubIssueDiscoveryRunner:
        return PersistentGitHubIssueDiscoveryRunner(GitHubDiscoveryStateStore(self.state_path))
