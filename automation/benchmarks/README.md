# P01 — $0 local model selection benchmark

This is experimental selection evidence for the future UnitTestAgent. T04 has
not started. No production provider is selected, and model-generated tests are
neither written to application paths nor executed or verified. Human review of
quality and resource evidence is required before choosing a runtime.

P01-A permits local implementation and deterministic fake-response tests only.
Commit, push, PR changes, remote uploads and hosted benchmark execution require
the later explicitly approved P01-B phase. Running the real harness locally
fails before downloads: it requires GitHub's standard Ubuntu 24.04 hosted image.

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
1,536, seed 42, temperature 0, top-k 1, top-p 1, min-p 0 and repeat penalty 1.
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
| Full harness / workflow | 2,580 seconds / 45 minutes |
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
No application test runner is invoked on generated code. Actual hosted latency,
quality, availability and memory results remain **NOT RUN** until the reviewed
harness is explicitly approved, committed and pushed and its workflow completes.
