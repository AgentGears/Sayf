# M0.3 — Revision + Staleness

**Status:** implementation candidate

## Objective

M0.3 adds deterministic effective-state semantics to the immutable M0.1/M0.2 history without adding mutable status columns, projection tables, or another authority store.

The slice is intentionally narrow. It implements:

1. explicit qualification of dependency claims;
2. explicit record invalidation;
3. linear supersession lineage;
4. deterministic transitive staleness over qualified dependencies;
5. effective-state and affected-record queries sufficient to exercise the first revision/invalidation feedback loop.

Evidence sufficiency, policy snapshots, gates, releases, runtime-observation authority, and the general M0.5 `why`/`impact`/`timeline` surfaces remain deferred.

## Authority boundary: relation claim != state transition

M0.2 already allowed relations named `depends_on`, `invalidates`, and `supersedes`, but explicitly assigned them no effective-state consequences.

M0.3 does **not** retroactively reinterpret those historical relation claims as authority. Doing so would silently change the meaning of an existing immutable ledger.

Instead, M0.3 introduces three reserved qualification event types:

```text
sayf.dependency.bound.v1
sayf.record.invalidated.v1
sayf.record.superseded.v1
```

Each event binds one already-existing immutable relation by:

```text
schema_version
relation_id
relation_content_hash
```

The event stream is exactly:

```text
state:<relation_id>
```

The relation must already exist earlier in ledger history, its content hash must match exactly, and its relation type must match the qualification event:

- `sayf.dependency.bound.v1` -> `depends_on`;
- `sayf.record.invalidated.v1` -> `invalidates`;
- `sayf.record.superseded.v1` -> `supersedes`.

A relation can receive at most one M0.3 state activation. Malformed, mismatched, duplicate, forward-referencing, or self-loop state events make M0.3 semantic projection fail closed.

This preserves a strict distinction between recording a relationship claim and adopting that relationship into effective state.

## Effective-state axes

M0.3 deliberately avoids collapsing all lifecycle meaning into one status enum. Each record has three independent derived axes:

```text
validity:   valid | invalidated
revision:   current | superseded
freshness:  fresh | stale
```

A record may therefore be, for example, valid but stale, or invalidated and also superseded. The axes preserve the reason for state instead of hiding it behind a precedence rule.

### Validity

A qualified `invalidates` relation has direction:

```text
cause -> invalidated record
```

The target record becomes `invalidated`. Multiple independent invalidation causes are permitted and retained in deterministic event order.

### Revision

A qualified `supersedes` relation has direction:

```text
new revision -> old revision
```

M0.3 commits narrowly to linear revision lineages:

- source and target must be different records;
- source and target must have the same `RecordType`;
- one old record may have only one direct superseder;
- one new record may directly supersede only one old record;
- a record already superseded earlier in history cannot later become the source of a new supersession transition.

These rules keep the effective revision tip deterministic. Branch/merge revision semantics are deferred until a concrete use case requires them.

### Freshness

A qualified `depends_on` relation has direction:

```text
dependent -> dependency
```

Only **qualified** dependency relations participate in staleness. Unqualified M0.2 `depends_on` relations remain inert relationship claims.

An invalidated or superseded record is a staleness root. Staleness propagates in reverse over qualified dependency edges:

```text
A invalidated/superseded
^
| depends_on
B becomes stale
^
| depends_on
C becomes stale
```

The root record itself is not marked stale merely because it is invalidated or superseded; its direct axis records that transition. Dependents become stale because an input they rely on is no longer current/valid.

Propagation is deterministic and cycle-safe. For each root, the projection retains one canonical shortest dependency path to each affected record, using qualification sequence and relation ID as deterministic traversal order. M0.3 does not claim that this stored path is every possible causal path; full multi-path explainability remains an M0.5 concern.

## Semantic snapshot freshness

State activations use the same compare-and-append boundary introduced for M0.2 typed writes.

M0.3 first verifies and projects one complete semantic snapshot, validates the requested activation against that exact state, then appends only if the ledger event count and head hash are unchanged inside the `BEGIN IMMEDIATE` append transaction.

Any concurrent append forces revalidation. A state transition is therefore never committed against stale semantic state.

M0.3 semantic validation is also part of subsequent typed record/relation writes. A privileged low-level caller can still append a syntactically valid generic ledger event, but malformed reserved M0.3 state history blocks later semantic operations rather than being ignored.

## Authorization and trust boundary

The actor stored on a state-transition event is provenance, not an authorization credential. M0.3 does not add RBAC, policy evaluation, signatures, or a new permission subsystem.

