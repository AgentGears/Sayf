# M0.4 — Evidence + Gates

Status: implementation candidate

## 1. Objective

M0.4 adds the first deterministic policy authority above M0.3 revision/staleness while preserving the existing distinction between evidence, policy, and authority.

The narrow slice is:

```text
ChangeSet
  + VerificationReceipt(s)
  + PolicySnapshot
        |
        v
    GateRequest
        |
        v
 deterministic evaluation
        |
        v
 GateDecision (permit | block)
        |
        v
 stale when a bound effective-state input changes
```

A gate decision is not release authority. `Release` remains deferred to M0.5.

## 2. Claim ceilings

M0.4 does not promote evidence existence into truth or a gate permit into global approval.

- `VerificationReceipt(result=pass)` means the named verification contract produced a pass result for the exact bound subject using the exact bound evidence, under the recorded environment and limitations.
- `VerificationReceipt(result=partial)` may preserve a bounded partial verified claim, but it does not satisfy an M0.4 v1 policy requirement.
- `GateDecision(outcome=permit)` means only that the exact gate request satisfied the exact bound policy under the exact effective-state fingerprints captured at evaluation.
- `GateDecision(outcome=block)` means the exact request did not satisfy that policy at evaluation. It does not mean the subject is incorrect.
- `GateDecision(fresh)` means the bound effective-state inputs are unchanged since evaluation. It does not mean they were all usable or positive.
- `minimum_passes` counts distinct qualifying receipt records. It does not establish verifier independence, evidence independence, statistical independence, or diversity of underlying evidence.
- No M0.4 state means releasable, safe, correct, complete, or approved outside the bound policy contract.

## 3. Authority boundary

The immutable ledger remains authoritative. M0.4 semantic records are stored as normal typed record-creation events but cannot be created through the generic `record create` surface.

The following record types now have implemented contracts and dedicated semantic APIs:

- `VerificationReceipt`
- `PolicySnapshot`
- `GateRequest`
- `GateDecision`

`RiskAssessment`, `Release`, `RuntimeObservation`, and `FeedbackCase` remain reserved.

The actor on an M0.4 record is provenance, not a cryptographically authenticated credential. Local invocation authority still comes from the host/application boundary. M0.4 policy snapshots constrain deterministic evidence sufficiency; they do not pretend that actor IDs are authenticated identities.

### Upgrade trust boundary

M0.4 adoption assumes the input ledger was semantically valid under the immediately preceding milestone before M0.4 semantics are enabled. Under the supported M0.3 API surface, the four M0.4 record types were mechanically reserved and therefore could not appear in a valid M0.3 semantic history.

Sayf does not currently carry an external signed runtime-version witness that can prove which software version created every historical event. A caller that bypassed the supported M0.3 semantic APIs and inserted a future-shaped low-level event was already operating inside the documented local privileged-write boundary. M0.4 does not add a heavyweight storage-schema migration solely to authenticate that historical runtime provenance. If cross-version provenance becomes a trust requirement, it needs an explicit migration/checkpoint contract rather than inference from type names.

This boundary does not permit forward references inside M0.4 records: every M0.4 cross-record binding still has to reference content that already existed earlier in the immutable ledger.

## 4. Exact record bindings

M0.4 never binds another record by ID alone. Cross-record references use:

```text
record_id
content_hash
```

A semantic binding fails closed when the record does not exist, the immutable content hash differs, or the bound record appears later in ledger history than the record that claims to bind it.

This prevents a later record from retroactively satisfying an earlier verification, request, or decision.

## 5. Verification receipts

A `VerificationReceipt` contains:

- exact subject binding;
- verification contract name;
- result: `pass | fail | partial | inconclusive`;
- one or more exact evidence-record bindings;
- non-empty environment description;
- limitations;
- independence classification;
- bounded verified claim when applicable;
- prohibited generalizations.

A `pass` requires a non-empty `verified_claim`. `fail` and `inconclusive` may not assert a verified claim. A `partial` receipt may preserve a bounded partial claim but remains non-passing for v1 gate sufficiency.

Receipt creation records evidence; it does not itself grant gate authority. A receipt may remain historically meaningful even when its subject/evidence is already stale or invalidated. Gate evaluation handles current usability separately.

## 6. Policy snapshots

M0.4 v1 intentionally does not introduce a general-purpose policy language.

A `PolicySnapshot` contains a non-empty ordered list of verification requirements:

```text
contract
minimum_passes >= 1
```

Contract names must be unique within one policy snapshot. `minimum_passes` counts distinct usable PASS receipt records for that contract. M0.4 v1 does not infer independence from different receipt IDs, different actors, or different evidence records.

A policy snapshot is immutable. Changing policy means creating a new snapshot and explicitly superseding/invalidating the old snapshot when that old authority should stop being reusable.

## 7. Gate requests

M0.4 v1 gates an exact `ChangeSet`.

A `GateRequest` binds:

- one exact `ChangeSet` subject;
- one exact `PolicySnapshot`;
- zero or more exact `VerificationReceipt` records.

Every bound receipt must verify the same exact subject binding as the request. A request may contain insufficient, failed, partial, inconclusive, or stale evidence; this is intentional because a deterministic BLOCK decision should be able to preserve the failed gate attempt rather than preventing the attempt from being recorded.

## 8. Deterministic evaluation

Evaluation is fail-closed.

The evaluator considers these records as bound inputs:

