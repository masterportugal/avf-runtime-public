from __future__ import annotations
import argparse, json
from datetime import datetime, timezone
from pathlib import Path
from .github_discovery import GitHubDiscoveryStateStore, GitHubIssueDiscoverySpec, PersistentGitHubIssueDiscoveryRunner
from .http_transport import HttpTransportPolicy, SafeHttpFetcher
from .scheduler import RecurrentDiscoveryScheduler

def _load_json(path:Path): return json.loads(path.read_text(encoding="utf-8"))

def run_discovery_cycle(*,root:Path,state_dir:Path,now:datetime|None=None,fetcher=None)->dict:
    now=now or datetime.now(timezone.utc)
    specs=[GitHubIssueDiscoverySpec.model_validate(x) for x in _load_json(root/"data/github_discovery_specs.v0.25.json")]
    policies=_load_json(root/"data/source_policies.v0.25.json")
    state_dir.mkdir(parents=True,exist_ok=True)
    store=GitHubDiscoveryStateStore(state_dir/"github_discovery_state.json")
    manifest=RecurrentDiscoveryScheduler().plan(specs,store.load_all(),policies,now=now)
    live_policy=next((p for p in policies if p.get("policy_id")=="host-api-github-com"),None)
    if not live_policy or not live_policy.get("live_fetch_allowed"): raise RuntimeError("GitHub source policy is not live-enabled")
    if fetcher is None:
        fetcher=SafeHttpFetcher(HttpTransportPolicy(allowed_hosts=["api.github.com"],timeout_seconds=15,max_response_bytes=2_000_000,max_attempts=2,backoff_seconds=0.25))
    runner=PersistentGitHubIssueDiscoveryRunner(store); changed_rows=[]; executed=0; observed=0; by_id={x.spec_id:x for x in specs}
    for entry in manifest:
        if not entry.executable: continue
        spec=by_id[entry.spec_id]; payload_raw=fetcher(spec.api_url)
        if int(payload_raw.status_code)!=200: raise RuntimeError(f"GitHub discovery fetch failed for {spec.spec_id}: HTTP {payload_raw.status_code}")
        try: payload=json.loads(payload_raw.body)
        except json.JSONDecodeError as exc: raise RuntimeError(f"GitHub discovery returned invalid JSON for {spec.spec_id}") from exc
        result=runner.run(spec,payload,run_at=now); executed+=1; observed+=len(result.observed)
        for signal in result.new_or_changed:
            changed_rows.append({"spec_id":spec.spec_id,"platform":spec.platform,"signal_key":signal.signal_key,"fingerprint":signal.fingerprint,"title":signal.title,"url":signal.url,"updated_at":signal.updated_at,"observed_at":now.isoformat()})
    signal_path=state_dir/"signals.ndjson"; existing=set()
    if signal_path.exists():
        for line in signal_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row=json.loads(line); existing.add((row.get("signal_key"),row.get("fingerprint")))
    new_rows=[r for r in changed_rows if (r["signal_key"],r["fingerprint"]) not in existing]
    if new_rows:
        with signal_path.open("a",encoding="utf-8") as fh:
            for row in new_rows: fh.write(json.dumps(row,ensure_ascii=False,sort_keys=True)+"\n")
    receipt={"run_id":now.isoformat(),"status":"PASS","executed_specs":executed,"observed_signals":observed,"new_or_changed_signals":len(new_rows),"owner_out_of_pocket_usd":0,"source_policy":"host-api-github-com","state_path":"runtime_state/github_discovery_state.json","signals_path":"runtime_state/signals.ndjson"}
    receipt_path=state_dir/"last_run.json"
    if executed>0 or not receipt_path.exists(): receipt_path.write_text(json.dumps(receipt,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    return receipt

def main(argv:list[str]|None=None)->int:
    parser=argparse.ArgumentParser(); parser.add_argument("--root",default="."); parser.add_argument("--state-dir",default="runtime_state"); args=parser.parse_args(argv)
    receipt=run_discovery_cycle(root=Path(args.root).resolve(),state_dir=Path(args.state_dir).resolve()); print(json.dumps(receipt,sort_keys=True)); return 0
if __name__=="__main__": raise SystemExit(main())
