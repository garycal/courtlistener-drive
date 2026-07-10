"""Read-only winfspy filesystem backed by a CourtListener DocketIndex.

The tree is flat: a single root directory containing 00_INDEX.txt,
00_docket.json, and one .pdf per downloadable RECAP document. Everything is
immutable after construction, so operations need no global lock; PDF bytes are
pulled lazily on first read and cached to disk by cl_client.
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

from winfspy import (
    FileSystem,
    BaseFileSystemOperations,
    enable_debug_log,
    FILE_ATTRIBUTE,
    NTStatusObjectNameNotFound,
    NTStatusNotADirectory,
    NTStatusEndOfFile,
    NTStatusMediaWriteProtected,
)
from winfspy.plumbing.win32_filetime import filetime_now
from winfspy.plumbing.security_descriptor import SecurityDescriptor

import cl_client

# Read/execute for everyone, but no write/delete/append -> read-only ACL.
_RO_SDDL = "O:BAG:BAD:P(A;;FRFX;;;WD)(A;;FRFX;;;SY)(A;;FRFX;;;BA)"


class Node:
    def __init__(self, path, attributes, security_descriptor, *, vfile=None,
                 filetime=None, file_size=0):
        self.path = path
        self.attributes = attributes
        self.security_descriptor = security_descriptor
        now = filetime or filetime_now()
        self.creation_time = now
        self.last_access_time = now
        self.last_write_time = now
        self.change_time = now
        self.index_number = 0
        self.file_size = file_size
        self.vfile = vfile
        self._data = None  # transient in-memory cache while open

    @property
    def is_dir(self):
        return bool(self.attributes & FILE_ATTRIBUTE.FILE_ATTRIBUTE_DIRECTORY)

    @property
    def allocation_size(self):
        unit = 4096
        return ((self.file_size + unit - 1) // unit) * unit

    def get_file_info(self):
        return {
            "file_attributes": self.attributes,
            "allocation_size": self.allocation_size,
            "file_size": self.file_size,
            "creation_time": self.creation_time,
            "last_access_time": self.last_access_time,
            "last_write_time": self.last_write_time,
            "change_time": self.change_time,
            "index_number": self.index_number,
        }

    def __repr__(self):
        return f"Node:{self.path}"


class OpenedObj:
    def __init__(self, node):
        self.node = node

    def __repr__(self):
        return f"OpenedObj:{self.node.path}"


class DocketFileSystemOperations(BaseFileSystemOperations):
    def __init__(self, index: "cl_client.DocketIndex", cache_dir: Path, log=lambda *_: None):
        super().__init__()
        self.index = index
        self.cache_dir = Path(cache_dir)
        self.log = log

        sd = SecurityDescriptor.from_string(_RO_SDDL)
        self._root_path = PureWindowsPath("/")
        root = Node(self._root_path, FILE_ATTRIBUTE.FILE_ATTRIBUTE_DIRECTORY, sd)
        self._entries: dict[PureWindowsPath, Node] = {self._root_path: root}

        total = 0
        for vf in index.files:
            path = self._root_path / vf.name
            self._entries[path] = Node(
                path,
                FILE_ATTRIBUTE.FILE_ATTRIBUTE_ARCHIVE
                | FILE_ATTRIBUTE.FILE_ATTRIBUTE_READONLY,
                sd,
                vfile=vf,
                filetime=vf.filetime,
                file_size=vf.size,
            )
            total += vf.size

        self._volume_info = {
            "total_size": max(total, 4096),
            "free_size": 0,  # read-only, nothing free
            "volume_label": index.label,
        }

    # ----- node data loading ------------------------------------------------ #
    def _load(self, node: Node) -> bytes:
        if node._data is not None:
            return node._data
        data = cl_client.fetch_bytes(node.vfile, self.cache_dir, log=self.log)
        node._data = data
        if len(data) != node.file_size:
            node.file_size = len(data)  # trust actual bytes over API metadata
        return data

    # ----- read operations -------------------------------------------------- #
    def get_volume_info(self):
        return self._volume_info

    def get_security_by_name(self, file_name):
        node = self._entries.get(PureWindowsPath(file_name))
        if node is None:
            raise NTStatusObjectNameNotFound()
        return (node.attributes, node.security_descriptor.handle,
                node.security_descriptor.size)

    def get_security(self, file_context):
        return file_context.node.security_descriptor

    def open(self, file_name, create_options, granted_access):
        node = self._entries.get(PureWindowsPath(file_name))
        if node is None:
            raise NTStatusObjectNameNotFound()
        return OpenedObj(node)

    def close(self, file_context):
        file_context.node._data = None  # free RAM; disk cache persists

    def get_file_info(self, file_context):
        return file_context.node.get_file_info()

    def read_directory(self, file_context, marker):
        node = file_context.node
        if not node.is_dir:
            raise NTStatusNotADirectory()

        entries = []
        if node.path != self._root_path:
            parent = self._entries[node.path.parent]
            entries.append({"file_name": ".", **node.get_file_info()})
            entries.append({"file_name": "..", **parent.get_file_info()})

        for path, obj in self._entries.items():
            try:
                rel = path.relative_to(node.path)
            except ValueError:
                continue
            if len(rel.parts) != 1:
                continue
            entries.append({"file_name": path.name, **obj.get_file_info()})

        entries.sort(key=lambda x: x["file_name"])
        if marker is None:
            return entries
        for i, e in enumerate(entries):
            if e["file_name"] == marker:
                return entries[i + 1:]
        return entries

    def get_dir_info_by_name(self, file_context, file_name):
        path = file_context.node.path / file_name
        node = self._entries.get(path)
        if node is None:
            raise NTStatusObjectNameNotFound()
        return {"file_name": file_name, **node.get_file_info()}

    def read(self, file_context, offset, length):
        node = file_context.node
        if node.is_dir:
            raise NTStatusNotADirectory()
        data = self._load(node)
        if offset >= len(data):
            raise NTStatusEndOfFile()
        return data[offset: offset + length]

    def get_stream_info(self, file_context, buffer, length, p_bytes_transferred):
        # No alternate data streams.
        p_bytes_transferred[0] = 0
        return

    # ----- everything mutating is refused ----------------------------------- #
    def _ro(self, *args, **kwargs):
        raise NTStatusMediaWriteProtected()

    create = _ro
    overwrite = _ro
    write = _ro
    flush = _ro
    cleanup = _ro
    rename = _ro
    set_basic_info = _ro
    set_file_size = _ro
    set_security = _ro
    can_delete = _ro
    set_delete = _ro
    set_volume_label = _ro


def create_file_system(index, mountpoint, cache_dir, *, debug=False, log=lambda *_: None):
    if debug:
        enable_debug_log()
    ops = DocketFileSystemOperations(index, cache_dir, log=log)
    mp = Path(mountpoint)
    is_drive = mp.parent == mp
    fs = FileSystem(
        str(mountpoint),
        ops,
        sector_size=512,
        sectors_per_allocation_unit=8,
        volume_creation_time=filetime_now(),
        volume_serial_number=0,
        file_info_timeout=5000,
        case_sensitive_search=0,
        case_preserved_names=1,
        unicode_on_disk=1,
        persistent_acls=1,
        post_cleanup_when_modified_only=1,
        um_file_context_is_user_context2=1,
        file_system_name="CourtListener",
        read_only_volume=True,
        reject_irp_prior_to_transact0=not is_drive,
        debug=debug,
    )
    return fs
