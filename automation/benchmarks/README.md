# P01 — $0 local model selection benchmark

This is experimental selection evidence for the future UnitTestAgent. T04 has
not started. No production provider is selected, and model-generated tests are
neither written to application paths nor executed or verified. Human review of
quality and resource evidence is required before choosing a runtime.

P01-A permits local implementation and deterministic fake-response tests only.
Commit, push, PR changes, remote uploads and hosted benchmark execution require
the later explicitly approved P01-B phase. Running the real harness locally
fails before downloads: it requires GitHub's standard Ubuntu 24.04 hosted image.

## P01 hosted run history

Run 1 used commit `2d7144827b3401a6ed92b722f80ce0baee3eb45e`. The reported
hosted evidence exposed prompt-shape ambiguity and sensitivity to the 90-second
CPU inference timeout. Candidate failures caused a nonzero benchmark exit; no
production winner was selected. Original hosted evidence is not replaced here.

P01-R2 changes only the common schema presentation and output/time budget,
with host-owned revision/configuration metadata and regression tests. Its single
final hosted candidate comparison is **NOT RUN**. This phase prepares local
review evidence only: no commit, push, workflow trigger or T04 implementation.

## Workflow and cost

`.github/workflows/model-benchmark.yml` runs on pushes to `test-agent-workflow`
only when its own file, this benchmark package or its harness test file changes.
This supports the first approved branch push before default-branch installation.
There is no dispatch dependency, PR trigger, schedule, matrix or cache. Ordinary
CI is unchanged and does not invoke a model. The benchmark uses the runner's
Python, Git, CMake and compiler, with no package installation or application
dependency changes.

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
startup bounds remain unchanged. Overhead allowances require hosted validation;
no R2 runtime has been measured locally.

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
No application test runner is invoked on generated code. R2 hosted latency,
quality, availability and memory results remain **NOT RUN** until the reviewed
correction is approved, committed and separately approved for push and its
workflow completes. Run 1 history above does not establish R2 results.
