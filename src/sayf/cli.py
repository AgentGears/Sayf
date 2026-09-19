from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from sayf.domain import Actor, ActorKind, EventDraft
from sayf.storage import SQLiteEventStore

app = typer.Typer(help="Sayf evidence-driven engineering control plane.")
ledger_app = typer.Typer(help="Inspect and mutate the append-only causal ledger.")
app.add_typer(ledger_app, name="ledger")

DEFAULT_DB = Path(".sayf/ledger.sqlite3")


@app.command()
def init(
    root: Annotated[Path, typer.Argument(help="Project root to initialize.")] = Path("."),
) -> None:
    db = root / DEFAULT_DB
    SQLiteEventStore(db).initialize()
    typer.echo(f"Initialized Sayf ledger at {db}")


@ledger_app.command("append")
def append_event(
    event_type: Annotated[str, typer.Option("--type", help="Event type.")],
    stream: Annotated[str, typer.Option("--stream", help="Logical stream ID.")] = "project",
    actor_kind: Annotated[ActorKind, typer.Option("--actor-kind")] = ActorKind.HUMAN,
    actor_id: Annotated[str, typer.Option("--actor-id")] = "local-user",
    payload: Annotated[str, typer.Option("--payload", help="JSON object payload.")] = "{}",
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
) -> None:
    try:
        payload_value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"payload is not valid JSON: {exc}") from exc
    if not isinstance(payload_value, dict):
        raise typer.BadParameter("payload must be a JSON object")

    draft = EventDraft(
        stream_id=stream,
        event_type=event_type,
        actor=Actor(kind=actor_kind, id=actor_id),
        payload=payload_value,
    )
    event = SQLiteEventStore(db).append(draft)
    typer.echo(event.model_dump_json(indent=2))


@ledger_app.command("show")
def show_events(
    db: Annotated[Path, typer.Option("--db")] = DEFAULT_DB,
) -> None:
    events = SQLiteEventStore(db).events()
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
