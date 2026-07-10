# courtlistener-drive

Mount any **CourtListener docket** as a **read-only Windows drive** you can browse
in Explorer and drag documents out of. Each RECAP document shows up as a nicely
named PDF; the bytes are downloaded lazily on first access and cached on disk.

```
V:\
├── 00_INDEX.txt      (human-readable docket sheet)
├── 00_docket.json    (machine-readable manifest of every document)
├── 0001 2025-01-10 Complaint.pdf
├── 0001-1 2025-01-10 Civil Cover Sheet.pdf
├── 0001-2 2025-01-10 Exhibit A to Complaint.pdf
├── 0002 2025-01-10 Proposed Summons to be issued.pdf
└── ... one .pdf per available document / attachment
```

## Usage

```cmd
run.cmd https://www.courtlistener.com/docket/69536831/utherverse-inc-v-quinn/
run.cmd 69536831 --mount V:
```

Options:

| flag | meaning |
|------|---------|
| `--mount V:` | drive letter to use (default: first free letter, scanning Z→E) |
| `--token XXXX` | CourtListener API token (default: `~/.claude/courtlistener-token.txt` or `$COURTLISTENER_API_TOKEN`) |
| `--cache DIR` | where to cache downloaded PDFs (default: `%LOCALAPPDATA%\courtlistener-drive\cache\<docket-id>`) |
| `-d` | WinFsp debug logging |

The window stays open while mounted. Press **Ctrl+C** (or close the window) to
unmount. You can also just drag files out first — copies are real, independent
files on your disk.

Accepts a full docket URL or a bare docket id. To find the id, it's the number
in `courtlistener.com/docket/<ID>/...`.

## How it works

1. **Index** (`cl_client.py`) — hits the CourtListener v4 REST API
   (`/dockets`, `/docket-entries`), pages through every entry and its
   `recap_documents`, and builds one virtual file per *available* document.
   Filenames are `NNNN[-att]  date  description.pdf`. Two synthetic files —
   `00_INDEX.txt` and `00_docket.json` — describe the whole docket (including
   documents that aren't downloadable, e.g. sealed or not-yet-in-RECAP).
2. **Filesystem** (`clfs.py`) — a read-only [WinFsp](https://winfsp.dev/)
   filesystem via `winfspy`. The tree is immutable; every mutating operation
   returns `STATUS_MEDIA_WRITE_PROTECTED`, and the volume is flagged read-only.
3. **Lazy fetch** — on first read of a PDF, bytes are pulled from the public
   `storage.courtlistener.com` bucket (Internet Archive fallback) and cached to
   disk keyed by the document's SHA-1. Re-reads are served from cache; delete the
   cache folder to reclaim space.

Re-run the command to **refresh** — the index is rebuilt from the API each mount,
so new filings appear; already-cached PDFs are reused.

## Notes / limitations

- **Antivirus pre-download.** When Windows first mounts the drive, Defender's
  real-time scanner reads every file, which downloads the whole docket up front
  (a one-time cost; afterwards the drive works fully offline from cache). This is
  a side effect of exposing a real drive, not a bug.
- **Sealed / non-RECAP documents** have no downloadable PDF. They're listed in
  `00_INDEX.txt` (Avail = no) but not exposed as files.
- **A token is optional** but recommended — it raises API rate limits. Public
  RECAP PDFs download without one.
- Read-only by design. There is intentionally no way to write, rename, or delete.

## Prerequisites

- [WinFsp](https://winfsp.dev/) runtime (already installed at
  `C:\Program Files (x86)\WinFsp`).
- The bundled `.venv` (Python 3.14) with `winfspy` + `requests`.

### Rebuilding the venv

`winfspy` compiles a CFFI extension against WinFsp's **developer** SDK
(`winfsp/winfsp.h` + `winfsp-x64.lib`), which the stock runtime installer does
*not* include. Those files are **not** checked in (the headers are GPLv3 and the
import lib is derived from the WinFsp DLL) — generate them locally into
`winfsp-sdk/` following [`winfsp-sdk/README.md`](winfsp-sdk/README.md).

Once `winfsp-sdk/` is populated, rebuild the venv:

```cmd
py -3.14 -m venv .venv

rem 1) headers must sit where winfspy looks (the registered WinFsp install dir);
rem    this needs one elevation (equivalent to WinFsp's "Developer" feature):
powershell -Command "Start-Process -Verb RunAs -Wait powershell -Args '-Command','Copy-Item ""%CD%\winfsp-sdk\inc\winfsp\*"" ""C:\Program Files (x86)\WinFsp\inc\winfsp\"" -Force'"

rem 2) point the linker at the generated import lib, then install:
set LIB=%CD%\winfsp-sdk\lib;%LIB%
.venv\Scripts\python -m pip install -r requirements.txt
```

If your WinFsp version differs from v2.1, regenerate the headers/lib for your
version (headers from `github.com/winfsp/winfsp/tree/<tag>/inc/winfsp`; lib via
`dumpbin /exports "…\winfsp-x64.dll"` → build a `.def` → `lib /def /machine:x64`).

## License

MIT — see [LICENSE](LICENSE). This covers the code in this repo only.
[WinFsp](https://winfsp.dev/) and its SDK headers (fetched separately, not
included here) are licensed by the WinFsp project under GPLv3 / commercial.
