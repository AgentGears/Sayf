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
- exact schema validation excludes only objects whose names begin with SQLite's literal reserved `sqlite_` prefix; lookalike user names such as `sqlitex` remain visible to validation;
- database triggers reject event updates, deletes, and replacement-insert collisions during normal access;
- first-use ownership is serialized **before the target ledger path is inspected** by an exclusive lock in a tiny non-authoritative SQLite coordination sidecar;
- coordination filenames use a fixed-length SHA-256 key of the normalized, case-folded resolved target path so lexical/case aliases share the same initialization barrier conservatively;
- recovery markers use a separate, stricter fixed-length target identity based on the resolved path with only operating-system-native path normalization, preventing a conservative coordination collision from authorizing recovery of a distinct case-sensitive target;
- before reserving a missing target, the lock owner writes the recovery token to a same-directory staging file, flushes and fsyncs it, then atomically publishes the target-specific pending-initialization marker; POSIX fsyncs the containing directory, while Windows uses a write-through rename so the marker name transition is durable before target reservation; target reservation occurs only after the platform durability step succeeds;
- after acquiring the barrier and durably publishing the marker, the owner reserves the target path with exclusive file creation and creates the v1 schema inside a single SQLite transaction;
- an interrupted initialization is recoverable only when the target-specific marker exists and the target remains schema-less SQLite storage; arbitrary existing or user-schema-bearing databases remain fail-closed;
- recovery authorization is durably revoked before initialization reports success: POSIX removes the marker and fsyncs its parent directory, while Windows performs a write-through rename from the authoritative marker name to a non-authoritative revoked tombstone before best-effort tombstone cleanup; failure of the platform revocation step makes initialization fail, and retry must re-establish durable marker absence before it can report success;
- a global unkeyed SHA-256 hash chain provides an internal chain-consistency check;
- verification checks contiguous `1..N` sequence state, canonical stored representations, hash linkage, and canonical event hashes;
- authoritative read operations hold one explicit SQLite read transaction from `quick_check` and exact schema/metadata validation through event-history consumption, so the usability result describes one database snapshot;
- initialization, authoritative listing, and append fail closed when existing local history is invalid;
- append passes through the initialization barrier and then re-verifies the existing local ledger under the same immediate write transaction before extending it;
- read-only inspection and verification never initialize or mutate a missing database;
- current state is derived from events rather than stored as mutable semantic truth;
- larger immutable artifacts will use content-addressed filesystem storage in M0.2;
- human-readable Markdown/JSON files are projections, not authoritative state.

The coordination sidecar and pending marker contain no authoritative Sayf state. The sidecar exists only for cross-process first-use serialization. The marker records only that Sayf had begun creating one specific recovery identity under that barrier so a crash-interrupted schema-less target can be retried safely. A partially written staging file is not a recovery authorization token; only the fully published final marker is recognized. On POSIX, the marker rename is not considered durably published until the containing directory has also been fsynced, and marker deletion is not considered durably revoked until the directory has been fsynced after unlink. On Windows, publication and revocation use write-through rename operations; revocation moves the authoritative marker name to a non-authoritative tombstone so a surviving tombstone can never authorize recovery. A committed ledger does not make initialization successful while recovery authorization remains uncertain.

## Trust boundary

The M0.1 local hash chain is **not an authenticity proof** against an adversary with arbitrary write access to the SQLite database.

An actor who can bypass the database guards can modify, remove, insert, or renumber events and recompute the affected hash chain, yielding a self-consistent ledger that local verification cannot distinguish from an originally accepted history. The same trust limitation applies to truncating the current tail.

Therefore M0.1 establishes **local usability and consistency**, not externally anchored historical authenticity. It detects SQLite integrity failures surfaced by `quick_check`, unsupported or altered local schema, malformed records, noncanonical stored representations, sequence gaps, broken links, and content rewrites whose affected hashes were not recomputed consistently. A successful read-side result describes one transactionally consistent SQLite snapshot. It cannot prove that the current self-consistent ledger is the same ledger previously observed by an external party.

Authenticity after local-store compromise requires an externally anchored mechanism such as a signed checkpoint, independently stored chain head, replicated witness, transparency log, or equivalent trust anchor. That capability is deliberately deferred beyond M0.1.

