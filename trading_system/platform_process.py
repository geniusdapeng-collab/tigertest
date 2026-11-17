"""Platform-native process observation without parsing external command text."""
from __future__ import annotations

import ctypes
import errno
import sys

PROCESS_OBSERVATION_SCHEMA = "tiger.process-observation/v1"
PROC_PGRP_ONLY = 2
PROC_PIDTBSDINFO = 3
SZOMB = 5


class ProcessQueryError(RuntimeError):
    """A process query failed for a reason other than ESRCH."""


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


_LIBPROC = None


def _libproc():
    global _LIBPROC
    if sys.platform != "darwin":
        raise ProcessQueryError("Darwin libproc is unavailable on this platform")
    if _LIBPROC is None:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        library.proc_listpids.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int,
        ]
        library.proc_listpids.restype = ctypes.c_int
        library.proc_pidinfo.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
        ]
        library.proc_pidinfo.restype = ctypes.c_int
        _LIBPROC = library
    return _LIBPROC


def _raise_query(operation: str, code: int | None = None) -> None:
    code = ctypes.get_errno() if code is None else code
    raise ProcessQueryError(f"{operation} failed with errno {code}")


def group_pids(pgid: int) -> list[int]:
    """Return all visible PIDs in one process group using libproc."""
    if type(pgid) is not int or pgid <= 1:
        raise ProcessQueryError("Invalid process-group identity")
    library = _libproc()
    ctypes.set_errno(0)
    needed = library.proc_listpids(PROC_PGRP_ONLY, pgid, None, 0)
    if needed < 0:
        _raise_query("proc_listpids(size)")
    if needed == 0:
        if ctypes.get_errno() == errno.ESRCH:
            return []
        _raise_query("proc_listpids")
    capacity = needed + 64 * ctypes.sizeof(ctypes.c_int32)
    buffer = ctypes.create_string_buffer(capacity)
    ctypes.set_errno(0)
    used = library.proc_listpids(PROC_PGRP_ONLY, pgid, buffer, capacity)
    if used < 0:
        code = ctypes.get_errno()
        if code == errno.ESRCH:
            return []
        _raise_query("proc_listpids(read)", code)
    if used == 0:
        if ctypes.get_errno() == errno.ESRCH:
            return []
        _raise_query("proc_listpids(read)")
    count = used // ctypes.sizeof(ctypes.c_int32)
    values = (ctypes.c_int32 * count).from_buffer(buffer)
    return sorted({value for value in values if value > 1})


def process_identity(pid: int) -> dict | None:
    """Read one process status and parent/group identity.

    ``None`` means the kernel reported ESRCH. Every other failed query raises
    so callers cannot turn observation failure into a clean exit.
    """
    if type(pid) is not int or pid <= 1:
        raise ProcessQueryError("Invalid process identity")
    info = _ProcBsdInfo()
    ctypes.set_errno(0)
    read = _libproc().proc_pidinfo(
        pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info),
    )
    if read == 0:
        code = ctypes.get_errno()
        if code == errno.ESRCH:
            return None
        _raise_query("proc_pidinfo", code)
    if read < ctypes.sizeof(info):
        raise ProcessQueryError("proc_pidinfo returned a truncated process record")
    if info.pbi_pid != pid:
        raise ProcessQueryError("proc_pidinfo returned another PID")
    return {
        "schema": PROCESS_OBSERVATION_SCHEMA,
        "pid": int(info.pbi_pid),
        "ppid": int(info.pbi_ppid),
        "pgid": int(info.pbi_pgid),
        "status": int(info.pbi_status),
        "zombie": int(info.pbi_status) == SZOMB,
    }


def process_group_observation(pgid: int) -> dict:
    """Return a fail-closed projection for a retained process-group leader."""
    listed = group_pids(pgid)
    members = []
    for pid in listed:
        observation = process_identity(pid)
        if observation is None:
            continue
        if observation["pgid"] != pgid:
            raise ProcessQueryError("A listed PID reports another process group")
        members.append(observation)
    leader = next((row for row in members if row["pid"] == pgid), None)
    leader_listed = pgid in listed
    return {
        "schema": PROCESS_OBSERVATION_SCHEMA,
        "pgid": pgid,
        "leader_present": leader_listed,
        "leader": leader,
        "members": members,
        "all_zombies": leader_listed and all(row["zombie"] for row in members),
    }
