# M0 — Causal Ledger

**Status:** implementation baseline

## Objective

M0 establishes the authoritative substrate that lets Sayf reconstruct why engineering state exists, what evidence supported it, and what downstream state becomes stale when an upstream premise changes.

The ledger itself must function without an LLM.

## Scope

M0 implements six capabilities:

1. immutable event ledger;
2. typed records;
3. typed relationship graph;
4. content-addressed artifact storage;
5. deterministic current-state and staleness projections;
6. provenance and impact queries.

## Non-goals

M0 is not an IDE, coding agent, workflow engine, deployment system, graph database product, or replacement for Spec Kit/Aegis.

## Record families planned for M0

### Discovery / semantic

`IdeaSeed`, `ProblemFrame`, `Claim`, `Assumption`, `Hypothesis`, `Evidence`, `Experiment`, `Observation`, `Requirement`, `Constraint`, `Decision`, `IntentRevision`.

### Engineering

`BaselineSnapshot`, `TaskGraph`, `Task`, `ExecutionAttempt`, `ChangeSet`, `VerificationReceipt`, `RiskAssessment`.

### Authority / lifecycle

`PolicySnapshot`, `GateRequest`, `GateDecision`, `Release`, `RuntimeObservation`, `FeedbackCase`.

### Provenance

`InteractionReceipt`, `Artifact`, `ExternalReference`.

These record types are introduced incrementally after M0.1 proves the event substrate.

## Core relationship vocabulary

Epistemic: `supports`, `contradicts`, `falsifies`, `assumes`, `derived_from`, `observes`.

Authority/versioning: `supersedes`, `accepts`, `rejects`, `governed_by`.

Engineering: `implements`, `depends_on`, `produces`, `modifies`, `verified_by`, `covers`.

Lifecycle: `permits`, `blocks`, `releases`, `violates`, `reopens`, `invalidates`.

Provenance: `created_from`, `discussed_in`, `references`.

## Effective-state vocabulary

The minimum state vocabulary is:

- `current`;
- `potentially_stale`;
- `stale`;
- `superseded`;
- `invalidated`.

A contradiction does not rewrite history. It can make dependent state `potentially_stale`; explicit revision or deterministic bound-input change can make authority `stale`.

## M0 invariants

### M0-I1 — No silent mutation

Historical semantic state is superseded or invalidated by new events; it is never silently rewritten.

### M0-I2 — No missing provenance

Every durable state transition identifies the actor and creating event.

### M0-I3 — No unbound authority

Future gate decisions must bind exact versions of intent, baseline, policy, change set, evidence, and risk inputs.

### M0-I4 — No stale gate reuse

If a bound authoritative input is superseded or invalid under policy, the old gate cannot remain valid.

### M0-I5 — No hidden downstream impact

Sayf must be able to enumerate downstream dependents and explain each relationship path.

### M0-I6 — Unknown is not pass

Missing evidence is not successful evidence; an uninspected relationship is not proof that no relationship exists; absence of a contradiction is not proof of a claim.

### M0-I7 — Unknown or invalid storage is fail-closed

An existing database must satisfy the supported SQLite, schema, canonical-storage, and local-history checks before authoritative read or mutation. Initialization does not silently repair, migrate, convert, or legitimize unknown or damaged storage.

## M0.1 — Immutable Ledger

M0.1 freezes the event envelope and persistence behavior.

### Event envelope

```text
event_id
sequence
stream_id
event_type
occurred_at
actor
payload
previous_event_hash
event_hash
```

`sequence` is globally contiguous from `1..N` for valid local history. The previous hash is the global ledger head, not merely the prior event in the same logical stream.

### Actor kinds

- `human`
- `agent`
- `tool`
- `policy_engine`
- `runtime_adapter`
- `system`

Actor type alone does not confer trust or authority.

### JSON-native values

Event payloads and actor metadata are normalized into detached JSON-native values before authoritative persistence. Supported values are JSON objects, arrays, valid UTF-8 strings, booleans, integers, finite floating-point numbers, and `null`. Non-finite numbers, non-string object keys, invalid Unicode text, circular/non-JSON Python objects, and excessive recursive structures are rejected.

The CLI additionally rejects non-standard JSON constants and duplicate object keys before an event draft is created.

### Canonical storage and hashing

The hash input includes the complete event envelope except `event_hash`, serialized with deterministic JSON key ordering and compact separators. SHA-256 is used for the M0 chain.

