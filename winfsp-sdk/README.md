# winfsp-sdk (generated locally — not checked in)

`winfspy` compiles a CFFI extension against WinFsp's **developer** SDK, which the
stock WinFsp *runtime* installer does not include. Rather than redistribute
WinFsp's headers (GPLv3) and a DLL-derived import library, this folder is
populated locally. Nothing here is committed to git.

Populate it once (matching your **installed** WinFsp version — check with
`(Get-Item 'C:\Program Files (x86)\WinFsp\bin\winfsp-x64.dll').VersionInfo`):

## 1. Headers  ->  `winfsp-sdk/inc/winfsp/`

Download the four headers from the matching tag, e.g. for v2.1:

```
inc/winfsp/{winfsp.h, fsctl.h, launch.h, winfsp.hpp}
```

from `https://github.com/winfsp/winfsp/tree/v2.1/inc/winfsp`.

winfspy's build hardcodes the include path to the **registered install dir**, so
also copy `winfsp.h` + `fsctl.h` into `C:\Program Files (x86)\WinFsp\inc\winfsp\`
(one elevation — this is exactly what WinFsp's "Developer" installer feature does).

## 2. Import library  ->  `winfsp-sdk/lib/winfsp-x64.lib`

Generate it from the installed DLL (run in a VS Developer prompt):

```bat
dumpbin /exports "C:\Program Files (x86)\WinFsp\bin\winfsp-x64.dll" > exports.txt
rem  -> build winfsp-x64.def:  "LIBRARY winfsp-x64" + "EXPORTS" + one name per line
lib /def:winfsp-x64.def /machine:x64 /out:lib\winfsp-x64.lib
```

## 3. Build winfspy

```bat
set LIB=%CD%\winfsp-sdk\lib;%LIB%
.venv\Scripts\python -m pip install -r requirements.txt
```
