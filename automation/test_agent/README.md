# Trusted test-agent boundaries (T02 through T07)

This package uses only the Python standard library. It parses model proposals,
checks trusted context and test-edit policy, and constructs immutable changes.
T02 does not write edits, execute commands/tests, load skills, contact providers,
or publish results. T03 adds bounded GitHub reads and context collection. T04
adds advisory staged inference, authenticated skills, and a verification handshake.
T04 itself has no writer, test executor, runtime bootstrap, or production workflow.
T05 adds a separate trusted Docker verifier described below. Run
the automation tests from the root:

```sh
backend/.venv/bin/python -m pytest automation/tests -q
```

## Public boundary

`patches.prepare_changes(payload, context)` accepts JSON text or a plain mapping.
Invalid contracts raise `ContractError`; policy violations raise `PolicyError`.
`patches.assess_proposal` instead returns a host `PolicyAssessment` with
`proposal-accepted`/`proposal-rejected`, changes or a bounded rule description.
Acceptance means policy validation only. It supplies no verified/final status.
Pass untrusted input through these APIs, not the internal typed policy function.

Every model object rejects unknown fields, including status, permission, command,
context and limit overrides. JSON duplicate keys are also rejected. Schema version
is integer `1`; hashes are lowercase hexadecimal (40 for source Git identities,
64 for content). JSON input is bounded to 1 MiB; edit arrays have a 32-entry parsing
cap before the smaller policy limit. Frozen typed contracts validate construction.

A model proposal has exactly these fields:

```json
{
  "schema_version": 1,
  "source": {
    "schema_version": 1,
    "repository": "owner/repository",
    "pr_number": 12,
    "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "merge_base_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "head_sha": "cccccccccccccccccccccccccccccccccccccccc",
    "policy_sha": "dddddddddddddddddddddddddddddddddddddddd",
    "run_id": "run-12"
  },
  "decisions": [{
    "target": "frontend",
    "action": "generate",
    "selected_source_path": "frontend/src/value.ts",
    "plan": {
      "changed_behavior": "The value is accepted",
      "existing_test_evidence": [],
      "rationale": "Cover acceptance",
      "intended_test_files": ["frontend/src/value.test.ts"],
      "skill_ids": []
    }
  }],
  "edits": [{
    "target": "frontend",
    "path": "frontend/src/value.test.ts",
    "operation": "create",
    "original_sha256": null,
    "content": "import {it,expect} from 'vitest'\nimport {value} from './value'\nit('accepts', () => expect(value).toBe(1))\n"
  }]
}
```

Both `frontend` and `backend` decisions may appear together. `generate` requires
creates; `update` requires at least one update for that target and may contain
supplemental creates. `no-tests-needed`
and `unsupported` have no edits. Intended files must match each target's edits.
Plans are model claims. Empty skill IDs are valid; no skill use is fabricated.
Frontend runtime edits require `selected_source_path`: one available, captured,
changed head source, excluding tests/configuration/fixtures. Backend and non-edit
decisions use null; older T02 data may omit it. The runtime binds the explicit
subject, exact test path, operation, language and current API into edit and repair
requests. Frontend subjects also enter the accepted changes digest.
`SkillUseRecord` is a separate host record of actual skill loading with
ID, version, content hash, loaded-content hash, target and schema version.

`VerifierResult` and `HostResult` are host-only contracts. Verification records
carry profile/command IDs (not executable commands), baseline/patched kind,
execution result, collected count, exit code and a report reference with at most
64 KiB of declared output. A passed record requires exit 0 and collected tests.
Final statuses are `verified`, `verification-failed`, `baseline-failed`,
`unavailable`, `stale`, `unsupported`. Declaring `verified` requires passed
baseline and patched evidence in the host contract. T02 performs no execution;
T05 owns aggregation, actual evidence and source binding. Model deserialization
cannot construct these host contracts.

## Host context and allowed edits

The host constructs `TrustedContext` with a canonical absolute repository root,
validated `SourceIdentity`, immutable `changed_files`, and immutable
`permitted_test_paths`. The latter contains candidate test paths selected by
trusted context code for the changed behavior; generated tests need not already
be changed files. It only narrows these global path families:

- `frontend/src/**/*.test.ts` and `frontend/src/**/*.test.tsx`
- `backend/tests/**/test_*.py`

The model must echo the exact source identity. It cannot supply/enlarge either
host path set. T03 will collect the actual changed-file/context boundary; T02
does not infer relationships between source and tests or fetch any PR data.

Host policy limits: **8 edits**, **65,536 UTF-8 bytes per proposed file**,
**262,144 generated bytes total**. Duplicate target paths are rejected. The same
per-file bound limits reads of existing test content. Creates require a missing
target; updates require an existing regular file and the exact current SHA-256.
Operations never silently change meaning.

Paths must already be canonical ASCII POSIX relative paths; ambiguous paths are
rejected instead of normalized into acceptance. This excludes traversal, empty
or dot components, absolute/drive/UNC paths, backslashes, percent encoding,
Unicode normalization tricks, reserved Windows names, trailing dots and streams.
Reads use directory-relative no-follow handles at every component, rejecting
symlinks, hardlinks, directories, FIFOs and special/executable file modes. Ordinary
test modes `0600`, `0640`, `0644`, `0664` are supported; creates propose host-owned
`0644`. Platforms without required safe descriptor operations fail closed.

## Deterministic changes and heuristic limits

`TrustedChanges.to_bytes()` emits canonical UTF-8 JSON with sorted keys/paths,
host source/context, validated operations/content/modes, host-computed sizes and
new content SHA-256. This is a data representation, not executable patch text.
No filesystem writes occur. A future writer must revalidate original content,
target identity/mode and path safety immediately before applying changes with
appropriate concurrency controls; validation alone is a snapshot, not a lock.

Lexical checks use the same modifiers for direct and recognized `each` chains,
rejecting frontend skip/only/todo/conditional/expected-failure mechanisms and
backend skip/skipif/xfail/importorskip/expectedFailure mechanisms, including
several common bracket, focused-test and unittest variants. Pre-existing matched
text may remain while assertions change. Obvious loss of test declarations,
empty files or absence of recognizable assertions is rejected. Assertions can be
changed or some removed when tests and other recognizable assertions remain.

These checks are not language parsers or proof against weakening. Comments and
strings can cause false positives; aliases, computed members, uncommon/multiline
call chains, balanced removal/introduction of identical mechanisms, per-test
emptying and semantic weakening can escape detection. They also conservatively
reject refactors reducing test declaration count and tests using unrecognized
custom assertion/test APIs. Future sandboxed verification and human review are
still required. Runtime/provider selection, a writer, a verifier, CI integration
and GitHub publication remain outside T02/T03.

## T03 exact PR context

`context.collect_context(reader, run_id=..., workflow_id=...,
expected_head_sha=..., policy_sha=...)` accepts trusted caller inputs, using a
fresh `github.GitHubReader(expected_repository, transport)` for each collection.
The policy SHA identifies the trusted host code/configuration, never PR text.
The bundle contains the T02 `SourceIdentity` plus numeric repository, workflow,
run and attempt identities. It supplies evidence; it never decides whether
behavior requires tests and contains no agent, provider, writer or verifier.

