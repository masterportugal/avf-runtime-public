from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from pydantic import BaseModel, Field


class PromotedOpportunity(BaseModel):
    candidate_key: str
    platform: str
    friction_family: str
    evidence_count: int = Field(ge=1)
    evidence_urls: list[str] = Field(min_length=1)
    evidence_signal_keys: list[str] = Field(min_length=1)
    representative_titles: list[str] = Field(min_length=1)
    status: str
    owner_customer_recruitment_required: bool = False
    required_out_of_pocket_usd: float = 0
    research_disposition: str | None = None
    research_rationale: str | None = None


class DeterministicOpportunityPromoter:
    """Convert recurrent public signals into a research queue without paid AI.

    This is intentionally conservative. A single issue never becomes a build candidate.
    At least two distinct issue signals in the same platform/friction family are required
    before the queue marks the theme READY_FOR_RESEARCH.
    """

    FAMILIES = {
        "extension_compatibility": ("compatib", "extension", "plugin", "crash", "break", "regression"),
        "extensibility_gap": ("filter", "hook", "event", "extensib", "selector", "api", "entrypoint"),
        "automation_workflow": ("scheduled", "bulk", "automation", "continuous integration", "ci ", "workflow"),
        "security_tooling": ("vulnerab", "security", "scanner", "fsrt", "authorization"),
        "ai_developer_workflow": ("agents.md", " agent", "ai ", "skills"),
        "data_reporting_export": ("report", "export", "csv", "column", "template"),
    }

    def classify(self, title: str) -> str:
        t = f" {title.lower()} "
        scores = {family: sum(1 for term in terms if term in t) for family, terms in self.FAMILIES.items()}
        best = max(scores, key=lambda k: (scores[k], k))
        return best if scores[best] else "other_friction"

    @staticmethod
    def _key(platform: str, family: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", platform.lower()).strip("-")
        return f"{slug}-{family}"

    @staticmethod
    def _load_decisions(decisions_path: Path | None) -> dict[str, dict]:
        if decisions_path is None or not decisions_path.exists():
            return {}
        payload = json.loads(decisions_path.read_text(encoding="utf-8"))
        return {str(x["candidate_key"]): x for x in payload.get("decisions", [])}

    def promote_rows(self, rows: list[dict], *, decisions: dict[str, dict] | None = None) -> list[PromotedOpportunity]:
        decisions = decisions or {}
        groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in rows:
            platform = str(row.get("platform") or "Unknown")
            family = self.classify(str(row.get("title") or ""))
            groups[(platform, family)].append(row)
        out = []
        for (platform, family), group in sorted(groups.items()):
            by_signal = {}
            for row in group:
                by_signal[str(row.get("signal_key"))] = row
            distinct = list(by_signal.values())
            urls = sorted({str(r.get("url")) for r in distinct if r.get("url")})
            keys = sorted(by_signal)
            titles = [str(r.get("title") or "") for r in distinct[:5]]
            candidate_key = self._key(platform, family)
            status = "READY_FOR_RESEARCH" if len(distinct) >= 2 else "NEEDS_MORE_EVIDENCE"
            disposition = None
            rationale = None
            decision = decisions.get(candidate_key)
            if decision and decision.get("suppress_until_new_evidence", True):
                reviewed = set(map(str, decision.get("reviewed_signal_keys", [])))
                current = set(keys)
                if current.issubset(reviewed):
                    status = "SUPPRESSED_RESEARCH_DECISION"
                    disposition = str(decision.get("disposition") or "SUPPRESSED")
                    rationale = str(decision.get("rationale") or "Previously researched with no new evidence")
            out.append(PromotedOpportunity(
                candidate_key=candidate_key,
                platform=platform,
                friction_family=family,
                evidence_count=len(distinct),
                evidence_urls=urls or ["about:blank"],
                evidence_signal_keys=keys,
                representative_titles=titles or [family],
                status=status,
                research_disposition=disposition,
                research_rationale=rationale,
            ))
        return out

    def build_from_ndjson(self, signal_path: Path, *, decisions_path: Path | None = None) -> list[PromotedOpportunity]:
        rows=[]
        if signal_path.exists():
            for line in signal_path.read_text(encoding="utf-8").splitlines():
                if line.strip(): rows.append(json.loads(line))
        return self.promote_rows(rows, decisions=self._load_decisions(decisions_path))

    def write_queue(self, signal_path: Path, queue_path: Path, *, decisions_path: Path | None = None) -> list[PromotedOpportunity]:
        opportunities=self.build_from_ndjson(signal_path, decisions_path=decisions_path)
        queue_path.parent.mkdir(parents=True,exist_ok=True)
        payload={
            "format_version":1,
            "opportunities":[x.model_dump(mode="json") for x in opportunities],
        }
        text=json.dumps(payload,ensure_ascii=False,indent=2,sort_keys=True)+"\n"
        if not queue_path.exists() or queue_path.read_text(encoding="utf-8") != text:
            queue_path.write_text(text,encoding="utf-8")
        return opportunities
