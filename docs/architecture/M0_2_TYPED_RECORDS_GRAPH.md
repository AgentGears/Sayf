# M0.2 — Typed Records + Graph

**Status:** implementation baseline

## Objective

M0.2 turns the M0.1 immutable event ledger into a typed causal model without introducing a second authoritative database or weakening the frozen v1 ledger schema.

The slice provides:

1. immutable typed records;
2. immutable typed directed relations;
3. deterministic graph replay and adjacency traversal;
4. bounded explicit relationship-path queries;
5. SHA-256 content-addressed artifact bytes bound to immutable `Artifact` records.

Revision lineage, effective state, supersession semantics, staleness propagation, evidence sufficiency, policy, gate decisions, release authority, and runtime feedback remain later milestones.

## Authority model

The M0.1 event ledger remains the authoritative source of semantic history. M0.2 does **not** add mutable record or graph tables to the ledger schema.

Two reserved event types represent typed semantic creation:

```text
sayf.record.created.v1
sayf.relation.created.v1
```

A graph is a deterministic replay projection of the complete locally verified event history. Projection state is disposable and reconstructible. Human-readable or future cached graph projections are not source-of-truth state.

The low-level generic event-store API remains capable of appending arbitrary event types. Normal CLI use refuses the two reserved M0.2 event names and directs callers through typed record/relation commands. If a privileged/library caller inserts a malformed reserved event, M0.1 may still regard the generic event chain as locally consistent, but M0.2 projection fails closed and refuses subsequent typed operations until the semantic history is handled explicitly.

## Immutable identity and provenance

For M0.2, a record or relation ID is exactly its immutable creation event ID:

```text
record.id   == record.creating_event_id
relation.id == relation.creating_event_id
```

The reserved creation event stream is bound to the same identity:

```text
record:<record_id>
relation:<relation_id>
```

This gives each object one unambiguous creation event and reuses the M0.1 `events.event_id` uniqueness constraint as the cross-process identity guard. Future revisions do not mutate a record under the same ID; M0.3 will model revision/supersession as new immutable state plus explicit relationships.

Every record and relation preserves:

- exact creating event ID;
- creating ledger sequence;
- creation time;
- creating actor;
- schema version;
- deterministic canonical content hash.

## Record envelope

```text
id
 type
schema_version
created_at
created_by
created_sequence
creating_event_id
content_hash
payload
```

M0.2 freezes the typed envelope and record-type vocabulary. Except for `Artifact`, concrete semantic payload schemas are intentionally not invented in this slice. Their payload is a canonical JSON object and later milestones may introduce type-specific contracts as those semantics become authoritative.

Record types:

### Discovery / semantic

`IdeaSeed`, `ProblemFrame`, `Claim`, `Assumption`, `Hypothesis`, `Evidence`, `Experiment`, `Observation`, `Requirement`, `Constraint`, `Decision`, `IntentRevision`.

### Engineering

`BaselineSnapshot`, `TaskGraph`, `Task`, `ExecutionAttempt`, `ChangeSet`, `VerificationReceipt`, `RiskAssessment`.

### Authority / lifecycle

`PolicySnapshot`, `GateRequest`, `GateDecision`, `Release`, `RuntimeObservation`, `FeedbackCase`.

### Provenance

`InteractionReceipt`, `Artifact`, `ExternalReference`.

The record `content_hash` is SHA-256 over canonical JSON containing the record type, record schema version, and record payload. It is independent of ledger sequence and actor provenance; the M0.1 event hash separately binds the full event envelope and ledger position.

## Relation envelope

```text
id
type
source_id
target_id
schema_version
created_at
created_by
created_sequence
creating_event_id
content_hash
metadata
```

Relations are directed and form a multigraph: multiple distinct relation IDs may connect the same source and target with the same relation type. M0.2 does not infer deduplication or semantic equivalence from endpoints alone.

Relation vocabulary:

- epistemic: `supports`, `contradicts`, `falsifies`, `assumes`, `derived_from`, `observes`;
- authority/versioning: `supersedes`, `accepts`, `rejects`, `governed_by`;
- engineering: `implements`, `depends_on`, `produces`, `modifies`, `verified_by`, `covers`;
- lifecycle: `permits`, `blocks`, `releases`, `violates`, `reopens`, `invalidates`;
- provenance: `created_from`, `discussed_in`, `references`.

M0.2 records these relationship claims; it does not yet assign effective-state or authority consequences to them. In particular, the presence of `supersedes` or `invalidates` does not itself implement M0.3 staleness semantics.

The relation `content_hash` is SHA-256 over canonical relation type, source ID, target ID, schema version, and metadata.

## Deterministic graph replay

`CausalGraph` consumes the complete authoritative history in contiguous ledger sequence order `1..N`.

Replay rules:

1. unknown non-reserved event types remain in sequence but do not create graph objects;
2. malformed reserved record/relation events make semantic projection fail closed;
3. every relation source and target must refer to a record already created earlier in the same history;
4. forward references are not permitted;
5. record/relation IDs are unique because they are ledger event IDs;
6. adjacency order is deterministic by creating ledger sequence and then object ID.

