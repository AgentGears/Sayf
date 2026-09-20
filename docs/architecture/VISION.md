# Sayf Vision

## Purpose

Sayf is an evidence-driven engineering control plane for agentic software development.

It exists to preserve a mechanically inspectable chain from problem understanding to engineering action and, eventually, runtime reality.

## First principle

> **Agents propose. Evidence supports. Policy constrains. Sayf governs state.**

Probabilistic systems are useful for research, planning, implementation, review, and classification. They must not become authoritative simply by emitting confident text.

Sayf therefore separates:

- **reasoning surfaces** — humans, agents, tools, workflows;
- **authoritative state** — immutable events, accepted revisions, evidence, policy snapshots, gate decisions, provenance, and derived staleness.

## Three feedback loops

Sayf is intended to close three nested loops:

1. **Discovery / intent formation** — idea, problem, evidence, hypothesis, experiment, observation, intent revision.
2. **Engineering control** — intent, baseline, task graph, execution, verification, integration, policy-bound gate, release.
3. **Reality learning** — runtime observation, classification, impact propagation, and reopening of engineering or intent formation.

A failure is not automatically a coding defect. It may invalidate an implementation, task decomposition, baseline assumption, requirement, hypothesis, or the original problem framing.

## North-star query

Sayf should eventually answer:

> Why does the system currently believe Release R satisfies the accepted intent, and what evidence or assumptions could invalidate that belief?

The answer must be reconstructable from durable state rather than chat memory.

## Integration stance

Sayf is not intended to replace agentic workflow systems. It should integrate with them.

Examples:

- Spec-driven systems can propose specifications, plans, and task graphs.
- Governance/method packs can produce intent drafts, evidence bundles, drift checks, and review artifacts.
- Coding agents can produce changes and execution receipts.
- CI and runtime systems can produce verification and observation evidence.

Sayf owns the authoritative lineage connecting those inputs.

## Local-first boundary

The first implementation is deliberately local-first and inspectable:

- Python;
- SQLite;
- content-addressed artifacts;
- deterministic projections;
- Git-friendly human-readable views.

Distributed infrastructure, web UI, multi-tenant RBAC, and autonomous deployment are deferred until the semantics are proven.
