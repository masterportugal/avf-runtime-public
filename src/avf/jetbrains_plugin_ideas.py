from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, Field

from .http_transport import HttpTransportPolicy, SafeHttpFetcher


class JetBrainsPluginIdeaSignal(BaseModel):
    issue_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    created_ms: int = Field(ge=0)
    updated_ms: int = Field(ge=0)

    @property
    def signal_key(self) -> str:
        return f"youtrack:{self.issue_id}"

    @property
    def fingerprint(self) -> str:
        blob=json.dumps({"issue_id":self.issue_id,"summary":self.summary,"updated_ms":self.updated_ms},sort_keys=True,separators=(",",":"))
        return hashlib.sha256(blob.encode()).hexdigest()


class JetBrainsPluginIdeasResult(BaseModel):
    status: str
    fetched: bool = False
    signal_count: int = 0
    signals: list[JetBrainsPluginIdeaSignal] = Field(default_factory=list)
    next_eligible_at: str | None = None


class JetBrainsPluginIdeasCollector:
    HOST="youtrack.jetbrains.com"
    CADENCE=timedelta(hours=24)
    QUERY="tag: plugin-welcome"

    @staticmethod
    def _load_last_fetch(state_path: Path) -> datetime | None:
        if not state_path.exists(): return None
        try:
            raw=json.loads(state_path.read_text(encoding="utf-8"))
            value=str(raw.get("last_fetch_at") or "")
            if not value: return None
            dt=datetime.fromisoformat(value.replace("Z","+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    @staticmethod
    def _parse(payload: object) -> list[JetBrainsPluginIdeaSignal]:
        if not isinstance(payload,list): return []
        out=[]
        for row in payload:
            if not isinstance(row,dict): continue
            issue_id=str(row.get("idReadable") or "").strip()
            summary=str(row.get("summary") or "").strip()
            if not issue_id or not summary: continue
            tags=row.get("tags") or []
            names={str(x.get("name") or "").strip() for x in tags if isinstance(x,dict)}
            if "plugin-welcome" not in names: continue
            out.append(JetBrainsPluginIdeaSignal(
                issue_id=issue_id,summary=summary,
                created_ms=max(0,int(row.get("created") or 0)),
                updated_ms=max(0,int(row.get("updated") or 0)),
            ))
        return sorted(out,key=lambda x:(-x.updated_ms,x.issue_id))

    def collect(self, *, state_path: Path, now: datetime | None=None, fetcher=None) -> JetBrainsPluginIdeasResult:
        now=now or datetime.now(timezone.utc)
        last=self._load_last_fetch(state_path)
        if last is not None and now < last+self.CADENCE:
            return JetBrainsPluginIdeasResult(status="SKIPPED_NOT_DUE",next_eligible_at=(last+self.CADENCE).isoformat())
        query=quote(self.QUERY,safe="")
        url=(f"https://{self.HOST}/api/issues?query={query}"
             "&fields=idReadable,summary,created,updated,tags(name)&$top=100")
        if fetcher is None:
            fetcher=SafeHttpFetcher(HttpTransportPolicy(
                allowed_hosts=[self.HOST],timeout_seconds=20,max_response_bytes=2_000_000,
                max_attempts=2,backoff_seconds=0.25,
            ),extra_headers={"Accept":"application/json","User-Agent":"avf-public-runtime/1.0"})
        response=fetcher(url)
        if int(response.status_code)!=200:
            return JetBrainsPluginIdeasResult(status=f"HTTP_{int(response.status_code)}",fetched=True)
        try: payload=json.loads(response.body)
        except json.JSONDecodeError: return JetBrainsPluginIdeasResult(status="INVALID_JSON",fetched=True)
        signals=self._parse(payload)
        state_path.parent.mkdir(parents=True,exist_ok=True)
        state_path.write_text(json.dumps({"last_fetch_at":now.isoformat(),"status":"PASS","signal_count":len(signals)},indent=2,sort_keys=True)+"\n",encoding="utf-8")
        return JetBrainsPluginIdeasResult(status="PASS",fetched=True,signal_count=len(signals),signals=signals,next_eligible_at=(now+self.CADENCE).isoformat())


def append_jetbrains_plugin_idea_signals(signal_path: Path, result: JetBrainsPluginIdeasResult, *, observed_at: datetime | None=None) -> int:
    if result.status!="PASS" or not result.signals: return 0
    observed_at=observed_at or datetime.now(timezone.utc)
    existing=set()
    if signal_path.exists():
        for line in signal_path.read_text(encoding="utf-8").splitlines():
            if not line.strip(): continue
            try:
                row=json.loads(line); existing.add((row.get("signal_key"),row.get("fingerprint")))
            except Exception: continue
    rows=[]
    for s in result.signals:
        key=(s.signal_key,s.fingerprint)
        if key in existing: continue
        rows.append({
            "spec_id":"jetbrains-youtrack-plugin-welcome",
            "platform":"JetBrains",
            "signal_key":s.signal_key,
            "fingerprint":s.fingerprint,
            "title":s.summary,
            "url":f"https://youtrack.jetbrains.com/issue/{s.issue_id}",
            "updated_at":s.updated_ms,
            "created_at":s.created_ms,
            "observed_at":observed_at.isoformat(),
            "plugin_welcome":True,
        })
    if rows:
        signal_path.parent.mkdir(parents=True,exist_ok=True)
        with signal_path.open("a",encoding="utf-8") as fh:
            for row in rows: fh.write(json.dumps(row,ensure_ascii=False,sort_keys=True)+"\n")
    return len(rows)


class JetBrainsPluginIdeaCandidate(BaseModel):
    candidate_key: str
    platform: str = "JetBrains Marketplace"
    issue_id: str
    title: str
    updated_ms: int = Field(ge=0)
    status: str = "READY_FOR_GAP_VALIDATION"
    owner_customer_recruitment_required: bool = False
    required_out_of_pocket_usd: float = 0


class JetBrainsPluginIdeaQueueBuilder:
    @staticmethod
    def _slug(value:str)->str:
        return re.sub(r"[^a-z0-9]+","-",value.lower()).strip("-")[:70] or "idea"

    def build_from_signal_rows(self, rows:list[dict]) -> list[JetBrainsPluginIdeaCandidate]:
        latest={}
        for row in rows:
            if row.get("spec_id")!="jetbrains-youtrack-plugin-welcome" or not row.get("plugin_welcome"): continue
            issue_id=str(row.get("signal_key") or "").replace("youtrack:","",1)
            title=str(row.get("title") or "").strip()
            if not issue_id or not title: continue
            cur=latest.get(issue_id)
            if cur is None or int(row.get("updated_at") or 0) >= int(cur.get("updated_at") or 0): latest[issue_id]=row
        out=[]
        for issue_id,row in latest.items():
            title=str(row["title"])
            out.append(JetBrainsPluginIdeaCandidate(
                candidate_key=f"jetbrains-plugin-idea-{issue_id.lower()}-{self._slug(title)}",
                issue_id=issue_id,title=title,updated_ms=max(0,int(row.get("updated_at") or 0)),
            ))
        return sorted(out,key=lambda x:(-x.updated_ms,x.issue_id))

    def build_from_ndjson(self, signal_path:Path)->list[JetBrainsPluginIdeaCandidate]:
        rows=[]
        if signal_path.exists():
            for line in signal_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try: rows.append(json.loads(line))
                    except json.JSONDecodeError: continue
        return self.build_from_signal_rows(rows)

    def write_queue(self, signal_path:Path, queue_path:Path)->list[JetBrainsPluginIdeaCandidate]:
        candidates=self.build_from_ndjson(signal_path)
        queue_path.parent.mkdir(parents=True,exist_ok=True)
        text=json.dumps({"format_version":1,"opportunities":[x.model_dump(mode="json") for x in candidates]},ensure_ascii=False,indent=2,sort_keys=True)+"\n"
        if not queue_path.exists() or queue_path.read_text(encoding="utf-8")!=text: queue_path.write_text(text,encoding="utf-8")
        return candidates
