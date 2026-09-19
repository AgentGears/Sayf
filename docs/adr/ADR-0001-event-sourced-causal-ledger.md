# ADR-0001 — Event-Sourced Causal Ledger

**Status:** Accepted

## Context

Sayf must preserve why engineering state exists and how later evidence changes confidence in or validity of downstream state. Reconstructing that history from mutable documents or chat transcripts would make provenance and supersession unreliable.

M0 also needs to remain local-first, inspectable, and small enough to reason about directly.

## Decision

Sayf will use an append-only event ledger as the authoritative history for M0.

For the initial implementation:

- SQLite stores the ordered ledger;
- event payloads are immutable after append;
- a global SHA-256 hash chain provides tamper evidence;
- database triggers reject event updates and deletes;
- current state is derived from events rather than stored as mutable semantic truth;
- larger immutable artifacts will use content-addressed filesystem storage in M0.2;
- human-readable Markdown/JSON files are projections, not authoritative state.

## Why SQLite

SQLite provides transactional local persistence, deterministic ordering, recursive query capability for later graph projections, broad portability, and a very small operational surface.

A graph database, distributed log, ORM, or event-streaming platform would add infrastructure before Sayf has proven its causal semantics.

## Consequences

### Positive

- complete local history can be replayed;
- historical mutation is visible;
- supersession and invalidation can be expressed without rewriting old state;
- adapters do not need an LLM to inspect authoritative history;
- later projections can be rebuilt from source events.

### Negative

- schema evolution must be explicit;
- projections must be rebuildable and version-aware;
- distributed multi-writer operation is deferred;
- cryptographic tamper evidence is not equivalent to an external trust anchor.

## Alternatives considered

### Mutable relational state only

Rejected because it makes historical reasoning and exact provenance harder to reconstruct.

### Git as the sole ledger

Rejected because Git is valuable provenance for source code but is not a sufficient structured authority store for claims, evidence, gates, observations, and derived state.

### Graph database first

Rejected because graph traversal is needed, but a graph database is not. SQLite adjacency tables and recursive CTEs are sufficient for M0.

### Distributed event platform

Deferred until there is evidence that local SQLite semantics are insufficient.

## Revisit trigger

Revisit the storage decision only when measured requirements demonstrate that single-node SQLite cannot meet required concurrency, durability, graph traversal, or deployment constraints without distorting the domain model.
