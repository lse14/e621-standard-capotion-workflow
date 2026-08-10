from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
from threading import Lock
from typing import Callable, Literal


PathPickerPurpose = Literal["source_dataset", "output_dataset", "replacement_csv"]
TkLoader = Callable[[], tuple[object, object]]
DialogRunner = Callable[[PathPickerPurpose, str | None], str | None]


class NativePathPickerBusyError(RuntimeError):
    pass


class NativePathPickerUnavailableError(RuntimeError):
    pass


def _load_tk() -> tuple[object, object]:
    import tkinter
    from tkinter import filedialog

    return tkinter, filedialog


def _select_with_windows_dialog(purpose: PathPickerPurpose, current_path: str | None) -> str | None:
    request = {
        "purpose": purpose,
        "initialDirectory": NativePathPicker._initialdir(purpose, current_path),
    }
    encoded_request = base64.b64encode(json.dumps(request).encode("utf-8")).decode("ascii")
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$request = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('__ENCODED_REQUEST__')) | ConvertFrom-Json
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;

[Flags]
internal enum FileOpenOptions : uint
{
    None = 0,
    FOS_PICKFOLDERS = 0x00000020,
    FOS_FORCEFILESYSTEM = 0x00000040,
    FOS_PATHMUSTEXIST = 0x00000800,
    FOS_FILEMUSTEXIST = 0x00001000,
}

internal enum SIGDN : uint
{
    FILESYSPATH = 0x80058000,
}

[StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
internal struct COMDLG_FILTERSPEC
{
    [MarshalAs(UnmanagedType.LPWStr)]
    public string pszName;

    [MarshalAs(UnmanagedType.LPWStr)]
    public string pszSpec;
}

[ComImport]
[Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IShellItem
{
    void BindToHandler(IntPtr bindContext, ref Guid bhid, ref Guid riid, out IntPtr value);
    void GetParent(out IShellItem parent);
    void GetDisplayName(SIGDN name, out IntPtr value);
    void GetAttributes(uint mask, out uint attributes);
    int Compare(IShellItem other, uint hint, out int order);
}

[ComImport]
[Guid("42f85136-db7e-439c-85f1-e4075d135fc8")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IFileDialog
{
    [PreserveSig]
    int Show(IntPtr owner);
    void SetFileTypes(
        uint count,
        [In, MarshalAs(UnmanagedType.LPArray, SizeParamIndex = 0)] COMDLG_FILTERSPEC[] filters);
    void SetFileTypeIndex(uint index);
    void GetFileTypeIndex(out uint index);
    void Advise(IntPtr eventsSink, out uint cookie);
    void Unadvise(uint cookie);
    void SetOptions(FileOpenOptions options);
    void GetOptions(out FileOpenOptions options);
    void SetDefaultFolder(IShellItem folder);
    void SetFolder(IShellItem folder);
    void GetFolder(out IShellItem folder);
    void GetCurrentSelection(out IShellItem selection);
    void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string name);
    void GetFileName(out IntPtr name);
    void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string title);
    void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string label);
    void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string label);
    void GetResult(out IShellItem selection);
    void AddPlace(IShellItem location, uint placement);
    void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string extension);
    void Close(int result);
    void SetClientGuid(ref Guid clientGuid);
    void ClearClientData();
    void SetFilter(IntPtr filter);
}

