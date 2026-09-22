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
VerificationReceipt V-risk = PASS for C1, evidence includes RA1 + RA1 evidence
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
subject                   exact RecordBinding to ChangeSet
method                    non-empty normalized assessment method/profile name
findings                  ordered tuple of zero or more RiskFinding entries
overall_conclusion        acceptable | attention_required | unacceptable | inconclusive
evidence                  zero or more exact prior RecordBindings
environment               non-empty JSON object
assumptions               ordered tuple of normalized text
limitations               ordered tuple of normalized text
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

An assessment may contain zero findings and/or zero evidence bindings. That is permitted because M0.6 records the assessor's bounded claim rather than inventing a universal minimum-evidence rule. A project that requires a stronger evidence basis must enforce that through its qualification contract and verifier. Zero findings or an `acceptable` conclusion never creates authority by itself.

## 4. Validation rules

RiskAssessment creation requires:

1. the subject exists earlier in the ledger and is exactly a `ChangeSet`;
2. every evidence binding exists earlier in the ledger and matches exact record ID + content hash;
3. evidence bindings are unique by record ID;
4. finding IDs are unique within the assessment;
5. `mitigation`, when present, is non-empty after normalization;
6. `overall_conclusion = acceptable` is permitted even with findings or with none, but it is only the assessor's bounded conclusion and creates no authority;
7. generic `record create` cannot create a RiskAssessment;
8. malformed privileged RiskAssessment history fails the semantic facade closed.

RiskAssessment creation uses the existing exact semantic-head compare-and-append rule. Any concurrent ledger change after validation forces failure/revalidation; the supported API does not append against a stale semantic snapshot.

## 5. Qualification using existing M0.4 machinery

M0.6 does not alter the M0.4 GateDecision algorithm.

A project that requires risk qualification creates a normal VerificationReceipt for the exact ChangeSet. When that receipt binds a RiskAssessment, M0.6 requires two closure rules before the receipt can be appended through the supported API and again during historical replay:

1. the receipt subject must exactly equal the RiskAssessment's ChangeSet subject binding;
2. the receipt must also bind every exact evidence record bound by that RiskAssessment.

The second rule makes the complete bounded risk evidence basis part of the existing M0.4 evaluated-input set. If a RiskAssessment itself cites another RiskAssessment, the same rule applies recursively because every RiskAssessment present in receipt evidence is validated. Chronology prevents cyclic forward references.

A policy then requires the receipt's project-defined contract, for example:

```text
VerificationReceipt
  subject = C1
  contract = "risk:qualified"
  result = pass
  evidence = [RA1, E1, E2, ...]  # RA1 plus every exact record RA1 assessed
  verified_claim = "the bound risk assessment satisfies the project's bounded risk qualification contract"

PolicySnapshot
  requirements = [
    ...,
    { contract = "risk:qualified", minimum_passes = 1 }
  ]
```

The contract string is project-defined. M0.6 does not interpret particular contract names, does not inspect `overall_conclusion` to grant authority, and does not silently infer that a RiskAssessment has been qualified.

A passing receipt does not prove the assessment is globally correct, the system is safe, the risk is zero, or the verifier is independent unless that independence is established by the receipt's own bounded context and external trust model.

## 6. Staleness and revision semantics

RiskAssessment is an ordinary semantic record participating in M0.3 effective state.

If the assessment is invalidated, superseded, or becomes stale through qualified dependencies, a GateDecision that evaluated a receipt/evidence chain containing that assessment becomes stale under the existing M0.4 full evaluated-input fingerprints.

Because M0.6 requires receipt evidence closure, changes to the RiskAssessment's bound evidence records also change evaluated-input state and therefore stale a dependent GateDecision and downstream M0.5 Release authority.

M0.6 therefore does not add a second risk-staleness engine.

A new assessment for a changed understanding should normally supersede the old assessment through the existing M0.3 mechanism. Historical assessments remain historical records.

## 7. Explainability

M0.6 does not add a second explainability graph or new risk-specific edge authority to M0.5.

The existing M0.5 gate/evidence explanation surface exposes the qualification basis because the qualifying receipt directly binds both the RiskAssessment and the RiskAssessment's exact evidence closure:

```text
GateDecision
 -> VerificationReceipt
 -> RiskAssessment
 -> E1 / E2 / ... as sibling receipt-evidence inputs
```

`risk show` exposes the RiskAssessment record itself, including its exact subject/evidence bindings and current M0.3 effective state. Consumers can therefore distinguish the assessment's own evidence declaration from the receipt's qualification closure without inventing a new permanent relation type.

These are recorded evidence/authority relationships, not proof that a finding caused an outcome or that an assessment is true.

`why` and `impact` retain their existing depth/result/expansion bounds and claim ceilings.

## 8. Historical replay

M0.6 semantic replay validates every RiskAssessment against its creation-time chronology and exact bindings. It also validates every VerificationReceipt that binds one or more RiskAssessments against the subject/evidence-closure rules above.

A privileged caller cannot create authoritative-looking RiskAssessment or risk-qualification history merely by writing syntactically valid JSON. Supported semantic reads/writes must reject:

- a RiskAssessment whose subject is not a ChangeSet;
- a subject/evidence binding with a wrong content hash;
- a forward binding;
- duplicate evidence IDs;
- duplicate finding IDs;
- malformed enum/text/environment values;
- a receipt that uses a RiskAssessment for a different ChangeSet;
- a receipt that binds a RiskAssessment but omits any exact evidence record bound by that assessment.

Supported writes enforce these rules before append where the record is created through the M0.6-aware semantic facade. Historical replay independently rechecks them so privileged/raw ledger mutation fails closed.

The supported upgrade boundary assumes the input ledger was valid M0.5 history. M0.5 mechanically reserved RiskAssessment through supported semantic creation surfaces, so valid supported M0.5 history cannot already contain one.

## 9. API and CLI surface

Repository API:

```text
record_risk_assessment(spec, actor, record_id=None) -> Record
risk_assessment(record_id) -> RiskAssessmentPayload
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
VR1 = VerificationReceipt(
  subject=C1,
  contract="risk:qualified",
  PASS,
  evidence=[RA1, E1]
)
P1 = PolicySnapshot(require tests + risk:qualified)
G1 = GateRequest(C1, P1, receipts=[tests, VR1])
D1 = PERMIT
R1 = Release(C1, D1)

invalidate/supersede/stale RA1 or E1
  -> evaluated risk evidence input state changes
  -> D1 becomes stale under M0.4 evaluated-input fingerprints
  -> R1 authority becomes stale under M0.5 release authority fingerprint
```

Negative acceptance paths include malformed privileged RiskAssessment history, wrong subject type, wrong/forward evidence binding, duplicate findings/evidence, generic-surface impersonation, mismatched receipt/assessment subjects, incomplete receipt evidence closure, concurrent head movement, and a favorable assessment that is not qualified/required by policy producing no gate authority by itself.

## 11. Claim ceilings

The strongest legitimate M0.6 claims are deliberately narrow:

- `RiskAssessment.overall_conclusion = acceptable` means only that the recorded assessor concluded the exact ChangeSet was acceptable under the recorded method, evidence, environment, assumptions, and limitations.
- A PASS `risk:qualified` VerificationReceipt means only that the verifier's bounded contract passed for the exact subject/evidence it binds.
- Evidence closure means the gate fingerprints the exact assessment basis; it does not prove the basis is complete, independent, or correct.
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
