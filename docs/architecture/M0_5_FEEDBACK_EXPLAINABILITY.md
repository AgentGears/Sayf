# M0.5 — Feedback + Explainability

Status: implementation candidate

## 1. Objective

M0.5 closes the first bounded reality-learning loop above M0.4 without weakening the existing evidence/authority separation.

The narrow slice is:

```text
Assumption A1
     |
     v
ChangeSet C1
     |
 verification + policy
     v
GateDecision D1 = PERMIT (fresh)
     |
     v
Release R1
     |
 runtime behavior
     v
RuntimeObservation O1
     |
 classification
     v
FeedbackCase F1
     |
 explicit application
     v
qualified invalidation of A1
     |
     +--> C1 becomes stale through M0.3 dependency propagation
     +--> D1 becomes stale through M0.4 bound-input fingerprints
     +--> R1 authority becomes stale through its bound gate authority
```

M0.5 also exposes deterministic, bounded `why`, `impact`, and `timeline` queries over the recorded semantic relationships.

## 2. Authority boundary and claim ceilings

The immutable ledger remains the authority source. M0.5 activates three previously reserved semantic record types through dedicated APIs:

- `Release`
- `RuntimeObservation`
- `FeedbackCase`

`RiskAssessment` remains reserved.

M0.5 deliberately does **not** collapse evidence, observation, classification, and authority into one state:

- A `RuntimeObservation` is immutable evidence about a historical Release. It has no state authority merely because it records a failure.
- A `FeedbackCase` is an immutable classification and proposed effect. Opening the case does not mutate effective state.
- An invalidating feedback case gains state consequence only when its deterministic `invalidates` relation is explicitly qualified through the M0.5 managed feedback-application path, which emits the existing M0.3 `sayf.record.invalidated.v1` authority event.
- `ReleaseAuthorityFreshness.FRESH` means only that the exact Release record remains usable and the bound gate still has the same v1 fresh-PERMIT authority fingerprint captured at release time. It does not mean the deployed system is healthy, correct, safe, available, approved by a human, or still running.
- A failed runtime observation does not automatically stale a Release. Only an explicit state transition can change M0.3 state and thereby stale downstream authority.
- Actor IDs, release environment, runtime environment, and feedback rationale are provenance/context. M0.5 does not turn them into authenticated credentials or policy assertions.

## 3. Release contract

A v1 `Release` binds:

- a normalized logical `release_ref`;
- one exact immutable `ChangeSet` binding (`record_id` + `content_hash`);
- one exact immutable `GateDecision` binding;
- `authority_fingerprint_version = 1`;
- one v1 release-authority fingerprint;
- non-empty environment/context JSON.

Release creation is allowed only when:

1. the subject is the exact `ChangeSet` authorized by the bound gate decision;
2. the gate decision outcome is `permit`;
3. the gate decision is currently fresh under M0.4;
4. the ChangeSet is currently `not_invalidated/current/fresh` under M0.3.

`release_ref` is unique across valid M0.5 history.

### 3.1 Versioned release-authority fingerprint

M0.5 does not hash the complete evolving `GateDecisionStatus` or the complete `RecordEffectiveState` model into the Release payload. Doing that would make valid historical Release records sensitive to unrelated future fields added to projection models.

The v1 fingerprint hashes only the stable authority facts required by a Release:

```text
schema_version = 1
decision_id
outcome
freshness
```

The exact GateDecision record is independently bound by immutable ID and content hash. M0.4 already binds the gate's complete evaluated-input effective-state fingerprints, including the ChangeSet, policy, receipts, and evidence. M0.5 therefore does not duplicate a second full ChangeSet-state hash inside Release.

A historical Release is replay-validated against the ledger prefix immediately before its creation. A privileged caller cannot append an arbitrary Release payload and make it authoritative merely because the JSON shape is valid.

### 3.2 Authority freshness

Current `ReleaseStatus` exposes:

```text
authority_freshness: fresh | stale
stale_authority_record_ids: [...]
```

This terminology is intentionally separate from M0.3 `RecordEffectiveState.freshness`.

A Release's authority becomes stale when either:

- the Release record itself is no longer `not_invalidated/current/fresh`; or
- the currently derived gate authority fingerprint differs from the fingerprint captured at Release creation, including when the gate becomes stale.

