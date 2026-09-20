# M0.4 Scope Boundary

This file freezes the intended implementation scope before the primary exhaustive review begins.

## In scope

- dedicated semantic creation for `VerificationReceipt`, `PolicySnapshot`, `GateRequest`, `GateDecision`;
- exact record ID + content-hash bindings;
- verification result/claim ceiling validation;
- deterministic verification requirements with minimum passing receipt counts;
- immutable ChangeSet gate requests;
- historical replay verification of each gate decision;
- exact effective-state fingerprint binding at evaluation;
- current gate-decision freshness/staleness derivation;
- one decision per immutable request;
- semantic-head compare-and-append on all M0.4 semantic writes;
- dedicated CLI surfaces and adversarial tests;
- fail-closed handling of malformed privileged M0.4 history.

## Out of scope

- release authority;
- risk assessment;
- runtime feedback;
- authenticated actor identity;
- signatures;
- remote RBAC;
- human waiver/override;
- arbitrary policy expressions;
- mutable gate requests;
- appending evidence to an existing request;
- multiple decisions for one request;
- persistent/materialized gate projection;
- M0.5 explanation queries.

Any review finding that requires one of these out-of-scope mechanisms should first demonstrate a concrete M0.4 correctness or authority defect before expanding the milestone.
