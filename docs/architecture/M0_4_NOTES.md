# M0.4 Review Ordering

Primary review is not yet frozen.

Required sequence for this branch:

1. Implementation candidate and tests.
2. Cross-platform CI until green.
3. Exhaustive primary review across intent, structure, correctness, failures, trust, operability, maintainability, and evidence.
4. Remediate primary findings.
5. Freeze first-pass findings baseline on one immutable head.
6. Invoke Codex independently against that exact head.
7. Independently verify/reconcile every Codex finding.
8. Re-run exact-head CI after any changes.
9. Mark ready only when evidence closes.

Codex must not be used to discover the primary review surface.