The Release remains a historical Release record after its authority becomes stale. Staleness does not rewrite its original gate outcome or historical registration.

### 3.3 Artifact provenance boundary

M0.5 v1 Release binds the exact **logical ChangeSet and gate authority**, not an exact deployed byte artifact or deployment manifest. `release_ref` and environment are context, not attestation.

Therefore M0.5 v1 does **not** prove that a particular executable, container image, package digest, filesystem tree, or deployment target exactly corresponds to the ChangeSet. Byte-level deployment provenance requires a later explicit contract that binds content-addressed Artifacts/deployment receipts; it is not inferred from `release_ref` or environment metadata.

## 4. RuntimeObservation contract

A `RuntimeObservation` binds:

- one exact prior `Release`;
- outcome `nominal | degraded | failed | unknown`;
- non-empty summary;
- non-empty environment/context JSON;
- zero or more exact evidence-record bindings.

Every binding must refer to a record that existed before the observation. Duplicate evidence IDs are rejected.

Runtime observations remain valid historical observations even if the Release's current authority later becomes stale. Recording runtime behavior is not itself a state mutation.

## 5. FeedbackCase contract

A `FeedbackCase` binds:

- one exact prior `RuntimeObservation`;
- one exact prior target record;
- classification `falsifies | contradicts | violates | anomaly | informational`;
- proposed effect `none | invalidate_target`;
- non-empty rationale.

`invalidate_target` is valid only for `falsifies`, `contradicts`, or `violates` classifications.

The target may be any prior record in M0.5 v1. That is intentionally an asserted classification boundary: Sayf records who/what classified the runtime evidence against that target; it does not mechanically prove that the target is the true real-world root cause.

## 6. Explicit feedback application

Opening a FeedbackCase does not mutate state. `apply_feedback(case_id)` is the explicit adoption step for a case that proposes target invalidation.

Application reuses the M0.3 authority event rather than introducing another authority store:

1. derive a deterministic `invalidates` relation ID from the FeedbackCase ID;
2. create the exact relation `FeedbackCase -> target` with metadata binding the exact case content hash;
3. use the managed M0.5 qualification path to validate feedback eligibility and M0.3 invalidation semantics against one exact semantic snapshot;
4. append `sayf.record.invalidated.v1` only if that snapshot's event count and head hash still match under the existing compare-and-append boundary.

The intermediate relation is inert. If the process stops after relation creation but before qualification, replay reports `pending_qualification` rather than pretending invalidation occurred.

The generic `state invalidate` / `CausalRepository.invalidate()` path refuses a managed deterministic feedback relation. This prevents a pending relation from bypassing M0.5 feedback eligibility while preserving the same underlying M0.3 state-event format for authoritative qualification.

### 6.1 Eligibility for new authority

Immediately before qualifying **new** feedback authority, M0.5 requires all source records to remain usable under M0.3:

- the FeedbackCase;
- its bound RuntimeObservation;
- every evidence record bound by that RuntimeObservation.

Each must be:

```text
validity:  not_invalidated
revision:  current
freshness: fresh
```

Eligibility, M0.3 invalidation validation, and the eventual qualification append are bound to the same semantic snapshot. If any ledger write occurs after validation, the exact-head guard rejects the append and `apply_feedback` replays/revalidates before retrying.

If any source becomes unusable, a not-yet-applied case cannot create new invalidation authority. This rule also applies when the deterministic relation already exists in `pending_qualification` state after an interrupted attempt.

Historical replay independently reconstructs the ledger prefix immediately before every managed feedback qualification event and revalidates this eligibility rule. A privileged caller therefore cannot make ineligible feedback authority acceptable merely by appending a syntactically valid M0.3 invalidation event.

Once qualification has occurred validly, the application remains historical fact. Later invalidation of the FeedbackCase, observation, or evidence does not erase the already-recorded M0.3 transition.

### 6.2 Retry and concurrency semantics

Feedback application is designed to converge for compatible competing callers and unrelated head movement:

- if another caller creates the same deterministic relation first, the next snapshot recognizes the compatible pending relation and continues;
- if another caller qualifies the relation first, the next snapshot returns the existing `applied` status;
- if unrelated ledger movement causes the relation-creation or qualification append to lose the exact-head race, the bounded retry loop takes a new semantic snapshot instead of treating unchanged `not_applied`/`pending_qualification` as a terminal error;
- eligibility is revalidated on every retry and is checked in the same snapshot used by managed qualification;
- if a source becomes unusable during the race window, the stale append fails its head comparison and the next replay rejects qualification;
- incompatible content at the deterministic relation ID fails closed.