The reader exposes workflow/PR metadata, exact-commit PR associations,
paginated PR files, exact-SHA compares, commit/tree inventory and Git blobs.
`GitHubHTTPTransport(token=None)` uses HTTPS GET exclusively to
`api.github.com`, a 10-second socket timeout, fixed endpoint/query grammar,
bounded responses and no redirects. Credentials are explicitly injected;
environment variables, credential files and repository URLs are never read.
Pagination consumes only the next-page indication and constructs its own page
parameters. Tests inject deterministic responses and require neither network
nor credentials. T03 has not been smoke-tested against the live API.

Collection requires one open same-repository PR with the run's exact head.
Repository names and numeric IDs must agree for run, base and head; forks,
missing/duplicate/ambiguous associations and malformed metadata fail closed.
Metadata is checked again at the end to catch PR changes during collection.
GitHub does not provide atomic multi-request snapshots; later use must
revalidate the identity again.

The base-to-head compare pins the merge-base. A second merge-base-to-head
compare supplies the file records, cross-checked against the first compare and
every paginated PR file. Counts, names, statuses, rename predecessors and blob
SHAs must agree. Commit evidence must reach the exact head. The changed-file
limit is below GitHub's compare-file ceiling; comparisons over 250 commits,
truncated tree inventories and unsupported file statuses fail closed.

Removed files have historical content only; renames retain both names and the
predecessor at the exact merge-base. Content comes from the pinned commit's
tree, with blob SHA/size and Git blob digest validation. Symlinks, submodules
and executable file modes are not read as source. Missing patches stay missing;
hunk/count checks identify insufficient patches without fabricating text.
Oversized, binary and unsupported evidence has explicit states. Secret-shaped
paths such as `.env` are excluded, including their patches. Other paths outside
the source/test/config allowlists retain change metadata without content.

Host constants bound collection:

| Resource | Limit |
| --- | --- |
| Changed files / pages per list | 100 / 4 (30 entries per page) |
| Patch / source / existing test per file | 16 / 64 / 32 KiB |
| Config or fixture per file | 16 KiB |
| Complete context, including canonical JSON | 512 KiB |
| Selected tests / permitted test paths | 16 / 32 |
| Test reference scan | 64 files, 256 KiB |
| Config and fixture files combined | 8 |
| Tree inventory / compare commit evidence | 4,000 entries / 250 commits |
| GitHub reads / response / total response bytes | 160 / 1 MiB / 8 MiB |

Only changed frontend TS/TSX, backend Python, existing tests and fixed known
test configuration/fixtures supply textual evidence. No imports are followed
for source collection. A bounded scan of known test paths finds named,
colocated, same-feature/module and lexical module-reference relationships.
Reasons are host-generated and recorded per test; matching text remains
untrusted data and is not proof of coverage. Named/reference tests take priority
over broader directory matches. Test-count omissions and unreadable reference
evidence have explicit markers; inventory/scan/total budget exhaustion fails
closed. No missing evidence means “no tests needed.”

Configuration paths are fixed: frontend Vite/setup, backend pyproject/conftest
and root pytest.ini. Fixtures are limited to the repository's known frontend
todoFixture and backend mocks, selected only when referenced by chosen tests
or configuration. These files are data, even when they contain instructions.
AGENTS, README, skill-looking files, PR bodies and arbitrary URLs cannot change
limits, select credentials, register skills or cause execution.

`ContextBundle.to_bytes()` emits canonical UTF-8 JSON with sorted keys/paths,
schema, source/run identity, evidence states, reasons, limits and usage;
`sha256` hashes those bytes. No local absolute paths or authentication are
serialized. `bundle.trusted_context(repository_root)` supplies immutable
changed/current paths and permitted test paths to unchanged T02 validation.
Candidates include readable selected tests and missing named tests for readable
changed source. They confer no test-required decision. The caller must prepare
and independently validate an exact-head checkout before later T02 filesystem
checks; the bridge does not verify checkout identity.

`automation/tests/t03_fixtures.py:sample_bytes()` generates a sanitized,
reproducible frontend/backend handoff sample from the same fixtures as the tests.
Collection never reads the local checkout, imports PR code, runs application
tests, evaluates text, follows source-selected URLs or changes any files.
Reference/coverage inference, broader layouts, provider/runtime selection,
verification, CI integration and publication remain future tasks.

## T04 advisory UnitTestAgent

`agent.UnitTestAgent(bundle, repository_root, provider)` consumes a trusted T03
`ContextBundle` and a host-prepared exact-head checkout. `run()` returns an
immutable `AgentResult`; it does not install its proposed tests. The immutable
`UNIT_TEST_AGENT` definition describes its role, advisory authority, host
authority, and prohibitions. No definition comes from analyzed repository text.
T02 contracts and policy and T03 collection remain unchanged.

P01's completed CPU experiments selected Qwen2.5-Coder while showing that a
monolithic complete-proposal request exceeds useful bounded CPU inference.
T04 therefore requests a compact **decision separately for each represented
target**, followed by **edits only for generate/update**. The host translates
strict stage JSON into `TargetDecision`/`ProposedEdit`, assembles `ModelProposal`,
and calls the existing public `assess_proposal`. No model emits a complete T02
proposal or selects source identities, permissions, skills, or provider settings.

The explicit state transitions are:

```text
INITIAL -> DECIDING -> [GENERATING_EDITS -> DECIDING for another target]
                   -> PROPOSAL_READY -> FINISHED (no edits)
                                     -> AWAITING_VERIFICATION (edits)
AWAITING_VERIFICATION -> REVIEWING_VERIFICATION -> FINISHED
AWAITING_VERIFICATION -> REPAIRING (frontend, once) -> AWAITING_VERIFICATION
Any active phase -> FAILED on a contract, policy, or budget rejection
```

The bracketed branch is optional; a final edit stage goes directly to
PROPOSAL_READY. FINISHED means the reasoning lifecycle is complete. The separate
`outcome` is `no-tests-needed`, `unsupported`, or the exact host verdict after
verification; FINISHED alone never means verified. Any accepted edited proposal
must return `proposal-awaiting-verification` first. Re-running an instance,
resuming a terminal result, or skipping the handshake is rejected.

### Stage contracts and bounded projections

Every stage returns exactly `binding`, `target`, and these fields:

| Stage | Additional response fields |
| --- | --- |
| Decision | `action`, `changed_behavior`, `existing_test_evidence`, `rationale`, `intended_test_files` |
| Edits | `edits`: objects with `path`, `operation`, `original_sha256`, `content` |
| Frontend compiler repair | `replacement_content`: complete source at the host-fixed accepted path |

Decision responses contain no test code. Evidence citations must name available,
unclipped existing tests in that target's projection; no-tests-needed requires a
citation. This validates evidence provenance, not the model's coverage judgment.
Non-edit decisions have zero intended paths. Intended edit paths must be T03
candidates within T02's test families. Edit generation receives the accepted
decision, intended paths, bounded evidence and full-original content SHA-256
for readable existing tests; proposed updates still undergo T02's filesystem
and original-hash checks. All existing edit-count, byte, path, source, and
anti-weakening checks remain authoritative.

`project_context` sorts evidence deterministically and selects only the current
stack's changed metadata, source/hunk evidence, related tests, candidates,
configuration and fixtures. Root pytest configuration belongs only to backend.
Edits further restrict tests/candidates to intended paths. Source/test bodies
are at most 4 KiB each; patches/configuration/fixtures at most 2 KiB each.
Clipping is explicit, with content digests for evidence bodies. A clipped test
cannot supply a decision citation or an update original. Total projection
overflow fails rather than silently discarding evidence. No source imports,
arbitrary local files, README/AGENTS instructions or URLs are followed.

