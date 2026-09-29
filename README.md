# AVF Public Runtime

Minimal recurrent discovery runtime for Autonomous Venture Factory.

- Standard GitHub-hosted Ubuntu runner only
- Scheduled every 5 minutes (GitHub's minimum supported schedule interval)
- Source-level discovery cadence remains controlled independently by each discovery spec
- Public-source data only
- No secrets, payment credentials, product source, or financial data
- Durable state is committed only when it changes
- Manual and scheduled changes converge on latest `main`; conflicting stale writes fail closed