This makes the operation idempotent at the semantic level without introducing locks or a second mutable authority store.

## 7. Explainability projection

M0.5 derives explanation edges from immutable records and qualified state events. The projection is disposable.

Edge propagation classes are explicit:

- `state` — qualified M0.3 dependency/invalidation/supersession relationships;
- `authority` — verification, gate, and release input bindings;
- `historical_reference` — runtime and feedback references that describe history but do not automatically propagate state.

`why(record)` walks upstream recorded relationships. `impact(record)` walks downstream recorded relationships. `timeline(record)` reports ledger events in which the record participated as a created record, relation endpoint, state-transition endpoint, or semantic binding.

### 7.1 Query claim ceiling

`why` and `impact` return deterministic bounded **recorded relationship paths**. They are not proof of truth, physical causality, completeness of real-world impact, or automatic state propagation.

Multiple simple paths to the same endpoint are preserved because distinct paths may carry materially different authority or provenance. Cycle prevention is path-local rather than a global endpoint deduplication rule.

Callers that need only actual M0.3 staleness should use effective-state/staleness data and inspect edge propagation classes rather than treating every explainability path as a state consequence.

### 7.2 Bounds

Explainability traversal is bounded by:

- `max_depth`;
- `max_results`;
- `max_expansions`.

The default expansion budget is `10_000`, matching the existing M0.2 correctness-first approach. Exceeding a result or work bound fails the query rather than silently returning a partial set that could be mistaken for complete coverage.

`timeline` has an independent entry bound and likewise fails on overflow.

## 8. Historical replay and malformed privileged history

M0.5 semantic records are ordinary immutable record-creation events with dedicated payload contracts. They cannot be created through generic `record create`.

During replay:

- Release records are recomputed against the exact pre-release ledger prefix;
- RuntimeObservation and FeedbackCase bindings are checked for exact content hash, expected type where applicable, and chronology;
- every managed feedback qualification is revalidated against the exact pre-qualification prefix, including source usability;
- a deterministic feedback relation with incompatible immutable content fails closed;
- duplicate `release_ref` values fail closed;
- malformed, forged, or historically ineligible privileged M0.5 history poisons the semantic facade rather than being ignored.

Supported M0.5 adoption assumes the input ledger was semantically valid under the immediately preceding milestone. The prior M0.4 supported API mechanically reserved `Release`, `RuntimeObservation`, and `FeedbackCase`, so valid M0.4 history could not contain them through normal semantic creation.

As in M0.4, Sayf does not cryptographically attest which historical runtime/version created every event. A caller able to bypass supported APIs remains inside the documented privileged local-write trust boundary, but M0.5 replay still rejects privileged history that violates the milestone's deterministic semantic rules.

## 9. Concurrency

Normal M0.5 semantic creation retains the exact semantic-head compare-and-append rule:

1. verify/replay one complete semantic snapshot;
2. validate/build the semantic record against that snapshot;
3. append only when event count and head hash still match under `BEGIN IMMEDIATE`.

Any concurrent ledger commit forces revalidation.

Feedback application spans two immutable writes by design (relation creation, then managed M0.5 qualification using the M0.3 invalidation event). Its deterministic relation identity and retry/re-read rules provide crash recovery and compatible concurrent convergence without making the inert relation authoritative before qualification. The authority-producing qualification specifically checks eligibility, relation semantics, and exact ledger head from one snapshot.

## 10. Performance boundary

M0.5 remains correctness-first.

The full M0.4 GateProjection validates historical gate decisions once. Historical Release validation does **not** construct another complete GateProjection for every Release. For each Release prefix, M0.5 constructs only the M0.3 effective-state projection required to derive the bound gate's historical status.

Historical managed feedback qualifications also reconstruct the M0.3 prefix immediately before qualification so source eligibility cannot be bypassed by privileged history.

With D gate decisions, R releases, F managed feedback qualifications, and N events, replay therefore remains approximately O((D + R + F) × N)-class prefix work rather than nesting D×N gate replay inside each Release. This is still not a high-throughput design.

No mutable projection cache or second authority store is introduced. A future accelerator requires a measured forcing function and must remain verifiable/disposable.

