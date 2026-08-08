from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProfileId = Literal["e621", "danbooru"]


@dataclass(frozen=True)
class ProfileDefinition:
    profile: ProfileId
    available: bool
    reason: str | None
    runtime_ids: tuple[str, ...]


E621_PROFILE = ProfileDefinition(
    profile="e621",
    available=True,
    reason=None,
    runtime_ids=("caption-e621", "classify-e621", "replace-e621", "ocr-paddle", "nl", "policy", "token-budget", "export"),
)
DANBOORU_PROFILE = ProfileDefinition(
    profile="danbooru",
    available=True,
    reason=None,
    # Caption/Classify share the existing isolated runtimes. Replace is
    # intentionally absent and is completed as a profile skip by Core.
    runtime_ids=("caption-e621", "classify-e621", "ocr-paddle", "nl", "policy", "token-budget", "export"),
)
PROFILE_REGISTRY: dict[str, ProfileDefinition] = {
    E621_PROFILE.profile: E621_PROFILE,
    DANBOORU_PROFILE.profile: DANBOORU_PROFILE,
}


class ProfileUnavailableError(ValueError):
    pass


def get_profile(profile: str) -> ProfileDefinition:
    try:
        return PROFILE_REGISTRY[profile]
    except KeyError as exc:
        raise ProfileUnavailableError(f"unknown profile:{profile}") from exc


def require_available(profile: str) -> ProfileDefinition:
    definition = get_profile(profile)
    if not definition.available:
        raise ProfileUnavailableError(definition.reason or f"profile_not_available:{profile}")
    return definition


def module_availability(profile: str, module_id: str, *, enabled: bool) -> str:
    """Return the only permissible initial status for a module/profile pair."""
    definition = require_available(profile)
    if module_id == "replace" and definition.profile != "e621":
        return "skipped"
    if module_id == "export":
        return "pending"
    if module_id not in {"caption", "classify", "replace", "ocr", "nl", "count_review", "dropout", "token_budget"}:
        raise ValueError(f"unknown module:{module_id}")
    return "pending" if enabled else "skipped"
