from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import BaseModel, ValidationError

from sayf.artifacts import ArtifactStoreError
from sayf.causal import CausalRepository
from sayf.domain import Actor, ActorKind, EventDraft
from sayf.graph import GraphDirection, GraphProjectionError
from sayf.records import (
    RECORD_CREATED_EVENT_TYPE,
    RELATION_CREATED_EVENT_TYPE,
    RecordDraft,
    RecordType,
    RelationDraft,
    RelationType,
)
from sayf.state import STATE_EVENT_TYPES
from sayf.storage import LedgerReadError, SQLiteEventStore

app = typer.Typer(help="Sayf evidence-driven engineering control plane.")
ledger_app = typer.Typer(help="Inspect and mutate the append-only causal ledger.")
record_app = typer.Typer(help="Create and inspect immutable typed records.")
relation_app = typer.Typer(help="Create immutable typed relations.")
graph_app = typer.Typer(help="Traverse the deterministic typed-record graph.")
state_app = typer.Typer(help="Qualify and inspect revision/staleness state.")
artifact_app = typer.Typer(help="Register and verify content-addressed artifacts.")
app.add_typer(ledger_app, name="ledger")
app.add_typer(record_app, name="record")
app.add_typer(relation_app, name="relation")
app.add_typer(graph_app, name="graph")
app.add_typer(state_app, name="state")
app.add_typer(artifact_app, name="artifact")

