# M0.4 Review Map

This document is a working review surface, not the frozen first-pass findings register.

## Objective

Implement the narrow M0.4 Evidence + Gates contract without expanding into release authority, generic policy execution, remote identity, or M0.5 explainability.

## Requirements under review

- Verification receipts bind exact immutable subject/evidence records and preserve bounded claims.
- Policy snapshots define deterministic verification sufficiency without a general-purpose policy language.
- Gate requests bind one exact ChangeSet, one exact policy snapshot, and an immutable receipt set.
- Gate decisions are recomputable from ledger history rather than trusted because they were appended.
- Unknown, stale, invalidated, superseded, failed, partial, and inconclusive inputs do not become pass.
- Gate decisions become stale when any bound effective-state fingerprint changes.
- A BLOCK decision may remain fresh when its bound input state is unchanged.
- One immutable gate request has at most one decision.
- Semantic validation/append remains protected by exact ledger-head compare-and-append.
- Generic record creation cannot impersonate M0.4 semantic record types.

## Components

- `src/sayf/records.py`: M0.4 typed payload contracts and semantic-only record creation boundary.
- `src/sayf/gates.py`: binding validation, deterministic evaluation, historical decision replay verification, current gate freshness.
- `src/sayf/causal.py`: authoritative semantic facade and compare-and-append integration.
- `src/sayf/cli.py`: dedicated verification/policy/gate surfaces.
- `tests/test_m04_gates.py`: core semantics, replay integrity, concurrency, staleness.
- `tests/test_m04_contract_edges.py`: payload/contract boundaries.
- `tests/test_m04_cli.py`: end-to-end CLI slice.
- `docs/architecture/M0_4_EVIDENCE_GATES.md`: milestone contract and claim ceilings.

## Trust boundaries

- Ledger consistency remains local, not externally authenticated.
- Actor identity remains provenance, not cryptographic authentication.
- Dedicated M0.4 APIs establish semantic ceremony but invocation authority remains the local host/application boundary.
- Policy snapshots constrain evidence sufficiency only; they do not authenticate verifiers.
- A permit is not release authority.

## Failure boundaries to review

- malformed/future/mismatched record bindings;
- forged privileged gate decisions;
- duplicate decisions;
- stale subject, policy, request, receipt, or evidence;
- state changes that preserve coarse axes but change causal provenance;
- unrelated ledger appends;
- concurrent ledger-head changes between validation and append;
- multiple requirements and minimum-pass counts;
- failed/partial/inconclusive verification receipts;
- ambiguous policy changes without explicit supersession;
- replay cost and denial-of-service characteristics;
- payload normalization/hash stability;
- CLI strict JSON and generic-surface bypass attempts.

## Deliberate non-scope

- `RiskAssessment`;
- `Release`;
- `RuntimeObservation`;
- `FeedbackCase`;
- authenticated identity / signatures / RBAC;
- waivers / overrides;
- arbitrary policy expressions;
- mutable requests;
- multiple decisions per request;
- persistent gate caches;
- M0.5 `why`, `impact`, `timeline`.
