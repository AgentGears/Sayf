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

- **M0.1 Immutable Ledger** — append-only events, actors, hash chain, SQLite persistence, replay/verification.
- **M0.2 Typed Records + Graph** — records, relations, artifacts, traversal.
- **M0.3 Revision + Staleness** — supersession and dependency invalidation.
- **M0.4 Evidence + Gates** — verification receipts, policy snapshots, gate decisions.
- **M0.5 Feedback + Explainability** — runtime observations, feedback cases, `why`, `impact`, and `timeline`.

This branch implements **M0.1**.

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

The SQLite ledger rejects event updates and deletes at the database layer. `sayf ledger verify` independently recomputes the canonical local hash chain to check **internal chain consistency**. It detects corruption or rewrites when the stored hashes were not recomputed consistently.

M0.1 does **not** claim cryptographic authenticity against an adversary with arbitrary write access to the database: such an actor can rewrite records and recompute every affected hash, or truncate the ledger tail, while leaving a self-consistent local chain. Detecting that class of attack requires an external checkpoint, signature, replicated witness, or equivalent trust anchor. ADR-0001 records this boundary explicitly.

Read-only commands such as `sayf ledger show` and `sayf ledger verify` require an existing initialized ledger; they do not create one as a side effect.

## Design invariants

1. **No silent mutation** — accepted historical state is superseded, never rewritten.
2. **No missing provenance** — durable state identifies the actor and event that created it.
3. **No unsupported authority** — an agent assertion is not authoritative merely because a model produced it.
4. **No stale reuse** — authoritative decisions bind exact versions of their inputs.
5. **No hidden downstream impact** — invalidated premises must be traceable to affected decisions and releases.
6. **Unknown is not pass** — missing evidence is not successful evidence.

## Development

```bash
python -m pip install -e '.[dev]'
ruff check .
pytest
```

See [`docs/architecture/VISION.md`](docs/architecture/VISION.md), [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md), and [`docs/architecture/M0_CAUSAL_LEDGER.md`](docs/architecture/M0_CAUSAL_LEDGER.md).

## License

MIT
