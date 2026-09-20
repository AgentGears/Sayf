# M0.4 Design Decisions

Working design notes for the implementation candidate.

## D-01 — Gate subject is ChangeSet in v1

M0.4 v1 gates an exact `ChangeSet` rather than introducing a generic gate target ontology. This is the narrowest path toward later release justification and avoids silently defining semantics for gating intent, policy, or arbitrary records.

## D-02 — Policy is deterministic sufficiency, not a policy language

A policy snapshot contains ordered verification requirements (`contract`, `minimum_passes`). M0.4 does not embed expressions, scripts, arbitrary predicates, or external policy runtimes.

## D-03 — Actor provenance is not authentication

Receipt/policy/request/decision actors remain provenance. M0.4 does not use actor IDs as secure authorization credentials. Invocation authority remains the local host/application boundary.

## D-04 — Requests are immutable evidence sets

A request binds an immutable subject, policy snapshot, and receipt set. New evidence requires a new request. This avoids hidden mutation and makes the decision fully replayable.

## D-05 — One request, one decision

An immutable request may receive at most one M0.4 decision. Input changes stale the decision; they do not cause a second contradictory decision for the same request. Re-evaluation requires a new request.

## D-06 — Historical evaluation is independently recomputed

Replay reconstructs M0.3 effective state immediately before each decision and recomputes the gate evaluation. A syntactically valid privileged `GateDecision` record is not trusted by shape alone.

## D-07 — Effective state is fingerprinted, not reduced to three labels

Each evaluated input binds a hash of the complete `RecordEffectiveState`, not only validity/revision/freshness. A new invalidation cause or changed staleness path therefore invalidates the old gate decision even when the coarse labels remain unchanged.

## D-08 — BLOCK can be fresh

Decision freshness describes whether evaluation inputs changed. It is independent from the permit/block result. A decision that correctly blocked an already-invalidated input is fresh until that bound state changes.

## D-09 — Release remains outside M0.4

`PERMIT` is policy-bounded gate authority only. It is not release registration or release authority. `Release` remains reserved for the later milestone contract.
