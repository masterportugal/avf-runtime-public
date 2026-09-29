from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from pydantic import BaseModel, Field


class MarketplaceGapCandidate(BaseModel):
    candidate_key: str
    platform: str
    search_keyword: str
    total_zero_result_searches: int = Field(ge=1)
    observations: int = Field(ge=1)
    filters_seen: list[str] = Field(default_factory=list)
    status: str = "READY_FOR_GAP_VALIDATION"
    owner_customer_recruitment_required: bool = False
    required_out_of_pocket_usd: float = 0


class MarketplaceGapQueueBuilder:
    @staticmethod
    def _slug(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:80] or "unknown"

    def build_from_signal_rows(self, rows: list[dict]) -> list[MarketplaceGapCandidate]:
        groups: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            if row.get("spec_id") != "atlassian-marketplace-zero-search":
                continue
            title=str(row.get("title") or "")
            prefix="Zero-result marketplace search: "
            keyword=title[len(prefix):].strip() if title.startswith(prefix) else title.strip()
            if not keyword:
                continue
            groups[keyword.lower()].append({**row, "_keyword": keyword})
        out=[]
        for _, group in groups.items():
            keyword=group[0]["_keyword"]
            count=sum(max(0, int(x.get("marketplace_search_count") or 0)) for x in group)
            if count <= 0:
                continue
            filters=set()
            for x in group:
                for field in ("product_filter","hosting_filter","category_filter","other_filter"):
                    val=x.get(field)
                    if val:
                        filters.add(f"{field}:{val}")
            out.append(MarketplaceGapCandidate(
                candidate_key=f"atlassian-zero-search-{self._slug(keyword)}",
                platform="Atlassian Marketplace",
                search_keyword=keyword,
                total_zero_result_searches=count,
                observations=len(group),
                filters_seen=sorted(filters),
            ))
        return sorted(out, key=lambda x: (-x.total_zero_result_searches, x.search_keyword.lower()))

    def build_from_ndjson(self, signal_path: Path) -> list[MarketplaceGapCandidate]:
        rows=[]
        if signal_path.exists():
            for line in signal_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try: rows.append(json.loads(line))
                    except json.JSONDecodeError: continue
        return self.build_from_signal_rows(rows)

    def write_queue(self, signal_path: Path, queue_path: Path) -> list[MarketplaceGapCandidate]:
        candidates=self.build_from_ndjson(signal_path)
        queue_path.parent.mkdir(parents=True,exist_ok=True)
        payload={"format_version":1,"opportunities":[x.model_dump(mode="json") for x in candidates]}
        text=json.dumps(payload,ensure_ascii=False,indent=2,sort_keys=True)+"\n"
        if not queue_path.exists() or queue_path.read_text(encoding="utf-8") != text:
            queue_path.write_text(text,encoding="utf-8")
        return candidates