1. the gate request;
2. the subject;
3. the policy snapshot;
4. every receipt in request order;
5. every evidence record referenced by those receipts, preserving first occurrence.

A bound input is usable only when its M0.3 effective state is:

```text
validity:  not_invalidated
revision:  current
freshness: fresh
```

For M0.4 v1:

- every bound non-pass receipt adds a blocking reason;
- stale/invalidated/superseded bound inputs add blocking reasons;
- only usable `pass` receipts count toward policy requirements;
- every policy requirement must reach `minimum_passes`;
- absence of evidence never becomes pass.

If there are no blocking reasons, the outcome is `permit`; otherwise it is `block`.

Reasons, satisfied requirements, missing requirements, and evaluated input order are deterministic.

## 9. Historical decision verification

`GateDecision` is not trusted merely because a privileged caller managed to append a syntactically valid record.

During replay, Sayf reconstructs the M0.3 effective state at the ledger prefix immediately before each gate decision and recomputes the deterministic evaluation of the bound request. The stored `GateDecision` payload must exactly equal that recomputed result.

A forged permit, altered reason set, or mismatched input snapshot poisons semantic replay fail-closed.

A gate request may have at most one M0.4 decision. Re-evaluation after inputs change requires a new `GateRequest`; M0.4 does not create contradictory decisions for one immutable request.

## 10. Gate invalidation / staleness

A decision stores an evaluated-input binding for every record used by evaluation:

```text
record_id
content_hash
effective_state_hash
```

`effective_state_hash` is the SHA-256 hash of the complete canonical M0.3 `RecordEffectiveState`, including invalidation/supersession provenance and stale causes.

Current gate freshness is derived by recomputing these bindings:

- unchanged bindings => gate decision is `fresh`;
- any changed content/effective-state binding => gate decision is `stale`.

The decision record's own M0.3 state must also remain `not_invalidated/current/fresh`.

This deliberately means a BLOCK decision can be fresh. Freshness answers whether its evaluation inputs changed, not whether they were positive.

Unrelated ledger events that do not alter any bound effective state do not stale the decision.

## 11. Concurrency

M0.4 semantic creation and gate evaluation use the existing semantic compare-and-append boundary:

1. verify/replay one complete semantic snapshot;
2. validate/build the new receipt/policy/request/decision against that snapshot;
3. append only if event count and head hash still match inside the immediate write transaction.

Any concurrent ledger commit forces revalidation rather than allowing a decision against a stale semantic head.

## 12. CLI surface

```text
sayf verification record --payload <json>
sayf policy register --payload <json>
sayf gate request --payload <json>
sayf gate evaluate <request_id>
sayf gate show <decision_id>
sayf gate list
```

Generic `sayf record create` rejects the four M0.4 semantic record types.

## 13. Performance boundary

Gate projection is correctness-first.

For every historical gate decision, replay reconstructs the M0.3 prefix immediately before that decision so the stored outcome can be independently recomputed. This can approach O(D × N) history work for D decisions over N events, in addition to normal complete ledger verification and M0.3 projection.

No mutable gate cache or second authority store is introduced in M0.4. A future accelerator requires a measured forcing function and must remain verifiable/disposable.

## 14. M0.4 invariants

1. Evidence existence is not gate authority.
2. Policy existence is not gate authority until an exact request binds it.
3. Gate decisions bind exact immutable inputs, never IDs alone.
4. Future records cannot retroactively satisfy earlier semantic bindings.
5. Unknown or insufficient evidence is not pass.
6. Non-pass receipts do not satisfy a verification requirement.
7. Gate decision outcome is deterministic for the exact historical request snapshot.
8. Privileged forged decisions fail historical replay.
9. One immutable gate request has at most one decision.
10. A decision becomes stale when any bound effective-state fingerprint changes.
11. A BLOCK decision may be fresh.
12. A gate permit is not release authority.
13. M0.4 semantic writes retain the semantic-head TOCTOU guard.
14. `minimum_passes` is a receipt-count condition, not an inferred independence guarantee.
15. Supported M0.4 adoption starts from semantically valid prior-milestone history; historical runtime provenance is not cryptographically attested.

## 15. Acceptance slice

The primary M0.4 acceptance path is:

```text
ChangeSet C1
Evidence E1
VerificationReceipt V1: contract=tests, result=pass, subject=C1, evidence=E1
PolicySnapshot P1: require tests >= 1 pass
GateRequest G1: subject=C1, policy=P1, receipts=[V1]
GateDecision D1: permit, fresh

ChangeSet C2 supersedes C1
=> D1 remains historically permit
=> D1 current freshness becomes stale
```

A complementary negative path is:

```text
PolicySnapshot P2: require tests + lint
GateRequest G2 binds only passing tests receipt
=> deterministic BLOCK
=> BLOCK remains fresh until one of its bound input states changes
```

## 16. Deferred scope

M0.4 does not implement:

- release registration or release authority;
- risk-assessment contracts;
- human override/waiver semantics;
- authenticated actor identities, signatures, or remote RBAC;
- arbitrary policy expressions;
- gate-request mutation or receipt attachment after creation;
- multiple decisions for one request;
- general `why` / `impact` / `timeline` explanation surfaces;
- persistent materialized gate projections;
- cryptographic attestation of historical runtime/version provenance.

Those require explicit forcing functions or later milestone contracts rather than being inferred into M0.4.
