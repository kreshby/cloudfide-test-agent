# Taskly solution — initial outline

## Goal

Establish a reproducible baseline and ordinary CI before developing a custom
test agent. This document is an outline, not a completed solution.

## Current baseline

Reviewed source: `9addcc7d6279ea8fd080f069b8d5f5404ffd281c`.
The original checks passed locally: 18 frontend tests, 21 backend tests, and
frontend typecheck. See [baseline evidence](docs/baseline/T01.md) for exact
commands, exit codes, coverage denominators, and warnings.

## Planned architecture

The existing application has a React/TypeScript frontend and Python/FastAPI
backend. Test-agent architecture and runtime provider selection are pending.

## Custom agent

Pending. No custom agent, provider adapter, inference, or test generation exists.

## Reusable skills

Pending. No project-specific agent skills have been implemented.

## Verification

Ordinary CI runs the existing unit suites, frontend typecheck, and coverage.
Local validation is recorded in the baseline evidence. Hosted results must be
checked on the exact pushed commit before claiming success.

## GitHub integration

CI covers pull requests targeting `main` and pushes to `main` and
`test-agent-workflow`. Custom PR reporting is pending. `main` remains the original
baseline; any future workflow requiring default-branch installation is pending.

## Evidence

[T01 evidence](docs/baseline/T01.md) records the unchanged baseline and verified
action pins. CI uploads only two explicitly named JSON reports, at most 128 KiB
each, retained for one day. Other generated reports stay outside Git history.

## Coverage

Original frontend totals: statements 46.42%, branches 43.79%, functions 40.25%,
lines 47.68%. Backend terminal total: 42%, with branch coverage enabled.
Coverage improvement is pending; existing include/exclude settings are unchanged.

## Security

[Engineering rules](AGENTS.md) define constraints for future work. CI uses only
`contents: read`, verified action SHAs, and hosted `ubuntu-24.04` runners, without
persisted checkout credentials, model credentials, or dependency caches.
Runtime permission enforcement and sandbox isolation are pending.

## Limitations

Runtime benchmarking, agent operation, generated tests, sandbox verification,
and custom PR publication are pending. Baseline dependency warnings are recorded
without changing dependencies. The unit suites do not exercise a live database.

## Reproduction

Use Node.js 22.23.2 and Python 3.12.13. Follow the existing [README](README.md)
installation commands and the exact checks in [T01 evidence](docs/baseline/T01.md).
No model access or PostgreSQL service is needed for the unit checks.
