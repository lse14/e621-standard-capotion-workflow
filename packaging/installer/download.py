"""Verified, resumable HTTP downloads for frozen installer artifacts."""
from __future__ import annotations

import hashlib
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit


_CONTENT_RANGE = re.compile(r"^bytes (?P<start>\d+)-(?P<end>\d+)/(?P<total>\d+)$")
_CHUNK_SIZE = 1024 * 1024
_USER_AGENT = "Anima-Source-Bootstrap/1.0"


class ArtifactLike(Protocol):
    artifact_id: str
    url: str
    allowed_hosts: tuple[str, ...]
    size_bytes: int
    sha256: str
    relative_path: str


class ResponseLike(Protocol):
    status: int
    headers: object
    url: str

    def read(self, size: int = -1) -> bytes: ...


class TransportLike(Protocol):
    def open(self, url: str, headers: dict[str, str], allowed_hosts: tuple[str, ...]) -> ResponseLike: ...


class ManualDownloadRequired(RuntimeError):
    """An artifact was not verified and the official manual source is shown."""

    def __init__(self, artifact: ArtifactLike, reason: str) -> None:
        super().__init__(
            f"{reason}; "
            f"Official URL: {artifact.url}; "
            f"Target file: {artifact.relative_path}; "
            f"Expected size: {artifact.size_bytes}; "
            f"SHA-256: {artifact.sha256}"
        )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: tuple[str, ...]) -> None:
        super().__init__()
        self._allowed_hosts = frozenset(host.lower() for host in allowed_hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if not _allowed_https_url(newurl, self._allowed_hosts):
            raise urllib.error.URLError("redirect uses an unapproved HTTPS host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibTransport:
    """Production transport with redirect checks at each urllib redirect."""

    def open(self, url: str, headers: dict[str, str], allowed_hosts: tuple[str, ...]):  # type: ignore[no-untyped-def]
        opener = urllib.request.build_opener(_SafeRedirectHandler(allowed_hosts))
        request = urllib.request.Request(url, headers=headers, method="GET")
        response = opener.open(request, timeout=60)
        return _UrllibResponse(response)


class _UrllibResponse:
    def __init__(self, response) -> None:  # type: ignore[no-untyped-def]
        self._response = response
        self.status = int(response.getcode())
        self.headers = response.headers
        self.url = str(response.geturl())

    def read(self, size: int = -1) -> bytes:
        return self._response.read(size)


def _allowed_https_url(url: str, allowed_hosts: frozenset[str]) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower() in allowed_hosts
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _header(response: ResponseLike, name: str) -> str | None:
    headers = response.headers
    if hasattr(headers, "get"):
        value = headers.get(name)  # type: ignore[union-attr]
        return str(value) if value is not None else None
    return None


def _digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(_CHUNK_SIZE), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def verify_file(path: Path, artifact: ArtifactLike) -> bool:
    try:
        size, sha256 = _digest(path)
    except OSError:
        return False
    return size == artifact.size_bytes and sha256 == artifact.sha256


def _remove_if_file(path: Path) -> None:
    if path.exists() and path.is_file():
        path.unlink()


def _failure_count_after_attempt(partial: Path, offset: int, failures: int) -> int:
    try:
        if partial.is_file() and partial.stat().st_size > offset:
            return 0
    except OSError:
        pass
    return failures + 1


def _publish_if_verified(partial: Path, complete: Path, artifact: ArtifactLike) -> Path | None:
    if not partial.is_file() or not verify_file(partial, artifact):
        return None
    os.replace(partial, complete)
    return complete


def _validate_response_url(response: ResponseLike, artifact: ArtifactLike) -> None:
    if not _allowed_https_url(response.url, frozenset(host.lower() for host in artifact.allowed_hosts)):
        raise ManualDownloadRequired(artifact, "Download redirect uses an unapproved allowed host")


def _validate_range(response: ResponseLike, offset: int, expected_size: int) -> None:
    content_range = _header(response, "Content-Range")
    match = _CONTENT_RANGE.fullmatch(content_range or "")
    if match is None:
        raise ValueError("range response has no valid Content-Range")
    start = int(match.group("start"))
    end = int(match.group("end"))
    total = int(match.group("total"))
    if start != offset or end < start or total != expected_size or end >= total:
        raise ValueError("range response does not match the expected artifact")


def _write_response(response: ResponseLike, partial: Path, *, append: bool, expected_size: int) -> None:
    mode = "ab" if append else "wb"
    with partial.open(mode) as destination:
        while True:
            chunk = response.read(_CHUNK_SIZE)
            if not chunk:
                break
            destination.write(chunk)
            if destination.tell() > expected_size:
                raise ValueError("download exceeds the expected artifact size")


def _terminal_http_error(artifact: ArtifactLike, error: urllib.error.HTTPError) -> ManualDownloadRequired | None:
    if error.code in {401, 403, 404}:
        return ManualDownloadRequired(artifact, f"Download failed with HTTP {error.code}")
    return None


def download_verified(
    artifact: ArtifactLike,
    cache_root: str | Path,
    *,
    transport: TransportLike | None = None,
    attempts: int = 3,
) -> Path:
    """Return a verified cache file or raise an actionable, non-ambiguous error."""
    if attempts < 1:
        raise ValueError("download attempts must be positive")
    cache = Path(cache_root)
    cache.mkdir(parents=True, exist_ok=True)
    complete = cache / artifact.sha256
    partial = cache / f"{artifact.sha256}.partial"
    if complete.exists():
        if complete.is_file() and verify_file(complete, artifact):
            return complete
        _remove_if_file(complete)
    if partial.exists() and (not partial.is_file() or partial.stat().st_size >= artifact.size_bytes):
        _remove_if_file(partial)
    active_transport = transport or UrllibTransport()
    failures = 0
    while failures < attempts:
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": _USER_AGENT}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            response = active_transport.open(artifact.url, headers, artifact.allowed_hosts)
            _validate_response_url(response, artifact)
            if offset and response.status == 200:
                _remove_if_file(partial)
                continue
            if offset:
                if response.status != 206:
                    raise ValueError(f"range request returned HTTP {response.status}")
                _validate_range(response, offset, artifact.size_bytes)
            elif response.status != 200:
                raise ValueError(f"download request returned HTTP {response.status}")
            _write_response(response, partial, append=bool(offset), expected_size=artifact.size_bytes)
        except ManualDownloadRequired:
            raise
        except urllib.error.HTTPError as exc:
            terminal = _terminal_http_error(artifact, exc)
            if terminal is not None:
                raise terminal from exc
            failures = _failure_count_after_attempt(partial, offset, failures)
            continue
        except ValueError as exc:
            if partial.exists() and partial.is_file() and partial.stat().st_size >= artifact.size_bytes:
                actual_size, actual_sha256 = _digest(partial)
                _remove_if_file(partial)
                raise ManualDownloadRequired(
                    artifact,
                    f"Download checksum mismatch: received {actual_size} bytes with SHA-256 {actual_sha256}",
                ) from exc
            failures = _failure_count_after_attempt(partial, offset, failures)
            continue
        except (OSError, urllib.error.URLError):
            verified = _publish_if_verified(partial, complete, artifact)
            if verified is not None:
                return verified
            failures = _failure_count_after_attempt(partial, offset, failures)
            continue
        if verify_file(partial, artifact):
            os.replace(partial, complete)
            return complete
        try:
            actual_size, actual_sha256 = _digest(partial)
        except OSError:
            failures = _failure_count_after_attempt(partial, offset, failures)
            continue
        if actual_size >= artifact.size_bytes:
            _remove_if_file(partial)
            raise ManualDownloadRequired(
                artifact,
                f"Download checksum mismatch: received {actual_size} bytes with SHA-256 {actual_sha256}",
            )
        failures = _failure_count_after_attempt(partial, offset, failures)
    raise ManualDownloadRequired(artifact, "Download failed after bounded retries; a resumable partial may remain")
