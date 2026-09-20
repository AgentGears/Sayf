# ADR-0001 — Event-Sourced Causal Ledger

**Status:** Accepted

## Context

Sayf must preserve why engineering state exists and how later evidence changes confidence in or validity of downstream state. Reconstructing that history from mutable documents or chat transcripts would make provenance and supersession unreliable.

M0 also needs to remain local-first, inspectable, and small enough to reason about directly.

## Decision

Sayf will use an append-only event ledger as the authoritative history for M0.

For the initial implementation:

- SQLite stores the ordered ledger;
- event payloads and actor metadata are normalized to JSON-native values before persistence;
- stored timestamps and JSON documents use a canonical representation;
- a versioned exact schema identifies a supported Sayf v1 ledger;
- SQLite `quick_check` validates the local database container before authoritative use;
- database triggers reject event updates, deletes, and replacement-insert collisions during normal access;
- first-use ownership is serialized **before the target ledger path is inspected** by an exclusive lock in a tiny non-authoritative SQLite coordination sidecar;
- coordination filenames use a fixed-length SHA-256 key of the normalized, case-folded resolved target path so lexical/case aliases share the same initialization barrier conservatively;
- recovery markers use a separate, stricter fixed-length target identity based on the resolved path with only operating-system-native path normalization, preventing a conservative coordination collision from authorizing recovery of a distinct case-sensitive target;
- before reserving a missing target, the lock owner creates that small non-authoritative pending-initialization marker, which survives independently of the target's schema transaction;
- after acquiring the barrier, the owner reserves the target path with exclusive file creation and creates the v1 schema inside a single SQLite transaction;
- an interrupted initialization is recoverable only when the target-specific marker exists and the target remains schema-less SQLite storage; arbitrary existing or user-schema-bearing databases remain fail-closed;
- a stale pending marker beside an already valid Sayf ledger is cleared after the ledger passes normal validation;
- a global unkeyed SHA-256 hash chain provides an internal chain-consistency check;
- verification checks contiguous `1..N` sequence state, canonical stored representations, hash linkage, and canonical event hashes;
- initialization, authoritative listing, and append fail closed when existing local history is invalid;
- append passes through the initialization barrier and then re-verifies the existing local ledger under the same immediate write transaction before extending it;
- read-only inspection and verification never initialize or mutate a missing database;
- current state is derived from events rather than stored as mutable semantic truth;
- larger immutable artifacts will use content-addressed filesystem storage in M0.2;
- human-readable Markdown/JSON files are projections, not authoritative state.

The coordination sidecar and pending marker contain no authoritative Sayf state. The sidecar exists only for cross-process first-use serialization. The marker records only that Sayf had begun creating one specific recovery identity under that barrier so a crash-interrupted schema-less target can be retried safely.

## Trust boundary

The M0.1 local hash chain is **not an authenticity proof** against an adversary with arbitrary write access to the SQLite database.

An actor who can bypass the database guards can modify, remove, insert, or renumber events and recompute the affected hash chain, yielding a self-consistent ledger that local verification cannot distinguish from an originally accepted history. The same trust limitation applies to truncating the current tail.

Therefore M0.1 establishes **local usability and consistency**, not externally anchored historical authenticity. It detects SQLite integrity failures surfaced by `quick_check`, unsupported or altered local schema, malformed records, noncanonical stored representations, sequence gaps, broken links, and content rewrites whose affected hashes were not recomputed consistently. It cannot prove that the current self-consistent ledger is the same ledger previously observed by an external party.

Authenticity after local-store compromise requires an externally anchored mechanism such as a signed checkpoint, independently stored chain head, replicated witness, transparency log, or equivalent trust anchor. That capability is deliberately deferred beyond M0.1.

## Why SQLite

SQLite provides transactional local persistence, deterministic ordering, recursive query capability for later graph projections, broad portability, and a very small operational surface. It also provides the cross-process locking primitive used by first-use coordination, avoiding another runtime dependency.

A graph database, distributed log, ORM, or event-streaming platform would add infrastructure before Sayf has proven its causal semantics.

## Consequences

### Positive

