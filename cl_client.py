"""CourtListener docket -> virtual-file index.

Talks to the CourtListener v4 REST API, enumerates every docket entry and its
RECAP documents, and turns them into a flat list of `VFile` records that the
virtual filesystem serves as read-only files.

PDF bytes are fetched lazily (on first read) from the public
storage.courtlistener.com bucket, with an Internet Archive fallback.
"""

from __future__ import annotations

import os
import re
import json
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

CL_API = "https://www.courtlistener.com/api/rest/v4"
STORAGE_BASE = "https://storage.courtlistener.com/"
USER_AGENT = "courtlistener-drive/1.0 (personal read-only docket mount)"

# Windows FILETIME epoch offset (100ns ticks between 1601-01-01 and 1970-01-01).
_FILETIME_EPOCH = 116444736000000000


# --------------------------------------------------------------------------- #
# Token / URL helpers
# --------------------------------------------------------------------------- #
def load_token(explicit: str | None = None) -> str | None:
    """Resolve a CourtListener API token: arg -> env -> ~/.claude token file."""
    if explicit:
        return explicit.strip()
    env = os.environ.get("COURTLISTENER_API_TOKEN") or os.environ.get("CL_API_TOKEN")
    if env:
        return env.strip()
    for candidate in (
        Path.home() / ".claude" / "courtlistener-token.txt",
        Path.home() / ".courtlistener-token",
        Path.home() / ".courtlistener",
    ):
        try:
            if candidate.is_file():
                txt = candidate.read_text(encoding="utf-8").strip()
                # File may be "Token abc..." or "COURTLISTENER_API_TOKEN=abc" or bare.
                m = re.search(r"([0-9a-fA-F]{40})", txt)
                if m:
                    return m.group(1)
                return txt.split("=")[-1].split()[-1].strip() or None
        except OSError:
            continue
    return None


def parse_docket_ref(ref: str) -> int:
    """Accept a full CourtListener docket URL or a bare docket id -> int id."""
    ref = ref.strip()
    if ref.isdigit():
        return int(ref)
    m = re.search(r"/docket/(\d+)", ref)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{5,})", ref)
    if m:
        return int(m.group(1))
    raise ValueError(f"Could not find a docket id in: {ref!r}")


def _iso_to_filetime(iso: str | None) -> int | None:
    if not iso:
        return None
    s = iso.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    unix_100ns = int(dt.timestamp() * 10_000_000)
    return _FILETIME_EPOCH + unix_100ns


