# M0.6 — Risk + Qualification

Status: implementation contract

## 1. Objective

M0.6 adds an explicit risk-evidence layer without creating a second approval authority.

The narrow slice is:

```text
ChangeSet C1
   |
   v
RiskAssessment RA1
   |
 independent/bounded qualification using existing M0.4 VerificationReceipt
   v
VerificationReceipt V-risk = PASS for C1, evidence includes RA1
   |
 policy requirement, e.g. contract = "risk:qualified"
   v
PolicySnapshot P1
   |
   v
GateRequest G1 -> GateDecision D1
   |
   v
Release R1 (M0.5, only if D1 is a fresh PERMIT)
```

M0.6 closes a specific governance gap: M0.4 can prove that named verification contracts were satisfied, and M0.5 can prove that a Release was registered under a fresh PERMIT, but neither milestone provides a first-class bounded record of the risk considered for that ChangeSet.

M0.6 therefore activates `RiskAssessment` as evidence. It does **not** create a `RiskApproval`, `RiskAcceptance`, waiver, override, or alternate gate authority.

## 2. Governance boundary

The immutable ledger remains the authority source.

The governing distinctions are:

```text
risk observation != risk assessment
risk assessment != qualification
qualification != policy
policy != gate decision
PERMIT != proof of safety
Release != proof of zero risk
```

A `RiskAssessment` is an immutable assessment authored under recorded context. It may be wrong, incomplete, stale, contradicted, superseded, or later falsified.

A RiskAssessment has no authority merely because its assessor conclusion is favorable. If a project wants risk assessment to constrain a gate, the project must bind the assessment as evidence to a `VerificationReceipt` whose contract is explicitly required by the exact `PolicySnapshot` used by the GateRequest.

M0.6 reuses M0.4 authority rather than adding a parallel risk-decision store.

Actor IDs remain provenance, not authenticated identity or RBAC.

## 3. RiskAssessment contract

A v1 RiskAssessment binds one exact prior `ChangeSet` and records:

```text
subject                 exact RecordBinding to ChangeSet
method                   non-empty normalized assessment method/profile name
findings                 ordered tuple of zero or more RiskFinding entries
overall_conclusion       acceptable | attention_required | unacceptable | inconclusive
evidence                 zero or more exact prior RecordBindings
environment              non-empty JSON object
assumptions              ordered tuple of normalized text
limitations              ordered tuple of normalized text
prohibited_generalizations ordered tuple of normalized text
```

A `RiskFinding` contains:

```text
finding_id               unique within the assessment
category                 non-empty normalized project-native category
statement                non-empty normalized risk statement
severity                 low | moderate | high | critical | unknown
likelihood               unlikely | possible | likely | unknown
mitigation               optional non-empty normalized text
residual_severity        low | moderate | high | critical | unknown
```

M0.6 intentionally does not define arithmetic risk scores, probability percentages, monetary exposure, or a universal risk matrix. Those mechanisms require a later forcing function and domain-specific calibration.

Findings are assessment claims, not objective facts. `severity`, `likelihood`, `residual_severity`, and `overall_conclusion` are explicitly assessor judgments recorded for provenance.

## 4. Validation rules

RiskAssessment creation requires:

1. the subject exists earlier in the ledger and is exactly a `ChangeSet`;
2. every evidence binding exists earlier in the ledger and matches exact record ID + content hash;
3. evidence bindings are unique by record ID;
4. finding IDs are unique within the assessment;
5. `mitigation`, when present, is non-empty after normalization;
6. `overall_conclusion = acceptable` is permitted even with findings, but it is only the assessor's bounded conclusion and creates no authority;
7. generic `record create` cannot create a RiskAssessment;
8. malformed privileged RiskAssessment history fails the semantic facade closed.

RiskAssessment creation uses the existing exact semantic-head compare-and-append rule. Any concurrent ledger change after validation forces revalidation.

## 5. Qualification using existing M0.4 machinery

M0.6 does not alter the M0.4 GateDecision algorithm.

A project that requires risk qualification creates a normal VerificationReceipt for the exact ChangeSet and includes the exact RiskAssessment as evidence. A policy then requires that receipt's contract, for example:

```text
VerificationReceipt
  subject = C1
  contract = "risk:qualified"
  result = pass
  evidence = [RA1, ...]
  verified_claim = "the bound risk assessment satisfies the project's bounded risk qualification contract"

PolicySnapshot
  requirements = [
    ...,
    { contract = "risk:qualified", minimum_passes = 1 }
  ]
```

