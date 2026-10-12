# Engineering rules

- Implement only the requested task. Read the relevant code, configuration, and
  repository state before editing; leave later tasks pending.
- Work on `test-agent-workflow`. Preserve `main` as the reviewed baseline and
  preserve unrelated working-tree and staged changes. Never force-push.
- Keep private assignments, CVs, correspondence, planning documents, and personal
  notes out of source control, logs, and uploaded artifacts.
- Report only checks and measurements actually executed. Include the source SHA,
  commands, exit codes, and observed results; distinguish setup failures from
  application failures. Never invent test, coverage, or runtime results.
- Preserve the existing test stack, pinned dependencies, lockfiles, and coverage
  denominator. Do not weaken assertions or change application behavior to hide
  baseline failures.
- Treat generated agent code, tests, patches, and output as untrusted input.
  Security boundaries must be fixed by trusted configuration; the test-agent
  runtime must never expand its own permissions, network access, or write scope.
- Never execute arbitrary model-selected shell commands. Future runtime execution
  must use explicitly permitted operations and validated arguments.
- Do not persist credentials in files, generated output, logs, commits, or
  artifacts. Do not activate paid services without explicit approval. Runtime
  provider selection remains pending; ordinary CI must not require a model.
- Use read-only CI permissions, standard hosted runners, verified full action
  commit SHAs, explicit timeouts, and no dependency caches unless justified.
  Upload only explicitly named small evidence files, with one-day retention for
  intermediate results; never upload dependencies, environments, build outputs,
  HTML reports, images, secrets, or model files.
- Run meaningful verification for the change and inspect both the working-tree
  and staged diffs before committing. State unresolved limitations and do not
  claim a GitHub workflow passed until its actual run completes successfully.
