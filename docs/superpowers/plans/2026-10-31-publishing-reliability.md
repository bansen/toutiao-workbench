# Publishing reliability implementation plan

> Execute in this session with tests before changes. User explicitly authorized the full repair; preserve existing local data and publishing constraints.

**Goal:** produce supported drafts before their slots and submit them reliably without weakening factual checks or duplicating submissions.

**Architecture:** separate evidence review from generation and time-critical submission. Store an application-generated approval tied to exact draft and source fingerprints; freshly re-read sources at submission, reuse approval only when fingerprints, policy, age and deterministic checks match. Resolve generator concerns through independent evidence review, never by deleting them blindly.

**Stack:** existing FastAPI, SQLite, Playwright and compatible model service.

- [x] Reproduce false rejection of attributed forecasts/plans/company reports, missing-evidence resolutions, false title counts, catalog discovery, and unchanged-source repeated QA.
- [x] Add `workbench/draft_review.py`: validated issue resolutions, source/draft fingerprints and approval freshness checks. Cover edits, source changes, policy expiry and untrusted/expired sources.
- [x] Update `ai_tools.py`: independently adjudicate generator concerns with quotes from actual draft/source; unsupported assertions still fail. Read actual leaf documents, keep catalog pages as discovery only; gather links outside article navigation without treating them as evidence.
- [x] Centralize AI draft qualification and one bounded repair in `micro_runtime.py`. Audit retained drafts once per review policy, preserve all original concerns, exclude active/published sources. Dedup permanently only after a source is qualified; retain bounded failure attempts/backoff.
- [x] Certify pending exact snapshots ahead of schedule; never edit a queued body. Publish re-fetches sources and uses valid approval, with fresh model review only if sources changed. Keep account/flag checks, ledger dedup, quotas, deadlines and no catch-up.
- [x] Add bounded read-only receipt grace for delayed record visibility; never click publish twice. Expose next-slot readiness and reviewed inventory in the UI.
- [ ] Run all automated and isolated creator-browser regressions. Deploy after database backup. Fill future news/tool slots under 50% allocation.
- [ ] Observe a real saved scheduled slot through submission and public/creator receipt. Report exact remaining limitations; synchronize code/docs without local data or secrets.