_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str, maxlen: int = 150) -> str:
    name = _ILLEGAL.sub(" ", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > maxlen:
        name = name[:maxlen].rstrip(" .")
    return name or "document"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class VFile:
    """One virtual file exposed at the root of the drive."""

    name: str
    size: int
    kind: str  # "doc" (lazy PDF) or "mem" (in-memory bytes)
    urls: tuple[str, ...] = ()
    cache_key: str = ""          # sha1 (docs) used as the disk-cache filename
    data: bytes = b""            # populated for kind == "mem"
    filetime: int | None = None  # creation/write time shown in Explorer
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass
class DocketIndex:
    docket_id: int
    case_name: str
    docket_number: str
    court: str
    files: list[VFile]

    @property
    def label(self) -> str:
        base = self.docket_number or f"docket-{self.docket_id}"
        return sanitize(f"{base}", 31)[:31]


# --------------------------------------------------------------------------- #
# API session
# --------------------------------------------------------------------------- #
def make_session(token: str | None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    if token:
        s.headers["Authorization"] = f"Token {token}"
    return s


def _get_json(session: requests.Session, url: str, params=None, retries: int = 4) -> dict:
    last = None
    for attempt in range(retries):
        r = session.get(url, params=params, timeout=60)
        if r.status_code == 429:  # rate limited -> back off
            wait = int(r.headers.get("Retry-After", 2 ** attempt))
            time.sleep(min(wait, 30))
            last = r
            continue
        r.raise_for_status()
        return r.json()
    if last is not None:
        last.raise_for_status()
    raise RuntimeError(f"GET failed: {url}")


def fetch_docket_meta(session: requests.Session, docket_id: int) -> dict:
    data = _get_json(session, f"{CL_API}/dockets/{docket_id}/")
    return data


def _resolve_court(session: requests.Session, court_url: str) -> str:
    if not court_url:
        return ""
    try:
        data = _get_json(session, court_url)
        return data.get("full_name") or data.get("short_name") or ""
    except Exception:
        # court_url ends in /courts/<id>/ ; fall back to the id.
        m = re.search(r"/courts/([^/]+)/", court_url)
        return m.group(1) if m else ""


def iter_docket_entries(session: requests.Session, docket_id: int):
    """Yield every docket entry (with nested recap_documents), following cursor pages."""
    url = f"{CL_API}/docket-entries/"
    params = {"docket": docket_id, "order_by": "recap_sequence_number", "page_size": 100}
    while url:
        data = _get_json(session, url, params=params)
        params = None  # `next` already carries the query string
        for entry in data.get("results", []):
            yield entry
        url = data.get("next")


def _first_line(text: str | None) -> str:
    if not text:
        return ""
    return text.strip().splitlines()[0] if text.strip() else ""


# --------------------------------------------------------------------------- #
# Index builder
# --------------------------------------------------------------------------- #
def build_index(docket_id: int, token: str | None, log=print) -> DocketIndex:
    session = make_session(token)

    log(f"Fetching docket {docket_id} metadata ...")
    meta = fetch_docket_meta(session, docket_id)
    case_name = meta.get("case_name") or meta.get("case_name_full") or f"Docket {docket_id}"
    docket_number = meta.get("docket_number") or ""
    court = _resolve_court(session, meta.get("court") or "")
    log(f"  {case_name}  ({docket_number}, {court})")

    files: list[VFile] = []
    used_names: set[str] = set()
    index_lines: list[str] = [
        f"{case_name}",
        f"Docket: {docket_number}    Court: {court}",
        f"CourtListener: https://www.courtlistener.com/docket/{docket_id}/",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "#      Date        Doc  Pages  Avail  Description",
        "-" * 78,
    ]
    manifest: list[dict] = []

    def unique(name: str) -> str:
        base, ext = os.path.splitext(name)
        candidate = name
        n = 2
        while candidate.lower() in used_names:
            candidate = f"{base} ({n}){ext}"
            n += 1
        used_names.add(candidate.lower())
        return candidate

    log("Enumerating docket entries ...")
    n_entries = n_docs = n_avail = 0
    for entry in iter_docket_entries(session, docket_id):
        n_entries += 1
        entry_no = entry.get("entry_number")
        entry_date = (entry.get("date_filed") or "")[:10]
        entry_desc = _first_line(entry.get("description"))
        docs = entry.get("recap_documents") or []

        index_lines.append(
            f"{str(entry_no or ''):<6} {entry_date:<11} "
            f"{'':<4} {'':<6} {'':<6} {entry_desc[:60]}"
        )

        for d in docs:
            n_docs += 1
            doc_num = d.get("document_number") or entry_no or ""
            attn = d.get("attachment_number")
            desc = d.get("description") or entry_desc or "Document"
            avail = bool(d.get("is_available"))
            pages = d.get("page_count")
            size = d.get("file_size") or 0
            filepath_local = d.get("filepath_local")
            filepath_ia = d.get("filepath_ia")
            sha1 = d.get("sha1") or ""
            date = (d.get("date_upload") or entry.get("date_filed") or "")[:10]

            tag = f"{doc_num}" + (f"-{attn}" if attn else "")
            index_lines.append(
                f"{'':<6} {date:<11} {tag:<4} {str(pages or ''):<6} "
                f"{'yes' if avail else 'no':<6} {sanitize(desc)[:60]}"
            )
            manifest.append(
                {
                    "entry_number": entry_no,
                    "document_number": doc_num,
                    "attachment_number": attn,
                    "description": desc,
                    "date": date,
                    "page_count": pages,
                    "file_size": size,
                    "is_available": avail,
                    "sha1": sha1,
                    "absolute_url": d.get("absolute_url"),
                }
            )

            if not avail:
                continue
            urls: list[str] = []
            if filepath_local:
                urls.append(STORAGE_BASE + str(filepath_local).lstrip("/"))
            if filepath_ia:
                urls.append(str(filepath_ia))
            if not urls:
                continue

            n_avail += 1
            pad = f"{int(doc_num):04d}" if str(doc_num).isdigit() else str(doc_num)
            att = f"-{attn}" if attn else ""
            fname = unique(sanitize(f"{pad}{att}  {date}  {desc}") + ".pdf")
            files.append(
                VFile(
                    name=fname,
                    size=int(size) if size else 0,
                    kind="doc",
                    urls=tuple(urls),
                    cache_key=sha1 or fname,
                    filetime=_iso_to_filetime(date),
                )
            )

    # Human-readable index + machine-readable manifest as in-drive files.
    index_txt = ("\r\n".join(index_lines) + "\r\n").encode("utf-8")
    files.insert(
        0,
        VFile(name="00_INDEX.txt", size=len(index_txt), kind="mem", data=index_txt),
    )
    manifest_json = json.dumps(
        {"docket_id": docket_id, "case_name": case_name,
         "docket_number": docket_number, "court": court, "documents": manifest},
        indent=2,
    ).encode("utf-8")
    files.insert(
        1,
        VFile(name="00_docket.json", size=len(manifest_json), kind="mem", data=manifest_json),
    )

    log(f"  {n_entries} entries, {n_docs} documents, {n_avail} downloadable PDFs.")
    return DocketIndex(
        docket_id=docket_id,
        case_name=case_name,
        docket_number=docket_number,
        court=court,
        files=files,
    )


# --------------------------------------------------------------------------- #
# Lazy download (disk-cached)
# --------------------------------------------------------------------------- #
_dl_session: requests.Session | None = None


def _download_session() -> requests.Session:
    global _dl_session
    if _dl_session is None:
        _dl_session = requests.Session()
        _dl_session.headers.update({"User-Agent": USER_AGENT})
    return _dl_session


def fetch_bytes(vfile: VFile, cache_dir: Path, log=None) -> bytes:
    """Return the full bytes for a doc VFile, downloading + caching on first use."""
    if vfile.kind == "mem":
        return vfile.data

    safe = sanitize(vfile.cache_key, 200) or "doc"
    cache_path = cache_dir / f"{safe}.pdf"
    with vfile._lock:
        if cache_path.is_file() and cache_path.stat().st_size > 0:
            return cache_path.read_bytes()

        session = _download_session()
        last_err = None
        for url in vfile.urls:
            try:
                r = session.get(url, timeout=120)
                if r.status_code == 200 and r.content:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    tmp = cache_path.with_suffix(".part")
                    tmp.write_bytes(r.content)
                    tmp.replace(cache_path)
                    if log:
                        log(f"downloaded {vfile.name} ({len(r.content):,} bytes)")
                    return r.content
                last_err = f"HTTP {r.status_code}"
            except requests.RequestException as e:
                last_err = str(e)
        raise IOError(f"Could not fetch {vfile.name}: {last_err}")
