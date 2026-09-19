# ADR-0001 — Event-Sourced Causal Ledger

**Status:** Accepted

## Context

Sayf must preserve why engineering state exists and how later evidence changes confidence in or validity of downstream state. Reconstructing that history from mutable documents or chat transcripts would make provenance and supersession unreliable.

M0 also needs to remain local-first, inspectable, and small enough to reason about directly.

## Decision

Sayf will use an append-only event ledger as the authoritative history for M0.

For the initial implementation:

- SQLite stores the ordered ledger;
- event payloads are immutable after append through database guards;
- a global unkeyed SHA-256 hash chain provides an internal chain-consistency check;
- independent verification detects malformed rows, broken links, and content rewrites when stored hashes were not recomputed consistently;
- database triggers reject event updates and deletes;
- read-only inspection and verification never initialize or mutate a missing database;
- current state is derived from events rather than stored as mutable semantic truth;
- larger immutable artifacts will use content-addressed filesystem storage in M0.2;
- human-readable Markdown/JSON files are projections, not authoritative state.

## Trust boundary

The M0.1 local hash chain is **not an authenticity proof** against an adversary with arbitrary write access to the SQLite database.

An actor who can bypass the database guards can modify one or more events and recompute the modified event hash plus every descendant `previous_event_hash`/`event_hash`, yielding a self-consistent chain that local verification cannot distinguish from the original. The same trust limitation applies to truncating the current tail.

Therefore M0.1 guarantees only that the stored ledger is internally self-consistent with its own hashes and schema. It can detect accidental corruption, malformed records, unrecomputed rewrites, and broken chain links. It cannot prove that the current self-consistent ledger is the same ledger previously observed by an external party.

Authenticity after local-store compromise requires an externally anchored mechanism such as a signed checkpoint, independently stored chain head, replicated witness, transparency log, or equivalent trust anchor. That capability is deliberately deferred beyond M0.1.

## Why SQLite

SQLite provides transactional local persistence, deterministic ordering, recursive query capability for later graph projections, broad portability, and a very small operational surface.

A graph database, distributed log, ORM, or event-streaming platform would add infrastructure before Sayf has proven its causal semantics.

## Consequences

### Positive

- complete local history can be replayed;
- malformed rows, unrecomputed content changes, and interior chain discontinuities are detectable by independent verification;
- supersession and invalidation can be expressed without rewriting old state;
- adapters do not need an LLM to inspect authoritative history;
- later projections can be rebuilt from source events;
- read operations do not silently create authoritative state.

### Negative

- schema evolution must be explicit;
- projections must be rebuildable and version-aware;
- distributed multi-writer operation is deferred;
- the local hash chain alone does not authenticate history after arbitrary local database compromise;
- malicious recomputation of a modified chain and ledger-tail truncation are not detectable without an external trust anchor;
- external checkpoint/signature/witness design is deferred.

## Alternatives considered

### Mutable relational state only

Rejected because it makes historical reasoning and exact provenance harder to reconstruct.

### Git as the sole ledger

Rejected because Git is valuable provenance for source code but is not a sufficient structured authority store for claims, evidence, gates, observations, and derived state.

### Graph database first

Rejected because graph traversal is needed, but a graph database is not. SQLite adjacency tables and recursive CTEs are sufficient for M0.

### Distributed event platform

Deferred until there is evidence that local SQLite semantics are insufficient.

### External checkpointing in M0.1

Deferred because M0.1 is proving the local event and provenance semantics first. A future milestone can add authenticated checkpoints without changing the meaning of historical events.

## Revisit trigger

Revisit the storage/trust design when measured requirements demonstrate that single-node SQLite cannot meet required concurrency, durability, graph traversal, deployment, or integrity requirements without distorting the domain model. Any requirement to authenticate history after arbitrary local-store compromise is an explicit trigger to add external checkpointing, signing, witnessing, or an equivalent trust mechanism.
