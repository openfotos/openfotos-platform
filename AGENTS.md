# OpenFotos agent instructions

These instructions apply to the entire repository.

## Engineering standard

Build production-quality code that is easy to understand, test, operate, and change. Correctness,
privacy, and authorization are required. Do not leave placeholders, silent
failure paths, or knowingly incomplete behavior unless the active session plan explicitly calls
for a scaffold. If a scaffold is necessary, make its boundary and next acceptance test clear.

Prefer the simplest design that fully satisfies the current requirement. Do not add abstractions,
configuration, dependencies, services, extension points, or generalized frameworks for possible
future needs. Reuse a concept only when it is truly the same concept. Keep unrelated concerns
separate.

Every function must perform one coherent task at one level of abstraction. Split a function when
it mixes policy with mechanics, validation with side effects, orchestration with detailed work, or
several independently changing operations. Do not split code into tiny wrappers that merely move
complexity elsewhere. Choose names that make the task and boundary obvious.

Keep control flow shallow and explicit. Prefer immutable values and explicit inputs and outputs.
Make state transitions, authorization decisions, object-key construction, and model compatibility
checks visible and independently testable. Avoid hidden global state and action at a distance.

Comments must explain a necessary reason, constraint, security property, or surprising tradeoff.
Do not narrate straightforward code, repeat names, preserve abandoned approaches, or add large
comment blocks where clearer structure and naming would suffice. Delete stale comments when code
changes.

## Working method

Before changing code, read the relevant product-plan section, `docs/SESSION_PLAN.md`, existing
tests, and nearby implementation. Search the repository before asking the user for facts the code
can answer. Preserve established contracts unless the task explicitly changes them.

Make each change a small vertical slice with one observable outcome. Keep desktop, server, storage,
contracts, and vision responsibilities within their documented boundaries. Treat tenant filters,
private object access, idempotency, selfie disposal, and model-version checks as invariants.

Test behavior and boundaries rather than implementation details. Add tests for meaningful failure
modes, especially authorization, tenant isolation, retries, checksums, lifecycle transitions, and
privacy cleanup. Do not write tests that only restate the implementation. Run the narrowest useful
checks while developing and the repository check script before handing off.

Do not commit secrets, production identifiers, model weights, client photographs, selfies, face
crops, or embeddings. Use synthetic or explicitly consented fixtures only. Never weaken a privacy
or authorization rule to make a test or deadline pass.

## Completion standard

A task is complete only when the requested behavior works, relevant tests and static checks pass,
failure behavior is intentional, and documentation or environment examples reflect material
changes. Report what changed, what was verified, and any concrete remaining limitation. Keep the
worktree reviewable and do not mix unrelated cleanup into the task.
