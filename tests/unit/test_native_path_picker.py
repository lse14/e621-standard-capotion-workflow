from __future__ import annotations

import base64
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.native_path_picker import (
    NativePathPicker,
    NativePathPickerBusyError,
    NativePathPickerUnavailableError,
    _select_with_windows_dialog,
)


class FakeRoot:
    def __init__(self) -> None:
        self.withdrawn = False
        self.destroyed = False

    def withdraw(self) -> None:
        self.withdrawn = True

    def destroy(self) -> None:
        self.destroyed = True


class FakeTk:
    def __init__(self) -> None:
        self.roots: list[FakeRoot] = []

    def Tk(self) -> FakeRoot:
        root = FakeRoot()
        self.roots.append(root)
        return root


class FakeFileDialog:
    def __init__(
        self,
        *,
        directory_result: str = "",
        file_result: str = "",
        raises: Exception | None = None,
    ) -> None:
        self.directory_result = directory_result
        self.file_result = file_result
        self.raises = raises
        self.directory_calls: list[dict[str, object]] = []
        self.file_calls: list[dict[str, object]] = []

    def askdirectory(self, **kwargs: object) -> str:
        self.directory_calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.directory_result

    def askopenfilename(self, **kwargs: object) -> str:
        self.file_calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.file_result


class NativePathPickerTests(unittest.TestCase):
    @patch("anima_core.native_path_picker.subprocess.run")
    def test_windows_dialog_uses_the_explorer_common_item_dialog_for_folders(self, run: object) -> None:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")  # type: ignore[attr-defined]

        self.assertIsNone(_select_with_windows_dialog("source_dataset", None))

        command = run.call_args.args[0]  # type: ignore[attr-defined]
        script = base64.b64decode(command[-1]).decode("utf-16-le")
        self.assertIn("IFileDialog", script)
        self.assertIn("FOS_PICKFOLDERS", script)
        self.assertIn("SHCreateItemFromParsingName", script)
        self.assertNotIn("FolderBrowserDialog", script)

    @patch("anima_core.native_path_picker.subprocess.run")
    def test_windows_dialog_uses_a_common_item_csv_filter(self, run: object) -> None:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")  # type: ignore[attr-defined]

        self.assertIsNone(_select_with_windows_dialog("replacement_csv", None))

        command = run.call_args.args[0]  # type: ignore[attr-defined]
        script = base64.b64decode(command[-1]).decode("utf-16-le")
        self.assertIn("COMDLG_FILTERSPEC", script)
        self.assertIn("FOS_FILEMUSTEXIST", script)
        self.assertIn("UnmanagedType.LPArray", script)

    @patch("anima_core.native_path_picker.subprocess.run")
    def test_windows_dialog_uses_the_foreground_window_as_owner(self, run: object) -> None:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")  # type: ignore[attr-defined]

        self.assertIsNone(_select_with_windows_dialog("source_dataset", None))

        command = run.call_args.args[0]  # type: ignore[attr-defined]
        self.assertNotIn("-WindowStyle", command)
        script = base64.b64decode(command[-1]).decode("utf-16-le")
        self.assertIn("GetForegroundWindow", script)
        self.assertIn("[AnimaExplorerPathDialog]::Show(", script)
        self.assertIn(", $owner)", script)
        self.assertNotIn("ShowDialog", script)

    def test_default_path_selection_uses_a_native_dialog_runner_without_tk(self) -> None:
        calls: list[tuple[str, str | None]] = []

        def native_dialog(purpose: str, current_path: str | None) -> str | None:
            calls.append((purpose, current_path))
            return r"E:\picked\source"

        picker = NativePathPicker(dialog_runner=native_dialog)

        self.assertEqual(r"E:\picked\source", picker.select("source_dataset", r"E:\typed\source"))
        self.assertEqual([("source_dataset", r"E:\typed\source")], calls)

    def test_source_and_output_use_directory_dialog_with_distinct_mustexist(self) -> None:
        tkinter = FakeTk()
        dialog = FakeFileDialog(directory_result=r"E:\picked")
        picker = NativePathPicker(tk_loader=lambda: (tkinter, dialog))

        self.assertEqual(r"E:\picked", picker.select("source_dataset", r"E:\typed\source"))
        self.assertEqual(r"E:\picked", picker.select("output_dataset", r"E:\typed\output"))

        self.assertEqual([True, False], [call["mustexist"] for call in dialog.directory_calls])
        self.assertEqual([], dialog.file_calls)
        self.assertTrue(all(root.withdrawn and root.destroyed for root in tkinter.roots))

    def test_replacement_uses_csv_file_filter_and_cancellation_is_none(self) -> None:
        tkinter = FakeTk()
        dialog = FakeFileDialog(file_result="")
        picker = NativePathPicker(tk_loader=lambda: (tkinter, dialog))

        self.assertIsNone(picker.select("replacement_csv", r"E:\rules\replace.csv"))

        self.assertEqual((("CSV files", "*.csv"),), dialog.file_calls[0]["filetypes"])
        self.assertEqual([], dialog.directory_calls)
        self.assertEqual(1, len(tkinter.roots))
        self.assertTrue(tkinter.roots[0].withdrawn)
        self.assertTrue(tkinter.roots[0].destroyed)

    def test_invalid_purpose_busy_and_loader_failure_have_stable_errors(self) -> None:
        unavailable = NativePathPicker(tk_loader=lambda: (_ for _ in ()).throw(ImportError("missing")))
        with self.assertRaises(ValueError):
            unavailable.select("other", None)  # type: ignore[arg-type]
        with self.assertRaises(NativePathPickerUnavailableError):
            unavailable.select("source_dataset", None)

        busy = NativePathPicker(tk_loader=lambda: (FakeTk(), FakeFileDialog()))
        self.assertTrue(busy._dialog_lock.acquire(blocking=False))
        try:
            with self.assertRaises(NativePathPickerBusyError):
                busy.select("source_dataset", None)
        finally:
            busy._dialog_lock.release()

    def test_dialog_failure_destroys_root_before_reporting_unavailable(self) -> None:
        tkinter = FakeTk()
        picker = NativePathPicker(
            tk_loader=lambda: (tkinter, FakeFileDialog(raises=RuntimeError("tcl failure"))),
        )

        with self.assertRaises(NativePathPickerUnavailableError):
            picker.select("replacement_csv", None)

        self.assertEqual(1, len(tkinter.roots))
        self.assertTrue(tkinter.roots[0].destroyed)


if __name__ == "__main__":
    unittest.main()
