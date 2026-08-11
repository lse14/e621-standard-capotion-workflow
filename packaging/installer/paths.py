"""Project-contained staging, archive expansion, and transactional publication."""
from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Mapping


_RESERVED = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{index}" for index in range(1, 10)), *(f"LPT{index}" for index in range(1, 10)),
}


class PathSafetyError(ValueError):
    """A path, archive member, or transaction escaped its permitted root."""


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _is_reparse(path: Path) -> bool:
    information = os.lstat(path)
    attributes = getattr(information, "st_file_attributes", 0)
    return stat.S_ISLNK(information.st_mode) or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PathSafetyError("relative path is invalid")
    normalized = value.replace("/", "\\")
    path = PureWindowsPath(normalized)
    if path.is_absolute() or path.drive or path.root or normalized.startswith("\\"):
        raise PathSafetyError("relative path is invalid")
    parts = normalized.split("\\")
    for part in parts:
        device = part.split(".", 1)[0].upper()
        if not part or part in {".", ".."} or ":" in part or part.endswith((".", " ")) or device in _RESERVED:
            raise PathSafetyError("relative path is invalid")
    return "\\".join(parts)


def _assert_safe_existing_chain(root: Path, candidate: Path) -> None:
    if not root.is_dir() or _is_reparse(root):
        raise PathSafetyError("project root is unsafe")
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise PathSafetyError("path escapes project root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if not current.exists():
            break
        if _is_reparse(current):
            raise PathSafetyError("reparse point is not allowed")


def assert_within_root(root: str | Path, candidate: str | Path) -> Path:
    resolved_root = _absolute(root)
    resolved_candidate = _absolute(candidate)
    try:
        common = os.path.commonpath((os.path.normcase(str(resolved_root)), os.path.normcase(str(resolved_candidate))))
    except ValueError as exc:
        raise PathSafetyError("path escapes project root") from exc
    if os.path.normcase(common) != os.path.normcase(str(resolved_root)):
        raise PathSafetyError("path escapes project root")
    _assert_safe_existing_chain(resolved_root, resolved_candidate)
    return resolved_candidate


def _assert_safe_tree(root: Path) -> None:
    if not root.exists() or _is_reparse(root):
        raise PathSafetyError("path is unsafe")
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise PathSafetyError("path cannot be inspected") from exc
        for entry in entries:
            child = Path(entry.path)
            if _is_reparse(child):
                raise PathSafetyError("reparse point is not allowed")
            if entry.is_dir(follow_symlinks=False):
                pending.append(child)
            elif not entry.is_file(follow_symlinks=False):
                raise PathSafetyError("path contains unsupported entry")


def _remove_tree(root: Path, target: Path) -> None:
    target = assert_within_root(root, target)
    if not target.exists():
        return
    if not target.is_dir() or _is_reparse(target):
        raise PathSafetyError("cleanup target is unsafe")
    _assert_safe_tree(target)
    shutil.rmtree(target)


def _remove_file(root: Path, target: Path) -> None:
    target = assert_within_root(root, target)
    if not target.exists():
        return
    if not target.is_file() or _is_reparse(target):
        raise PathSafetyError("cleanup target is unsafe")
    target.unlink()


@dataclass(frozen=True)
class ProjectLayout:
    project_root: Path
    runtime_root: Path
    installer_root: Path
    bootstrap: Path
    cache: Path
    staging: Path
    transactions: Path
    logs: Path

    @classmethod
    def create(cls, project_root: str | Path) -> "ProjectLayout":
        root = _absolute(project_root)
        if not root.is_dir() or _is_reparse(root):
            raise PathSafetyError("project root is unsafe")
        runtime_root = assert_within_root(root, root / ".runtime-build")
        installer_root = assert_within_root(root, runtime_root / "source-bootstrap")
        return cls(
            root,
            runtime_root,
            installer_root,
            installer_root / "bootstrap",
            installer_root / "cache",
            installer_root / "staging",
            installer_root / "transactions",
            runtime_root / "logs",
        )

    def ensure_directories(self) -> None:
        for path in (self.bootstrap, self.cache, self.staging, self.transactions, self.logs):
            assert_within_root(self.project_root, path)
            path.mkdir(parents=True, exist_ok=True)
            if _is_reparse(path):
                raise PathSafetyError("installer directory is unsafe")


def _zip_member_relative(info: zipfile.ZipInfo) -> tuple[str, bool]:
    raw_name = info.filename
    is_directory = info.is_dir() or raw_name.endswith(("/", "\\"))
    name = raw_name.rstrip("/\\") if is_directory else raw_name
    if not name:
        raise PathSafetyError("unsafe archive member")
    unix_mode = (info.external_attr >> 16) & 0o170000
    if unix_mode == 0o120000:
        raise PathSafetyError("archive link member is not allowed")
    if unix_mode not in {0, 0o100000, 0o040000}:
        raise PathSafetyError("archive member type is unsupported")
    if unix_mode == 0o040000 and not is_directory:
        raise PathSafetyError("archive member type is unsupported")
    if unix_mode == 0o100000 and is_directory:
        raise PathSafetyError("archive member type is unsupported")
    try:
        return safe_relative(name), is_directory
    except PathSafetyError as exc:
        raise PathSafetyError("unsafe archive member") from exc


def safe_extract_zip(archive_path: str | Path, destination: str | Path) -> None:
    archive = _absolute(archive_path)
    destination_path = _absolute(destination)
    if not archive.is_file() or _is_reparse(archive) or destination_path.exists():
        raise PathSafetyError("archive input or destination is unsafe")
    existing_parent = destination_path.parent
    while not existing_parent.exists():
        existing_parent = existing_parent.parent
    if _is_reparse(existing_parent):
        raise PathSafetyError("archive destination is unsafe")
    try:
        with zipfile.ZipFile(archive) as source:
            members = [(_zip_member_relative(info), info) for info in source.infolist()]
            paths = [relative.casefold() for (relative, _), _info in members]
            if len(paths) != len(set(paths)):
                raise PathSafetyError("archive member case collision")
            destination_path.mkdir(parents=True)
            for (relative, is_directory), info in members:
                target = destination_path / Path(relative.replace("\\", os.sep))
                if is_directory:
                    target.mkdir(parents=True, exist_ok=False)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(info) as input_stream, target.open("xb") as output_stream:
                    shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
    except PathSafetyError:
        if destination_path.exists():
            shutil.rmtree(destination_path, ignore_errors=True)
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        if destination_path.exists():
            shutil.rmtree(destination_path, ignore_errors=True)
        raise PathSafetyError("archive cannot be safely extracted") from exc
    _assert_safe_tree(destination_path)


def _relative_to_root(layout: ProjectLayout, path: Path) -> str:
    return safe_relative(str(path.relative_to(layout.project_root)).replace("/", "\\"))


def _journal_path(layout: ProjectLayout) -> Path:
    return layout.transactions / f"{uuid.uuid4().hex}.json"


def _write_journal(layout: ProjectLayout, path: Path, value: dict[str, object]) -> None:
    target = assert_within_root(layout.project_root, path)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, target)


