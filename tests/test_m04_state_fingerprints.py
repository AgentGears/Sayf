from __future__ import annotations

from pathlib import Path

from sayf.causal import CausalRepository
from sayf.domain import Actor, ActorKind
from sayf.gates import (
    GateDecisionFreshness,
    GateRequestSpec,
    PolicySnapshotSpec,
    VerificationReceiptSpec,
)
from sayf.records import (
    RecordDraft,
    RecordType,
    RelationDraft,
    RelationType,
    VerificationIndependence,
    VerificationRequirement,
    VerificationResult,
)

ACTOR = Actor(kind=ActorKind.HUMAN, id="m04-fingerprint")
VERIFIER = Actor(kind=ActorKind.TOOL, id="m04-verifier")
ENGINE = Actor(kind=ActorKind.POLICY_ENGINE, id="m04-engine")


def repository(tmp_path: Path) -> CausalRepository:
    return CausalRepository.from_paths(
        tmp_path / ".sayf" / "ledger.sqlite3",
        tmp_path / ".sayf" / "objects",
    )


def create(repo: CausalRepository, record_id: str, record_type: RecordType) -> None:
    repo.create_record(
        RecordDraft(record_id=record_id, record_type=record_type), actor=ACTOR
    )


def qualify_invalidation(
    repo: CausalRepository,
    *,
    relation_id: str,
    cause_id: str,
    target_id: str,
) -> None:
    repo.create_relation(
        RelationDraft(
            relation_id=relation_id,
            relation_type=RelationType.INVALIDATES,
            source_id=cause_id,
            target_id=target_id,
        ),
        actor=ACTOR,
    )
    repo.invalidate(relation_id, actor=ACTOR)


def create_permit_fixture(repo: CausalRepository) -> None:
    create(repo, "change1", RecordType.CHANGE_SET)
    create(repo, "evidence1", RecordType.EVIDENCE)
    repo.record_verification(
        VerificationReceiptSpec(
            subject_id="change1",
            contract="tests",
            result=VerificationResult.PASS,
            evidence_record_ids=("evidence1",),
            environment={"platform": "test"},
            independent_from_generation=VerificationIndependence.NOT_APPLICABLE,
            verified_claim="tests passed for the bound change",
        ),
        actor=VERIFIER,
        record_id="receipt1",
    )
    repo.register_policy_snapshot(
        PolicySnapshotSpec(
            name="policy-v1",
            requirements=(VerificationRequirement(contract="tests"),),
        ),
        actor=ACTOR,
        record_id="policy1",
    )
    repo.request_gate(
        GateRequestSpec(
            subject_id="change1",
            policy_snapshot_id="policy1",
            verification_receipt_ids=("receipt1",),
        ),
        actor=ACTOR,
        record_id="request1",
    )
    repo.evaluate_gate("request1", actor=ENGINE, record_id="decision1")


def test_second_invalidation_cause_stales_decision_without_axis_change(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    create(repo, "change1", RecordType.CHANGE_SET)
    create(repo, "evidence1", RecordType.EVIDENCE)
    create(repo, "obs1", RecordType.OBSERVATION)
    qualify_invalidation(
        repo,
        relation_id="invalidate_evidence_1",
        cause_id="obs1",
        target_id="evidence1",
    )

    repo.record_verification(
        VerificationReceiptSpec(
            subject_id="change1",
            contract="tests",
            result=VerificationResult.PASS,
            evidence_record_ids=("evidence1",),
            environment={"platform": "test"},
            independent_from_generation=VerificationIndependence.NOT_APPLICABLE,
            verified_claim="tests passed for the bound change",
        ),
        actor=VERIFIER,
        record_id="receipt1",
    )
    repo.register_policy_snapshot(
        PolicySnapshotSpec(
            name="policy",
            requirements=(VerificationRequirement(contract="tests"),),
        ),
        actor=ACTOR,
        record_id="policy1",
    )
    repo.request_gate(
        GateRequestSpec(
            subject_id="change1",
            policy_snapshot_id="policy1",
            verification_receipt_ids=("receipt1",),
        ),
        actor=ACTOR,
        record_id="request1",
    )
    repo.evaluate_gate("request1", actor=ENGINE, record_id="decision1")

    before = repo.effective_state().state("evidence1")
    assert before.validity.value == "invalidated"
    assert repo.gate_status("decision1").freshness is GateDecisionFreshness.FRESH

    create(repo, "obs2", RecordType.OBSERVATION)
    qualify_invalidation(
        repo,
        relation_id="invalidate_evidence_2",
        cause_id="obs2",
        target_id="evidence1",
    )

    after = repo.effective_state().state("evidence1")
    assert after.validity == before.validity
    assert after.revision == before.revision
    assert after.freshness == before.freshness
    assert after.invalidation_relation_ids != before.invalidation_relation_ids

    status = repo.gate_status("decision1")
    assert status.freshness is GateDecisionFreshness.STALE
    assert "evidence1" in status.stale_input_record_ids


def test_policy_supersession_stales_prior_gate_decision(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_permit_fixture(repo)
    assert repo.gate_status("decision1").freshness is GateDecisionFreshness.FRESH

    repo.register_policy_snapshot(
        PolicySnapshotSpec(
            name="policy-v2",
            requirements=(VerificationRequirement(contract="tests"),),
        ),
        actor=ACTOR,
        record_id="policy2",
    )
    repo.create_relation(
        RelationDraft(
            relation_id="sup_policy2_policy1",
            relation_type=RelationType.SUPERSEDES,
            source_id="policy2",
            target_id="policy1",
        ),
        actor=ACTOR,
    )
    repo.supersede("sup_policy2_policy1", actor=ACTOR)

    status = repo.gate_status("decision1")
    assert status.freshness is GateDecisionFreshness.STALE
    assert "policy1" in status.stale_input_record_ids


def test_decision_invalidation_stales_its_own_status(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_permit_fixture(repo)
    assert repo.gate_status("decision1").freshness is GateDecisionFreshness.FRESH

    create(repo, "obs1", RecordType.OBSERVATION)
    qualify_invalidation(
        repo,
        relation_id="invalidate_decision",
        cause_id="obs1",
        target_id="decision1",
    )

    status = repo.gate_status("decision1")
    assert status.freshness is GateDecisionFreshness.STALE
    assert status.stale_input_record_ids[0] == "decision1"


def test_bound_evidence_dependency_invalidation_stales_decision(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_permit_fixture(repo)
    create(repo, "assumption1", RecordType.ASSUMPTION)
    repo.create_relation(
        RelationDraft(
            relation_id="evidence_depends_assumption",
            relation_type=RelationType.DEPENDS_ON,
            source_id="evidence1",
            target_id="assumption1",
        ),
        actor=ACTOR,
    )
    repo.bind_dependency("evidence_depends_assumption", actor=ACTOR)

    # Binding a dependency to a still-current root does not change the derived
    # effective state, so the old decision remains fresh at this point.
    assert repo.gate_status("decision1").freshness is GateDecisionFreshness.FRESH

    create(repo, "obs1", RecordType.OBSERVATION)
    qualify_invalidation(
        repo,
        relation_id="invalidate_assumption",
        cause_id="obs1",
        target_id="assumption1",
    )

    status = repo.gate_status("decision1")
    assert status.freshness is GateDecisionFreshness.STALE
    assert "evidence1" in status.stale_input_record_ids
