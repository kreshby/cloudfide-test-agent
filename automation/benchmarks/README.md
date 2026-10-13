# P01 — $0 local model selection benchmark

This is experimental selection evidence for the future UnitTestAgent. P01 model
selection is complete. T04 has not started.
Model-generated tests were neither written to application paths nor executed or
verified. The production workflow is **NOT YET IMPLEMENTED** and **NOT YET VERIFIED**.
Running the real harness locally fails before downloads: it requires GitHub's
standard Ubuntu 24.04 hosted image.

## P01 decision

Two real GitHub-hosted runs supplied the selection evidence. Both workflows
completed with a failure conclusion; evidence validation and upload succeeded.
The measurements below summarize the reported hosted evidence, without replacing
the original artifacts or copying raw model responses. `n/a` means unavailable
in the supplied summary, never zero.

Run 1: [Actions run 38317258513](https://github.com/kreshby/cloudfide-test-agent/actions/runs/38317258513),
commit `2d7144827b3401a6ed92b722f80ce0baee3eb45e`.

| Candidate | Outcome | Quality | JSON / contract / policy | Median / total inference (s) | Generation (tok/s) | Peak RSS (bytes) |
| --- | --- | --- | --- | --- | --- | --- |
| qwen3-4b-q4km | completed | 5/50 | 0% / 0% / 0% | 40.44 / 202.46 | 14.26 | 6,293,258,240 |
| qwen3-8b-q4km | inference-timeout | 0/50 | n/a | n/a | n/a | 9,745,895,424 |
| qwen25-coder-7b-q4km | inference-timeout | 30/50 | 80% / 60% / 60% | 78.88 / 353.18 | 8.91 | 8,701,718,528 |

Qwen3-8B's first case timed out at approximately 90.09 seconds. Qwen2.5-Coder
scored 10/10 on already-covered, 9/10 on backend-update and 8/10 on
backend-generate; mixed-stack timed out. Run 1 also exposed prompt-shape ambiguity
and sensitivity to the 90-second CPU inference timeout.

Final corrective run: [Actions run 38320645069](https://github.com/kreshby/cloudfide-test-agent/actions/runs/38320645069),
commit `b6ee69ea568e449fce2fb12aabb8ecc688967181`, revision **P01-R2**.
All candidates used 512 completion tokens and 90 seconds per scenario.

| Candidate | Outcome | Quality | Timed-out inference (s) | Peak RSS (bytes) |
| --- | --- | --- | --- | --- |
| qwen3-4b-q4km | inference-timeout | 0/50 | ~90.09 | 5,615,484,928 |
| qwen3-8b-q4km | inference-timeout | 0/50 | ~90.09 | 9,746,051,072 |
| qwen25-coder-7b-q4km | inference-timeout | 0/50 | ~90.09 | 8,498,057,216 |

No candidate passed R2. The monolithic P01 benchmark prompt/request shape is too
slow under the selected 512-token / 90-second CPU bound.

**Selected T04 runtime candidate: `qwen25-coder-7b-q4km`,
Qwen2.5-Coder-7B-Instruct-GGUF, Q4_K_M.** This is a documented engineering
selection based on Run 1 evidence, not a claim that R2 passed:

- It produced by far the strongest actual task-quality evidence and was the only
  candidate to demonstrate meaningful valid structured output.
- It recognized already-covered behavior as no-tests-needed and produced strong
  backend update/generate decisions.
- Its observed approximately 8.7 GB peak memory fits the 16 GB hosted runner.
- Qwen3-4B was faster/lighter but materially weaker in structured/task quality;
  Qwen3-8B was slower/heavier and produced no useful scored evidence.

P01 selects the model/runtime candidate, not the final production prompt shape.
T04 must use compact/staged inference rather than copying the monolithic P01
benchmark interaction. Production workflow remains **NOT YET IMPLEMENTED** and
**NOT YET VERIFIED**. No paid API/provider is selected. The candidate is documented
here only; benchmark runtime selection and all three candidate pins are unchanged.
P01 is complete. T04 is not started, and no third hosted
benchmark run is requested.

## Workflow and cost

`.github/workflows/model-benchmark.yml` is manual-only via `workflow_dispatch`,
with no inputs, push trigger, PR trigger, schedule, matrix or cache. P01 selection
is complete; future T04 development pushes must not automatically spend runner
time repeating model selection. The benchmark remains reproducible manually
after this workflow exists on the default branch. Ordinary CI is unchanged and
does not invoke a model. The benchmark uses the runner's Python, Git, CMake and
compiler, with no package installation or application dependency changes.

The job requires the public `kreshby/cloudfide-test-agent` repository and
`ubuntu-24.04`, with `contents: read`, checkout credentials disabled and a
45-minute timeout. [Standard hosted runner minutes are free for public
repositories](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
No inference provider, model key, paid runner, hosted API or fallback is present.
Only the small evidence files described below are eligible for artifact upload;
no model or dependency storage service is used.

## Reproducible acquisition and inference

The host builds [llama.cpp v0.6.0](https://github.com/ggml-org/llama.cpp/releases/tag/v0.6.0)
from exact commit `d81235049384534c167caea52b85a694f6103d14`, verifies the
checkout SHA and records source acquisition time, build time and executable
version output. Source and binaries stay in runner temporary storage. Build
flags select native CPU Release, no CUDA/Vulkan/BLAS, no tests/examples, no UI
build or prebuilt UI download, and no OpenSSL. These UI flags prevent the pinned
build from fetching separate assets or npm dependencies. No mutable installer is
used. The reviewed action tags resolve to the
full commit SHAs already used by Ordinary CI.

Exactly these candidates are acquired sequentially from their public official
repositories. `model_benchmark.py` pins revision, filename, exact byte count and
SHA-256 for each; a missing file, redirect outside trusted storage, checksum
mismatch or bound failure remains a failure without substitution.

| ID | Public model repository | Quantization | Pinned bytes |
| --- | --- | --- | --- |
| qwen3-4b-q4km | [Qwen/Qwen3-4B-GGUF](https://huggingface.co/Qwen/Qwen3-4B-GGUF) | Q4_K_M | 2,497,280,256 |
| qwen3-8b-q4km | [Qwen/Qwen3-8B-GGUF](https://huggingface.co/Qwen/Qwen3-8B-GGUF) | Q4_K_M | 5,027,783,488 |
| qwen25-coder-7b-q4km | [Qwen/Qwen2.5-Coder-7B-Instruct-GGUF](https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF) | Q4_K_M | 4,683,073,536 |

Downloads use unauthenticated HTTPS with no proxy or credential discovery.
Only the fixed repository/revision/file URLs and Hugging Face storage redirects
under `.huggingface.co` or `.hf.co` are accepted. Redirect query strings are never
logged or persisted. A new storage host is a recorded failure requiring review.
llama.cpp uses only the verified local file, offline mode and loopback HTTP.
It receives no tools, URLs to fetch, MCP configuration or ambient environment.

All candidates use the identical messages and examples, native GGUF Jinja chat
templates, CPU threads 4 (including batch), context 8,192, maximum completion
512, seed 42, temperature 0, top-k 1, top-p 1, min-p 0 and repeat penalty 1.
Prompt cache reuse and context shifting are disabled. Qwen3 is consistently
non-thinking via llama.cpp `--reasoning off`/budget 0 (sets the template's
`enable_thinking=false`), plus the common `/no_think` system instruction.
The [pinned server interface](https://github.com/ggml-org/llama.cpp/blob/d81235049384534c167caea52b85a694f6103d14/tools/server/README.md)
applies the chat template and tokenizes it before native `/completion`; there is
no hosted compatible API call. Raw generated content is scored without thought,
fence or prose stripping. No JSON grammar hides structured-output failures.
Messages, inference settings, rendered-prompt digest/suffix and prompt token
counts are evidence. Template formatting can differ across GGUFs; task text does
not. No candidate-specific prompt tuning occurs.

The common system message lists allowed enum values in prose, followed by one
parseable shape example with dummy source values and no scenario paths or behavior.
It explicitly places `edits` beside `decisions` at the top level, forbids nested
edits and union literals, and requires exactly one decision per changed target
(two distinct decisions for mixed frontend/backend context). The example is only
a nesting guide; source identity and decisions must come from the supplied context.
Raw output still goes directly to T02 parsing without repair or normalization.

## Scenarios and scoring

Every case has an exact synthetic `SourceIdentity`, complete bounded old/head
source evidence, trusted test candidates, relevant existing tests and fixed
host-only expectations. This approximates T03's handoff without reading a live
PR or invoking T03 collection. Expected decisions and perfect test responses
are never sent to a model. Example inputs are supplied equally to make the
conservative behavior rubric reproducible.

| Scenario | Expected target decisions | Expected edit |
| --- | --- | --- |
| frontend-generate | frontend generate | Create `frontend/src/SubmitButton.test.tsx`; whitespace disabled, Alice enabled |
| backend-update | backend update | Update `backend/tests/test_tasks.py`; zero limit returns 422; copy supplied original SHA-256 |
| already-covered | frontend no-tests-needed | No edits; existing clamp test already asserts negative to zero and positive preservation |
| backend-generate | backend generate | Create `backend/tests/test_greeting.py`; Ada greeting response |
| mixed-stack | frontend generate; backend no-tests-needed | Create `frontend/src/TaskCount.test.tsx`; hide zero, display two; preserve covered health behavior |

Ten equal one-point gates per scenario: response, single raw JSON object, T02
contract, T02 policy, exact target decisions, operations, trusted expected paths,
absence of forbidden host fields, substantive test and behavior evidence.
Target-level gate results are also recorded. All five scenarios stay in the
50-point denominator, including unavailable/unattempted cases. Covered cases
must have no edits and cite existing coverage with behavior rationale; blindly
generating loses decision, path/operation and quality points. Empty/tautological
or irrelevant test content cannot achieve full quality.

`ModelProposal.from_payload` and `assess_proposal` are unchanged and authoritative.
Policy checks use a separate temporary repository containing only trusted fixture
originals and a host-created `TrustedContext`; no model content is written there.
Parser validity, policy rejection reason and each quality gate remain separate.
Python syntax is inspected through AST data; frontend inspection uses conservative
lexical patterns with comments removed. This rubric recognizes specific imports,
requests/rendering and assertions. It can reject equivalent syntax and cannot
prove correctness or resist every deliberately deceptive string. Policy acceptance
and rubric scores do not imply compilation, coverage or verification. Bounded raw
responses permit human assessment; no LLM judge or automatic winner is used.

## Host bounds and lifecycle

| Resource | Bound |
| --- | --- |
| Full harness / workflow | 2,640 seconds / 45 minutes |
| llama.cpp acquisition and build combined | 420 seconds (fetch 90; configure 30; build 300; shared deadline) |
| Model acquisition / startup including warmup | 180 / 90 seconds per candidate |
| Inference including formatting/tokenization | 90 seconds per scenario |
| Download socket inactivity / redirects | 10 seconds / 5 requests |
| Synthetic context / raw model content | 16 / 24 KiB per scenario |
| Local HTTP response / captured stdout or stderr | 128 / 16 KiB per stream; excess process output drained/discarded |
| Model bytes / free-disk reserve | 6 / 2 GiB; exact expected file size and streaming reserve checks |
| Build preflight | At least 4 GiB free (2 GiB build allowance plus reserve) |
| Process address space / core dumps | 12 GiB inherited limit / disabled |
| Uploaded evidence | At most 1 MiB combined, one-day retention |

R2 uses the section 9 output-reduction fallback to preserve the 45-minute workflow.
The initially requested 768-token / 120-second pair cannot fit the unchanged phase
bounds: `3 candidates × 5 scenarios × 120 seconds = 1,800 seconds` of inference,
plus `420 + 3 × (180 + 90) = 1,230 seconds` of setup/acquisition/startup, totals
`3,030 seconds` (50 minutes 30 seconds), before checkout, scoring, cleanup and
evidence upload. Keeping the old global deadline would truncate that worst case.

The final common output limit is reduced from 1,536 to **512 tokens**; the per-case
timeout stays **90 seconds** under this fallback. At Run 1's reported approximately
8.5 generation tokens/s, `512 / 8.5 ≈ 60.24 seconds` leaves approximately 29.76
seconds of the request window for template rendering, tokenization and prompt
processing. This is arithmetic from reported Run 1 throughput, not an R2 measurement
or guarantee that every answer fits the token budget.

The final configured phase maximum is `420 + 3 × (180 + 90 + 5 × 90) = 2,580 seconds`
(43 minutes), allowing all 15 cases to consume their full timeouts without relying
on early candidate failure. `TOTAL_SECONDS` increases from 2,580 to **2,640 seconds**
(44 minutes), reserving 60 seconds for source-SHA lookup, supervision, scoring,
cleanup and evidence writing, plus another `2,700 - 2,640 = 60 seconds` for workflow
checkout, validation and upload. The 45-minute workflow and all setup/acquisition/
startup bounds remain unchanged. Phase arithmetic does not guarantee a completed
answer: the hosted R2 request timeouts are recorded above. No real model has been
run locally for this closeout.

Candidates run sequentially. After failure or completion, terminate the entire
server session group, remove all candidate data before the next acquisition and
record cache bytes plus disk free before/after cleanup. Request failures stop that
candidate rather than queuing behind a timed-out inference. Remaining cases are
explicit failures. Host termination unwinds cleanup; externally forced SIGKILL
cannot guarantee final evidence, and runner teardown remains the final cleanup.
Source/build directories are removed at the end. No GGUF, binary, model cache or
dependency directory is committed or uploaded.

Results record download bytes/hash/time separately from startup/warmup, per-case
wall time, prompt/completion tokens and llama.cpp generation tokens/sec when
available, total candidate/workflow duration, sampled process VmHWM, runner
CPU/memory/image/Python and disk free before model, after load and after cleanup.
VmHWM sampling runs every 50 ms; very short-lived peaks may be missed. Missing
measurements are null, never fabricated. This is one run per case with no warmup
inference beyond llama.cpp startup; CPU variability and sample size limit conclusions.

Machine-readable results identify this harness as `benchmark_revision: P01-R2`.
The host records the effective output-token limit (`configuration.completion_tokens`),
per-case timeout (`configuration.inference_seconds`) and SHA-256 of the exact UTF-8
system message (`configuration.system_prompt_sha256`), including setup-failure
evidence. Model responses cannot set these fields.

## Evidence and command safety

Only `benchmark-results.json` and `benchmark-summary.md` are uploaded by explicit
name. Raw responses are embedded in JSON and bounded individually; a second
budget step truncates evidence copies if JSON escaping exceeds 1 MiB and marks
truncation without changing scores. Upload validation rejects extra files and
symlinks. Artifact retention is one day. Local review files belong outside the
repository and are not included in this workflow's upload paths.

Subprocess argv comes only from fixed Git/CMake operations or the fixed llama.cpp
configuration, with host-owned temporary paths and pinned hashes. No shell is
used. Model/scenario text cannot choose argv, URLs, environment, limits, filenames,
permissions or verdicts. No `eval`, dynamic `exec`, `os.system`, shell interpolation,
Python import or JS/TS execution of generated content occurs. `ast.literal_eval`
only compares a parsed literal assertion value; it does not execute Python.

## Local deterministic verification

From the repository root, using the existing test environment:

```sh
backend/.venv/bin/python -m pytest automation/tests -q
backend/.venv/bin/python -m compileall -q automation
backend/.venv/bin/python -m pytest automation/tests/test_model_benchmark.py -q
```

Harness tests use synthetic proposals and fake inference, processes and network
objects. They test scoring failures, real T02 integration, bounding, deadlines,
cleanup, sequential lifecycle, pinned candidates and lack of model execution.
No application test runner is invoked on generated code. The two hosted runs and
their limitations are recorded in the P01 decision above. Deterministic harness
tests do not establish production test quality, latency or verification; the
selected candidate still requires T04 implementation with compact/staged inference.
