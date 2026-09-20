# Sayf

**Evidence-driven engineering control plane for agentic software development.**

Sayf maintains authoritative, traceable engineering state from uncertain intent through implementation, verification, release, and real-world feedback.

> **Agents propose. Evidence supports. Policy constrains. Sayf governs state.**

## Why Sayf

Agentic development is increasingly good at generating plans and code, but the durable state of *why* a change is justified is usually scattered across prompts, Markdown, CI output, reviews, and runtime telemetry. Sayf is designed to make that justification explicit, versioned, queryable, and challengeable.

The north-star query is:

> **Why does the system currently believe this release is justified, and what evidence or assumptions could invalidate that justification?**

## Architecture

Sayf separates probabilistic reasoning from authoritative state:

```text
Humans / Agents / Tools
        |
        v
Adapters and workflows
        |
        v
Sayf Control Plane
  Intent | Evidence | Policy | Gates | Feedback
        |
        v
Causal Ledger
  immutable events | typed records | relations | provenance | staleness
```

Sayf is intended to integrate with systems such as Spec Kit and Aegis rather than replace them.

## Current milestone

### M0 — Causal Ledger

M0 establishes the substrate beneath the control plane:

- **M0.1 Immutable Ledger** — append-only events, actors, local consistency verification, SQLite persistence, replay/verification.
- **M0.2 Typed Records + Graph** — immutable typed records/relations derived from ledger history, content-addressed artifacts, deterministic traversal and relationship paths.
- **M0.3 Revision + Staleness** — explicit relation qualification, invalidation, linear supersession, and deterministic dependency staleness.
- **M0.4 Evidence + Gates** — bounded verification receipts, immutable policy snapshots, exact gate requests, deterministic gate decisions, and decision staleness.
- **M0.5 Feedback + Explainability** — runtime observations, feedback cases, `why`, `impact`, and `timeline`.

The current codebase implements **M0.1 through M0.4**. Release authority, risk-assessment contracts, runtime feedback, and full explanation queries remain deliberately deferred.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

sayf init .

sayf record create \
  --type Assumption \
  --id rec_assumption \
  --payload '{"text":"The upstream API is stable"}'

sayf record create \
  --type IntentRevision \
  --id rec_intent \
  --payload '{"goal":"ship the integration"}'

sayf relation create \
  --type depends_on \
  --source rec_intent \
  --target rec_assumption \
  --id rel_intent_assumption

sayf state bind-dependency rel_intent_assumption

sayf record create \
  --type Observation \
  --id rec_observation \
  --payload '{"result":"upstream contract changed"}'

sayf relation create \
  --type invalidates \
  --source rec_observation \
  --target rec_assumption \
  --id rel_observation_invalidates_assumption

sayf state invalidate rel_observation_invalidates_assumption
sayf state show rec_intent
sayf state affected rec_assumption

sayf record create \
  --type ChangeSet \
  --id change_1 \
  --payload '{"summary":"integration change"}'

sayf record create \
  --type Evidence \
  --id evidence_tests \
  --payload '{"kind":"test-run"}'

sayf verification record \
  --id verification_tests \
  --payload '{"subject_id":"change_1","contract":"tests","result":"pass","evidence_record_ids":["evidence_tests"],"environment":{"platform":"local"},"independent_from_generation":"not_applicable","verified_claim":"tests passed for the bound change"}'

sayf policy register \
  --id policy_1 \
  --payload '{"name":"test-gate","requirements":[{"contract":"tests","minimum_passes":1}]}'

sayf gate request \
  --id gate_request_1 \
  --payload '{"subject_id":"change_1","policy_snapshot_id":"policy_1","verification_receipt_ids":["verification_tests"]}'

sayf gate evaluate gate_request_1 --id gate_decision_1
sayf gate show gate_decision_1

printf 'verification receipt\n' > receipt.txt
sayf artifact put receipt.txt \
  --id rec_receipt \
  --media-type text/plain
sayf artifact verify rec_receipt