- complete locally valid history can be replayed;
- authoritative reads and writes fail closed on a locally invalid ledger;
- malformed rows, noncanonical storage, sequence gaps, unrecomputed content changes, and chain discontinuities are detected before authoritative use;
- unknown or damaged databases are not silently repaired or converted;
- first-use initialization does not depend on filesystem hard-link capability;
- concurrent first-use ownership is decided before any contender classifies the target path;
- case-insensitive path aliases conservatively share one first-use barrier;
- conservative lock-key collisions cannot cross-authorize recovery of distinct case-sensitive targets;
- a process/host interruption during first-use initialization can be retried when Sayf's target-specific pending marker proves the schema-less target came from an interrupted Sayf creation attempt;
- supersession and invalidation can later be expressed without rewriting old semantic state;
- adapters do not need an LLM to inspect authoritative history;
- later projections can be rebuilt from source events;
- read operations do not silently create authoritative state.

### Negative

- schema evolution must be explicit and versioned;
- projections must be rebuildable and version-aware;
- a small non-authoritative SQLite coordination sidecar and, transiently, a pending marker accompany first-use initialization;
- conservative case folding can serialize distinct case-sensitive paths that differ only by case, reducing initialization concurrency but not correctness;
- on a case-insensitive POSIX filesystem, recovery through a differently cased spelling may fail closed because recovery identity is intentionally stricter than lock identity;
- M0.1 append re-verifies the full existing event history, making each append O(N) in ledger length and a sequence of N appends O(N²) overall;
- SQLite integrity checking adds additional local I/O before authoritative use;
- distributed multi-writer operation is deferred;
- the local hash chain alone does not authenticate history after arbitrary local database compromise;
- malicious recomputation of a modified chain and ledger-tail truncation are not detectable without an external trust anchor;
- external checkpoint/signature/witness design is deferred.

The O(N) pre-append verification cost is intentional for M0.1: correctness and a simple authority boundary take precedence over throughput while the causal semantics are being proven. Any later optimization must preserve fail-closed behavior, for example through authenticated/validated checkpoints or another mechanism that does not silently trust an unverified head.

## Alternatives considered

### Mutable relational state only

Rejected because it makes historical reasoning and exact provenance harder to reconstruct.

### Git as the sole ledger

Rejected because Git is valuable provenance for source code but is not a sufficient structured authority store for claims, evidence, gates, observations, and derived state.

### Graph database first

Rejected because graph traversal is needed, but a graph database is not. SQLite adjacency tables and recursive CTEs are sufficient for M0.

### Distributed event platform

Deferred until there is evidence that local SQLite semantics are insufficient.

### Trusting the stored chain head on append

Rejected for M0.1 because schema-valid storage can still contain locally invalid history. Extending an unverified head would let corrupted local history become the basis for new authoritative state.

### Filesystem hard-link installation

Rejected because it makes otherwise-valid SQLite deployments depend on a filesystem capability that is not universally available.

### Target-ledger locking without pre-creation ownership

Rejected because SQLite creates an `rwc` target before the first target transaction can acquire its lock, leaving a race where a contender can mistake an in-progress target for pre-existing unknown storage. The coordination lock must therefore be acquired independently before the target path is inspected.

### One case-folded identity for both locking and recovery

Rejected because conservative case folding is useful for serialization on case-insensitive filesystems but can collapse distinct target names on case-sensitive filesystems. Reusing that equivalence class for recovery would allow one target's pending marker to authorize a different schema-less target. Lock identity and recovery identity are therefore separate.

### Treating all empty SQLite files as interrupted initialization

Rejected because that would silently legitimize unrelated pre-existing storage. Recovery requires Sayf's target-specific pending marker, created under the first-use coordination barrier, and still accepts only schema-less SQLite as a recoverable target.

### External checkpointing in M0.1

Deferred because M0.1 is proving the local event and provenance semantics first. A future milestone can add authenticated checkpoints without changing the meaning of historical events.

## Revisit trigger

Revisit the storage/trust design when measured requirements demonstrate that single-node SQLite or full pre-append verification cannot meet required concurrency, durability, graph traversal, deployment, or integrity requirements without distorting the domain model. Any requirement to authenticate history after arbitrary local-store compromise is an explicit trigger to add external checkpointing, signing, witnessing, or an equivalent trust mechanism.
