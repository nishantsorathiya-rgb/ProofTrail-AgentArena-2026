# Organizer CI: approval boundary

## Status update — 25 October 2026

The [first organizer run](https://github.com/nishantsorathiya-rgb/ProofTrail-AgentArena-2026/actions/runs/40071997199)
completed with an overall failure. Its diagnostics reported that the technical
checks passed, but the organizer rejected result recording with HTTP 403 because
no competition submission entry existed. That attempt did **not** count as a
recorded organizer result. Do not treat successful local tests or secret creation
as a substitute for recorded competition checks.

Actions are enabled according to the owner, and `MESH_API_KEY` and
`SUBMISSION_TOKEN` have been configured privately. The connector cannot inspect
their values or independently verify the repository's Actions permission setting.
Before another push, confirm that the submission entry now exists, the dedicated
CI key has sufficient remaining quota, and a new execution's request, spending
and external-reporting effects are authorized. The first attempt's authorization
is consumed. No automatic rerun or provider-limit increase is approved here.

The following sections preserve the original preparation and budget proposal.
They are not an observation of current balances, current remaining quota or a
guarantee of a free checker run.

The [current public organizer instructions](https://career.krishnaik.in/hackathons/agent-arena-2026)
describe Mesh key validation as a metadata call that spends no credits. The
workflow still downloads the checker at execution time and does not pin its
request count or implementation. This published description does not establish
the key's current remaining quota or authorize another execution.

## Workflow and original budget proposal

The preserved organizer workflow triggers on every push. Permissions are
contents:read and id-token:write. It obtains a GitHub OIDC token with the specified
audience, uses SUBMISSION_TOKEN to download protected checks, passes MESH_API_KEY
to that code and sets an external organizer report endpoint. Concurrency can
cancel a superseded run; cancellation does not prove no request was charged.

The supplied brief describes metadata key validation. The workflow itself does
not reveal the downloaded checker's current model endpoints, request count,
retries, output-token limit or inference requirement. During original preparation,
that code had not been downloaded or executed. The later run above does not
establish the checker's future request behavior or an exact zero-cost envelope.

Proposed dedicated key label: ProofTrail-Organizer-CI-001. Never reuse B002 or a
general-purpose key. Prefer verified metadata-only scope if Mesh actually offers
it; that capability has not been confirmed. Do not assume an empty model list
denies inference. No Tavily key belongs in organizer CI.

Before any authorized push, confirm the checker contract. Preferred envelope is
one metadata request, zero completions, zero Tavily, no retries/reruns. If inference
is genuinely required, obtain a new owner approval for its exact model and calls.
A conservative alternative proposal, not approved, is model allow-list GPT-4.1-mini
only, at most one lifetime model request, 4096 lifetime model tokens, model USD cap
0.01, RPM/RPD 2/2, TPM 4096, one attempt, retries/fallback OFF, and the minimum
accepted overall key cap (historically USD 1). These provider caps are not proof
of a strict cumulative bill or exact checker output control.

Reserve USD 0.01 only if that alternate one-call envelope is explicitly approved.
At the historical saved input/output rates USD 0.40/1.60 per million, 4096 tokens
priced entirely as output would estimate USD 0.0065536. This is a planning example,
not a current price verification or guaranteed cap. Provider token accounting,
soft settlement limits, possible overshoot and unknown checker payloads remain.
If the contract cannot fit the approved envelope, stop instead of widening it.

The owner privately creates/configures any key and enters repository Actions
secrets MESH_API_KEY and SUBMISSION_TOKEN only after separate authorization.
No secrets or account API were accessed to prepare this file. One reviewed commit,
one push, one organizer execution, exact spending/routing limits and reporting
effects require explicit owner approval. No automatic retry or workflow dispatch.
Observe actual statuses for that exact commit before claiming critical CI passed.

## Separate publication from CI

The safest first-push proposal is to verify repository Actions are disabled under Settings > Actions > General, with no CI secrets entered, before authorizing that push. GitHub documents that disabling Actions prevents repository workflows from running. No setting was changed here. This keeps the original workflow byte-identical and separates public code review from unverified checker execution.

After code publication, confirming the checker contract, dedicated-key restrictions and separate CI approval remains necessary before enabling Actions or causing another push. Enabling Actions alone is not a claimed completed check. Any later empty commit or push to trigger the push-only organizer workflow requires its own explicit approval. A missing secret or [skip ci] message is not used as a substitute for disabling Actions.

Source: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository
