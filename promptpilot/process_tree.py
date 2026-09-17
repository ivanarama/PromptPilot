"""Owned subprocess trees with bounded lifetime.

PromptPilot providers routinely start through wrappers (``cmd.exe``, npm,
``ssh``) which then create the real agent.  Killing only the wrapper is not a
task cancellation: the agent can keep editing after the task is marked failed.

On Windows every owned process is created suspended, assigned to a private Job
Object, and only then resumed.  This closes the spawn/assignment race and lets
the kernel terminate exactly that job's descendants without touching unrelated
processes.  POSIX uses a private session/process group.
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Any, Sequence


class ProcessTreeError(OSError):
    """An owned process could not be placed inside its lifetime boundary."""


if os.name == "nt":  # pragma: no branch - definitions are platform-specific
    import ctypes
    from ctypes import wintypes

    _CREATE_SUSPENDED = 0x00000004
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _THREAD_SUSPEND_RESUME = 0x0002
    _TH32CS_SNAPTHREAD = 0x00000004
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _ERROR_NO_MORE_FILES = 18

    class _IOCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IOCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _CreateJobObjectW = _kernel32.CreateJobObjectW
    _CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _CreateJobObjectW.restype = wintypes.HANDLE

    _SetInformationJobObject = _kernel32.SetInformationJobObject
    _SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    _SetInformationJobObject.restype = wintypes.BOOL

    _AssignProcessToJobObject = _kernel32.AssignProcessToJobObject
    _AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _AssignProcessToJobObject.restype = wintypes.BOOL

    _TerminateJobObject = _kernel32.TerminateJobObject
    _TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _TerminateJobObject.restype = wintypes.BOOL

    _OpenProcess = _kernel32.OpenProcess
    _OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _OpenProcess.restype = wintypes.HANDLE

    _CreateToolhelp32Snapshot = _kernel32.CreateToolhelp32Snapshot
    _CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    _Thread32First = _kernel32.Thread32First
    _Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    _Thread32First.restype = wintypes.BOOL

    _Thread32Next = _kernel32.Thread32Next
    _Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    _Thread32Next.restype = wintypes.BOOL

    _OpenThread = _kernel32.OpenThread
    _OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _OpenThread.restype = wintypes.HANDLE

    _ResumeThread = _kernel32.ResumeThread
    _ResumeThread.argtypes = [wintypes.HANDLE]
    _ResumeThread.restype = wintypes.DWORD

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL


def _windows_error(prefix: str) -> ProcessTreeError:
    code = ctypes.get_last_error()
    detail = ctypes.WinError(code)
    return ProcessTreeError(code, f"{prefix}: {detail.strerror}")


def _new_windows_job():
    job = _CreateJobObjectW(None, None)
    if not job:
        raise _windows_error("CreateJobObjectW failed")
    limits = _ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits), ctypes.sizeof(limits)):
        error = _windows_error("SetInformationJobObject failed")
        _CloseHandle(job)
        raise error
    return job


def _assign_windows_process(job, pid: int) -> None:
    process = _OpenProcess(_PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, pid)
    if not process:
        raise _windows_error(f"OpenProcess({pid}) failed")
    try:
        if not _AssignProcessToJobObject(job, process):
            raise _windows_error(f"AssignProcessToJobObject({pid}) failed")
    finally:
        _CloseHandle(process)


def _resume_windows_process(pid: int) -> None:
    """Resume the one primary thread of a CREATE_SUSPENDED process."""
    snapshot = _CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        raise _windows_error("CreateToolhelp32Snapshot failed")
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        found = _Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32OwnerProcessID == pid:
                thread = _OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if not thread:
                    raise _windows_error(f"OpenThread({entry.th32ThreadID}) failed")
                try:
                    previous_count = _ResumeThread(thread)
                    if previous_count == 0xFFFFFFFF:
                        raise _windows_error(f"ResumeThread({entry.th32ThreadID}) failed")
                    if previous_count != 1:
                        raise ProcessTreeError(
                            f"unexpected suspend count {previous_count} for process {pid}")
                finally:
                    _CloseHandle(thread)
                return
            entry.dwSize = ctypes.sizeof(entry)
            found = _Thread32Next(snapshot, ctypes.byref(entry))
        code = ctypes.get_last_error()
        if code not in (0, _ERROR_NO_MORE_FILES):
            raise _windows_error("Thread32Next failed")
        raise ProcessTreeError(f"suspended process {pid} has no primary thread")
    finally:
        _CloseHandle(snapshot)


class OwnedProcess:
    """A subprocess whose descendants share a private lifetime boundary."""

    def __init__(self, process: subprocess.Popen, *, job=None, group_id=None):
        self.process = process
        self._job = job
        self._group_id = group_id

    @classmethod
    def start(cls, args: Sequence[str] | str, **kwargs: Any) -> "OwnedProcess":
        """Start *args* so no child can escape before the boundary exists."""
        if os.name != "nt":
            kwargs["start_new_session"] = True
            process = subprocess.Popen(args, **kwargs)
            return cls(process, group_id=process.pid)

        job = _new_windows_job()
        process = None
        try:
            flags = int(kwargs.pop("creationflags", 0)) | _CREATE_SUSPENDED
            process = subprocess.Popen(args, creationflags=flags, **kwargs)
            _assign_windows_process(job, process.pid)
            _resume_windows_process(process.pid)
            return cls(process, job=job)
        except BaseException:
            # The child is still suspended unless the final resume succeeded.
            # Closing a configured job kills an assigned process; proc.kill()
            # covers failures that happened before assignment.
            if process is not None:
                try:
                    _TerminateJobObject(job, 1)
                except Exception:
                    pass
                try:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            _CloseHandle(job)
            raise

    def terminate(self) -> None:
        """Terminate this owned tree and only this tree."""
        if os.name == "nt":
            if self._job and not _TerminateJobObject(self._job, 1):
                error = _windows_error("TerminateJobObject failed")
                try:
                    self.process.kill()
                except OSError:
                    pass
                raise error
            return
        group_id = self._group_id
        if group_id is None:
            return
        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self._group_id = None

    def close(self) -> None:
        """Release the boundary, killing descendants still inside it."""
        if os.name == "nt":
            job = self._job
            if job:
                # Do not rely only on KILL_ON_JOB_CLOSE.  A provider or one of
                # its native helpers can retain/duplicate a handle to the job,
                # in which case closing our handle is not the last close and
                # descendants would survive a normally completed wrapper.
                #
                # Terminate the private kernel job itself: unlike a PID/PPID
                # walk this cannot race PID reuse and cannot select an
                # unrelated process.  Detached tasks never enter this job.
                if not _TerminateJobObject(job, 1):
                    # Keep the live handle: a later cleanup retry must not see
                    # a consumed boundary and falsely report success.
                    raise _windows_error("TerminateJobObject during close failed")
                if not _CloseHandle(job):
                    raise _windows_error("CloseHandle(job) failed")
                self._job = None
            return
        self.terminate()

    def __enter__(self) -> "OwnedProcess":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def run_owned(args: Sequence[str] | str, *, timeout: float | None = None,
              capture_output: bool = False, **kwargs: Any) -> subprocess.CompletedProcess:
    """Small ``subprocess.run`` equivalent with owned-tree timeout semantics."""
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr may not be used with capture_output")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE

    tree = OwnedProcess.start(args, **kwargs)
    process = tree.process
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            tree.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            raise
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    finally:
        tree.close()