Each request includes the exact host source identity and complete T03 bundle
digest. The host hashes canonical source/context/stage/target/input bytes into
the binding. Edit bindings also include the accepted decision and frontend
language facts derived from the path (`.ts`: no JSX; `.tsx`: JSX allowed).
Repair bindings include that path, prior content, current accepted changes,
bounded compiler diagnostics and verifier command evidence digests, and attempt
number 1. The model must echo the
expected binding and target. Unknown fields, tools, duplicate keys, non-finite
numbers, fences, surrounding prose and stale bindings fail. There is no JSON
repair or coercion. Native JSON schema constrains the bounded completions.

### Fixed trusted skills

`skills.REGISTRY` is immutable host configuration. Each entry fixes the ID,
version, repository-relative path, SHA-256 and target applicability. The four
version `1.0.0` files under `automation/test_agent/trusted_skills/` are:

| Skill | Applicability | SHA-256 |
| --- | --- | --- |
| analyze-change | both targets | `fb879bcaa02da848e53b7f2ed0b586e122b7759de574bccf79abb38c1b02d284` |
| frontend-unit-tests | frontend | `7d9fa26070d887afaf8f9cece72e197754d3283cb550e732b1251fa67b425952` |
| backend-unit-tests | backend | `3ed8aa01cc9120212cc1a920fadf9fa29b66e1d855c392513652b523fcdacad8` |
| verify-test-patch | both targets | `92ebfefbb247366f4aad15336f712fc2dd152d129e823ef1f1dc14d0ee26ff96` |

Loading uses T02's descriptor-relative no-follow inspection. Missing files,
symlinks including ancestors, hardlinks, directories/special files, unsafe modes,
unknown IDs, traversal, oversized bodies and hash mismatches fail closed.
Only the fixed trusted installation root is used. No PR, environment variable,
directory discovery, URL or model output registers skills. Decision requests
insert analyze-change plus the target skill; edit requests insert the target
skill; review requests insert verify-test-patch. Each inserted body produces an
actual T02 `SkillUseRecord` with matching expected and loaded hashes. FakeProvider
tests compare exact loaded bodies and records with captured requests.

### Provider boundary and selected pin

`provider.Provider.infer(ProviderRequest, timeout)` is independent of llama.cpp.
`FakeProvider` consumes finite raw transcripts and captures immutable requests.
`LocalLlamaProvider` accepts only a host-owned integer port (1024–65535; default
8765) at **127.0.0.1**. It exposes no URL, API key, credentials, external host,
paid fallback, redirect, proxy, model download or process startup option.

The selected runtime pin is:

```text
id: qwen25-coder-7b-q4km
repository: Qwen/Qwen2.5-Coder-7B-Instruct-GGUF
revision: 13fb94bfda8c8cf22497dc57b78f391a9acb426a
filename: qwen2.5-coder-7b-instruct-q4_k_m.gguf
bytes: 4683073536
sha256: 509287f78cb4d4cf6b3843734733b914b2c158e43e22a7f4bf5e963800894d3c
llama.cpp: d81235049384534c167caea52b85a694f6103d14
```

