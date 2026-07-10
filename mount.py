"""Mount a CourtListener docket as a read-only Windows drive.

    python mount.py https://www.courtlistener.com/docket/69536831/utherverse-inc-v-quinn/
    python mount.py 69536831 --mount V:

Requires WinFsp (installed) and the winfspy binding (in this venv). PDFs are
downloaded lazily on first access and cached under %LOCALAPPDATA%.
"""

from __future__ import annotations

import os
import sys
import string
import signal
import argparse
import threading
from pathlib import Path

import cl_client
import clfs


def _default_cache_dir(docket_id: int) -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "courtlistener-drive" / "cache" / str(docket_id)


def _pick_free_drive(preferred: str | None = None) -> str:
    used = {c for c in string.ascii_uppercase if Path(f"{c}:\\").exists()}
    if preferred:
        letter = preferred.rstrip(":\\").upper()
        if letter in used:
            raise SystemExit(f"Drive {letter}: is already in use. Pick another with --mount.")
        return f"{letter}:"
    for letter in reversed(string.ascii_uppercase[3:]):  # Z..E, skip A-C
        if letter not in used:
            return f"{letter}:"
    raise SystemExit("No free drive letter found. Free one or pass --mount.")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Mount a CourtListener docket as a read-only drive you can drag files from."
    )
    ap.add_argument("docket", help="CourtListener docket URL or bare docket id")
    ap.add_argument("-m", "--mount", default=None,
                    help="Drive letter to mount at (e.g. V:). Default: first free letter.")
    ap.add_argument("--token", default=None,
                    help="CourtListener API token (default: env or ~/.claude/courtlistener-token.txt).")
    ap.add_argument("--cache", default=None, help="Directory for cached PDFs.")
    ap.add_argument("-d", "--debug", action="store_true", help="Enable WinFsp debug logging.")
    args = ap.parse_args(argv)

    try:
        docket_id = cl_client.parse_docket_ref(args.docket)
    except ValueError as e:
        raise SystemExit(str(e))

    token = cl_client.load_token(args.token)
    print(f"[courtlistener-drive] docket {docket_id} "
          f"({'authenticated' if token else 'anonymous'})")

    index = cl_client.build_index(docket_id, token, log=lambda m: print("  " + m))
    if len([f for f in index.files if f.kind == "doc"]) == 0:
        print("[warn] No downloadable PDFs found on this docket "
              "(documents may be sealed or not yet uploaded to RECAP).")

    mountpoint = _pick_free_drive(args.mount)
    cache_dir = Path(args.cache) if args.cache else _default_cache_dir(docket_id)

    fs = clfs.create_file_system(
        index, mountpoint, cache_dir,
        debug=args.debug, log=lambda m: print("  [dl] " + m),
    )

    fs.start()
    print(f"\n[courtlistener-drive] MOUNTED  {mountpoint}\\   "
          f"({len(index.files)} files)")
    print(f"  Case : {index.case_name}")
    print(f"  Cache: {cache_dir}")
    print("  Open the drive in Explorer and drag documents out. Read-only.")
    print("  Press Ctrl+C here (or close this window) to unmount.\n")

    stop = threading.Event()

    def _handle(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    try:
        signal.signal(signal.SIGBREAK, _handle)  # console close / Ctrl+Break (Windows)
    except (AttributeError, ValueError):
        pass

    try:
        stop.wait()
    finally:
        print("\n[courtlistener-drive] unmounting ...")
        fs.stop()
        print("[courtlistener-drive] unmounted.")


if __name__ == "__main__":
    main()