def _read_journal(layout: ProjectLayout, path: Path) -> list[dict[str, object]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PathSafetyError("transaction journal is invalid") from exc
    if not isinstance(value, dict) or set(value) != {"schemaVersion", "entries"} or value.get("schemaVersion") != 1 or not isinstance(value.get("entries"), list):
        raise PathSafetyError("transaction journal is invalid")
    entries: list[dict[str, object]] = []
    for raw in value["entries"]:
        if not isinstance(raw, dict) or set(raw) != {
            "targetRelativePath", "stageRelativePath", "backupRelativePath", "previousMoved", "promoted",
        }:
            raise PathSafetyError("transaction journal is invalid")
        if type(raw["previousMoved"]) is not bool or type(raw["promoted"]) is not bool:
            raise PathSafetyError("transaction journal is invalid")
        for name in ("targetRelativePath", "stageRelativePath", "backupRelativePath"):
            safe_relative(raw[name])
        entries.append(raw)
    return entries


def _journal_target(layout: ProjectLayout, relative: object) -> Path:
    return assert_within_root(layout.project_root, layout.project_root / Path(safe_relative(relative).replace("\\", os.sep)))


def _is_contained(root: Path, candidate: Path) -> bool:
    try:
        common = os.path.commonpath((os.path.normcase(str(root)), os.path.normcase(str(candidate))))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(str(root))


def _assert_publish_target(layout: ProjectLayout, target: Path) -> None:
    allowed_roots = (layout.runtime_root / "runtimes", layout.project_root / "resource-library")
    if not any(_is_contained(root, target) for root in allowed_roots):
        raise PathSafetyError("publish target is protected")


def recover_transactions(layout: ProjectLayout) -> None:
    layout.ensure_directories()
    for journal in sorted(layout.transactions.glob("*.json"), key=lambda item: item.name):
        if _is_reparse(journal):
            raise PathSafetyError("transaction journal is unsafe")
        entries = _read_journal(layout, journal)
        for record in reversed(entries):
            target = _journal_target(layout, record["targetRelativePath"])
            backup = _journal_target(layout, record["backupRelativePath"])
            if record["promoted"] and target.exists():
                _remove_tree(layout.project_root, target)
            if record["previousMoved"] and backup.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, target)
        _remove_file(layout.project_root, journal)
    backups = layout.transactions / "backups"
    if backups.exists():
        _remove_tree(layout.project_root, backups)


