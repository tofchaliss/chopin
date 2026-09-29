# Target: Themis (tofchaliss/themis)

Themis is a Go security-intelligence platform: it ingests SBOM/VEX documents,
correlates vulnerabilities against CVE feeds, applies VEX overlay semantics,
governs enterprise positions, and publishes standards-based artifacts.

## Two code trees — know which one you are in

- **Phase-3 greenfield rebuild** — `internal/<context>/{domain,app,adapters}`
  with a composition root in `cmd/<context>`. The ONLY place new work lands.
  - Imports point inward only: domain <- app <- adapters.
  - `domain` is pure Go: no I/O, no logging, no framework imports.
  - `app` holds use cases and ports; depends only on `domain`.
  - `adapters` hold Postgres stores, HTTP handlers, feed ACLs, event
    consumers; the only ring that may import drivers and the shared platform
    packages (`internal/platform/{observability,eventbus,auth}`).
  - NO cross-context imports: contexts collaborate only through domain events
    (transactional outbox + bus) and read-only HTTP read APIs.
  - APIs are spec-first: `api/<context>.openapi.yaml`, handlers generated
    (`make generate-api-<context>`), never hand-edited.
- **Legacy v0.3.x monolith** — `internal/{domain,usecase,adapter,infrastructure}`
  and `cmd/themis`. FROZEN, reference only. Never plan or make changes there.

## Authoritative documents

- ADRs (`docs/adr/`) and EDRs (`docs/engineering/decisions/`) are the reason
  of record, over the legacy code and over intuition.
- `docs/engineering/STACK.md` and `docs/engineering/CONVENTIONS.md` are
  standing cross-cutting rules (R1: every node logs to console and
  OpenTelemetry via the shared package, never from domain/app; R2:
  self-documented config, secrets referenced never inlined).
- OpenSpec changes (`openspec/changes/phase3-*`) are the system of record for
  greenfield work.
- `.cursor/rules/*.mdc` describe the frozen monolith; do not treat them as
  intent.

## Needs the owner's approval (list under open_questions)

New package, dependency, service, module or directory structure; new
architectural pattern; API change; domain-model change; build or CI change;
security-model change.

## Quality gate and tests

- The gate is `make check`: build, vet-tags, test, lint, clean-arch,
  arch-test, coverage (includes integration tests), deadcode.
- Coverage tiers: `domain`/`app` 100%, adapters 90%, aggregate stores 80%; a
  new package must be registered in `scripts/check-coverage.sh`.
- Each greenfield store owns `adapters/store/migrations/`; up/down
  reversibility is required.

## Invariants that must never be weakened

- A Faultline is one card per canonical CVE; cards are never deleted, only
  superseded; lifecycle is forward-only.
- VEX overlays, never deletes; vendor VEX is gathered, not obeyed — it raises
  a proposal, it never auto-suppresses.
- A distro advisory's package list is scope, not N claims; an unknown claim
  class is treated as carrier everywhere.
- AI is advisory: the Intelligence Gateway proposes, humans or policy decide;
  a Decision capability never auto-accepts.
- Inbound auth (`X-API-Key`, `THEMIS_AUTH_REQUIRED`) and HMAC trust paths.
- `X-Themis-AI-Reason` and `X-Themis-AI-Detail` are two headers, never
  concatenated.
