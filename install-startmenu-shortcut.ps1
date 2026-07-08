# Create (or refresh) a Start Menu .lnk for Claude Overlay that carries an explicit
# System.AppUserModel.ID matching the id the app declares at runtime via
# SetCurrentProcessExplicitAppUserModelID (config.APP_ID).
#
# WHY: Windows backs a taskbar PIN with a Start Menu shortcut whose AppUserModelID
# matches the running window's. Our window is a frameless pythonw process with an
# explicit AUMID but there was NO matching Start Menu shortcut -> pinning degraded to
# the raw pythonw.exe, so the pin (a) would not relaunch the overlay once it was
# closed and (b) showed pythonw's generic Python icon instead of Clawd. Creating this
# shortcut (same AUMID + the Clawd icon + a real launch target) is what makes pinning
# behave like a normal app.
#
# WScript.Shell (the usual way to make a .lnk) CANNOT set System.AppUserModel.ID, so
# this builds the shortcut through IShellLink + IPropertyStore. ASCII-only on purpose
# (no PS 5.1 BOM/codepage trap); every path/string comes in as a parameter.
param(
  [Parameter(Mandatory=$true)][string]$Lnk,
  [Parameter(Mandatory=$true)][string]$Target,
  [string]$Arguments = "",
  [string]$WorkingDir = "",
  [string]$Icon = "",
  [Parameter(Mandatory=$true)][string]$AppId,
  [string]$Description = "Claude Overlay - screen-aware floating Claude Code chat",
  [int]$ShowCmd = 1
)

$ErrorActionPreference = "Stop"

$src = @'
using System;
using System.Text;
using System.Runtime.InteropServices;

namespace ClaudeOverlayShell {
  [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
  public class CShellLink {}

  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
   Guid("000214F9-0000-0000-C000-000000000046")]
  public interface IShellLinkW {
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder f, int cch, IntPtr fd, int flags);
    void GetIDList(out IntPtr ppidl);
    void SetIDList(IntPtr pidl);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder n, int cch);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string n);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder d, int cch);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string d);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder a, int cch);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string a);
    void GetHotkey(out short w);
    void SetHotkey(short w);
    void GetShowCmd(out int c);
    void SetShowCmd(int c);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder p, int cch, out int i);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string p, int i);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string rel, int reserved);
    void Resolve(IntPtr hwnd, int flags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string f);
  }

  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
   Guid("0000010b-0000-0000-C000-000000000046")]
  public interface IPersistFile {
    void GetClassID(out Guid id);
    [PreserveSig] int IsDirty();
    void Load([MarshalAs(UnmanagedType.LPWStr)] string f, int mode);
    void Save([MarshalAs(UnmanagedType.LPWStr)] string f, [MarshalAs(UnmanagedType.Bool)] bool remember);
    void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string f);
    void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string f);
  }

  [StructLayout(LayoutKind.Sequential, Pack = 4)]
  public struct PROPERTYKEY { public Guid fmtid; public uint pid; }

  // Minimal PROPVARIANT. Two fields overlap at offset 8 (a union) purely to force the
  // struct to the real 16-byte size on both x86 and x64, so InitPropVariantFromString
  // never writes past it. We only ever store/read a VT_LPWSTR here.
  [StructLayout(LayoutKind.Explicit)]
  public struct PROPVARIANT {
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(8)] public IntPtr p;
    [FieldOffset(8)] public long forceSize;
  }

  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
   Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")]
  public interface IPropertyStore {
    void GetCount(out uint n);
    void GetAt(uint i, out PROPERTYKEY key);
    void GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
    void SetValue(ref PROPERTYKEY key, ref PROPVARIANT pv);
    void Commit();
  }

  public static class Lnk {
    // ole32!PropVariantClear is always present; it frees the CoTaskMem string a
    // VT_LPWSTR PROPVARIANT owns. We build/read the PROPVARIANT by hand (VT_LPWSTR)
    // rather than lean on the propvarutil helpers, some of which aren't exported from
    // propsys.dll on every Windows build (InitPropVariantFromString is inline there).
    [DllImport("ole32.dll")]
    static extern int PropVariantClear(ref PROPVARIANT pv);

    const ushort VT_LPWSTR = 31;

    // PKEY_AppUserModel_ID = {9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}, 5
    static PROPERTYKEY AppUserModelIdKey() {
      return new PROPERTYKEY {
        fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };
    }

    public static void Create(string lnk, string target, string args, string workdir,
                              string icon, int iconIndex, string desc, string appId, int showCmd) {
      var link = (IShellLinkW)new CShellLink();
      link.SetPath(target);
      if (!string.IsNullOrEmpty(args))    link.SetArguments(args);
      if (!string.IsNullOrEmpty(workdir)) link.SetWorkingDirectory(workdir);
      if (!string.IsNullOrEmpty(icon))    link.SetIconLocation(icon, iconIndex);
      if (!string.IsNullOrEmpty(desc))    link.SetDescription(desc);
      link.SetShowCmd(showCmd);
      if (!string.IsNullOrEmpty(appId)) {
        var store = (IPropertyStore)link;
        var key = AppUserModelIdKey();
        PROPVARIANT pv = new PROPVARIANT();
        pv.vt = VT_LPWSTR;
        pv.p = Marshal.StringToCoTaskMemUni(appId);   // freed by PropVariantClear below
        try { store.SetValue(ref key, ref pv); store.Commit(); }
        finally { PropVariantClear(ref pv); }         // property store took its own copy
      }
      ((IPersistFile)link).Save(lnk, true);
    }
  }
}
'@

Add-Type -TypeDefinition $src -Language CSharp | Out-Null

# Make sure the Start Menu\Programs folder exists.
$dir = Split-Path -Parent $Lnk
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

# Quote the script-path argument so a path with spaces (e.g. a OneDrive folder) stays a
# single argument to pythonw.
$argStr = ""
if ($Arguments -ne "") { $argStr = '"' + $Arguments + '"' }

# Only reference an icon that actually exists (else the pin would show a blank icon).
$iconArg = ""
if ($Icon -ne "" -and (Test-Path $Icon)) { $iconArg = $Icon }

[ClaudeOverlayShell.Lnk]::Create($Lnk, $Target, $argStr, $WorkingDir, $iconArg, 0, $Description, $AppId, $ShowCmd)

# Prove the AUMID actually persisted (a real round-trip read from the saved .lnk via the
# shell property system, independent of the writer above). This matching id is the whole
# point of the shortcut, so fail loudly if it did not stick.
$shell  = New-Object -ComObject Shell.Application
$folder = $shell.Namespace((Split-Path -Parent $Lnk))
$item   = if ($folder) { $folder.ParseName((Split-Path -Leaf $Lnk)) } else { $null }
$got    = if ($item) { $item.ExtendedProperty("System.AppUserModel.ID") } else { $null }
if ($got -ne $AppId) {
  Write-Error "AUMID mismatch: wrote '$AppId' but read back '$got'"
  exit 2
}
Write-Output "OK:$got"
exit 0
