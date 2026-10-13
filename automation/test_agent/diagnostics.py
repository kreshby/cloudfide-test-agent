"""Host-owned finite failure vocabulary; no exception text crosses the handoff."""

from enum import StrEnum
from types import MappingProxyType


class Diagnostic(StrEnum):
    PROVIDER_HTTP = "provider-http"
    PROVIDER_TIMEOUT = "provider-timeout"
    PROVIDER_TOKEN_BUDGET = "provider-token-budget"
    PROVIDER_TRUNCATED = "provider-completion-truncated"
    PROVIDER_JSON = "provider-json"
    PROVIDER_PROTOCOL = "provider-protocol"
    SCHEMA = "decision-schema"
    BINDING = "decision-binding"
    EVIDENCE = "decision-evidence"
    PATH = "decision-path"
    POLICY = "policy-rejected"
    VERIFIER = "verifier-unavailable"
    CONTRACT = "agent-contract-failed"


EXPLANATIONS = MappingProxyType({
    Diagnostic.PROVIDER_HTTP: "The local provider HTTP exchange failed validation.",
    Diagnostic.PROVIDER_TIMEOUT: "The local provider exceeded its deadline.",
    Diagnostic.PROVIDER_TOKEN_BUDGET: "The rendered prompt exceeded the reserved token budget.",
    Diagnostic.PROVIDER_TRUNCATED: "The local completion exhausted its budget or reported truncation.",
    Diagnostic.PROVIDER_JSON: "The provider did not return bounded valid JSON.",
    Diagnostic.PROVIDER_PROTOCOL: "The local provider returned invalid protocol evidence.",
    Diagnostic.SCHEMA: "The stage response did not satisfy the trusted shape.",
    Diagnostic.BINDING: "The stage response did not match the exact request binding and target.",
    Diagnostic.EVIDENCE: "The decision cited unavailable or incomplete test evidence.",
    Diagnostic.PATH: "The decision selected invalid or unavailable test paths.",
    Diagnostic.POLICY: "Trusted test edit policy rejected the proposal.",
    Diagnostic.VERIFIER: "Trusted verification feedback was unavailable.",
    Diagnostic.CONTRACT: "The agent did not produce an accepted terminal result.",
})


def safe_diagnostic(value):
    # Require the enum internally. Strings supplied by exceptions or callers
    # cannot masquerade as host-owned categories.
    return value if type(value) is Diagnostic else Diagnostic.CONTRACT
