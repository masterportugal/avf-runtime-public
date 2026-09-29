from __future__ import annotations

from datetime import datetime, timezone
from pydantic import BaseModel

from .github_discovery import GitHubDiscoveryState, GitHubIssueDiscoverySpec


class SchedulerManifestEntry(BaseModel):
    spec_id: str
    due: bool
    executable: bool
    source_policy_id: str
    reason: str
    last_run_at: str | None = None
    cadence_hours: int


class RecurrentDiscoveryScheduler:
    """Fail-closed scheduler planner; network fetching remains an injected runtime action."""

    def plan(self, specs: list[GitHubIssueDiscoverySpec], states: dict[str, GitHubDiscoveryState], policies: list[dict], *, now: datetime | None = None) -> list[SchedulerManifestEntry]:
        now=now or datetime.now(timezone.utc)
        policy_by_id={p['policy_id']:p for p in policies}
        out=[]
        for spec in specs:
            st=states.get(spec.spec_id)
            last=st.last_run_at if st else None
            due=True
            if last:
                dt=datetime.fromisoformat(last.replace('Z','+00:00'))
                due=(now-dt).total_seconds() >= spec.cadence_hours*3600
            policy=policy_by_id.get(spec.source_policy_id)
            if not spec.enabled:
                executable=False; reason='SPEC_DISABLED'
            elif not policy:
                executable=False; reason='SOURCE_POLICY_MISSING'
            elif not bool(policy.get('live_fetch_allowed')):
                executable=False; reason='SOURCE_POLICY_LIVE_FETCH_BLOCKED'
            elif not due:
                executable=False; reason='NOT_DUE'
            else:
                executable=True; reason='DUE_AND_SOURCE_ALLOWED'
            out.append(SchedulerManifestEntry(spec_id=spec.spec_id,due=due,executable=executable,source_policy_id=spec.source_policy_id,reason=reason,last_run_at=last,cadence_hours=spec.cadence_hours))
        return sorted(out,key=lambda x:x.spec_id)
