from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path


PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
# The current prompt and its legacy predecessor are separate immutable resources.
LEGACY_PROMPT_VERSION = "nl-default-prompt-v1"
DEFAULT_PROMPT_VERSION = "nl-default-prompt-v2"
V5_PROMPT_VERSION = "nl-default-prompt-v3"
V4_BASE_PROMPT_VERSION = "nl-default-prompt-v4-base"
PROMPT_REQUIRED_SNIPPETS = {
    LEGACY_PROMPT_VERSION: (
        "Do not output JSON, XML, Markdown, code fences",
        "Describe visible adult content objectively",
        "120-180+ words",
    ),
    DEFAULT_PROMPT_VERSION: (
        "surrounding fixed JSON protocol",
        "same character",
        "non-human characters and creatures",
        "120-180+ words",
    ),
    V5_PROMPT_VERSION: (
        "short, medium, or long",
        "approximate glyph style",
        "carrier",
        "bubble",
        "untrusted data",
    ),
    V4_BASE_PROMPT_VERSION: (
        "exactly these keys: nl, count, layout, sameCharacterRepeated",
        "untrusted data",
        "complete visible text",
    ),
}
DEFAULT_PROMPT_RELATIVE_PATH = "resources\\nl-default-prompt-v2.txt"
DEFAULT_PROMPT_SOURCE_RELATIVE_PATH = "packaging\\resources\\nl-default-prompt-v2.txt"
DEFAULT_PROMPT_MANIFEST_RELATIVE_PATH = "manifests\\resources\\nl-default-prompt-v2.json"
MAX_PROMPT_BYTES = 65_536
MAX_PROMPT_MANIFEST_BYTES = 16_384


class NlProfileError(ValueError):
    pass


def _prompt_paths(prompt_version: str) -> tuple[str, str, str]:
    if prompt_version not in PROMPT_REQUIRED_SNIPPETS:
        raise NlProfileError("unknown NL prompt version")
    filename = f"{prompt_version}.txt"
    return (
        f"resources\\{filename}",
        f"packaging\\resources\\{filename}",
        f"manifests\\resources\\{prompt_version}.json",
    )


def _locate_default_prompt(
    install_root: str | Path | None = None,
    *,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
) -> tuple[Path, Path | None]:
    relative_path, source_relative_path, manifest_relative_path = _prompt_paths(prompt_version)
    configured = os.environ.get("ANIMA_INSTALL_ROOT")
    roots = [Path(install_root)] if install_root is not None else []
    if configured:
        roots.append(Path(configured))
    roots.extend(Path(__file__).resolve().parents)
    for base in roots:
        distributed = base / Path(relative_path.replace("\\", os.sep))
        if distributed.is_file():
            manifest = base / Path(manifest_relative_path.replace("\\", os.sep))
            return distributed, manifest
        source = base / Path(source_relative_path.replace("\\", os.sep))
        if source.is_file():
            return source, None
    raise NlProfileError("default NL prompt resource is unavailable")


def default_prompt_path(
    install_root: str | Path | None = None,
    *,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
) -> Path:
    """Locate the version-frozen default NL prompt resource."""
    return _locate_default_prompt(install_root, prompt_version=prompt_version)[0]


