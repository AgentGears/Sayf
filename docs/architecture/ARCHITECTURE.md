# Sayf Architecture Baseline

## 1. Layer model

Sayf separates four layers:

```text
Human / Agent / Tool Layer
          |
          v
Adapter / Workflow Layer
          |
          v
Control Plane
  intent | evidence | policy | gates | feedback
          |
          v
Causal Ledger
  events | records | relations | artifacts | projections
```

The Causal Ledger is the lowest authoritative layer. Higher layers may evolve without rewriting historical ledger state.

## 2. Authority boundary

An LLM, workflow, reviewer, CI job, or runtime adapter may produce an observation, proposal, or evidence artifact. It does not gain authority merely by producing text.

Future authoritative decisions must bind exact immutable inputs, including the accepted intent revision, baseline snapshot, policy snapshot, change set, evidence, and risk assessment.

## 3. Causal Ledger

M0 uses an append-only event ledger as the source of truth.

Each event includes:

- globally ordered sequence;
- stable event ID;
- logical stream ID;
- event type;
- UTC occurrence time;
- actor;
- structured payload;
- previous event hash;
- event hash.

The hash chain is global rather than per stream. A modification to any historical event therefore breaks verification of that point in the ledger and all subsequent chain continuity.

SQLite triggers reject `UPDATE` and `DELETE` against the event table. Hash verification remains independent of those triggers so unauthorized historical modification can still be detected if the database guard is bypassed.

## 4. Persistent state strategy

M0 uses:

- SQLite for ordered events and later graph projections;
- filesystem content-addressed storage for larger immutable artifacts (M0.2+);
- generated Markdown/JSON projections for humans and adapters.

Human-readable projections are never authoritative storage.

## 5. Event sourcing

Semantic state is derived from events. Durable transitions are represented by new events rather than destructive updates.

Examples planned across M0:

```text
RecordCreated
RelationCreated
IntentAccepted
IntentSuperseded
BaselineRegistered
TaskGraphRegistered
VerificationRecorded
GateRequested
GateEvaluated
ReleaseRegistered
ObservationRegistered
FeedbackOpened
RecordInvalidated
```

M0.1 implements the generic immutable event substrate before introducing these higher-level domain handlers.

## 6. M0 milestone decomposition

### M0.1 — Immutable Ledger

- actor model;
- UUIDv7 identifiers;
- canonical serialization;
- append-only SQLite storage;
- tamper-evident hash chain;
- replay/listing;
- independent ledger verification;
- CLI and CI tests.

### M0.2 — Typed Records + Graph

- records;
- typed relations;
- content-addressed artifacts;
- adjacency traversal;
- provenance paths.

### M0.3 — Revision + Staleness

- revision lineage;
- supersession;
- effective state;
- deterministic staleness propagation.

### M0.4 — Evidence + Gates

- verification receipts;
- policy snapshots;
- gate requests;
- deterministic gate decisions;
- gate invalidation when bound inputs change.

### M0.5 — Feedback + Explainability

- releases;
- runtime observations;
- feedback cases;
- `why`, `impact`, and `timeline` queries.

## 7. Architectural invariants

1. Durable historical records are not silently rewritten.
2. Every durable transition has an actor and creating event.
3. Authority is explicit and version-bound.
4. Stale authority is not silently reusable.
5. Downstream impact must be explainable by relationship paths.
6. Unknown is not equivalent to pass.
7. The ledger must operate without an LLM.

## 8. Deliberate exclusions

M0 does not require a graph database, ORM, vector database, distributed scheduler, web UI, Kubernetes, or custom coding-agent runtime.

Those are implementation choices above the causal semantics and should not be introduced before the core model demonstrates value.