## 11. CLI surface

```text
sayf release register --payload <json>
sayf release show <release_id>

sayf runtime record --payload <json>

sayf feedback open --payload <json>
sayf feedback show <case_id>
sayf feedback apply <case_id>

sayf explain why <record_id> --max-depth 8 --max-results 100 --max-expansions 10000
sayf explain impact <record_id> --max-depth 8 --max-results 100 --max-expansions 10000
sayf explain timeline <record_id> --max-entries 200
```

Generic `sayf record create` rejects `Release`, `RuntimeObservation`, and `FeedbackCase`. Generic `sayf state invalidate` also refuses the deterministic relation owned by a FeedbackCase; use `sayf feedback apply` so M0.5 eligibility is enforced at qualification.

## 12. Acceptance slice

The primary acceptance path is:

```text
Assumption A1
ChangeSet C1 --qualified depends_on--> A1
VerificationReceipt V1 = PASS for C1
PolicySnapshot P1 requires V1 contract
GateRequest G1 binds C1, P1, V1
GateDecision D1 = PERMIT, fresh
Release R1 binds C1 + D1
RuntimeObservation O1 = FAILED for R1, with runtime evidence E2
FeedbackCase F1 = falsifies A1, proposes invalidate_target

before apply(F1):
  A1 not_invalidated
  F1 not_applied
  D1 fresh
  R1 authority_freshness = fresh

apply(F1):
  deterministic invalidates relation F1 -> A1
  managed eligibility + M0.3 invalidation qualification on one exact snapshot

then:
  A1 invalidated
  C1 stale through qualified dependency
  D1 historically PERMIT but currently stale
  R1 historically registered but authority_freshness = stale
  why/impact/timeline expose the recorded paths and transition
```

Complementary negative paths include:

- a FeedbackCase/RuntimeObservation/runtime evidence that becomes unusable before application cannot create invalidation authority;
- a pending deterministic feedback relation remains inert if its source evidence becomes unusable before qualification;
- generic state invalidation cannot bypass the managed feedback eligibility boundary;
- a privileged ineligible qualification event fails semantic replay at its historical prefix;
- unrelated concurrent head movement during relation creation or qualification retries from a new snapshot and can converge;
- source invalidation racing qualification causes the stale append to lose the exact-head check, after which revalidation blocks authority;
- alternate explanation paths to the same endpoint are preserved;
- result/work bounds fail closed;
- a forged Release that did not have a historical fresh PERMIT fails semantic replay.

## 13. M0.5 invariants

1. Release authority requires an exact historical fresh M0.4 PERMIT for the exact ChangeSet.
2. Release authority fingerprint semantics are explicit and versioned.
3. `authority_freshness` is not M0.3 record freshness and is not runtime health.
4. Runtime observation is evidence, not state authority.
5. Feedback classification is not state authority until explicitly applied.
6. New feedback invalidation authority requires currently usable case/observation/runtime evidence in the same snapshot used for qualification.
7. Generic M0.3 invalidation cannot bypass the managed feedback qualification boundary.
8. Historical managed feedback qualification is replay-validated at the pre-qualification prefix.
9. Pending feedback relations are inert until qualification.
10. Compatible concurrent/retried application converges on one deterministic relation and one qualification.
11. Already-applied history is not erased by later source invalidation.
12. Explainability preserves alternate simple recorded paths subject to explicit depth/result/work bounds.
13. Explainability relationship paths are not proof of causality or truth.
14. Release v1 does not claim byte-level deployed-artifact provenance.
15. M0.5 semantic writes retain the exact-head TOCTOU guard.
16. Malformed or historically ineligible privileged M0.5 history fails closed.
17. `RiskAssessment` remains reserved.

## 14. Deferred scope

M0.5 does not implement:

- `RiskAssessment` contract or risk acceptance;
- exact deployed-artifact/deployment-manifest attestation;
- authenticated actor identities, signatures, or remote RBAC;
- autonomous deployment or rollback execution;
- human waiver/override semantics;
- a general-purpose policy language;
- automated root-cause inference from runtime observations;
- a claim that `why`/`impact` paths are complete real-world causality;
- persistent materialized feedback/explanation authority stores;
- cryptographic attestation of historical runtime/version provenance.

Those require explicit later contracts or measured forcing functions rather than being inferred into M0.5.
