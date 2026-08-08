from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path


TOKENIZER_IDENTITIES = {
    "tokenizer-qwen3-0.6b-anima-v1": "Qwen/Qwen3-0.6B",
    "tokenizer-qwen3-vl-4b-krea2-v1": "Qwen/Qwen3-VL-4B-Instruct",
}
TOKENIZER_ALLOWLIST = frozenset({
    "added_tokens.json", "config.json", "merges.txt", "special_tokens_map.json", "tokenizer.json",
    "tokenizer_config.json", "vocab.json", "vocab.txt",
})
MANIFEST_FIELDS = frozenset({
    "schemaVersion", "kind", "resourceId", "owner", "profile", "resourceVersion", "officialModelId", "revision",
    "tokenizerFamily", "contextLimit", "rootRelativePath", "files", "fingerprint", "distribution",
})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")


class TokenizerResourceError(ValueError):
    pass


@dataclass(frozen=True)
class TokenizerResource:
    resource_id: str
    fingerprint: str
    context_limit: int
    tokenizer: object


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TokenizerResourceError("tokenizer manifest has duplicate keys")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise TokenizerResourceError("tokenizer manifest contains a non-finite number")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _is_reparse(path: Path) -> bool:
    information = os.lstat(path)
    return stat.S_ISLNK(information.st_mode) or bool(
        getattr(information, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _safe_child(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or "/" in relative or relative not in TOKENIZER_ALLOWLIST:
        raise TokenizerResourceError("tokenizer file path is invalid")
    target = root / relative
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise TokenizerResourceError("tokenizer file escaped its resource") from exc
    if not target.is_file() or _is_reparse(target):
        raise TokenizerResourceError("tokenizer file is missing or unsafe")
    return target


def _load_tokenizer(path: Path) -> object:
    try:
        from tokenizers import Tokenizer
    except ImportError as exc:
        raise TokenizerResourceError("tokenizers runtime dependency is unavailable") from exc
    try:
        return Tokenizer.from_file(str(path))
    except Exception as exc:
        raise TokenizerResourceError("tokenizer.json cannot be loaded offline") from exc


def load_tokenizer_resource(
    resource_root: Path,
    manifest_relative_path: str,
    expected_resource_id: str,
    expected_fingerprint: str,
    expected_context_limit: int,
    *,
    tokenizer_loader=_load_tokenizer,
) -> TokenizerResource:
    if expected_resource_id not in TOKENIZER_IDENTITIES or not _SHA256.fullmatch(expected_fingerprint) or type(expected_context_limit) is not int or expected_context_limit < 1:
        raise TokenizerResourceError("frozen tokenizer identity is invalid")
    if not isinstance(manifest_relative_path, str) or manifest_relative_path.replace("/", "\\") != f"tokenizers\\{expected_resource_id}\\resource.json":
        raise TokenizerResourceError("tokenizer manifest path is invalid")
    root = resource_root.resolve()
    manifest = root / Path(manifest_relative_path.replace("\\", os.sep))
    if not manifest.is_file() or _is_reparse(manifest):
        raise TokenizerResourceError("tokenizer manifest is unavailable")
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"), object_pairs_hook=_strict_object, parse_constant=_reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, TokenizerResourceError) as exc:
        raise TokenizerResourceError("tokenizer manifest is invalid") from exc
    if not isinstance(value, dict) or set(value) != MANIFEST_FIELDS:
        raise TokenizerResourceError("tokenizer manifest fields are invalid")
    if (
        value.get("schemaVersion") != 3 or value.get("kind") != "tokenizer" or value.get("owner") != "token-budget"
        or value.get("profile") != "shared" or value.get("resourceId") != expected_resource_id
        or value.get("officialModelId") != TOKENIZER_IDENTITIES[expected_resource_id] or value.get("tokenizerFamily") != "qwen3"
        or not isinstance(value.get("revision"), str) or not _REVISION.fullmatch(value["revision"])
        or value.get("rootRelativePath") != f"tokenizers\\{expected_resource_id}"
    ):
        raise TokenizerResourceError("tokenizer manifest identity is invalid")
    if value.get("contextLimit") != expected_context_limit:
        raise TokenizerResourceError("tokenizer context limit does not match the frozen request")
    if value.get("distribution") != {"mode": "local-only", "sourceUrl": f"https://huggingface.co/{TOKENIZER_IDENTITIES[expected_resource_id]}", "licenseStatus": "unverified"}:
        raise TokenizerResourceError("tokenizer distribution is invalid")
    fingerprint = value.get("fingerprint")
    unsigned = {key: value[key] for key in sorted(MANIFEST_FIELDS - {"fingerprint"})}
    calculated = hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    if fingerprint != expected_fingerprint or fingerprint != calculated:
        raise TokenizerResourceError("tokenizer fingerprint does not match the frozen request")
    records = value.get("files")
    if not isinstance(records, list) or not records:
        raise TokenizerResourceError("tokenizer file list is invalid")
    paths: list[str] = []
    package_root = manifest.parent
    if _is_reparse(package_root):
        raise TokenizerResourceError("tokenizer resource root is unsafe")
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "sizeBytes", "sha256"}:
            raise TokenizerResourceError("tokenizer file record is invalid")
        path = record.get("path")
        size = record.get("sizeBytes")
        digest = record.get("sha256")
        if not isinstance(path, str) or type(size) is not int or size < 1 or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise TokenizerResourceError("tokenizer file record is invalid")
        target = _safe_child(package_root, path)
        if target.stat().st_size != size or _sha256_file(target) != digest:
            raise TokenizerResourceError("tokenizer file hash does not match the manifest")
        paths.append(path)
    if paths != sorted(paths) or len(paths) != len(set(paths)) or not {"config.json", "tokenizer.json"} <= set(paths):
        raise TokenizerResourceError("tokenizer file allowlist is invalid")
    actual = {path.name for path in package_root.iterdir() if path.is_file()}
    if actual != {"resource.json", *paths}:
        raise TokenizerResourceError("tokenizer resource contains an unknown file")
    return TokenizerResource(expected_resource_id, expected_fingerprint, expected_context_limit, tokenizer_loader(package_root / "tokenizer.json"))
