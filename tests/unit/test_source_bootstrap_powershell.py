from __future__ import annotations

import subprocess
import tempfile
import unittest
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "packaging" / "scripts" / "bootstrap_install.ps1"


class SourceBootstrapPowerShellTests(unittest.TestCase):
    def _bootstrap_text(self) -> str:
        self.assertTrue(BOOTSTRAP.is_file(), "source bootstrap PowerShell script must exist")
        return BOOTSTRAP.read_text(encoding="utf-8")

    def test_source_entrypoint_uses_bootstrap_not_legacy_ocr_prompt(self) -> None:
        batch = (ROOT / "Install-WebUI.bat").read_text(encoding="ascii")
        script = self._bootstrap_text()

        self.assertIn("bootstrap_install.ps1", batch)
        self.assertNotIn("desktop_control.ps1", batch)
        self.assertNotIn("OcrMode", batch)
        self.assertIn("[Environment]::Is64BitOperatingSystem", script)
        self.assertIn("Get-Volume", script)
        self.assertIn("install-manifest.json", script)
        self.assertIn("Get-FileHash", script)
        self.assertIn("Range", script)
        self.assertIn("install.py", script)
        self.assertIn("--bootstrap-runtime", script)
        self.assertNotIn("LOCALAPPDATA", script)

    def test_bootstrap_parses_in_windows_powershell(self) -> None:
        self.assertTrue(BOOTSTRAP.is_file(), "source bootstrap PowerShell script must exist")
        command = (
            "$tokens=$null; $errors=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "(Resolve-Path 'packaging\\scripts\\bootstrap_install.ps1'),[ref]$tokens,[ref]$errors) | Out-Null; "
            "if ($errors.Count) { $errors | ForEach-Object { $_.Message }; exit 1 }"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_bootstrap_normalizes_safe_relative_paths_without_accepting_absolute_paths(self) -> None:
        self.assertTrue(BOOTSTRAP.is_file(), "source bootstrap PowerShell script must exist")
        command = (
            "$tokens=$null; $errors=$null; "
            "$ast=[System.Management.Automation.Language.Parser]::ParseFile("
            "(Resolve-Path 'packaging\\scripts\\bootstrap_install.ps1'),[ref]$tokens,[ref]$errors); "
            "if ($errors.Count) { exit 1 }; "
            "$node=$ast.FindAll({ param($item) $item -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $item.Name -eq 'Assert-SafeRelativePath' },$true) | Select-Object -First 1; "
            "Invoke-Expression $node.Extent.Text; "
            "$rejected=$false; try { Assert-SafeRelativePath 'C:\\outside' | Out-Null } catch { $rejected=$true }; "
            "$trailingRejected=$false; try { Assert-SafeRelativePath 'Lib/' | Out-Null } catch { $trailingRejected=$true }; "
            "[ordered]@{value=(Assert-SafeRelativePath 'Lib/python.exe');directory=(Assert-SafeRelativePath 'Lib/' -AllowDirectory);rejected=$rejected;trailingRejected=$trailingRejected} | ConvertTo-Json -Compress"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(r"Lib\python.exe", value["value"])
        self.assertEqual("Lib", value["directory"])
        self.assertTrue(value["rejected"])
        self.assertTrue(value["trailingRejected"])

    def test_bootstrap_missing_manifest_fails_with_a_project_local_utf8_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            project_root = Path(temporary_name)
            completed = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(BOOTSTRAP), "-ProjectRoot", str(project_root),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertNotEqual(0, completed.returncode)
            logs = list((project_root / ".runtime-build" / "logs").glob("source-bootstrap-*.log"))
            self.assertEqual(1, len(logs))
            self.assertIn("Frozen install manifest is missing", logs[0].read_text(encoding="utf-8"))
            self.assertFalse((project_root / ".runtime-build" / "source-bootstrap").exists())


if __name__ == "__main__":
    unittest.main()