The SQLite representation is also canonical: stored actor/payload JSON must exactly match Sayf's canonical serialization, and stored timestamps must exactly match their normalized UTC ISO representation. Verification therefore rejects raw storage rewrites that would otherwise parse to the same semantic value, such as duplicate JSON keys, alternate whitespace/key ordering, or an equivalent timestamp spelling.

The M0.1 chain is an **internal consistency mechanism**, not an external authenticity anchor. Verification can detect malformed rows, noncanonical stored representations, non-contiguous sequence state, broken hash links, and content changes whose affected hashes were not recomputed. An actor with arbitrary write access can instead rewrite or renumber records and recompute the entire affected chain, or truncate the current tail, while preserving local consistency. Detecting that stronger adversary requires a future external checkpoint, signature, replicated witness, transparency log, or equivalent trust anchor.

### Local usability gate

Before an existing ledger is treated as authoritative, M0.1 applies three layers:

1. SQLite `quick_check` must report a healthy database container;
2. the exact supported v1 schema and metadata identity must match, with no unexpected user-defined tables, indexes, views, or triggers;
3. the complete event history must satisfy canonical representation, contiguous `1..N` sequencing, hash recomputation, and previous-hash linkage.

This gate applies to `init` on an existing ledger, authoritative event listing, and append. An append performs the check under the same `BEGIN IMMEDIATE` transaction before extending the chain, preventing new state from being based on locally invalid history.

Initialization creates a new ledger or validates the complete local usability of an existing one. First-use schema creation runs statement-by-statement inside a single `BEGIN EXCLUSIVE` SQLite transaction, so concurrent initializers serialize through the database writer lock and do not require filesystem hard-link support. An already-existing unknown or empty SQLite file is not silently converted. Initialization does not repair or migrate damaged storage. Read-only inspection and verification never create schema or files as a side effect.

At the database level, `UPDATE` and `DELETE` are rejected for event rows, and replacement-style inserts are rejected when the incoming row collides with an existing `sequence`, `event_id`, or `event_hash`. These guards protect normal database access; they are not an authenticity boundary against an actor able to alter the schema itself.

`ledger verify` streams the event history rather than materializing the full ledger. Its result is a statement about current local usability and consistency, not proof that no privileged writer has ever replaced the history.

The full pre-append scan is deliberately O(N) in ledger length in M0.1. This is a correctness-first choice while the causal semantics are being proven. Later checkpoint/caching work may optimize it only if the optimized path preserves fail-closed validation rather than trusting an unauthenticated stored head.

## M0 acceptance scenario

The end-of-M0 demonstration is:

```text
Assumption A1
  -> Intent I1
  -> Task / Change
  -> Verification
  -> Gate ALLOW
  -> Release R1
  -> Runtime Observation O1 falsifies A1
  -> impact propagation exposes affected intent/gate/release
  -> Intent I2 supersedes I1
  -> old Gate becomes STALE
```

Sayf must then answer both:

```text
why R1
impact A1
```

with explicit causal/provenance paths.

## M0.1 exit criteria

M0.1 is complete when:

1. event append is transactional and persists exactly one accepted event;
2. event sequence is deterministic and contiguous from `1..N`;
3. event payload and actor metadata are JSON-native and valid UTF-8 before persistence;
4. canonical stored JSON/timestamp representations are independently checked;
5. historical rows cannot be updated, deleted, or replaced through normal database access by colliding on event sequence, event ID, or event hash;
6. SQLite `quick_check` and exact v1 schema/metadata identity pass before authoritative use;
7. initialization is creation-only/idempotent for locally valid storage, serializes first-use creation transactionally without requiring hard links, and does not repair unknown, damaged, or history-invalid databases;
8. hash-chain verification succeeds for valid local history;
9. verification fails on malformed rows, noncanonical storage, sequence gaps, broken chain links, and modified event content when stored hashes were not recomputed consistently;
10. authoritative listing and append refuse locally invalid history;
11. read-only inspection/verification do not initialize missing or unrelated databases;
12. duplicate event IDs are rejected;
13. the CLI can initialize/validate, append, display, and verify a ledger and handles invalid input without tracebacks;
14. the trust boundary of the unanchored local hash chain is documented explicitly;
15. CI executes lint and tests on Python 3.12 under both Ubuntu and Windows.