The contract string is project-defined. M0.6 does not interpret particular contract names or silently infer that a RiskAssessment has been qualified.

A passing receipt does not prove the assessment is globally correct, the system is safe, the risk is zero, or the verifier is independent unless that independence is established by the receipt's own bounded context and external trust model.

## 6. Staleness and revision semantics

RiskAssessment is an ordinary semantic record participating in M0.3 effective state.

If the assessment is invalidated, superseded, or becomes stale through qualified dependencies, a GateDecision that evaluated a receipt/evidence chain containing that assessment becomes stale under the existing M0.4 full evaluated-input fingerprints.

M0.6 therefore does not add a second risk-staleness engine.

A new assessment for a changed understanding should normally supersede the old assessment through the existing M0.3 mechanism. Historical assessments remain historical records.

## 7. Explainability

M0.5 explainability must expose RiskAssessment evidence bindings through the existing authority/evidence chain:

```text
GateDecision
 -> VerificationReceipt
 -> RiskAssessment
 -> RiskAssessment evidence records
```

RiskAssessment edges are recorded evidence relationships, not proof that the identified finding caused an outcome.

`why` and `impact` retain their existing depth/result/expansion bounds and claim ceilings.

## 8. Historical replay

M0.6 semantic replay validates every RiskAssessment against its creation-time chronology and exact bindings.

A privileged caller cannot create authoritative-looking RiskAssessment history merely by writing syntactically valid generic JSON. Supported semantic reads/writes must reject:

- a RiskAssessment whose subject is not a ChangeSet;
- a subject/evidence binding with a wrong content hash;
- a forward binding;
- duplicate evidence IDs;
- duplicate finding IDs;
- malformed enum/text/environment values.

The supported upgrade boundary assumes the input ledger was valid M0.5 history. M0.5 mechanically reserved RiskAssessment through supported APIs, so valid supported M0.5 history cannot already contain one.

## 9. API and CLI surface

Repository API:

```text
record_risk_assessment(spec, actor, record_id=None) -> Record
```

CLI:

```text
sayf risk assess --payload <json> [--id <record_id>]
sayf risk show <risk_assessment_id>
```

`risk show` returns the immutable RiskAssessment record together with its current M0.3 effective state. It does not calculate a new score or decision.

## 10. Acceptance slice

The primary acceptance path is:

```text
C1 = ChangeSet
E1 = Evidence
RA1 = RiskAssessment(subject=C1, evidence=[E1], conclusion=acceptable)
VR1 = VerificationReceipt(subject=C1, contract="risk:qualified", PASS, evidence=[RA1])
P1 = PolicySnapshot(require tests + risk:qualified)
G1 = GateRequest(C1, P1, receipts=[tests, VR1])
D1 = PERMIT
R1 = Release(C1, D1)

invalidate/supersede/stale RA1
  -> VR1's evidence input state changes
  -> D1 becomes stale under M0.4 evaluated-input fingerprints
  -> R1 authority becomes stale under M0.5 release authority fingerprint
```

Negative acceptance paths include malformed privileged RiskAssessment history, wrong subject type, wrong/forward evidence binding, duplicate findings/evidence, generic-surface impersonation, and a favorable assessment that is not qualified/required by policy producing no gate authority by itself.

## 11. Claim ceilings

The strongest legitimate M0.6 claims are deliberately narrow:

- `RiskAssessment.overall_conclusion = acceptable` means only that the recorded assessor concluded the exact ChangeSet was acceptable under the recorded method, evidence, environment, assumptions, and limitations.
- A PASS `risk:qualified` VerificationReceipt means only that the verifier's bounded contract passed for the exact subject/evidence it binds.
- A GateDecision PERMIT means only that the exact GateRequest satisfied the exact PolicySnapshot at the exact evaluated state.
- A Release authorized by that gate is not proof of safety, zero residual risk, regulatory compliance, human approval, production health, or byte-level deployment identity.

## 12. Explicit non-scope

M0.6 does not add:

- numeric or probabilistic risk scoring;
- organization-wide risk taxonomy;
- waiver/exception authority;
- human approval or signatures;
- RBAC/authentication;
- arbitrary policy expressions over severity/likelihood fields;
- automated conversion of an assessor conclusion into GateDecision authority;
- risk aggregation across multiple ChangeSets/Releases;
- deployment artifact attestation;
- mutable risk dashboards or a second authority database.

Any of these requires a separate forcing function and explicit authority contract.