sayf ledger verify
```

## M0.1 — Immutable Ledger

M0.1 accepts JSON-native event payload and actor metadata values only. Non-finite numbers, non-JSON Python objects, invalid UTF-8 text, duplicate CLI JSON keys, and non-standard JSON constants are rejected before authoritative state is created.

A ledger is considered locally usable only after three layers pass: SQLite `quick_check`, exact supported v1 schema/metadata identity, and canonical event-history verification. Stored JSON and timestamps must retain their canonical representation, sequence numbers must be contiguous from `1..N`, and every event hash/link must recompute correctly. Schema inspection treats only SQLite's literal reserved `sqlite_` name prefix as SQLite-owned; similarly named user objects are not hidden from validation. Authoritative read operations hold one SQLite read transaction from container/schema validation through history consumption, so all three usability layers describe one consistent database snapshot.

`sayf init` creates a new ledger or validates the complete local usability of an existing one; it does not silently repair, migrate, or convert an unknown, damaged, or history-invalid database. Before inspecting the target ledger path, first-use initialization acquires an exclusive lock in a tiny non-authoritative SQLite coordination sidecar. The coordination key is fixed-length and conservatively derived from a normalized/case-folded resolved target path, so lexical and case aliases serialize through the same barrier without inheriting the ledger filename's component length. Recovery authorization is intentionally stricter: the pending marker is keyed from the resolved target using only the operating system's native path normalization, so a conservative lock-key collision cannot authorize recovery of a distinct case-sensitive target. Before reserving a missing target, the lock owner writes the marker to a same-directory staging file, flushes and fsyncs it, then publishes the final target-specific pending marker durably. On POSIX, publication is an atomic rename followed by a parent-directory fsync. On Windows, publication uses `MoveFileExW(..., MOVEFILE_WRITE_THROUGH)` so the name transition is write-through before target reservation. If a Windows recovery path encounters an existing pending marker, Sayf rewrites that marker through the same write-through transition before using it as recovery authorization. If marker publication or its required durability step fails, no target ledger is reserved. If the process or host stops after durable marker publication but before schema commit, a later initializer can recover only a marker-backed schema-less SQLite target for the same recovery identity; arbitrary existing databases without that marker remain fail-closed. Recovery authorization must also be durably revoked before `init` reports success. On POSIX, Sayf unlinks the pending marker and fsyncs its parent directory. On Windows, it performs a write-through rename from the authoritative `.pending` marker to a non-authoritative `.revoked` tombstone; only after that transition succeeds may initialization report success. Tombstone cleanup is best-effort because a `.revoked` file is never recognized as recovery authorization. A failed Windows revocation move leaves the pending marker in place and causes initialization to fail. The owner then reserves the target path with exclusive file creation and creates the schema in one SQLite transaction. Concurrent initializers therefore cannot misclassify another Sayf process's in-progress target as pre-existing storage, and the mechanism does not require filesystem hard-link support. `sayf ledger show` returns only locally verified history. `sayf ledger append` passes through the same initialization barrier before re-verifying the existing ledger inside its immediate write transaction, so new authoritative state is never appended to a locally invalid chain.

At the database level, M0.1 rejects direct historical `UPDATE` and `DELETE` operations and rejects replacement-style inserts that collide with an existing sequence, event ID, or event hash. These are local storage guards, not a cryptographic authenticity boundary against an actor able to alter the schema itself.

That correctness-first append policy is deliberately O(N) in current ledger length for M0.1. It is suitable for proving the causal semantics, but a later optimization will be needed before high-throughput use. Any such optimization must preserve fail-closed verification, for example through authenticated or otherwise validated checkpoints rather than blindly trusting a stored head.

M0.1 does **not** claim cryptographic authenticity against an adversary with arbitrary write access to the database: such an actor can rewrite records and recompute every affected hash, renumber history, or truncate the ledger tail while leaving a self-consistent local ledger. Detecting that class of attack requires an external checkpoint, signature, replicated witness, transparency log, or equivalent trust anchor. ADR-0001 records this boundary explicitly.

## M0.2 — Typed Records + Graph

M0.2 keeps the M0.1 SQLite v1 schema unchanged. Typed records and relations are represented by reserved immutable ledger events and replayed into a deterministic in-memory graph. The graph is a disposable projection, not a second authoritative state store.

A record or relation ID is exactly its creation event ID. The typed envelopes preserve creation actor, timestamp, ledger sequence, creating event ID, schema version, canonical content hash, and structured payload/metadata. Relations are directed, may form a multigraph, and can only reference records that already exist earlier in authoritative history; forward references and dangling edges fail closed.

Normal raw CLI append refuses the reserved `sayf.record.created.v1` and `sayf.relation.created.v1` event names. If a privileged low-level caller nevertheless writes malformed or currently unsupported reserved semantic history, M0.2 projection rejects it rather than treating malformed typed state as valid.

Typed writes validate one complete semantic snapshot and bind the eventual append to that snapshot's exact event count and head hash. The append re-verifies M0.1 history under `BEGIN IMMEDIATE`; if any event was committed after semantic validation, the typed write fails and must be revalidated rather than committing against stale semantics.

Graph neighbor queries support outbound, inbound, and combined traversal. Relationship-path searches are explicit, cycle-safe, and bounded by depth, result count, and total neighbor expansion. Exceeding a result or work bound fails the query rather than returning a partial path set that could be mistaken for complete causal coverage. Paths expose recorded relationships; they do not infer truth or causal certainty beyond those explicit edges.

Artifacts use SHA-256 content-addressed storage under `.sayf/objects/sha256/...`. Object bytes are verified against the digest, and an existing or concurrently appearing corrupt object at the expected address is rejected rather than silently overwritten. POSIX publication uses a no-replace hard-link installation of the already-fsynced staging inode and therefore fails closed on a filesystem that cannot provide the required hard-link primitive; Windows uses a write-through no-replace move. This CAS publication requirement is separate from M0.1 ledger initialization, which remains hard-link-independent. An object becomes referenced Sayf engineering state only when an immutable `Artifact` record binds its digest, byte length, metadata, actor, and creating event into the ledger. Unreferenced CAS bytes are not authoritative records.

Except for typed contracts implemented by later milestones, active record payloads are canonical JSON objects. M0.4 activates `VerificationReceipt`, `PolicySnapshot`, `GateRequest`, and `GateDecision` only through their dedicated semantic APIs; they cannot be created through the generic record surface. `RiskAssessment`, `Release`, `RuntimeObservation`, and `FeedbackCase` remain mechanically reserved until their contracts exist. This preserves the rule that a future authoritative-looking type name does not acquire authority merely because generic JSON was stored under that name.

M0.2 relationship claims remain distinct from M0.3 state authority. In particular, `depends_on`, `invalidates`, and `supersedes` relations have no state consequence until an explicit M0.3 qualification event adopts that exact relation.

M0.2 reconstructs the graph from the fully verified event history for each operation. Typed writes then re-verify the ledger under the append transaction before comparing the semantic snapshot head. This is a correctness-first O(N) replay/verification tradeoff, not a high-throughput design. Future persistent projections or checkpoints may accelerate replay only if they remain verifiable and disposable rather than becoming another source of truth.

See [`docs/architecture/M0_2_TYPED_RECORDS_GRAPH.md`](docs/architecture/M0_2_TYPED_RECORDS_GRAPH.md) for the frozen M0.2 contract and trust boundaries.

## M0.3 — Revision + Staleness

M0.3 preserves the M0.2 distinction between a recorded relationship claim and authoritative effective state. Three new immutable event types qualify an already-existing relation by exact relation ID and relation content hash:

```text
sayf.dependency.bound.v1
sayf.record.invalidated.v1
sayf.record.superseded.v1
```

Only a qualified `depends_on` edge participates in staleness propagation. A qualified `invalidates` edge marks its target invalidated. A qualified `supersedes` edge marks the old target revision superseded and requires source/target records of the same type. M0.3 intentionally supports a linear supersession lineage and fails ambiguous branching/merge semantics closed.

Effective state exposes three independent axes rather than one overloaded status:

```text
validity:   not_invalidated | invalidated
revision:   current | superseded
freshness:  fresh | stale
```

The labels deliberately stop short of positive authority claims. `not_invalidated` means only that no qualified invalidation exists; `current` means only that no qualified supersession exists; and `fresh` means only that no qualified dependency path currently reaches an invalidated or superseded root. They do not mean true, accepted, verified, complete, safe, or pass.

Invalidated and superseded records are staleness roots. Their qualified transitive dependents become stale. Each stale record carries a deterministic canonical dependency path back to each root so the reason for staleness remains mechanically inspectable.

State transitions validate a complete semantic snapshot and use the M0.2 compare-and-append boundary, so any concurrent ledger change between validation and append forces revalidation. Malformed privileged M0.3 state history blocks subsequent semantic operations fail-closed.

The M0.3 actor field is provenance, not an authorization system. In the local-first milestone, permission to invoke a state-transition API comes from the host/application boundary. An agent-authored relation does not become effective merely because it exists; a caller with transition authority must explicitly qualify it. M0.4 adds deterministic policy/evidence sufficiency but does not turn actor IDs into authenticated credentials.

Effective-state projection is correctness-first and currently recomputes from fully verified history. Staleness derivation may revisit the qualified dependency graph once per invalidated/superseded root, so this is not yet a high-throughput design. Persistent projections or incremental invalidation indexes should be introduced only after measurement establishes a forcing function and only if they remain verifiable, disposable acceleration state.

`state affected` is intentionally narrower than the future M0.5 `impact` query: it reports records currently stale because the requested record is an invalidated or superseded root. It does not claim to enumerate policy, gate, release, or runtime consequences.

See [`docs/architecture/M0_3_REVISION_STALENESS.md`](docs/architecture/M0_3_REVISION_STALENESS.md) for the M0.3 contract, qualification boundary, and acceptance slice.

## M0.4 — Evidence + Gates

M0.4 activates four previously reserved semantic record contracts through dedicated APIs: `VerificationReceipt`, `PolicySnapshot`, `GateRequest`, and `GateDecision`. Cross-record semantic references bind both immutable record ID and exact content hash, and a binding must point to a record that already existed when the binding record was created. Malformed, mismatched, or forward M0.4 bindings fail semantic replay closed.

A verification receipt records a bounded result for an exact subject using exact evidence records. Results are `pass`, `fail`, `partial`, or `inconclusive`. A pass requires a bounded `verified_claim`; fail and inconclusive receipts cannot assert one. Receipt existence is evidence, not gate authority, and partial/inconclusive results do not satisfy a v1 policy requirement.

A policy snapshot intentionally contains only deterministic verification requirements: a contract name and `minimum_passes >= 1`. M0.4 does not introduce a general-purpose policy language. `minimum_passes` counts distinct bound receipt records that are usable and pass the same contract; it does **not** prove verifier independence, statistical independence, or diversity of underlying evidence unless a future policy contract explicitly models those properties.

A gate request immutably binds one exact `ChangeSet`, one exact policy snapshot, and a fixed receipt set. Every receipt must verify that exact ChangeSet. Requests may intentionally contain insufficient, failed, partial, inconclusive, stale, invalidated, or superseded inputs so failed gate attempts can be preserved as deterministic BLOCK decisions rather than disappearing before evaluation.

Gate evaluation considers the request, ChangeSet, policy, receipts, and bound evidence. A bound input is usable only when its M0.3 effective state is `not_invalidated/current/fresh`; only usable passing receipts count toward policy requirements. Unknown or missing evidence never becomes pass. The decision is `permit` only when there are no blocking reasons and every requirement is satisfied; otherwise it is `block`.

Every persisted gate decision is independently recomputed during replay from the ledger prefix immediately before that decision. A privileged caller cannot make a forged permit authoritative merely by appending a syntactically valid `GateDecision`; if the stored payload differs from the deterministic historical evaluation, semantic replay fails closed. One immutable request may have at most one M0.4 decision; evaluation against a changed input set requires a new request.

A decision also binds the complete canonical M0.3 effective-state fingerprint for every evaluated input, not only the three coarse labels. A later invalidation cause, supersession, dependency-derived staleness path, or other change to that exact effective state makes the historical decision currently stale. The decision record's own M0.3 state must also remain usable. Unrelated ledger events that do not change a bound effective state do not stale the decision.

Gate freshness is independent from gate outcome. A BLOCK decision can remain fresh when it still describes the same unchanged negative input state. Likewise, a historical PERMIT remains historically what was decided even after its current status becomes stale. `PERMIT` means only that the exact request satisfied the exact policy under the exact evaluated state; it does **not** mean released, globally approved, safe, correct, or complete. Release authority remains deferred.

M0.4 retains the exact semantic-head compare-and-append boundary for receipt, policy, request, and decision creation. A concurrent ledger commit after validation forces revalidation rather than allowing a decision to commit against a stale semantic snapshot.

Historical gate verification is correctness-first: replay can reconstruct an M0.3 prefix for each historical gate decision, so worst-case work can approach O(D × N) for D decisions over N events in addition to normal history verification/projection. No mutable gate cache or second authority store is introduced without a measured forcing function.

The dedicated CLI surface is:

```text
sayf verification record --payload <json>
sayf policy register --payload <json>
sayf gate request --payload <json>
sayf gate evaluate <request_id>
sayf gate show <decision_id>
sayf gate list
```

The actor field on M0.4 records remains provenance, not authenticated identity. The local host/application boundary controls who can invoke these semantic APIs; M0.4 does not invent signatures or remote RBAC.

See [`docs/architecture/M0_4_EVIDENCE_GATES.md`](docs/architecture/M0_4_EVIDENCE_GATES.md) for the M0.4 contract, claim ceilings, replay rules, and acceptance slice.

## Design invariants

1. **No silent mutation** — accepted historical state is superseded, never rewritten.
2. **No missing provenance** — durable state identifies the actor and event that created it.
3. **No unsupported authority** — an agent assertion, relation label, receipt, or type name is not authoritative merely because it was emitted.
4. **No stale reuse** — typed/state/gate writes bind the exact ledger head whose semantics they validated.
5. **No hidden downstream impact** — invalidated or superseded premises expose qualified stale dependents and gate decisions bind exact effective-state fingerprints.
6. **Unknown is not pass** — absence of invalidation, supersession, staleness, or evidence is not promoted into truth, acceptance, verification, or pass.
7. **Unknown storage is fail-closed** — existing authority stores are validated before authoritative read or mutation.
8. **Derived semantic state is disposable** — authoritative history remains in immutable ledger events.
9. **Bounded queries are explicit** — relationship-path limits fail rather than silently presenting partial results as complete.
10. **CAS publication is no-clobber** — an existing or racing digest address is verified/reused or rejected, never overwritten by normal publication.
11. **Relation claim is not adoption** — M0.3 state consequences require explicit qualification of an exact immutable relation.
12. **Evidence is not gate authority** — M0.4 gate outcomes require an exact immutable request and policy, not merely the presence of verification records.
13. **Gate permit is not release authority** — M0.4 deliberately stops before release registration or approval.
14. **Historical decisions remain challengeable** — later changes to bound effective-state fingerprints stale prior gate decisions without rewriting them.

## Development

```bash
python -m pip install -e '.[dev]'
ruff check .
pytest
```

CI executes the suite on Python 3.12 under both Ubuntu and Windows.

See [`docs/architecture/VISION.md`](docs/architecture/VISION.md), [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md), [`docs/architecture/M0_CAUSAL_LEDGER.md`](docs/architecture/M0_CAUSAL_LEDGER.md), [`docs/architecture/M0_2_TYPED_RECORDS_GRAPH.md`](docs/architecture/M0_2_TYPED_RECORDS_GRAPH.md), [`docs/architecture/M0_3_REVISION_STALENESS.md`](docs/architecture/M0_3_REVISION_STALENESS.md), and [`docs/architecture/M0_4_EVIDENCE_GATES.md`](docs/architecture/M0_4_EVIDENCE_GATES.md).

## License

MIT