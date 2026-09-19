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

`sequence` is globally monotonic. The previous hash is the global ledger head, not merely the prior event in the same logical stream.

### Actor kinds

- `human`
- `agent`
- `tool`
- `policy_engine`
- `runtime_adapter`
- `system`

Actor type alone does not confer trust or authority.

### Canonical hashing

The hash input includes the complete event envelope except `event_hash`, serialized with deterministic JSON key ordering and compact separators. SHA-256 is used for the M0 chain.

### Storage enforcement

SQLite serializes append operations with an immediate transaction and rejects `UPDATE` and `DELETE` on the event table using database triggers.

`ledger verify` recomputes the chain independently so historical tampering remains detectable if those guards are bypassed.

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

1. event append is transactional;
2. event order is deterministic;
3. historical rows cannot be updated or deleted through normal database access;
4. hash-chain verification succeeds for valid history;
5. verification fails at the first modified historical event if storage guards are bypassed;
6. duplicate event IDs are rejected;
7. the CLI can initialize, append, display, and verify a ledger;
8. CI executes lint and tests on pull requests.
