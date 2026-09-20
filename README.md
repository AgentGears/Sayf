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
- **M0.3 Revision + Staleness** — supersession and dependency invalidation.
- **M0.4 Evidence + Gates** — verification receipts, policy snapshots, gate decisions.
- **M0.5 Feedback + Explainability** — runtime observations, feedback cases, `why`, `impact`, and `timeline`.

The current codebase implements **M0.1 and M0.2**. Revision/staleness effects and gate authority are deliberately not implemented yet.

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
  --type supports \
  --source rec_assumption \
  --target rec_intent \
  --id rel_support

sayf graph path rec_assumption rec_intent

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

Normal raw CLI append refuses the reserved `sayf.record.created.v1` and `sayf.relation.created.v1` event names. If a privileged low-level caller nevertheless writes malformed reserved semantic history, M0.2 projection rejects it rather than treating malformed typed state as valid.

Graph neighbor queries support outbound, inbound, and combined traversal. Relationship-path searches are explicit, cycle-safe, and bounded by depth, result count, and total neighbor expansion so a query cannot silently become unbounded work. Paths expose recorded relationships; they do not infer truth or causal certainty beyond those explicit edges.

Artifacts use SHA-256 content-addressed storage under `.sayf/objects/sha256/...`. Object bytes are verified against the digest, and an existing corrupt object at the expected address is rejected rather than silently overwritten. An object becomes referenced Sayf engineering state only when an immutable `Artifact` record binds its digest, byte length, metadata, actor, and creating event into the ledger. Unreferenced CAS bytes are not authoritative records.

Except for the typed `Artifact` descriptor, M0.2 intentionally keeps individual record payloads as canonical JSON objects. Revision semantics, supersession effects, staleness propagation, evidence sufficiency, policy, and gates remain M0.3/M0.4 responsibilities.

M0.2 currently reconstructs the graph from the fully verified event history for each operation. This is a correctness-first O(N) replay/materialization tradeoff, not a high-throughput design. Future persistent projections or checkpoints may accelerate replay only if they remain verifiable and disposable rather than becoming another source of truth.

See [`docs/architecture/M0_2_TYPED_RECORDS_GRAPH.md`](docs/architecture/M0_2_TYPED_RECORDS_GRAPH.md) for the frozen M0.2 contract and trust boundaries.

## Design invariants

1. **No silent mutation** — accepted historical state is superseded, never rewritten.
2. **No missing provenance** — durable state identifies the actor and event that created it.
3. **No unsupported authority** — an agent assertion is not authoritative merely because a model produced it.
4. **No stale reuse** — authoritative decisions bind exact versions of their inputs.
5. **No hidden downstream impact** — invalidated premises must be traceable to affected decisions and releases.
6. **Unknown is not pass** — missing evidence is not successful evidence.
7. **Unknown storage is fail-closed** — existing authority stores are validated before authoritative read or mutation.
8. **Derived graph state is disposable** — authoritative typed history remains in immutable ledger events.

## Development

```bash
python -m pip install -e '.[dev]'
ruff check .
pytest
```

CI executes the suite on Python 3.12 under both Ubuntu and Windows.

See [`docs/architecture/VISION.md`](docs/architecture/VISION.md), [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md), [`docs/architecture/M0_CAUSAL_LEDGER.md`](docs/architecture/M0_CAUSAL_LEDGER.md), and [`docs/architecture/M0_2_TYPED_RECORDS_GRAPH.md`](docs/architecture/M0_2_TYPED_RECORDS_GRAPH.md).

## License

MIT