## Why SQLite

SQLite provides transactional local persistence, deterministic ordering, recursive query capability for later graph projections, broad portability, and a very small operational surface. It also provides the cross-process locking primitive used by first-use coordination and snapshot isolation for read-side validation, avoiding another runtime dependency.

A graph database, distributed log, ORM, or event-streaming platform would add infrastructure before Sayf has proven its causal semantics.

## Consequences

### Positive

- complete locally valid history can be replayed;
- authoritative reads and writes fail closed on a locally invalid ledger;
- authoritative read validation and history consumption describe one SQLite snapshot rather than a mix of concurrently committed states;
- malformed rows, noncanonical storage, sequence gaps, unrecomputed content changes, and chain discontinuities are detected before authoritative use;
- unknown or damaged databases are not silently repaired or converted;
- first-use initialization does not depend on filesystem hard-link capability;
- concurrent first-use ownership is decided before any contender classifies the target path;
- case-insensitive path aliases conservatively share one first-use barrier;
- conservative lock-key collisions cannot cross-authorize recovery of distinct case-sensitive targets;
- the recovery token is fully published before target reservation, using a parent-directory fsync on POSIX or a write-through rename on Windows before a recoverable target may appear;
- successful initialization also durably revokes the recovery token, preventing stale recovery authorization from surviving a completed initialization;
- a process/host interruption after durable marker publication during first-use initialization can be retried when Sayf's target-specific pending marker proves the schema-less target came from an interrupted Sayf creation attempt;
- schema lookalikes such as `sqlitex` cannot evade exact-schema or interrupted-recovery validation by matching a wildcard approximation of `sqlite_`;
- supersession and invalidation can later be expressed without rewriting old semantic state;
- adapters do not need an LLM to inspect authoritative history;
- later projections can be rebuilt from source events;
- read operations do not silently create authoritative state.

### Negative

- schema evolution must be explicit and versioned;
- projections must be rebuildable and version-aware;
- a small non-authoritative SQLite coordination sidecar and, transiently, a pending marker accompany first-use initialization;
- durable marker publication and revocation require platform-specific filesystem operations: parent-directory fsync on POSIX and write-through rename transitions on Windows;
- a ledger can already be locally valid while `init` still reports failure if marker revocation durability cannot be established; retry is required before initialization is considered successful;
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

### Direct in-place creation of the pending marker

Rejected because an interruption during a direct write can leave a truncated or malformed final marker that wedges retry. The recovery token is staged in the same directory, flushed and fsynced, then atomically published: POSIX fsyncs the containing directory before target reservation, while Windows uses a write-through rename for the final name transition.

### Best-effort recovery-marker cleanup

Rejected because a failed or non-durable unlink can leave or resurrect stale recovery authorization. Marker revocation is therefore part of initialization success: POSIX requires deletion plus parent-directory fsync; Windows requires a write-through rename from the authoritative marker to a non-authoritative revoked tombstone before optional tombstone cleanup. If an earlier revocation step failed or its durable outcome is uncertain, a later successful initialization must first durably establish the marker's absence.

### Autocommit read validation followed by a separate history scan

Rejected because a concurrent direct writer can commit between schema validation and event consumption, causing one authoritative operation to mix states from different SQLite snapshots. Read-side validation and history consumption therefore remain inside one explicit read transaction.

### Treating all empty SQLite files as interrupted initialization

Rejected because that would silently legitimize unrelated pre-existing storage. Recovery requires Sayf's target-specific pending marker, created under the first-use coordination barrier, and still accepts only schema-less SQLite as a recoverable target.

### External checkpointing in M0.1

Deferred because M0.1 is proving the local event and provenance semantics first. A future milestone can add authenticated checkpoints without changing the meaning of historical events.

## Revisit trigger

Revisit the storage/trust design when measured requirements demonstrate that single-node SQLite or full pre-append verification cannot meet required concurrency, durability, graph traversal, deployment, or integrity requirements without distorting the domain model. Any requirement to authenticate history after arbitrary local-store compromise is an explicit trigger to add external checkpointing, signing, witnessing, or an equivalent trust mechanism.
