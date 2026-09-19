from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from sayf.domain import Actor, ActorKind, EventDraft
from sayf.storage import LedgerReadError, SQLiteEventStore

app = typer.Typer(help="Sayf evidence-driven engineering control plane.")
ledger_app = typer.Typer(help="Inspect and mutate the append-only causal ledger.")
app.add_typer(ledger_app, name="ledger")

DEFAULT_DB = Path(".sayf/ledger.sqlite3")


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
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
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
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(event.model_dump_json(indent=2))


@ledger_app.command("show")
def show_events(
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
) -> None:
    try:
        events = SQLiteEventStore(db).events()
    except LedgerReadError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps([event.model_dump(mode="json") for event in events], indent=2))


@ledger_app.command("verify")
def verify_ledger(
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
) -> None:
    result = SQLiteEventStore(db).verify()
    typer.echo(result.model_dump_json(indent=2))
    if not result.valid:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
