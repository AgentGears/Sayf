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
- **M0.2 Typed Records + Graph** — records, relations, artifacts, traversal.
- **M0.3 Revision + Staleness** — supersession and dependency invalidation.
- **M0.4 Evidence + Gates** — verification receipts, policy snapshots, gate decisions.
- **M0.5 Feedback + Explainability** — runtime observations, feedback cases, `why`, `impact`, and `timeline`.

The current codebase implements **M0.1**.

## M0.1 quick start

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

sayf init .
sayf ledger append \
  --type RecordCreated \
  --stream project:demo \
  --actor-kind human \
  --actor-id local-user \
  --payload '{"record_id":"r1"}'

sayf ledger show
sayf ledger verify
```

M0.1 accepts JSON-native event payload and actor metadata values only. Non-finite numbers, non-JSON Python objects, invalid UTF-8 text, duplicate CLI JSON keys, and non-standard JSON constants are rejected before authoritative state is created.

A ledger is considered locally usable only after three layers pass: SQLite `quick_check`, exact supported v1 schema/metadata identity, and canonical event-history verification. Stored JSON and timestamps must retain their canonical representation, sequence numbers must be contiguous from `1..N`, and every event hash/link must recompute correctly.

`sayf init` creates a new ledger or validates the complete local usability of an existing one; it does not silently repair, migrate, or convert an unknown, damaged, or history-invalid database. Before inspecting the target ledger path, first-use initialization acquires an exclusive lock in a tiny non-authoritative SQLite coordination sidecar. The lock owner then reserves the target path with exclusive file creation and creates the schema in one SQLite transaction. Concurrent initializers therefore cannot misclassify another Sayf process's in-progress target as pre-existing storage, and the mechanism does not require filesystem hard-link support. `sayf ledger show` returns only locally verified history. `sayf ledger append` passes through the same initialization barrier before re-verifying the existing ledger inside its immediate write transaction, so new authoritative state is never appended to a locally invalid chain.

At the database level, M0.1 rejects direct historical `UPDATE` and `DELETE` operations and rejects replacement-style inserts that collide with an existing sequence, event ID, or event hash. These are local storage guards, not a cryptographic authenticity boundary against an actor able to alter the schema itself.

That correctness-first append policy is deliberately O(N) in current ledger length for M0.1. It is suitable for proving the causal semantics, but a later optimization will be needed before high-throughput use. Any such optimization must preserve fail-closed verification, for example through authenticated or otherwise validated checkpoints rather than blindly trusting a stored head.

M0.1 does **not** claim cryptographic authenticity against an adversary with arbitrary write access to the database: such an actor can rewrite records and recompute every affected hash, renumber history, or truncate the ledger tail while leaving a self-consistent local ledger. Detecting that class of attack requires an external checkpoint, signature, replicated witness, transparency log, or equivalent trust anchor. ADR-0001 records this boundary explicitly.

## Design invariants

1. **No silent mutation** — accepted historical state is superseded, never rewritten.
2. **No missing provenance** — durable state identifies the actor and event that created it.
3. **No unsupported authority** — an agent assertion is not authoritative merely because a model produced it.
4. **No stale reuse** — authoritative decisions bind exact versions of their inputs.
5. **No hidden downstream impact** — invalidated premises must be traceable to affected decisions and releases.
6. **Unknown is not pass** — missing evidence is not successful evidence.
7. **Unknown storage is fail-closed** — existing authority stores are validated before authoritative read or mutation.

## Development

```bash
python -m pip install -e '.[dev]'
ruff check .
pytest
```

CI executes the suite on Python 3.12 under both Ubuntu and Windows.

See [`docs/architecture/VISION.md`](docs/architecture/VISION.md), [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md), and [`docs/architecture/M0_CAUSAL_LEDGER.md`](docs/architecture/M0_CAUSAL_LEDGER.md).

## License

MIT
