# V7.5 tools and skills reliability implementation plan

**Goal:** Make manual football research and news dependable, broaden verified European/South American sources, show the full league table and European fixtures, and expose player portrait retrieval.

**Architecture:** Preserve scoped evidence and independent support verification. Source discovery, article extraction, structured sports data, editorial execution, and workflow lifecycle remain separate. All work starts with explicit user action and each action ends after one cycle.

**Tech stack:** Existing Python/FastAPI/SQLite and React/TypeScript; no new service required.

**Authorization:** The user requested autonomous implementation and explicitly authorized real calls using configured model credentials. Paid acceptance tests use isolated databases, never print credentials, and do not overwrite personal results. Canonical main remains the only working branch; preserve a rollback tag and database/key backup.

## Acceptance contract

- Reliability: at least 95% completion on a declared, representative real-model news sample; record all attempts, including failures. A smaller sample is clearly labeled, not generalized into production reliability.
- Content: retain material names, dates, numbers, qualifications and source attribution. No verification bypass, summary-as-full-text, invented standings or identity guesses.
- Speed: compare the same sample/configuration against V7.4; median no slower, report tail latency and concurrency. Network/source coverage reports identify excluded or unavailable publishers honestly.
- Coverage: validated European and South American media discovery plus article extraction; full domestic league standings; available European fixtures; explicit photo tool with provenance.
- Manual operation: startup, page reads and elapsed time do not launch research or models; one click is one cycle, cancellable and idempotent.
- Delivery: full Python suite, UI tests, production build, desktop/narrow browser checks, independent review and real local service smoke test.

## Tasks

- [x] Preserve baseline and inspect actual failed runs, provider timing and source failures.
- [x] Add reproductions for source extraction/network failures; implement validated sources and bounded parallel fallbacks; probe live index and article reads.
- [x] Add tests for one-shot workflow lifecycle and startup/GET inactivity; remove periodic execution and waiting UI.
- [x] Add full league table, European schedule support and identity-checked portrait retrieval, with provider/parser tests and live probes.
- [x] Reproduce editorial/model failures in isolated live runs; fix protocol/tool/skill issues without reducing factual coverage or source verification; test cancellations and source version changes.
- [x] Integrate portrait tool and skills; compare throughput using isolated configured credentials; record sample, failures, timing and editorial content checks.
- [x] Run all required checks and browser QA, review changes, fix findings, document measured acceptance and release with backup.

## Delivery state

Code, isolated live acceptance, full regression, production build and browser QA are complete. Local activation remains pending the user stopping the old V7.4 process: automatic approval rejected the combined stop/start command without a specific reason. The three old waiting workflows were stopped through supported application endpoints and all personal record IDs and the database key were verified intact.