[ComImport]
[Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")]
internal class FileOpenDialog
{
}

public static class AnimaNativePathPicker
{
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
}

public static class AnimaExplorerPathDialog
{
    private const int ERROR_CANCELLED = unchecked((int)0x800704C7);

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = true)]
    private static extern int SHCreateItemFromParsingName(
        [MarshalAs(UnmanagedType.LPWStr)] string path,
        IntPtr bindContext,
        ref Guid iid,
        out IShellItem shellItem);

    public static string Show(string purpose, string initialDirectory, IntPtr owner)
    {
        IFileDialog dialog = (IFileDialog)new FileOpenDialog();
        try
        {
            FileOpenOptions options;
            dialog.GetOptions(out options);
            if (String.Equals(purpose, "replacement_csv", StringComparison.Ordinal))
            {
                dialog.SetTitle("Select replacement CSV");
                dialog.SetFileTypes(1, new[]
                {
                    new COMDLG_FILTERSPEC { pszName = "CSV files (*.csv)", pszSpec = "*.csv" },
                });
                dialog.SetFileTypeIndex(1);
                options |= FileOpenOptions.FOS_FORCEFILESYSTEM
                    | FileOpenOptions.FOS_PATHMUSTEXIST
                    | FileOpenOptions.FOS_FILEMUSTEXIST;
            }
            else
            {
                dialog.SetTitle("Select dataset folder");
                dialog.SetOkButtonLabel("Select Folder");
                options |= FileOpenOptions.FOS_PICKFOLDERS
                    | FileOpenOptions.FOS_FORCEFILESYSTEM
                    | FileOpenOptions.FOS_PATHMUSTEXIST;
            }

            dialog.SetOptions(options);
            SetInitialFolder(dialog, initialDirectory);

            int result = dialog.Show(owner);
            if (result == ERROR_CANCELLED)
            {
                return null;
            }
            if (result != 0)
            {
                Marshal.ThrowExceptionForHR(result);
            }

            IShellItem selection;
            dialog.GetResult(out selection);
            if (selection == null)
            {
                return null;
            }
            try
            {
                return GetFileSystemPath(selection);
            }
            finally
            {
                ReleaseComObject(selection);
            }
        }
        finally
        {
            ReleaseComObject(dialog);
        }
    }

    private static void SetInitialFolder(IFileDialog dialog, string initialDirectory)
    {
        if (String.IsNullOrWhiteSpace(initialDirectory) || !Directory.Exists(initialDirectory))
        {
            return;
        }

        IShellItem folder;
        Guid shellItemId = typeof(IShellItem).GUID;
        if (SHCreateItemFromParsingName(initialDirectory, IntPtr.Zero, ref shellItemId, out folder) != 0 || folder == null)
        {
            return;
        }
        try
        {
            dialog.SetFolder(folder);
        }
        finally
        {
            ReleaseComObject(folder);
        }
    }

    private static string GetFileSystemPath(IShellItem selection)
    {
        IntPtr path = IntPtr.Zero;
        selection.GetDisplayName(SIGDN.FILESYSPATH, out path);
        try
        {
            return path == IntPtr.Zero ? null : Marshal.PtrToStringUni(path);
        }
        finally
        {
            if (path != IntPtr.Zero)
            {
                Marshal.FreeCoTaskMem(path);
            }
        }
    }

    private static void ReleaseComObject(object value)
    {
        if (value != null && Marshal.IsComObject(value))
        {
            Marshal.ReleaseComObject(value);
        }
    }
}
'@
$owner = [AnimaNativePathPicker]::GetForegroundWindow()
$selected = [AnimaExplorerPathDialog]::Show([string]$request.purpose, [string]$request.initialDirectory, $owner)
if ($null -ne $selected) { [Console]::Out.Write($selected) }
""".replace("__ENCODED_REQUEST__", encoded_request)
    encoded_script = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    powershell = Path(os.environ.get("SystemRoot", r"C:\\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    completed = subprocess.run(
        [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand", encoded_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise RuntimeError("Windows native path dialog failed")
    selected = completed.stdout.strip()
    return selected or None


class NativePathPicker:
    def __init__(self, *, dialog_runner: DialogRunner | None = None, tk_loader: TkLoader | None = None) -> None:
        self._dialog_runner = dialog_runner or _select_with_windows_dialog
        self._tk_loader = tk_loader
        self._dialog_lock = Lock()

    def select(self, purpose: PathPickerPurpose, current_path: str | None) -> str | None:
        if purpose not in ("source_dataset", "output_dataset", "replacement_csv"):
            raise ValueError("invalid path picker purpose")
        if not self._dialog_lock.acquire(blocking=False):
            raise NativePathPickerBusyError("path picker is busy")

        root: object | None = None
        try:
            if self._tk_loader is None:
                return self._dialog_runner(purpose, current_path)
            tkinter, filedialog = self._tk_loader()
            root = tkinter.Tk()  # type: ignore[attr-defined]
            root.withdraw()  # type: ignore[attr-defined]
            initialdir = self._initialdir(purpose, current_path)
            if purpose == "replacement_csv":
                selected = filedialog.askopenfilename(  # type: ignore[attr-defined]
                    parent=root,
                    initialdir=initialdir,
                    title="Select replacement CSV",
                    filetypes=(("CSV files", "*.csv"),),
                )
            else:
                selected = filedialog.askdirectory(  # type: ignore[attr-defined]
                    parent=root,
                    initialdir=initialdir,
                    title="Select dataset folder",
                    mustexist=purpose == "source_dataset",
                )
            return str(selected) if selected else None
        except Exception as exc:
            raise NativePathPickerUnavailableError("native path picker unavailable") from exc
        finally:
            if root is not None:
                try:
                    root.destroy()  # type: ignore[attr-defined]
                except Exception:
                    pass
            self._dialog_lock.release()

    @staticmethod
    def _initialdir(purpose: PathPickerPurpose, current_path: str | None) -> str | None:
        if not current_path:
            return None
        candidate = os.path.normpath(current_path)
        if purpose == "replacement_csv" or not os.path.isdir(candidate):
            parent = os.path.dirname(candidate)
            return parent or None
        return candidate