DEFAULT_DB = Path(".sayf/ledger.sqlite3")
DEFAULT_OBJECTS = Path(".sayf/objects")
_RESERVED_TYPED_EVENT_TYPES = {
    RECORD_CREATED_EVENT_TYPE,
    RELATION_CREATED_EVENT_TYPE,
    *STATE_EVENT_TYPES,
}


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _parse_payload(payload: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise typer.BadParameter(f"payload is not valid strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise typer.BadParameter("payload must be a JSON object")
    return value


def _actor(kind: ActorKind, actor_id: str) -> Actor:
    try:
        return Actor(kind=kind, id=actor_id)
    except ValidationError as exc:
        raise typer.BadParameter(f"actor fields are invalid: {exc}") from exc


def _repository(db: Path, objects: Path) -> CausalRepository:
    return CausalRepository.from_paths(db, objects)


def _echo_model(model: BaseModel) -> None:
    typer.echo(model.model_dump_json(indent=2))


def _echo_models(models: list[BaseModel] | tuple[BaseModel, ...]) -> None:
    typer.echo(json.dumps([model.model_dump(mode="json") for model in models], indent=2))


def _domain_error(exc: Exception) -> None:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1) from exc


@app.command()
def init(
    root: Annotated[
        Path,
        typer.Argument(help="Project root to initialize or validate."),
    ] = Path("."),
) -> None:
    db = root / DEFAULT_DB
    try:
        SQLiteEventStore(db).initialize()
    except LedgerReadError as exc:
        _domain_error(exc)
    typer.echo(f"Sayf ledger ready at {db}")


@ledger_app.command("append")
def append_event(
    event_type: Annotated[str, typer.Option("--type", help="Event type.")],
    stream: Annotated[str, typer.Option("--stream", help="Logical stream ID.")] = "project",
    actor_kind: Annotated[ActorKind, typer.Option("--actor-kind")] = ActorKind.HUMAN,
    actor_id: Annotated[str, typer.Option("--actor-id")] = "local-user",
    payload: Annotated[str, typer.Option("--payload", help="JSON object payload.")] = "{}",
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
) -> None:
    if event_type in _RESERVED_TYPED_EVENT_TYPES:
        raise typer.BadParameter(
            "typed Sayf event types are reserved; use the typed/state commands"
        )
    payload_value = _parse_payload(payload)
    try:
        draft = EventDraft(
            stream_id=stream,
            event_type=event_type,
            actor=Actor(kind=actor_kind, id=actor_id),
            payload=payload_value,
        )
    except ValidationError as exc:
        raise typer.BadParameter(f"event fields are invalid: {exc}") from exc

    try:
        event = SQLiteEventStore(db).append(draft)
    except LedgerReadError as exc:
        _domain_error(exc)
    _echo_model(event)


@ledger_app.command("show")
def show_events(
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
) -> None:
    try:
        events = SQLiteEventStore(db).events()
    except LedgerReadError as exc:
        _domain_error(exc)
    _echo_models(events)


@ledger_app.command("verify")
def verify_ledger(
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
) -> None:
    result = SQLiteEventStore(db).verify()
    _echo_model(result)
    if not result.valid:
        raise typer.Exit(code=1)


@record_app.command("create")
def create_record(
    record_type: Annotated[RecordType, typer.Option("--type")],
    payload: Annotated[str, typer.Option("--payload")] = "{}",
    record_id: Annotated[str | None, typer.Option("--id")] = None,
    actor_kind: Annotated[ActorKind, typer.Option("--actor-kind")] = ActorKind.HUMAN,
    actor_id: Annotated[str, typer.Option("--actor-id")] = "local-user",
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    objects: Annotated[Path, typer.Option("--objects")] = DEFAULT_OBJECTS,
) -> None:
    values: dict[str, Any] = {
        "record_type": record_type,
        "payload": _parse_payload(payload),
    }
    if record_id is not None:
        values["record_id"] = record_id
    try:
        draft = RecordDraft.model_validate(values)
    except ValidationError as exc:
        raise typer.BadParameter(f"record fields are invalid: {exc}") from exc

    try:
        record = _repository(db, objects).create_record(
            draft,
            actor=_actor(actor_kind, actor_id),
        )
    except (LedgerReadError, GraphProjectionError) as exc:
        _domain_error(exc)
    _echo_model(record)


@record_app.command("show")
def show_record(
    record_id: Annotated[str, typer.Argument()],
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    objects: Annotated[Path, typer.Option("--objects")] = DEFAULT_OBJECTS,
) -> None:
    try:
        record = _repository(db, objects).graph().record(record_id)
    except (LedgerReadError, GraphProjectionError) as exc:
        _domain_error(exc)
    _echo_model(record)


@record_app.command("list")
def list_records(
    record_type: Annotated[RecordType | None, typer.Option("--type")] = None,
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    objects: Annotated[Path, typer.Option("--objects")] = DEFAULT_OBJECTS,
) -> None:
    try:
        records = _repository(db, objects).graph().records
    except (LedgerReadError, GraphProjectionError) as exc:
        _domain_error(exc)
    if record_type is not None:
        records = tuple(record for record in records if record.type is record_type)
    _echo_models(records)


@relation_app.command("create")
def create_relation(
    relation_type: Annotated[RelationType, typer.Option("--type")],
    source_id: Annotated[str, typer.Option("--source")],
    target_id: Annotated[str, typer.Option("--target")],
    metadata: Annotated[str, typer.Option("--metadata")] = "{}",
    relation_id: Annotated[str | None, typer.Option("--id")] = None,
    actor_kind: Annotated[ActorKind, typer.Option("--actor-kind")] = ActorKind.HUMAN,
    actor_id: Annotated[str, typer.Option("--actor-id")] = "local-user",
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    objects: Annotated[Path, typer.Option("--objects")] = DEFAULT_OBJECTS,
) -> None:
    values: dict[str, Any] = {
        "relation_type": relation_type,
        "source_id": source_id,
        "target_id": target_id,
        "metadata": _parse_payload(metadata),
    }
    if relation_id is not None:
        values["relation_id"] = relation_id
    try:
        draft = RelationDraft.model_validate(values)
    except ValidationError as exc:
        raise typer.BadParameter(f"relation fields are invalid: {exc}") from exc

    try:
        relation = _repository(db, objects).create_relation(
            draft,
            actor=_actor(actor_kind, actor_id),
        )
    except (LedgerReadError, GraphProjectionError) as exc:
        _domain_error(exc)
    _echo_model(relation)


@graph_app.command("neighbors")
def graph_neighbors(
    record_id: Annotated[str, typer.Argument()],
    direction: Annotated[GraphDirection, typer.Option("--direction")] = GraphDirection.OUTBOUND,
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    objects: Annotated[Path, typer.Option("--objects")] = DEFAULT_OBJECTS,
) -> None:
    try:
        neighbors = _repository(db, objects).graph().neighbors(record_id, direction=direction)
    except (LedgerReadError, GraphProjectionError) as exc:
        _domain_error(exc)
    _echo_models(neighbors)


@graph_app.command("path")
def graph_path(
    start_id: Annotated[str, typer.Argument()],
    end_id: Annotated[str, typer.Argument()],
    direction: Annotated[GraphDirection, typer.Option("--direction")] = GraphDirection.OUTBOUND,
    max_depth: Annotated[int, typer.Option("--max-depth")] = 8,
    max_paths: Annotated[int, typer.Option("--max-paths")] = 20,
    max_expansions: Annotated[int, typer.Option("--max-expansions")] = 10_000,
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    objects: Annotated[Path, typer.Option("--objects")] = DEFAULT_OBJECTS,
) -> None:
    try:
        paths = _repository(db, objects).graph().provenance_paths(
            start_id,
            end_id,
            direction=direction,
            max_depth=max_depth,
            max_paths=max_paths,
            max_expansions=max_expansions,
        )
    except (LedgerReadError, GraphProjectionError, ValueError) as exc:
        _domain_error(exc)
    _echo_models(paths)


@state_app.command("bind-dependency")
def bind_dependency(
    relation_id: Annotated[str, typer.Argument()],
    actor_kind: Annotated[ActorKind, typer.Option("--actor-kind")] = ActorKind.HUMAN,
    actor_id: Annotated[str, typer.Option("--actor-id")] = "local-user",
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    objects: Annotated[Path, typer.Option("--objects")] = DEFAULT_OBJECTS,
) -> None:
    try:
        event = _repository(db, objects).bind_dependency(
            relation_id,
            actor=_actor(actor_kind, actor_id),
        )
    except (LedgerReadError, GraphProjectionError) as exc:
        _domain_error(exc)
    _echo_model(event)


@state_app.command("invalidate")
def invalidate_record(
    relation_id: Annotated[str, typer.Argument()],
    actor_kind: Annotated[ActorKind, typer.Option("--actor-kind")] = ActorKind.HUMAN,
    actor_id: Annotated[str, typer.Option("--actor-id")] = "local-user",
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    objects: Annotated[Path, typer.Option("--objects")] = DEFAULT_OBJECTS,
) -> None:
    try:
        event = _repository(db, objects).invalidate(
            relation_id,
            actor=_actor(actor_kind, actor_id),
        )
    except (LedgerReadError, GraphProjectionError) as exc:
        _domain_error(exc)
    _echo_model(event)


@state_app.command("supersede")
def supersede_record(
    relation_id: Annotated[str, typer.Argument()],
    actor_kind: Annotated[ActorKind, typer.Option("--actor-kind")] = ActorKind.HUMAN,
    actor_id: Annotated[str, typer.Option("--actor-id")] = "local-user",
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    objects: Annotated[Path, typer.Option("--objects")] = DEFAULT_OBJECTS,
) -> None:
    try:
        event = _repository(db, objects).supersede(
            relation_id,
            actor=_actor(actor_kind, actor_id),
        )
    except (LedgerReadError, GraphProjectionError) as exc:
        _domain_error(exc)
    _echo_model(event)


@state_app.command("show")
def show_effective_state(
    record_id: Annotated[str, typer.Argument()],
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    objects: Annotated[Path, typer.Option("--objects")] = DEFAULT_OBJECTS,
) -> None:
    try:
        state = _repository(db, objects).effective_state().state(record_id)
    except (LedgerReadError, GraphProjectionError) as exc:
        _domain_error(exc)
    _echo_model(state)


@state_app.command("affected")
def show_affected_records(
    record_id: Annotated[str, typer.Argument()],
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    objects: Annotated[Path, typer.Option("--objects")] = DEFAULT_OBJECTS,
) -> None:
    try:
        states = _repository(db, objects).effective_state().affected(record_id)
    except (LedgerReadError, GraphProjectionError) as exc:
        _domain_error(exc)
    _echo_models(states)


@artifact_app.command("put")
def put_artifact(
    source: Annotated[Path, typer.Argument()],
    media_type: Annotated[str | None, typer.Option("--media-type")] = None,
    name: Annotated[str | None, typer.Option("--name")] = None,
    metadata: Annotated[str, typer.Option("--metadata")] = "{}",
    record_id: Annotated[str | None, typer.Option("--id")] = None,
    actor_kind: Annotated[ActorKind, typer.Option("--actor-kind")] = ActorKind.HUMAN,
    actor_id: Annotated[str, typer.Option("--actor-id")] = "local-user",
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    objects: Annotated[Path, typer.Option("--objects")] = DEFAULT_OBJECTS,
) -> None:
    try:
        record, descriptor = _repository(db, objects).register_artifact_file(
            source,
            actor=_actor(actor_kind, actor_id),
            media_type=media_type,
            name=name,
            metadata=_parse_payload(metadata),
            record_id=record_id,
        )
    except (LedgerReadError, GraphProjectionError, ArtifactStoreError, ValidationError) as exc:
        _domain_error(exc)
    typer.echo(
        json.dumps(
            {
                "record": record.model_dump(mode="json"),
                "artifact": descriptor.model_dump(mode="json"),
            },
            indent=2,
        )
    )


@artifact_app.command("verify")
def verify_artifact(
    record_id: Annotated[str, typer.Argument()],
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
    objects: Annotated[Path, typer.Option("--objects")] = DEFAULT_OBJECTS,
) -> None:
    try:
        result = _repository(db, objects).verify_artifact_record(record_id)
    except (LedgerReadError, GraphProjectionError, ArtifactStoreError, ValidationError) as exc:
        _domain_error(exc)
    _echo_model(result)
    if not result.valid:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
