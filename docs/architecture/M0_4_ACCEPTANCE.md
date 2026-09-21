# M0.4 Acceptance Scenarios

These scenarios are executable contract targets for M0.4.

## Positive path

1. Create `ChangeSet C1`.
2. Create `Evidence E1`.
3. Record `VerificationReceipt V1` for `C1`, contract `tests`, result `pass`, evidence `E1`.
4. Register `PolicySnapshot P1` requiring one passing `tests` receipt.
5. Create `GateRequest G1` binding `C1`, `P1`, and `V1`.
6. Evaluate `G1`.
7. Result is `GateDecision D1(outcome=permit)`.
8. `D1` is currently `fresh`.
9. Create `ChangeSet C2` and qualify `C2 supersedes C1`.
10. `D1` remains historically `permit` but current freshness becomes `stale`.

## Missing evidence path

1. Register a policy requiring `tests` and `lint`.
2. Request a gate with only a passing `tests` receipt.
3. Evaluation produces `block`.
4. The decision records `lint` as unsatisfied.
5. The block decision remains fresh while its exact input states remain unchanged.

## Negative receipt path

1. Bind a `fail`, `partial`, or `inconclusive` receipt.
2. The receipt does not satisfy its policy requirement.
3. Evaluation blocks.
4. Absence of another passing receipt never becomes pass by default.

## Privileged forgery path

1. Append a syntactically valid `GateDecision(outcome=permit)` through a privileged low-level API for a request whose deterministic evaluation is `block`.
2. M0.4 replay recomputes the historical evaluation.
3. Projection fails closed because the stored decision does not match the recomputed result.

## Forward binding path

1. Append a verification receipt that binds a record created later in history.
2. Append the referenced record afterward.
3. Full graph existence does not legitimize the earlier binding.
4. M0.4 projection fails closed because the bound input did not exist when the receipt was created.

## Concurrency path

1. Build a valid gate decision against semantic head H.
2. Another writer appends any ledger event before the decision append commits.
3. Compare-and-append rejects the decision because the event count/head hash changed.
4. The request must be re-evaluated against the new semantic head.