def publish_directories(layout: ProjectLayout, staged_directories: Mapping[str, str | Path]) -> None:
    if not staged_directories:
        return
    layout.ensure_directories()
    records: list[dict[str, object]] = []
    transaction_id = uuid.uuid4().hex
    for index, (raw_target, raw_stage) in enumerate(staged_directories.items()):
        target_relative = safe_relative(raw_target)
        target = _journal_target(layout, target_relative)
        _assert_publish_target(layout, target)
        stage = assert_within_root(layout.project_root, raw_stage)
        assert_within_root(layout.staging, stage)
        if not stage.is_dir() or _is_reparse(stage):
            raise PathSafetyError("staged directory is unsafe")
        _assert_safe_tree(stage)
        backup = layout.transactions / "backups" / transaction_id / f"entry-{index}"
        records.append(
            {
                "targetRelativePath": target_relative,
                "stageRelativePath": _relative_to_root(layout, stage),
                "backupRelativePath": _relative_to_root(layout, backup),
                "previousMoved": False,
                "promoted": False,
            }
        )
    journal = _journal_path(layout)
    journal_value: dict[str, object] = {"schemaVersion": 1, "entries": records}
    _write_journal(layout, journal, journal_value)
    try:
        for record in records:
            target = _journal_target(layout, record["targetRelativePath"])
            stage = _journal_target(layout, record["stageRelativePath"])
            backup = _journal_target(layout, record["backupRelativePath"])
            if target.exists():
                if not target.is_dir() or _is_reparse(target):
                    raise PathSafetyError("existing target is unsafe")
                _assert_safe_tree(target)
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, backup)
                record["previousMoved"] = True
                _write_journal(layout, journal, journal_value)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, target)
            record["promoted"] = True
            _write_journal(layout, journal, journal_value)
        backups = layout.transactions / "backups"
        if backups.exists():
            _remove_tree(layout.project_root, backups)
        _remove_file(layout.project_root, journal)
    except Exception:
        recover_transactions(layout)
        raise


def cleanup_failure(layout: ProjectLayout) -> None:
    layout.ensure_directories()
    _remove_tree(layout.project_root, layout.bootstrap)
    _remove_tree(layout.project_root, layout.staging)
    _remove_tree(layout.project_root, layout.transactions)
    for path in sorted(layout.cache.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_file() and path.suffix.lower() != ".partial":
            _remove_file(layout.project_root, path)
        elif path.is_dir() and not any(path.iterdir()):
            _remove_tree(layout.project_root, path)


def cleanup_success(layout: ProjectLayout) -> None:
    layout.ensure_directories()
    for path in (layout.bootstrap, layout.cache, layout.staging, layout.transactions):
        _remove_tree(layout.project_root, path)