Each inference uses the pinned server's native
[`/apply-template`, `/tokenize`, `/completion` endpoints](https://github.com/ggml-org/llama.cpp/blob/d81235049384534c167caea52b85a694f6103d14/tools/server/README.md).
All three share one absolute deadline. Socket inactivity timeouts plus a deadline
timer closing the socket bound slow response headers/bodies. Status codes other
than 200, unsupported encodings, oversized responses, malformed token metadata,
context overflow and truncation fail. Sampling is fixed: seed 42, temperature 0,
top-k 1, top-p 1, min-p 0, repeat penalty 1, no stream and no prompt cache.
The adapter requires bounded completion-token evidence from the pinned server's
[`tokens_predicted` response field](https://github.com/ggml-org/llama.cpp/blob/d81235049384534c167caea52b85a694f6103d14/tools/server/server-task.cpp).
It cannot attest which
model a loopback server loaded; T07 must start and verify the pinned runtime.
No real model inference or adapter/server integration has been demonstrated here.

### Limits and trace

Limits are trusted constants, never stage payload options:

| Resource | Bound |
| --- | --- |
| Targets / inference calls / rounds per stage per target | 2 / 5 / 1 |
| Active agent wall time, accumulated across run/resume | 1,000 seconds |
| Each inference including formatting/tokenization | 300 seconds or remaining active budget |
| Decision / edits / repair completion tokens | 256 / 768 / 768 |
| Decision / edits / repair raw response | 4 / 16 / 16 KiB |
| Canonical provider request / target projection | 24 / 16 KiB |
| HTTP request or response / total HTTP calls | 128 KiB each / at most 15 |
| Tokenized prompt plus completion | 8,192 tokens |
| Each trusted skill body | 2 KiB |
| Trace events / trace bytes | 48 / 64 KiB |
| Trusted verification records | 8 |

These are ceilings, not performance measurements. P01's CPU evidence motivates
smaller decision completions and separate targets; 768-token edits/repairs may
still time out and are not retried. Human/verifier wait time is excluded from
the active agent budget, which is preserved when resuming the same instance.
The concrete local adapter enforces deadlines during HTTP; arbitrary injected
providers are trusted host capabilities and must honor the timeout contract.
The agent also rejects late returns. No provider/clock injection comes from data.

The immutable canonical trace records exact source/context digest, definition
digest, state transitions, projection digest/bytes, stage/target/binding,
request/system/response digests and bytes, inserted skill records, T02 assessment,
feedback receipt digest/host status and final state. It omits raw prompts,
responses, generated code, interpretations, logs, credentials, environment and
absolute checkout paths. Two event slots and 1 KiB are reserved for terminal
failure. Trace request digests link skill records to exact captured requests;
the trace alone is not an independent archive of those request bodies.

### Trusted verification handshake and CLI

`resume(HostResult)` is valid only on AWAITING_VERIFICATION. It requires exact
source equality, bounded existing `VerifierResult` records, and their unchanged
T02 contract validation. VERIFIED requires successful baseline and patched
execution evidence. Host feedback directly selects the terminal outcome;
there is no model verification interpretation or runtime restart for a verdict.
Failed, not-run, baseline-failed, unavailable or stale evidence never becomes
verified. T05 must produce real trustworthy evidence tied
to the exact proposal and target profiles; Python host types are not evidence
authentication or a sandbox against a malicious trusted caller.

One frontend repair is eligible only after passing baseline typecheck and suite,
and a failed patched non-pretty TypeScript check (exit 2, no timeout/runtime
error/clipping). Every diagnostic must resolve to the sole accepted frontend
test path. Unknown codes, mixed files, raw logs, malformed or oversized messages
fail closed. At most eight diagnostics contain code, bounded line/column and a
fixed host-owned explanation; compiler-quoted source literals are never sent.
The runtime is stopped during verification and restarted only for this repair.
The replacement retains the original operation/path/source binding, undergoes
ordinary T02 policy again, and may not reduce recognizable tests/assertions.
A fresh isolated verifier repeats the guards, patched typecheck, targeted tests
and full suites. Its verdict is terminal even if it produces repairable errors.
There is no backend repair. Lexical checks cannot prove semantic assertion
strength or behavioral relevance; review remains necessary.

The CLI uses explicit provider selection and stdout only:

```sh
backend/.venv/bin/python -m automation.test_agent \
  --context /trusted/context.json --context-sha256 <collector-digest> \
  --repository-root /trusted/exact-head-checkout \
  --provider fake --transcript /trusted/stage-responses.json
```

Fake transcripts are JSON arrays of raw, already-bound response strings, at most
six entries; no placeholders or substitution occur at runtime. Local mode uses
`--provider local` and optional `--port`; it assumes a separately verified
server already exists. Mixing local mode with transcripts or fake mode with
ports is rejected. Input paths are explicitly supplied by the trusted caller;
bounded regular-file reads never execute their content. A host-pinned digest
and canonical T03 serialization are required for context loading. The digest
must arrive from trusted collection, never from the same untrusted PR/model
artifact. The CLI does not collect GitHub data or validate checkout identity.

`--resume-stdin` emits and flushes the awaiting proposal, then consumes one
bounded trusted JSON feedback line in the same process. That line has exactly
`proposal_sha256` and `host_result` (the serialized T02 host contract). The
proposal digest must match the emitted proposal. It then emits the resumed
result. Without this option the process exits with the awaiting draft. Exit 0
means a valid draft or completed reasoning lifecycle, not verified tests; exit 1
means rejection; invalid CLI options use argparse's exit 2. T07 should inspect
`state`/`outcome`. HostResult JSON is accepted only on this host input channel,
never from provider output. There is no unauthenticated persistent checkpoint.

Focused deterministic checks:

```sh
backend/.venv/bin/python -m pytest automation/tests/test_agent.py automation/tests/test_provider.py -q
backend/.venv/bin/python -m pytest automation/tests -q
backend/.venv/bin/python -m compileall -q automation
git diff --check
```

Tests inspect substantive Vitest/RTL and pytest/FastAPI fixture code as data.
They cover generation, exact-hash updates, covered/unsupported/mixed targets,
skills, state transitions, binding, forbidden tools, malformed/oversized output,
T02 rejection, budgets, CLI handshake and host verdict authority. HTTP is mocked;
no generated test, model, network request or application suite runs in these
tests. Live output reliability/latency, test correctness, compilation and coverage
are unmeasured by T04. T05 adds independent verification; T07 trusted runtime/CI
integration remains unimplemented. Production workflows and GitHub publication
are unchanged.

## T05 isolated deterministic verification

`verification.Verifier().verify(source, repository_root, accepted_changes)`
accepts an exact host `SourceIdentity`, a canonical absolute checkout, and the
`TrustedChanges` returned by T02 policy acceptance. Raw proposals, JSON command
requests, image names, environment settings, mount settings and shell commands
are not execution authorization. The optional stack IDs are only
`frontend-v1` and `backend-v1`; every affected stack must be included. These
select complete immutable sequences, rather than allowing a caller to omit a
guard. Executor injection is a trusted unit-test seam and never model input.

The caller must load this package and its verifier files from a trusted policy
installation, independently of the analyzed checkout. `TRUSTED_ROOT` identifies
that installation; the analyzed root never supplies a Dockerfile or helper.
This library does not authenticate a malicious Python host caller. T07 must
provide that trusted installation and orchestrate the T04 handshake. T05 neither
calls a provider nor resumes the model; its `report.host_result` can be passed
to T04 later. The unchanged host contracts remain authoritative.

### Source and proposal binding

Preparation checks HEAD, canonical Git root, the expected GitHub origin,
base/merge-base identity and the exact changed-file inventory against the
accepted T02 context. Staged changes and every tracked file's raw Git blob hash
are checked without running worktree clean filters, external diff, textconv,
hooks or fsmonitor. The only allowed dirty tracked preparation file is this
trusted README. Untracked application material is rejected. Tracked symlinks,
submodules, executable modes, unsafe filesystem types and hardlinks fail closed.
Source SHA and canonical accepted-changes SHA-256 accompany every command.

Dependency and runner changes are unsupported before image execution. Fixed
rules cover package/lock files, Vite/Vitest/Jest/Babel/TypeScript configuration,
Python requirements, pyproject/setup/tox/pytest/coverage configuration and every
conftest. All required current dependency/configuration files also have fixed
trusted SHA-256 pins. Unknown executable configuration and unsupported test
discovery conventions fail closed; PR instructions cannot broaden these rules.

Only bounded tracked application/test material is copied: frontend src and
Storybook TS/TSX, CSS/JSON, index and the fixed config/manifests; backend app/tests
Python and fixed manifests/configuration; root pytest.ini. Git/GitHub metadata,
automation, host dependencies/environments, caches, build/coverage/review output,
`.env*`, credential/SSH paths and model files are excluded. Input is capped at
1,000 admitted files, 2 MiB per file and 8 MiB total. Descriptor-relative reads
use no-follow/nonblocking handles and require regular single-link files.

The host creates private temporary baseline and patched trees from identical
checked bytes. The real checkout receives no application/test edits. Accepted
CREATE/UPDATE operations are revalidated immediately before descriptor-based
writes, including path, operation, missing target/original SHA, mode, inode and
link count. No model patch text is passed to git apply. Copied file permissions
are normalized for non-root reading only after revalidation/application.

### Build plane and execution plane

`build_image()` makes a fresh bounded build context containing exactly the
trusted Dockerfile, runner, shared profile definitions, collection hook,
package.json/package-lock.json and requirements.txt. It has **no PR source or
generated tests**. Node dependencies use npm ci with lifecycle scripts disabled;
Python uses the unchanged pinned requirements. Reduced npm socket concurrency
addresses observed intermittent build-network failures. Build network access
is fixed host configuration; no credentials, tokens, proxy environment,
credential files or host Docker client configuration are inherited. Each client
gets a fresh empty temporary HOME/Docker-config directory. No build secrets or
build arguments are accepted. Runtime installation is absent.

Official base versions match ordinary CI: Node 22.23.2 and Python 3.12.13,
both Debian bookworm slim. Successful registry pulls verified these pinned
references during development:

```text
node:22.23.2-bookworm-slim@sha256:48e4b67d85f87bd551df43704e24d252f56cc5f8e9718841aace50f19948f0f9
python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
```

Provenance records Dockerfile/helper/dependency hashes, base references, actual
build exit code, local image ID and actual RepoDigests. An image label binds the
entire trusted context. Before reuse, current preparation hashes, image ID,
entrypoint, user, environment and absence of volumes/healthchecks are inspected.
Changing trusted helpers invalidates old image evidence and requires rebuilding.

Only after building does sanitized input enter the execution plane. Each command
gets a fresh ephemeral container, a read-only sanitized `/input` bind, and these
fixed controls, checked against effective Docker inspect values before start:

| Control | Value |
| --- | --- |
| User | 10001:10001 |
| Network / IPC / PID | none / none / private |
| Capabilities / privileges | drop ALL; no-new-privileges; never privileged |
| Seccomp | daemon builtin default required by Docker info; never unconfined |
| Root filesystem | read-only |
| Memory / swap / CPUs / PIDs / open files | 1 GiB / no extra swap / 2 / 128 / 1024 |
| Tool wall timeout / ordinary Docker client timeout | 120 seconds / 20 seconds |
| Build timeout | 600 seconds |
| Writable tmpfs | /work 256 MiB; /tmp 64 MiB; /reports 1 MiB |
| Runtime logging | Docker log driver none; bounded captured data only |

There are no device, Docker-socket, credential, runner-workspace or writable host
mounts, port publishing, extra hosts or inherited host environment. Tmpfs is
nosuid/nodev; /tmp and /reports are also noexec. /work permits normal framework
operation. The runner sets an explicit minimal child environment. Containers
never install dependencies or access the network.

All host subprocesses use fixed executables, validated argv tuples, shell=False,
bounded pipes and hard deadlines. Timeout kills the client process group and
the finally block forcibly removes the actual container. A host-generated name
exists before create, so create timeouts and malformed IDs also get cleanup.
An exact-name daemon list must confirm absence; a failed client is never proof
of removal. Cleanup failure prevents verification success.

### Fixed profiles and UPDATE guards

The shared immutable registry fixes direct installed binaries; no npm scripts
or PR-selected commands run.

| Stack | Required sequence |
| --- | --- |
| frontend-v1 | baseline typecheck; baseline suite; patched typecheck; changed-test suite; full patched suite with V8 coverage |
| backend-v1 | baseline suite; changed-test suite; full patched suite with branch coverage |

All baseline guards across selected stacks run before patched checks. Test lists
come from host enumeration of recognized paths, not model filters. Targeted
paths come exclusively from accepted edits and undergo renewed path validation.
Full patched lists include every CREATE and UPDATE. Vitest CLI paths are substring
filters, so the trusted runner additionally removes every nonselected recognized
test from that command's temporary discovery tree and the host requires the
exact collected-file inventory in the report. No permanent test is removed.

CREATE paths are absent on baseline. UPDATE paths alone are excluded from its
guard list; every other pre-existing test remains. Frontend baseline typecheck
uses a small host-generated tsconfig extending the pinned project config,
preserving existing/default exclusions and adding only accepted UPDATE paths.
The patched typecheck uses the normal project config. TypeScript can still
follow imports of an excluded test; that conservatively fails the guard. An
empty baseline test inventory is unsupported, since the unchanged HostResult
contract requires actual successful baseline test collection.

Vitest uses its supported runner config loader because the default bundle loader
attempted to write inside read-only node_modules during actual smoke. This only
changes temporary config loading, not coverage include/exclude rules. Backend
uses Python -I, explicit pinned pyproject configuration, disabled ambient plugin
autoload and the image-owned collection hook. The full backend profile explicitly
loads pytest-cov and preserves source, branch and omit semantics. Full profiles
change coverage reporters/output locations only. Coverage has no pass threshold.

### Evidence, reports and verdicts

Framework schemas were checked against installed Vitest 4.1.11 reporter code and
pytest 9.1.1 JUnit implementation. JSON duplicate keys/non-finite values, invalid
types, impossible/inconsistent counts, duplicate files/cases, unexpected paths,
missing reports and zero collection fail closed. JUnit DTD/entities are rejected;
an image-owned pytest hook supplies the actual session.testscollected and file
inventory, rather than mislabeling collection errors as collected tests. Both
targeted and full suites require exit 0, positive collection, passed tests, no
failures/errors and nonzero collection in every selected file.

Docker exec supplies the host-observed tool exit code. After it terminates, a
fixed image-owned exporter stops every surviving process except itself and idle
PID 1 before reading named reports through no-follow regular single-link file
handles. Docker archive-copy did not expose tmpfs reports on the tested daemon;
the exporter instead sends a bounded canonical base64 JSON packet. The container
is then paused, inspected and removed. Framework files remain bounded to 128 KiB
each, report tmpfs to 1 MiB and transfer packets to 1 MiB. Reports are never read
as instructions or mounted writable on the host.

Host-owned temporary output contains bounded framework files and command JSON.
`VerificationReport.to_bytes()` excludes its host output-directory handle and
uses only relative report references. Detailed command evidence includes profile
and command IDs, kind, image/source/change identities, duration, actual exit,
timeout, test counts, report hashes/bytes and observed stdout/stderr hashes/bytes.
Each output pipe retains at most 32 KiB while hashing/counting drained bytes.
Raw logs are not persisted or rendered; workflow command syntax, ANSI escapes,
URLs and shell-looking output remain uninterpreted data. T06 owns later rendering.

Typecheck evidence has **no collected-test count** and is retained separately as
CommandEvidence. Its exit status still gates the final verdict. Actual test
executions alone produce existing VerifierResult records (at most six for both
stacks), which fit T04's unchanged eight-record handshake budget.

| Outcome | Host status |
| --- | --- |
| Exact source/context/edit mismatch | STALE |
| Dependency/configuration/layout unsupported | UNSUPPORTED |
| Image, daemon, isolation or cleanup unavailable | UNAVAILABLE |
| Unrelated baseline guard fails | BASELINE_FAILED |
| Patched typecheck, targeted/full tests or report/collection validation fails | VERIFICATION_FAILED |
| Every required guard and patched check passes | VERIFIED |

### Local validation and remaining boundaries

Deterministic orchestration tests execute no proposal code on the host:

```sh
backend/.venv/bin/python -m pytest automation/tests/test_verification.py -q
backend/.venv/bin/python -m pytest automation/tests -q
backend/.venv/bin/python -m compileall -q automation
git diff --check
```

The separately gated integration module builds the trusted image and exercises
actual baselines, good/broken/zero-collection synthetic proposals, timeout cleanup
and dummy-only isolation probes inside Docker:

```sh
T05_DOCKER_SMOKE=1 backend/.venv/bin/python -m pytest automation/tests/test_verification_docker.py -q
```

The gate prevents ordinary automation tests from requiring Docker/network/model
services. The smoke's portable bounded evidence is written outside the source
tree to `/mnt/data/T05-docker-evidence.json`. No real credential is used. The
review report records the actual command outcomes and image identity; the
presence of integration tests alone is not evidence that they passed.

Actual Docker execution on source
`b9dd41a28f223f210e73a3071656a422584c75e3` passed baseline frontend typecheck,
18 frontend tests and 21 backend tests. The good two-stack CREATE proposal
passed patched typecheck, one targeted test per stack, 19 full frontend tests
and 22 full backend tests, with the existing coverage settings. Each deliberately
broken targeted proposal collected one test, exited 1 and became
VERIFICATION_FAILED. Exact image identity, final smoke matrix and local check
results are recorded in the external T05 review evidence, not inferred from
these fixture definitions.

Docker shares the host kernel and trusts its daemon/runtime, local policy
installation and private temporary-directory ownership. This is not a VM or
an independent attestation of framework reports: hostile test code can affect
its framework's process/report content inside the sandbox. Host-observed exits,
strict schemas and collection checks do not prove semantic assertions or defeat
all report forgery. T02 lexical anti-weakening checks and human review remain
necessary. No model can alter host permissions, network, mounts or profiles.
Daemon failure can prevent confirmed removal; that is explicitly unavailable,
never a verified result. Report tampering, shared-kernel isolation and Linux/Docker
portability are residual limits, not hidden success claims.

T06 consumes this evidence without changing verifier verdicts; its boundaries
are described below. T07 orchestration is described in the final section.
No production workflow, ordinary CI edit, application edit, provider invocation,
model download, hosted service activation or GitHub write belongs to T05.

## T06 reports, publication adapter and artifact preparation

`reporting.prepare_report(bundle, agent_result, verification_report)` validates
the host-owned T03/T04/T02/T05 chain before rendering. The context digest and
complete source identities must agree, including the policy SHA. Accepted
changes must exactly match proposal edits and context path inventories; verifier
commands and results must bind to the head and SHA-256 of those exact canonical
changes. An agent feedback result must equal the verifier's host result.
Source workflow metadata supplies numeric CI run/attempt/repository identities;
it must agree with the source head and run ID. The separate `AgentRun` argument
supplies publication order. T07 always supplies it explicitly; the default exists
only for compatibility with the original offline T06 callers. Arbitrary execution dictionaries and
model-produced reporting objects are rejected. Caller-supplied Python objects
are trusted capabilities, not an authentication mechanism for uploaded JSON.

The comment separates **Agent analysis (advisory)** from **Host evidence
(authoritative)**. Behavior, rationale, target decisions and review suggestions
remain advisory. Only typed verifier evidence supplies execution status, fixed
profile IDs, baseline/patched outcomes, collected/passed/failed/error/skipped
counts, exit codes, timeouts and bounded relative report references/digests.
Typecheck has no test count. Raw logs, model responses, build output, arbitrary
error text and absolute host report handles are omitted.

| Final report status | Meaning |
| --- | --- |
| VERIFIED | The supplied host verdict observed successful required checks |
| VERIFICATION_FAILED | Patched verification or its evidence failed |
| BASELINE_FAILED | A required baseline guard failed |
| UNAVAILABLE | Verification is unavailable, including an awaiting proposal without evidence |
| STALE | Captured source or edits are stale |
| UNSUPPORTED | Analysis or verification is unsupported |
| NO_TESTS_NEEDED | Advisory decision without invented execution evidence |
| AGENT_FAILED | Agent lifecycle failed; any supplied host verdict is still shown separately |

Actual loaded skills come exclusively from canonical T04 request events. The
extractor checks the initial source/context/definition binding, exact request
fields, stage/target uniqueness, request budgets, every SkillUseRecord field,
schema and the fixed registry's version, target and expected/loaded hashes.
It deduplicates by target and skill ID, in sorted order. Plan `skill_ids` confer
no load evidence. Malformed or contradictory trace records fail closed; traces
are host-owned records, not independent cryptographic attestations of requests.

Untrusted display text encodes Markdown/HTML punctuation as visible entities
and replaces ASCII `@` with fullwidth `＠`, preventing user/team mentions.
Controls, ANSI escapes, surrogate and directional formatting characters are
rejected. Model text cannot insert links, headings, tables, fences or hidden
comments. Each advisory field has a 2 KiB UTF-8 display budget, with deterministic
clipping explicitly marked. The complete comment must fit **48 KiB** or rendering
fails with a fixed error. Run links are built from validated repository/run IDs;
patch links require a trusted numeric artifact receipt for that same run.

### Sticky publisher and future permission boundary

`publisher.publish(prepared_report, adapter, expected_author)` uses one exact
first-line hidden marker, `taskly-unit-test-agent-report:v2`. Its strict grammar
encodes repository/PR, head/policy/base/merge-base SHAs, repository ID, source CI
run/attempt, agent publication run/attempt and exact head repository identity. Only comments matching the expected numeric author ID, exact
login and account type participate. A copied marker from another author is
ignored. Zero owned comments creates one; one updates the same ID; duplicate
or malformed owned markers fail closed. No comments are deleted.

The adapter reads the PR before discovery and immediately before POST/PATCH.
It requires the same open PR, repository and numeric ID, head and base SHA.
Missing/closed/changed PRs return stale without a write. Validated numeric
`(workflow_run_id, run_attempt)` order prevents older publications from replacing
newer ones. Equal order requires identical identity and body for idempotence;
conflicting facts fail closed. Visible text never selects identity or order.

The concrete HTTP transport uses only `api.github.com` and its configured
repository, with explicit injected authentication, no redirects, proxies,
credential discovery, generic endpoints, GraphQL or shell. Only PR GET,
paginated issue-comments GET, issue-comment POST and known-comment PATCH are
available. Request bodies are at most 128 KiB; responses at most 1 MiB each and
4 MiB total per adapter. A publication allows seven requests, four pages of 30
comments, and 100 comments total. HTTP socket inactivity and absolute request
deadline are ten seconds. Exceptions suppress remote/token-bearing details.
All write tests use a deterministic fake; concrete HTTP tests replace connections.

T07 serializes publisher jobs per validated repository ID and PR number: GitHub issue comments have no
atomic compare-and-swap with PR metadata or comment ordering. The two reads
reduce races but cannot remove the final read/write gap. Writes with lost
responses are indeterminate and are not retried here; subsequent discovery is
required. Pagination exhaustion also fails instead of guessing ownership.

Only the dedicated publisher job receives `issues: write` after trusted-main installation. Context,
inference, patch generation, verification and artifact preparation keep read-only
or absent write credentials. Neither the model, generated tests nor Docker may
receive the publisher token. The production definition is prepared on the development branch; trusted-main
installation and live execution remain separate approval gates.

### Deterministic artifact data and delivery

`artifacts.build_bundle(bundle, agent_result, repository_root, verification_report)`
produces bounded bytes and an explicit upload specification, without writing,
applying, executing or uploading a patch. CREATE uses `/dev/null` to `b/path`;
UPDATE uses `a/path` to `b/path`. Sorted accepted T02 edits and host `difflib`
generate unified UTF-8 diffs, including missing-newline records. No model patch
text is accepted. UPDATE requires available exact-head T03 original bytes with
the authenticated Git blob identity, followed by a no-follow regular single-link
local read matching original SHA-256, content and T02 mode. Unsafe/missing
originals, symlink ancestors, hardlinks and special files make patch preparation
unavailable. CREATE requires a safely missing target. Sensitive and bulk paths
are excluded in addition to T02's test-path restrictions.

Only these fixed host-created entries are allowed; there are no directories,
recursive uploads, globs, dependencies, caches, images, model files or raw
provider transcripts:

| Entry | Maximum | Classification |
| --- | --- | --- |
| manifest.json | 16 KiB | manifest |
| verification.json | 128 KiB | host-verification |
| generated-tests.patch | 1 MiB | untrusted-test-patch-data |
| report-metadata.json | 16 KiB | host-report-metadata |

The sorted manifest inventories all other entries with byte sizes, SHA-256 and
classifications, plus schema, logical artifact name, source head/policy SHA and
run/attempt. It cannot self-hash. The **10 MiB total per-run ceiling** includes
the manifest; current entry limits are tighter. Future upload specs fix
intermediate retention to **1 day** and final report/patch retention to **7 days**;
other values, filenames and classifications are rejected.

Local preparation `PREPARED` is distinct from delivery `AVAILABLE`: an actual
trusted upload receipt is required before linking a download. Required edits
without a delivery receipt remain `UNAVAILABLE` and explicitly unresolved,
including an upload failure after VERIFIED. The execution verdict stays exactly
what the verifier observed. Non-edit decisions use `NOT_REQUIRED`. Missing
originals omit the patch while preserving host verification metadata.

Focused offline checks:

```sh
backend/.venv/bin/python -m pytest automation/tests/test_reporting.py automation/tests/test_publisher.py automation/tests/test_artifacts.py -q
```

T07 adds the installation/input handoff, pinned runtime bootstrap, per-PR
serialization, separate permissions and receipt delivery described below. This library does not authenticate an untrusted host process,
prove test semantics, implement uploads or attest the verifier's framework
files independently. No live GitHub write, artifact upload, Docker execution or
model invocation is needed to test T06.

## T07 trusted workflow wiring

The development branch contains `.github/workflows/test-agent.yml`; this is
reviewable offline wiring, not evidence of a live integration. Automatic runs
require the workflow on trusted `main`. Ordinary CI remains independent and
unchanged. The only triggers are completed `Ordinary CI` workflow runs and a
manual dispatch accepting `ordinary_ci_run_id`. Dispatch reprocesses the selected
CI evidence with a new agent run; it accepts no authoritative source or policy
SHA. Push-origin automatic CI runs are not applicable and never start generation;
manual dispatch rejects a push-origin source run.

Before expensive work, both the workflow and host require the exact public
`kreshby/cloudfide-test-agent` repository on a standard hosted `ubuntu-24.04`
runner. The API repository name and numeric ID must match the platform identity. The
public repository gate is refreshed after context collection, before source
checkout/model work.
The fixed CI workflow endpoint supplies the active workflow ID, name and path;
these must agree with the completed source run, its event, conclusion, attempt
and exact head. The workflow-run event snapshot must also agree. A bounded commit
association must identify exactly one current open PR, and direct PR reads must
confirm its base/head and head repository. Missing, stale or ambiguous identities
fail without a publication target. Branch names and event PR-array ordering grant
no authority.

A known fork receives an `UNSUPPORTED` report without T03 collection, checkout,
model or generated-code execution. T03's same-repository restriction remains
unchanged. A known same-repository PR whose source CI did not succeed receives
`UNAVAILABLE` without inventing a baseline verdict. After T03, changes without a
supported frontend/backend source or test target receive `UNSUPPORTED` before
inference. These fork/docs outcomes use a successful job conclusion and explicitly
state that no verification occurred. Runtime, agent, policy, verifier, publication
and unresolved delivery failures fail the workflow after reporting when possible.
An unsupported verifier outcome with required edits also remains unresolved.

### Code and execution boundaries

Both jobs check out `github.workflow_sha` into `trusted` with credential
persistence disabled. The host requires the exact main workflow ref, checks its
current run through the fixed agent workflow endpoint, and verifies the installed
checkout HEAD and clean tracked bytes. The policy SHA is the Git blob identity of
the installed `automation/test_agent/policy.py`, checked against the trusted
commit. The trusted workflow commit is also carried in the report/handoff.
PR copies of automation, policies and skills are data and are never imported.

Source acquisition uses a separate temporary checkout, the fixed public HTTPS
origin and validated exact head/base SHAs. It uses no token, credential helper,
hooks, external configuration, redirect or PR-selected remote. Before analysis,
HEAD, origin, merge-base and clean working-tree state must match the context.
The checkout is removed after preparation. The actual T04 `UnitTestAgent` runs
with `LocalLlamaProvider`. Only accepted T02 changes reach the unchanged T05
Docker verifier. Its bound actual `HostResult` is then passed to `agent.resume`;
no-edit decisions and unavailable verifier calls receive no invented execution
records. Feedback failure preserves supplied host verification facts.

| Job | Permissions | Timeout |
| --- | --- | --- |
| prepare: preflight, context, agent, verifier, artifact | actions/contents/pull-requests read | 45 minutes |
| publisher: revalidation and one sticky comment | contents/actions/pull-requests read; issues write | 5 minutes |

Top-level permissions are empty. The publisher token exists only in the publisher
job. The model child gets a fixed environment containing PATH, private HOME and
TMPDIR, LANG/LC_ALL, GIT_CONFIG_NOSYSTEM/GLOBAL, GIT_TERMINAL_PROMPT and
OMP_NUM_THREADS. It inherits no GitHub/Actions/cloud/SSH/proxy/token variables.
The original T05 build/execution environments, denied network, mounts and limits
remain unchanged; containers receive neither repository Git metadata, automation,
workflow files, host/model environments nor tokens.

### Pinned local runtime and bounds

`runtime.LocalRuntime` reuses P01's exact-commit CPU source build and process
supervisor. It offers no model selection, URL, shell command or provider fallback.
The one selected runtime is:

| Pin | Value |
| --- | --- |
| Runtime | qwen25-coder-7b-q4km |
| Model repository | Qwen/Qwen2.5-Coder-7B-Instruct-GGUF |
| Revision | 13fb94bfda8c8cf22497dc57b78f391a9acb426a |
| File | qwen2.5-coder-7b-instruct-q4_k_m.gguf |
| Bytes | 4683073536 |
| SHA-256 | 509287f78cb4d4cf6b3843734733b914b2c158e43e22a7f4bf5e963800894d3c |
| llama.cpp | v0.6.0, d81235049384534c167caea52b85a694f6103d14 |

Only the fixed GitHub llama source and model resolve URL are used. Model redirects
are limited to four and to HTTPS Hugging Face subdomains or approved `.hf.co`
object storage. Exact bytes and SHA-256 are required; an incomplete model is
removed. Model acquisition keeps a 2 GiB disk reserve and has a 180-second shared
absolute deadline with 10-second socket inactivity bounds. Setup is limited to
420 seconds, each startup to 90 seconds, source checkout to 120 seconds and the
host preparation to 2,640 seconds. Existing T03, T04 and T05 limits are unchanged:
GitHub reads use 10-second requests and the existing request/byte budgets; T04
allows six calls, 300 seconds per call and 1,000 active seconds overall; T05 limits
image build to 600 seconds and each tool to 120 seconds.

The loopback server uses port 8765, CPU only, four threads, context 8192, parallel
one, temperature zero, seed 42, Jinja, disabled reasoning, offline operation,
no web UI and no context shift. Its default prediction cap is 768 to accommodate
T04's edit stage; each T04 request retains its existing smaller stage cap. Both
process pipes retain at most 16 KiB and discard excess; raw captures are never
exported. Host children inherit a 12 GiB address-space limit, 6 GiB per-file limit
and disabled core dumps. Model memory is released before Docker build/verification;
one restart of the same already-verified model supports feedback. Finally blocks
stop the entire process group and remove temporary source/build/model files.
There is no model/dependency cache, package registry or paid service.

### Identity, handoff and delivery

The source identity retains the originating CI run/attempt, exact head, base,
merge-base and policy SHA. Agent publication identity retains the current agent
run/attempt. Sticky v2 ordering is lexicographic `(agent run ID, agent attempt)`;
a newer dispatch can therefore supersede a publication for the same source CI.
Equal order is idempotent only for identical identity and body. Existing owned
v1 markers fail closed and require review rather than automatic migration.
Expected author is fixed to GitHub's `github-actions[bot]`, numeric ID 41898282,
account type Bot. Known fork reports also bind the exact fork repository ID/name;
this does not authorize fork execution.

The publisher serializes on validated repository ID and PR number, with
`cancel-in-progress: false`. It reruns source/PR validation and requires its
concurrency target to match before accepting a handoff. The intermediate artifact
is `test-agent-handoff-<current run>-<attempt>`, contains only `handoff.json`, and
expires after one day. Download uses the current run only, never originating CI
artifacts. The canonical JSON schema is version 1 with exactly `schema_version`,
`identity`, `source`, `source_ci`, `trusted_workflow_sha` and `facts`. All nested
schemas are exact; duplicate keys, null identity substitutions, unknown fields,
noncanonical encoding, oversized data and mismatched facts are rejected. The
192 KiB maximum includes the whole handoff; its SHA-256 must match the trusted
producer job output. Identity is independently rederived from GitHub and the
installed policy. No pickle, eval, rendered comment, model transcript, arbitrary
provider text, raw logs, test code or absolute output handle is transported.
Each advisory decision carries only target, action, changed behavior and rationale
from the validated plan. Text retains TestPlan's 4,096-character contract and
T06's control/Unicode restrictions; display uses inert escaping and the existing
2 KiB per-field clipping budget. Advisory recommendations are shown separately
from host evidence. Actual loaded skills show both content and loaded-content
hashes. Pre-agent terminal envelopes contain no advisory or execution facts;
selected verification records are validated through T06 before rendering.

Final uploads use only T06's `ArtifactBundle` entries: `manifest.json`,
`verification.json`, `report-metadata.json` and, when prepared,
`generated-tests.patch`. The hard ceiling remains 10 MiB and final retention is
seven days. The final name uses only current run/attempt. A patch link requires
the numeric receipt from the successful pinned upload action. Failure to prepare
or upload required edits leaves delivery `UNAVAILABLE`; the supplied verifier
verdict is preserved and the workflow fails as unresolved. Non-edit/terminal
reports have `NOT_REQUIRED` delivery. Only the named small files are uploaded;
models, builds, dependencies, raw logs and repository dumps remain temporary.

### Installation gate and remaining limitations

Offline tests do not prove GitHub event shapes, artifact delivery, runner resource
availability, local inference quality or end-to-end execution. API reads are
bounded snapshots, not atomic transactions. A PR or source run can change before
publication; identity revalidation then fails closed. Publisher serialization
and ordering reduce races but cannot remove GitHub's final read/write gap. A lost
write response is indeterminate and is not retried automatically. If preflight,
metadata reads, handoff transfer or the publisher itself is unavailable, a comment
may be impossible even for an otherwise known PR. T05's shared-kernel/report-forgery
and semantic-test limitations remain. Handoff hashes authenticate transport from
the trusted job outputs, not a compromised producer process.

Live enablement requires human review, separate commit and push approval, green
Ordinary CI, then explicit trusted-main installation approval. A separately
approved same-repository demo must exercise automatic triggering, actual Qwen
edits, Docker verification, final artifact delivery and publication. A subsequent
commit must automatically update the same sticky comment. Until this gate has
actual evidence, final T07 acceptance remains incomplete.


### T07-B3 local release boundaries

The pinned native `/completion` endpoint supports `json_schema`. Its schema is
host-owned, derived from the same trusted stage shapes used in the prompt, and
digest-bound in the canonical provider request. The stage shape digest is part of
the binding; the exact context binds candidate paths and complete evidence.
Decision citations are constrained to available tests, and each target selects
at most one intended test file. Existing files require update; missing files
require generate. Host field, binding, evidence, path and T02 policy validation
still run after generation. The model cannot select a schema, endpoint or tool.

Real socket regressions cover framed close, keep-alive, chunked and EOF bodies.
The provider checks `HTTPResponse.isclosed()` before touching the retained socket
and rejects mismatched Content-Length. Native `truncated` concerns context;
`stop_type=limit` separately rejects exhausted output. The 16 KiB projection,
24 KiB host request, 8,192-token context and 256/768 decision/edit limits remain.
Review completion is bounded to 192 tokens: the exact failed verifier feedback
exhausted 128 tokens with native `stop_type=limit`; the new cap adds 64 tokens
of measured headroom while retaining the 2 KiB response and 512-byte text gates.

A measured exact PR #2 run used 6,495/4,727/4,389/3,160 prompt tokens and
222/330/177/690 generated tokens for frontend decision/edit and backend
decision/edit. All ended with EOS. Its longest call took 223.388 seconds and its
four calls took 717.298 seconds. The fixed 300-second call deadline provides
about 34% headroom; 1,000 active seconds reserves time for both verification
reviews and normal variation. Absolute deadlines and the six-call/no-retry
limits remain enforced. These measurements are local, not GitHub run results.

Failed results export only the finite `Diagnostic` vocabulary in handoff reason,
final metadata and reports. Exception strings and provider bodies are excluded.
Historical v1 handoffs with null reason remain readable; the current producer
always supplies an allowlisted code for a failed agent. Publication writes have
fixed HTTP 401/403/404/422/5xx/other, transport, JSON, receipt and timeout
categories. Valid 201 receipts succeed with either close or keep-alive framing.
No publisher status or local simulation proves live GitHub authorization.

The first complete rehearsal correctly rejected generated frontend TypeScript
with TS1005 and TS1161. Edit projections now replace the named test subject's
historical source with its complete captured current-head source. Subjects are
derived from accepted adjacent test paths and captured T03 evidence only; no
files are discovered, and no projection cap is enlarged. An incomplete existing
test still cannot authorize an update. The trusted prompt also requires plain
TypeScript in `.test.ts`, JSX only in `.test.tsx`, complete contents without code
fences, and a valid filter assertion at its observable boundary. Compiler and
Docker verification remain the authority for generated code.

Native decision schemas also couple action and path: generation permits only
missing captured candidates; update permits only complete available existing
tests. Non-edit actions permit no intended paths, and no-tests-needed requires
complete cited evidence. These are stricter renderings of existing host semantics,
not new model authority. The native schema is hash-bound in ProviderRequest;
the prompt retains the shared field shape, avoiding duplicate large path/schema
text in the 24 KiB request. T02 and binding validation remain independent gates.

The local release gate remains failed: the pinned Qwen runtime still produced a
frontend `.test.ts` rejected with TS1005 and TS1161 after the current-source and
syntax guidance changes. Both review completions succeeded within 192 tokens;
handoff and local publication preserved VERIFICATION_FAILED. This is not a
model qualification or a successful repeatability result. No model/runtime pin
or GitHub state was changed.

B9 frontend preflight uses the image-owned `structural.cjs` with the existing
locked TypeScript 5.8.3 API. Each accepted frontend edit passes the fixed
`frontend-patched-structural` operation after baseline guards and before patched
`tsc`/Vitest. Exact UTF-8 content is parsed as TS for `.test.ts` and TSX for
`.test.tsx`; syntax errors and forbidden JSX fail closed. New tests require a
value-bearing static relative import resolving uniquely to the bound source
(explicit `.ts`/`.tsx`, extensionless files or repository index conventions).
Updates retain established barrel/helper relationships. Only bounded categories,
counts and source/path/content/changes identities leave this parser; source is
never executed, rewritten or renamed. Structural failure stops patched tests and
has no repair loop. Any eligible compiler replacement runs the same preflight in
a fresh verifier; a byte-identical replacement is rejected before that verifier.
The authenticated frontend skill contains small illustrative TS/TSX patterns
within the unchanged 2 KiB limit. Projection-clipped test metadata omits redundant
content hashes; complete evidence keeps its exact hash within the 16 KiB budget.
