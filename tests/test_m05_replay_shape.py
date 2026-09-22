from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from sayf.causal import CausalRepository
from sayf.domain import Actor, ActorKind
from sayf.feedback import M05Projection, ReleaseSpec
from sayf.gates import (
    GateProjection,
    GateRequestSpec,
    PolicySnapshotSpec,
    VerificationReceiptSpec,
)
from sayf.records import (
    RecordDraft,
    RecordType,
    VerificationIndependence,
    VerificationRequirement,
    VerificationResult,
)

ACTOR = Actor(kind=ActorKind.HUMAN, id="m05-replay")
VERIFIER = Actor(kind=ActorKind.TOOL, id="m05-replay-verifier")
POLICY_ENGINE = Actor(kind=ActorKind.POLICY_ENGINE, id="m05-replay-policy")


def repository(tmp_path: Path) -> CausalRepository:
    return CausalRepository.from_paths(
        tmp_path / ".sayf" / "ledger.sqlite3",
        tmp_path / ".sayf" / "objects",
    )


def test_m05_projection_validates_gate_history_once_not_once_per_release(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    repo.create_record(
        RecordDraft(record_id="change1", record_type=RecordType.CHANGE_SET),
        actor=ACTOR,
    )
    repo.create_record(
        RecordDraft(record_id="evidence1", record_type=RecordType.EVIDENCE),
        actor=ACTOR,
    )
    repo.record_verification(
        VerificationReceiptSpec(
            subject_id="change1",
            contract="tests",
            result=VerificationResult.PASS,
            evidence_record_ids=("evidence1",),
            environment={"platform": "test"},
            independent_from_generation=VerificationIndependence.YES,
            verified_claim="tests passed for exact change1",
        ),
        actor=VERIFIER,
        record_id="receipt1",
    )
    repo.register_policy_snapshot(
        PolicySnapshotSpec(
            name="release-policy",
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
    repo.evaluate_gate("request1", actor=POLICY_ENGINE, record_id="decision1")

    for index in range(3):
        repo.register_release(
            ReleaseSpec(
                release_ref=f"R{index + 1}",
                subject_id="change1",
                gate_decision_id="decision1",
                environment={"stage": f"production-{index + 1}"},
            ),
            actor=ACTOR,
            record_id=f"release{index + 1}",
        )

    events = repo.event_store.events()
    original = GateProjection.from_events
    with patch(
        "sayf.feedback.GateProjection.from_events",
        wraps=original,
    ) as gate_replay:
        projection = M05Projection.from_events(events)

    assert len(
        [record for record in projection.graph.records if record.type is RecordType.RELEASE]
    ) == 3
    assert gate_replay.call_count == 1