In the local-first milestone, permission to invoke `bind_dependency`, `invalidate`, or `supersede` comes from the host/application boundary that controls access to Sayf. An agent-authored relation remains an inert claim until a caller with that transition capability explicitly qualifies it. The mere presence of agent text or an agent-created relation does not acquire effective-state authority by itself.

Policy-bound and evidence-bound authorization belongs to M0.4+. Adding an ad hoc M0.3 permission model would duplicate that future authority layer without solving the stronger arbitrary-database-writer trust boundary already documented by M0.1.

## Performance boundary

Effective-state projection is correctness-first. It reconstructs the fully verified ledger and typed graph, then derives staleness from the qualified dependency graph.

For each invalidated or superseded root, current derivation may traverse the qualified dependency graph once. In the worst case this is proportional to the number of roots multiplied by graph size, in addition to the existing complete-history verification/replay cost. `CausalRepository` also validates M0.3 state before subsequent semantic reads/writes, so large histories will eventually require measured optimization.

M0.3 intentionally does not add persistent mutable projections, caches, or incremental invalidation tables yet. Those should be introduced only after measurement establishes a forcing function and only as disposable, verifiable acceleration structures that cannot become a second authority store.

## Query surface

M0.3 adds:

```text
sayf state bind-dependency <relation_id>
sayf state invalidate <relation_id>
sayf state supersede <relation_id>
sayf state show <record_id>
sayf state affected <record_id>
```

`state show` returns the three effective-state axes plus transition provenance and canonical stale-cause paths.

`state affected` is intentionally narrower than the future M0.5 `impact` query. It returns records currently stale because the requested record is an invalidated or superseded staleness root. It does not claim to enumerate every semantic, policy, release, or runtime consequence.

Raw `ledger append` refuses the three reserved M0.3 event names. A privileged library caller can still write them, but semantic replay validates them fail-closed.

## Acceptance slice

The first M0.3 feedback slice is:

```text
Assumption A1
    ^
    | qualified depends_on
Intent I1

Observation O1 --qualified invalidates--> A1

A1 validity = invalidated
I1 freshness = stale
state affected A1 => I1 with dependency path I1 -> A1

Intent I2 --qualified supersedes--> I1

I1 revision = superseded
I2 revision = current
```

This is deliberately smaller than the eventual release-feedback scenario. It proves that Sayf can distinguish a recorded relationship from an adopted dependency, invalidate a premise, propagate deterministic downstream staleness, and supersede the affected intent without rewriting history.

## M0.3 invariants

1. **No retroactive authority** — pre-existing M0.2 relation labels do not acquire M0.3 effects without a qualification event.
2. **Exact relation binding** — a state event binds both relation ID and immutable relation content hash.
3. **No forward state references** — a relation must already exist before it can be qualified.
4. **Independent state axes** — validity, revision, and freshness remain separately explainable.
5. **Only qualified dependencies propagate staleness** — generic graph adjacency is not silently treated as authority.
6. **Deterministic propagation** — the same verified history yields the same affected set and canonical stale-cause paths.
7. **Linear supersession for M0.3** — ambiguous branching/merge revision lineages fail closed rather than being guessed.
8. **Semantic snapshot freshness** — activation commits only against the exact state snapshot that was validated.
9. **Malformed reserved state history fails closed** — subsequent semantic operations do not proceed through poisoned M0.3 history.
10. **No mutable authority store** — all effective state remains a disposable projection of immutable ledger history.
11. **Actor provenance is not authorization** — transition authority is supplied by the host boundary until policy-bound authority exists.
12. **Optimization requires evidence** — replay/projection acceleration must not silently become another source of truth.

## Exit criteria

M0.3 is complete when:

1. M0.2 `depends_on`, `invalidates`, and `supersedes` relations remain inert until explicitly qualified;
2. qualification events bind an earlier relation ID and exact content hash;
3. invalidation preserves direct cause/relation/event provenance;
4. supersession preserves direct superseder/relation/event provenance;
5. same-type linear supersession is enforced deterministically;
6. invalidated and superseded roots make qualified transitive dependents stale;
7. affected records carry a deterministic dependency path back to the stale root;
8. malformed, duplicate, mismatched, self-loop, or forward-referencing state activations fail closed;
9. state activation rejects concurrent ledger-head changes after semantic validation;
10. raw CLI append cannot impersonate reserved M0.3 state events;
11. the A1 -> I1 -> invalidation -> staleness -> I2 acceptance slice passes end to end;
12. all M0.1/M0.2 tests remain green on Ubuntu and Windows;
13. authorization and performance boundaries are documented without introducing premature policy or projection infrastructure.
