from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class DesktopControlTests(unittest.TestCase):
    def test_bat_entrypoints_delegate_to_the_single_control_script(self) -> None:
        for action, filename in (("Install", "Install-WebUI.bat"), ("Start", "Start-WebUI.bat"), ("Stop", "Stop-WebUI.bat")):
            contents = (ROOT / filename).read_text(encoding="ascii")
            self.assertIn("desktop_control.ps1", contents)
            self.assertIn(f"-Action {action}", contents)

    def test_launcher_uses_only_the_embedded_core_and_safe_stop_protocol(self) -> None:
        script = (ROOT / "packaging" / "scripts" / "desktop_control.ps1").read_text(encoding="utf-8")
        self.assertIn("runtimes\\core\\python.exe", script)
        self.assertIn("-B -I -m anima_core", script)
        self.assertIn("--check-runtime", script)
        self.assertIn("webui-", script)
        self.assertIn("Get-InstallationStateId", script)
        self.assertIn("Get-WebUiInstanceId", script)
        self.assertIn('("webui-{0}.json" -f $instanceId)', script)
        self.assertIn('("webui-{0}.stdout.log" -f $instanceId)', script)
        self.assertIn('("webui-{0}.stderr.log" -f $instanceId)', script)
        self.assertIn("$legacyState.port -eq $Port", script)
        self.assertIn("/api/application/shutdown", script)
        self.assertIn("Get-NetTCPConnection", script)
        self.assertIn("RNGCryptoServiceProvider", script)
        stop_body = script.partition("function Stop-WebUi")[2]
        self.assertNotIn("Stop-Process", stop_body)
        self.assertNotIn("taskkill", script.lower())

    def test_start_recovers_a_healthy_webui_when_the_state_file_is_missing(self) -> None:
        script = (ROOT / "packaging" / "scripts" / "desktop_control.ps1").read_text(encoding="utf-8")
        start_body = script.partition("function Start-WebUi")[2].partition("function Stop-WebUi")[0]
        stop_body = script.partition("function Stop-WebUi")[2]
        self.assertIn("netstat -ano -p tcp", script)
        self.assertIn("function Test-HealthyWebUi", script)
        self.assertIn("$health.protocolVersion -eq '1.0'", script)
        self.assertIn("$listeners = @(Get-Listener $Port)", start_body)
        self.assertIn("Test-HealthyWebUi $Port", start_body)
        self.assertIn('Start-Process "http://127.0.0.1:$Port/"', start_body)
        self.assertIn("(@(Get-Listener $Port).Count -eq 0)", stop_body)

    def test_install_ocr_mode_is_explicit_and_never_defaults_to_cpu(self) -> None:
        script = (ROOT / "packaging" / "scripts" / "desktop_control.ps1").read_text(encoding="utf-8")
        batch = (ROOT / "Install-WebUI.bat").read_text(encoding="ascii")
        self.assertIn("[ValidateSet('Prompt', 'None', 'Cpu', 'Gpu')][string]$OcrMode = 'Prompt'", script)
        self.assertIn("$OcrMode -ne 'Prompt'", script)
        self.assertIn("ocr_component.py", script)
        self.assertIn("-OcrMode None^|Cpu^|Gpu", batch)

    def test_control_script_parses_in_windows_powershell(self) -> None:
        command = (
            "$tokens=$null; $errors=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "(Resolve-Path 'packaging\\scripts\\desktop_control.ps1'),[ref]$tokens,[ref]$errors) | Out-Null; "
            "if ($errors.Count) { $errors | ForEach-Object { $_.Message }; exit 1 }"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_same_installation_uses_a_distinct_instance_id_for_each_port(self) -> None:
        command = (
            "$tokens=$null; $errors=$null; "
            "$ast=[System.Management.Automation.Language.Parser]::ParseFile("
            "(Resolve-Path 'packaging\\scripts\\desktop_control.ps1'),[ref]$tokens,[ref]$errors); "
            "$wanted=@('Get-InstallationStateId','Get-WebUiInstanceId'); "
            "$nodes=$ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $node.Name },$true); "
            "Invoke-Expression (($nodes | ForEach-Object { $_.Extent.Text }) -join \"`n\"); "
            "[ordered]@{first=(Get-WebUiInstanceId 'C:\\Program Files\\Anima Tool' 8765);second=(Get-WebUiInstanceId 'C:\\Program Files\\Anima Tool' 8766)} | ConvertTo-Json -Compress"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        values = json.loads(completed.stdout)
        self.assertNotEqual(values["first"], values["second"])
        self.assertTrue(values["first"].endswith("-8765"))
        self.assertTrue(values["second"].endswith("-8766"))
        self.assertEqual(values["first"].rsplit("-", 1)[0], values["second"].rsplit("-", 1)[0])


if __name__ == "__main__":
    unittest.main()