def _verify_default_prompt_manifest(manifest_path: Path, data: bytes, *, prompt_version: str) -> None:
    try:
        raw_manifest = manifest_path.read_bytes()
    except OSError as exc:
        raise NlProfileError("default NL prompt manifest is unavailable") from exc
    if len(raw_manifest) > MAX_PROMPT_MANIFEST_BYTES:
        raise NlProfileError("default NL prompt manifest exceeds its limit")
    try:
        value = json.loads(raw_manifest.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise NlProfileError("default NL prompt manifest is not strict UTF-8 JSON") from exc
    expected_fields = {"schemaVersion", "resourceId", "owner", "relativePath", "sizeBytes", "sha256"}
    digest = hashlib.sha256(data).hexdigest()
    if (
        not isinstance(value, dict)
        or set(value) != expected_fields
        or value.get("schemaVersion") != 1
        or value.get("resourceId") != prompt_version
        or value.get("owner") != "core"
        or value.get("relativePath") != _prompt_paths(prompt_version)[0]
        or type(value.get("sizeBytes")) is not int
        or value.get("sizeBytes") != len(data)
        or value.get("sha256") != digest
    ):
        raise NlProfileError("default NL prompt manifest identity or digest is invalid")


def load_default_system_prompt(
    install_root: str | Path | None = None,
    *,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
) -> str:
    """Return the frozen prompt text verbatim; re-summarising it in code is forbidden."""
    path, manifest_path = _locate_default_prompt(install_root, prompt_version=prompt_version)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise NlProfileError("default NL prompt resource is unreadable") from exc
    if manifest_path is not None:
        _verify_default_prompt_manifest(manifest_path, data, prompt_version=prompt_version)
    try:
        prompt = data.decode("utf-8-sig").replace("\r\n", "\n").strip()
    except UnicodeDecodeError as exc:
        raise NlProfileError("default NL prompt resource must be UTF-8") from exc
    if not prompt or "\x00" in prompt or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise NlProfileError("default NL prompt resource is empty or exceeds its limit")
    missing = [snippet for snippet in PROMPT_REQUIRED_SNIPPETS[prompt_version] if snippet not in prompt]
    if missing:
        raise NlProfileError("default NL prompt resource lost frozen constraints: " + "; ".join(missing))
    return prompt


def default_nl_prompt(
    install_root: str | Path | None = None,
    *,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
) -> dict[str, object]:
    """Stable interface for the API layer's default/restore-default prompt endpoint."""
    prompt = load_default_system_prompt(install_root, prompt_version=prompt_version)
    return {
        "promptVersion": prompt_version,
        "systemPrompt": prompt,
        "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }


@dataclass(frozen=True)
class NlApiProfile:
    profileId: str
    endpoint: str
    model: str
    backupModel: str | None
    apiCredentialRef: str
    systemPrompt: str
    apiPolicy: dict[str, object]

    @classmethod
    def from_dict(cls, value: object) -> "NlApiProfile":
        if not isinstance(value, dict) or set(value) != {"profileId", "endpoint", "model", "backupModel", "apiCredentialRef", "systemPrompt", "apiPolicy"}:
            raise NlProfileError("NL API profile fields are invalid")
        profile_id = value["profileId"]
        if not isinstance(profile_id, str) or not PROFILE_ID.fullmatch(profile_id):
            raise NlProfileError("NL API profile id is invalid")
        for field in ("endpoint", "model", "apiCredentialRef", "systemPrompt"):
            item = value[field]
            if not isinstance(item, str) or not item.strip() or "\x00" in item or len(item.encode("utf-8")) > 65_536:
                raise NlProfileError(f"NL API profile {field} is invalid")
        backup = value["backupModel"]
        if backup is not None and (not isinstance(backup, str) or not backup.strip() or "\x00" in backup or len(backup.encode("utf-8")) > 512):
            raise NlProfileError("NL API profile backupModel is invalid")
        if not isinstance(value["apiPolicy"], dict):
            raise NlProfileError("NL API profile policy is invalid")
        # Deliberately reject secret-shaped field names before persistence.
        if any("key" in key.casefold() or "token" in key.casefold() or "secret" in key.casefold() for key in value["apiPolicy"]):
            raise NlProfileError("NL API profile policy must not contain credentials")
        return cls(profile_id, value["endpoint"], value["model"], backup, value["apiCredentialRef"], value["systemPrompt"], dict(value["apiPolicy"]))


class NlApiProfileStore:
    """Ordinary local profile settings. Credential material is DPAPI-only and never accepted here."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else Path(os.environ["LOCALAPPDATA"]) / "AnimaDatasetTool" / "nl-api-profiles.json"

    def load_all(self) -> tuple[NlApiProfile, ...]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NlProfileError("NL API profile store is invalid JSON") from exc
        if not isinstance(value, dict) or set(value) != {"schemaVersion", "profiles"} or value["schemaVersion"] != 1 or not isinstance(value["profiles"], list):
            raise NlProfileError("NL API profile store is invalid")
        profiles = tuple(NlApiProfile.from_dict(item) for item in value["profiles"])
        if len({profile.profileId for profile in profiles}) != len(profiles):
            raise NlProfileError("NL API profile ids are duplicated")
        return profiles

    def save(self, profile: NlApiProfile) -> None:
        profiles = {item.profileId: item for item in self.load_all()}
        profiles[profile.profileId] = profile
        self.path.parent.mkdir(parents=True, exist_ok=True)
        target = {"schemaVersion": 1, "profiles": [asdict(profiles[key]) for key in sorted(profiles)]}
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(target, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    def delete(self, profile_id: str) -> None:
        if not PROFILE_ID.fullmatch(profile_id):
            raise NlProfileError("NL API profile id is invalid")
        profiles = {item.profileId: item for item in self.load_all()}
        if profile_id not in profiles:
            return
        del profiles[profile_id]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps({"schemaVersion": 1, "profiles": [asdict(profiles[key]) for key in sorted(profiles)]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
