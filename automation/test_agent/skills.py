"""Fixed host-owned skill registry, never selected by repository/model text."""

from dataclasses import dataclass
import hashlib
from pathlib import Path
from types import MappingProxyType

from .contracts import SkillUseRecord, Target
from .policy import PolicyError, _current_content, normalized_path


MAX_SKILL_BYTES = 2048
_TRUSTED_ROOT = Path(__file__).resolve().parents[2]


class SkillError(ValueError):
    """A trusted skill could not be authenticated."""


@dataclass(frozen=True)
class SkillEntry:
    skill_id: str
    version: str
    path: str
    sha256: str
    targets: tuple[Target, ...]


_BOTH = (Target.FRONTEND, Target.BACKEND)
REGISTRY = MappingProxyType({entry.skill_id: entry for entry in (
    SkillEntry("analyze-change", "1.0.0", "automation/test_agent/trusted_skills/analyze-change.md",
               "fb879bcaa02da848e53b7f2ed0b586e122b7759de574bccf79abb38c1b02d284", _BOTH),
    SkillEntry("frontend-unit-tests", "1.2.0", "automation/test_agent/trusted_skills/frontend-unit-tests.md",
               "a384facecc690f92810f3d2d36f98e98997ad760bf64db6ef66017a83927c0ae", (Target.FRONTEND,)),
    SkillEntry("backend-unit-tests", "1.0.0", "automation/test_agent/trusted_skills/backend-unit-tests.md",
               "3ed8aa01cc9120212cc1a920fadf9fa29b66e1d855c392513652b523fcdacad8", (Target.BACKEND,)),
    SkillEntry("verify-test-patch", "1.0.0", "automation/test_agent/trusted_skills/verify-test-patch.md",
               "92ebfefbb247366f4aad15336f712fc2dd152d129e823ef1f1dc14d0ee26ff96", _BOTH),
)})


@dataclass(frozen=True)
class LoadedSkill:
    body: str
    record: SkillUseRecord


def load_skill(skill_id: str, target: Target) -> LoadedSkill:
    if type(skill_id) is not str or skill_id not in REGISTRY:
        raise SkillError("unknown trusted skill")
    entry = REGISTRY[skill_id]
    if type(target) is not Target or target not in entry.targets:
        raise SkillError("unsupported skill target")
    try:
        # Reuse T02 descriptor-relative, no-follow, regular/non-linked file reads.
        normalized_path(entry.path)
        current = _current_content(_TRUSTED_ROOT, entry.path)
        if current is None:
            raise SkillError("missing trusted skill")
        content, _ = current
        if len(content) > MAX_SKILL_BYTES:
            raise SkillError("trusted skill byte limit")
        digest = hashlib.sha256(content).hexdigest()
        if digest != entry.sha256:
            raise SkillError("trusted skill hash mismatch")
        body = content.decode("utf-8")
    except (OSError, UnicodeError, PolicyError):
        raise SkillError("unsafe trusted skill file") from None
    return LoadedSkill(body, SkillUseRecord(entry.skill_id, entry.version,
                                           entry.sha256, digest, target))