The graph supports outbound, inbound, and combined neighbor traversal. Self-loop relations appear once under combined traversal.

Path queries are explicit relationship paths, not inferred causal truth. They are cycle-safe and bounded by configurable controls with hard implementation ceilings:

```text
max_depth      <= 32
max_paths      <= 100
max_expansions <= 100000
```

Default expansion work is capped at 10,000 neighbor expansions. Exceeding a configured bound fails the query rather than silently returning an unbounded partial search.

## Content-addressed artifacts

Artifact bytes are stored separately from SQLite under a SHA-256 content-addressed layout:

```text
.sayf/objects/
  sha256/
    <first-two-hex>/
      <64-hex-digest>
```

An `Artifact` record payload is a typed descriptor:

```text
digest      # lowercase sha256:<64 hex>
size_bytes
media_type
name
metadata
```

The object store stages bytes, hashes them, flushes the staging file, and publishes to the digest-derived path. Existing valid identical objects are reused. An existing object at the expected digest path whose bytes do not match that digest is treated as invalid storage and is never silently overwritten by normal Sayf artifact publication.

Artifact verification streams the object bytes and recomputes SHA-256. Verified record lookup also checks that the observed byte length equals the immutable descriptor's `size_bytes`.

The CAS digest proves local byte identity, not external authenticity, source trust, or semantic correctness. Those claims require explicit records/evidence and later policy/gate semantics.

A CAS object is not authoritative semantic Sayf state merely because its bytes exist. It becomes referenced engineering state only when an immutable `Artifact` record has been appended to the authoritative ledger. If object publication succeeds but record registration later fails, the resulting unreferenced object is an orphan CAS blob, not an accepted Sayf record. Garbage collection is deferred.

POSIX publication fsyncs the staged content and containing object directory after the final rename. Windows uses a write-through file move for publication. M0.2 does not claim stronger filesystem durability than those operations establish, and it does not extend the M0.1 external-authenticity boundary.

## Current performance tradeoff

M0.2 graph operations replay `SQLiteEventStore.events()`, which verifies and materializes the complete local event history before building an in-memory graph. Typed writes validate the current semantic projection and then use the M0.1 append path, which independently re-verifies local history.

This is correctness-first and intentionally not optimized for high-throughput or very large ledgers. Persistent projections/checkpoints may be introduced later only as disposable, verifiable acceleration structures; they must never silently become an alternative source of truth.

## CLI surface

M0.2 adds:

```text
sayf record create
sayf record show
sayf record list
sayf relation create
sayf graph neighbors
sayf graph path
sayf artifact put
sayf artifact verify
```

Existing M0.1 `init` and `ledger` commands remain available.

## M0.2 invariants

1. **One semantic source of truth** — typed record/relation state is derived from immutable ledger events.
2. **Exact creation provenance** — every record/relation ID is its creation event ID.
3. **No dangling edges** — relation endpoints must already exist.
4. **No semantic pass on malformed reserved history** — typed replay fails closed.
5. **No inferred truth** — graph paths expose recorded relationships; they do not manufacture causal certainty.
6. **Bounded traversal** — graph path search has explicit depth/result/work limits.
7. **Content-addressed artifacts** — object addresses are derived from verified bytes.
8. **No silent CAS repair** — a corrupt object at an expected digest path is rejected, not overwritten.
9. **Artifact bytes are not authority by existence** — ledger registration provides semantic provenance.
10. **M0.1 remains authoritative** — M0.2 introduces no ledger-schema migration or second authoritative graph store.

## M0.2 exit criteria

M0.2 is complete when:

1. the planned M0 record vocabulary is represented by a versioned immutable record envelope;
2. the documented relation vocabulary is represented by a versioned immutable directed relation envelope;
3. record and relation IDs are creation-event IDs with deterministic canonical content hashes;
4. typed events replay deterministically from complete contiguous M0.1 history;
5. malformed reserved events, dangling endpoints, invalid stream binding, or content-hash mismatches fail projection closed;
6. arbitrary non-reserved events can coexist without becoming graph state;
7. outbound/inbound adjacency traversal is deterministic;
8. relationship-path queries are cycle-safe and bounded by depth, result-count, and expansion limits;
9. artifacts are addressed by verified SHA-256 bytes and immutable descriptors;
10. corrupted objects are detected on verification/read and are not silently replaced;
11. an `Artifact` record binds the digest, size, metadata, actor, and creating event into authoritative history;
12. raw CLI append cannot impersonate reserved typed record/relation events;
13. duplicate record/relation identities are rejected under concurrent creation by the M0.1 event-ID uniqueness boundary;
14. M0.1 behavior and tests remain green;
15. CI runs lint and the full suite on Python 3.12 under Ubuntu and Windows;
16. the projection, CAS authority boundary, performance cost, and deferred M0.3/M0.4 semantics are documented explicitly.
