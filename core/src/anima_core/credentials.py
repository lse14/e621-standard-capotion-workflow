from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes
from pathlib import Path


REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class CredentialStoreError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_byte]]:
    buffer = (ctypes.c_byte * len(value)).from_buffer_copy(value)
    return _DataBlob(len(value), buffer), buffer


class DpapiCredentialStore:
    """Current-Windows-user encrypted secret store; normal config retains references only."""

    def __init__(self, root: str | Path | None = None) -> None:
        base = Path(root) if root is not None else Path(os.environ["LOCALAPPDATA"]) / "AnimaDatasetTool" / "credentials"
        self.root = base

    @staticmethod
    def _name(reference: str) -> str:
        if not REFERENCE.fullmatch(reference):
            raise CredentialStoreError("credential reference is invalid")
        return reference + ".dpapi"

    @staticmethod
    def _crypt(protect: bool, payload: bytes) -> bytes:
        if os.name != "nt":
            raise CredentialStoreError("DPAPI is available only on Windows")
        source, source_buffer = _blob(payload)
        output = _DataBlob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        if protect:
            success = crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output))
        else:
            success = crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output))
        if not success:
            raise CredentialStoreError("Windows DPAPI operation failed")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            kernel32.LocalFree(output.pbData)

    def save(self, reference: str, secret: str) -> None:
        if not isinstance(secret, str) or not secret or "\x00" in secret or len(secret.encode("utf-8")) > 16_384:
            raise CredentialStoreError("credential value is invalid")
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / self._name(reference)
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(self._crypt(True, secret.encode("utf-8")))
        os.replace(temporary, target)

    def load(self, reference: str) -> str:
        target = self.root / self._name(reference)
        try:
            value = self._crypt(False, target.read_bytes()).decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CredentialStoreError("credential cannot be read for the current Windows user") from exc
        if not value or "\x00" in value:
            raise CredentialStoreError("credential data is invalid")
        return value

    def delete(self, reference: str) -> None:
        target = self.root / self._name(reference)
        try:
            target.unlink()
        except FileNotFoundError:
            return
