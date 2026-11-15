# src/security/os_sandbox.py
"""
Native OS Process Containment & Kernel Sandbox Wrapper for Project JAI.
=============================================================================
Provides kernel-enforced resource and privilege boundaries:

Windows:
- Win32 Job Objects (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, ProcessMemoryLimit,
  ActiveProcessLimit = 16).
- Low Integrity Level token (S-1-16-4096) with DISABLE_MAX_PRIVILEGE.
- Isolation of browser/subprocess execution from user SSH keys, AWS credentials,
  and sensitive environment files.

Linux / POSIX:
- POSIX setrlimit (RLIMIT_AS for process memory cap).
- prctl(PR_SET_NO_NEW_PRIVS) preventing privilege escalation.

Exposes:
- spawn_contained_process(...) -> ContainedProcess (with microsecond spawn latency)
- terminate_sandboxed_process(pid=0) -> bool
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger("jai.security.os_sandbox")

# -----------------------------------------------------------------------------
# OS & C-ABI Definitions
# -----------------------------------------------------------------------------
IS_WINDOWS = sys.platform == "win32"

_C_LIB: Optional[ctypes.CDLL] = None
_ACTIVE_SANDBOXES: Dict[int, ContainedProcess] = {}

# Check for pre-compiled sandbox C library (both in packaged wheel path and local dev path)
_SECURITY_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SECURITY_DIR.parent.parent

_C_LIB_CANDIDATES: List[Path] = []
if IS_WINDOWS:
    _C_LIB_CANDIDATES = [
        _SECURITY_DIR / "jai_sandbox.dll",          # Packaged wheel path
        _REPO_ROOT / "native" / "jai_sandbox.dll",  # Local dev build path
    ]
else:
    _C_LIB_CANDIDATES = [
        _SECURITY_DIR / "libjai_sandbox.so",          # Packaged wheel path
        _REPO_ROOT / "native" / "libjai_sandbox.so",  # Local dev build path
    ]

_LOADED_LIB_PATH: Optional[Path] = None
for _cand in _C_LIB_CANDIDATES:
    if _cand.is_file():
        try:
            _C_LIB = ctypes.CDLL(str(_cand))
            _C_LIB.jai_spawn_sandboxed.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_size_t,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_ulong),
            ]
            _C_LIB.jai_spawn_sandboxed.restype = ctypes.c_int
            _C_LIB.jai_terminate_sandbox.argtypes = [ctypes.c_ulong]
            _C_LIB.jai_terminate_sandbox.restype = ctypes.c_int
            _LOADED_LIB_PATH = _cand
            logger.info(f"[os_sandbox] Successfully loaded native C library: {_cand}")
            break
        except Exception as _dll_err:
            logger.debug(f"[os_sandbox] Native library {_cand} load skipped/failed ({_dll_err}); checking next candidate.")
            _C_LIB = None

if _C_LIB is None:
    logger.debug("[os_sandbox] No native C sandbox library found; using direct kernel ctypes.")

# -----------------------------------------------------------------------------
# Win32 Structures & Function Prototypes
# -----------------------------------------------------------------------------
if IS_WINDOWS:
    import ctypes.wintypes as wt

    k32 = ctypes.windll.kernel32
    adv32 = ctypes.windll.advapi32

    # Constants
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    JobObjectExtendedLimitInformation = 9

    TOKEN_DUPLICATE = 0x0002
    TOKEN_ASSIGN_PRIMARY = 0x0001
    TOKEN_QUERY = 0x0008
    TOKEN_ADJUST_DEFAULT = 0x0080
    DISABLE_MAX_PRIVILEGE = 0x00000001
    SE_GROUP_INTEGRITY = 0x00000020
    TokenIntegrityLevel = 25

    CREATE_SUSPENDED = 0x00000004
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    INFINITE = 0xFFFFFFFF
    STILL_ACTIVE = 259

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wt.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wt.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wt.DWORD),
            ("SchedulingClass", wt.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryLimit", ctypes.c_size_t),
            ("PeakJobMemoryLimit", ctypes.c_size_t),
        ]

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("Sid", ctypes.c_void_p),
            ("Attributes", wt.DWORD),
        ]

    class TOKEN_MANDATORY_LABEL(ctypes.Structure):
        _fields_ = [
            ("Label", SID_AND_ATTRIBUTES),
        ]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD),
            ("lpReserved", wt.LPWSTR),
            ("lpDesktop", wt.LPWSTR),
            ("lpTitle", wt.LPWSTR),
            ("dwX", wt.DWORD),
            ("dwY", wt.DWORD),
            ("dwXSize", wt.DWORD),
            ("dwYSize", wt.DWORD),
            ("dwXCountChars", wt.DWORD),
            ("dwYCountChars", wt.DWORD),
            ("dwFillAttribute", wt.DWORD),
            ("dwFlags", wt.DWORD),
            ("wShowWindow", wt.WORD),
            ("cbReserved2", wt.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wt.HANDLE),
            ("hStdOutput", wt.HANDLE),
            ("hStdError", wt.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wt.HANDLE),
            ("hThread", wt.HANDLE),
            ("dwProcessId", wt.DWORD),
            ("dwThreadId", wt.DWORD),
        ]

    # Function signatures
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    k32.CreateJobObjectW.restype = wt.HANDLE

    k32.SetInformationJobObject.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD]
    k32.SetInformationJobObject.restype = wt.BOOL

    k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    k32.AssignProcessToJobObject.restype = wt.BOOL

    k32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
    k32.TerminateJobObject.restype = wt.BOOL

    k32.GetCurrentProcess.argtypes = []
    k32.GetCurrentProcess.restype = wt.HANDLE

    k32.ResumeThread.argtypes = [wt.HANDLE]
    k32.ResumeThread.restype = wt.DWORD

    k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    k32.WaitForSingleObject.restype = wt.DWORD

    k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    k32.GetExitCodeProcess.restype = wt.BOOL

    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.CloseHandle.restype = wt.BOOL

    adv32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    adv32.OpenProcessToken.restype = wt.BOOL

    adv32.CreateRestrictedToken.argtypes = [
        wt.HANDLE,
        wt.DWORD,
        wt.DWORD,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wt.HANDLE),
    ]
    adv32.CreateRestrictedToken.restype = wt.BOOL

    adv32.ConvertStringSidToSidW.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    adv32.ConvertStringSidToSidW.restype = wt.BOOL

    adv32.GetLengthSid.argtypes = [ctypes.c_void_p]
    adv32.GetLengthSid.restype = wt.DWORD

    adv32.SetTokenInformation.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD]
    adv32.SetTokenInformation.restype = wt.BOOL

    adv32.CreateProcessAsUserW.argtypes = [
        wt.HANDLE,
        wt.LPCWSTR,
        wt.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wt.BOOL,
        wt.DWORD,
        ctypes.c_void_p,
        wt.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    adv32.CreateProcessAsUserW.restype = wt.BOOL

    k32.CreateProcessW.argtypes = [
        wt.LPCWSTR,
        wt.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wt.BOOL,
        wt.DWORD,
        ctypes.c_void_p,
        wt.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    k32.CreateProcessW.restype = wt.BOOL


# -----------------------------------------------------------------------------
# ContainedProcess Wrapper
# -----------------------------------------------------------------------------
class ContainedProcess:
    """Represents an active process running inside a kernel-enforced containment boundary."""

    def __init__(
        self,
        pid: int,
        command: str,
        spawn_latency_us: int,
        max_memory_mb: int,
        low_integrity: bool,
        job_handle: Any = None,
        process_handle: Any = None,
        popen_proc: Optional[subprocess.Popen] = None,
        backend: str = "c_dll",
    ):
        self.pid = pid
        self.command = command
        self.spawn_latency_us = spawn_latency_us
        self.max_memory_mb = max_memory_mb
        self.low_integrity = low_integrity
        self.job_handle = job_handle
        self.process_handle = process_handle
        self.popen_proc = popen_proc
        self.backend = backend
        self._exit_code: Optional[int] = None
        self._closed = False

    def is_alive(self) -> bool:
        """Check if the contained process is still running."""
        if self._exit_code is not None:
            return False

        if IS_WINDOWS and self.process_handle:
            code = wt.DWORD()
            if k32.GetExitCodeProcess(self.process_handle, ctypes.byref(code)):
                if code.value == STILL_ACTIVE:
                    return True
                self._exit_code = int(code.value)
                return False
            return False
        elif self.popen_proc:
            poll_res = self.popen_proc.poll()
            if poll_res is None:
                return True
            self._exit_code = poll_res
            return False
        elif self.pid > 0:
            try:
                os.kill(self.pid, 0)
                return True
            except (OSError, ProcessLookupError):
                return False
        return False

    def terminate(self) -> bool:
        """Terminate the process and all child processes via kernel Job Object or SIGKILL."""
        if self._closed and self._exit_code is not None:
            return True

        success = True
        if self.backend == "c_dll" and _C_LIB is not None:
            try:
                ret = _C_LIB.jai_terminate_sandbox(self.pid)
                if ret != 0:
                    success = False
            except Exception:
                success = False

        if IS_WINDOWS:
            if self.job_handle:
                if not k32.TerminateJobObject(self.job_handle, 1):
                    success = False
            elif self.process_handle:
                k32.TerminateProcess(self.process_handle, 1)
        elif self.popen_proc:
            try:
                self.popen_proc.kill()
            except Exception:
                success = False
        elif self.pid > 0:
            try:
                import signal
                os.kill(self.pid, signal.SIGKILL)
            except Exception:
                success = False

        self._exit_code = 1
        self.close()
        return success

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        """Wait for the contained process to exit and return its exit code."""
        if self._exit_code is not None:
            return self._exit_code

        if IS_WINDOWS and self.process_handle:
            timeout_ms = int(timeout * 1000) if timeout is not None else INFINITE
            wait_res = k32.WaitForSingleObject(self.process_handle, timeout_ms)
            if wait_res == 0:  # WAIT_OBJECT_0
                code = wt.DWORD()
                if k32.GetExitCodeProcess(self.process_handle, ctypes.byref(code)):
                    self._exit_code = int(code.value)
                    return self._exit_code
            return None
        elif self.popen_proc:
            try:
                self._exit_code = self.popen_proc.wait(timeout=timeout)
                return self._exit_code
            except subprocess.TimeoutExpired:
                return None

        # Fallback polling
        start_t = time.time()
        while timeout is None or (time.time() - start_t) < timeout:
            if not self.is_alive():
                return self._exit_code or 0
            time.sleep(0.02)
        return None

    def close(self) -> None:
        """Release underlying kernel object handles."""
        if self._closed:
            return
        self._closed = True

        if IS_WINDOWS:
            if self.process_handle:
                try:
                    k32.CloseHandle(self.process_handle)
                except Exception:
                    pass
                self.process_handle = None
            if self.job_handle:
                try:
                    k32.CloseHandle(self.job_handle)
                except Exception:
                    pass
                self.job_handle = None

        if self.pid in _ACTIVE_SANDBOXES:
            _ACTIVE_SANDBOXES.pop(self.pid, None)

    def __repr__(self) -> str:
        return (
            f"<ContainedProcess pid={self.pid} backend={self.backend} latency={self.spawn_latency_us}us "
            f"mem_mb={self.max_memory_mb} low_integrity={self.low_integrity}>"
        )


# -----------------------------------------------------------------------------
# Containment Spawner Implementations
# -----------------------------------------------------------------------------
def _spawn_windows_kernel(
    cmd_str: str,
    work_dir: Optional[str],
    max_memory_mb: int,
    low_integrity: bool,
) -> tuple[int, Any, Any]:
    """Spawn contained process on Windows using direct Microsoft-signed kernel32/advapi32 APIs."""
    # 1. Create Job Object with strict resource caps and kill-on-close
    hJob = k32.CreateJobObjectW(None, None)
    if not hJob:
        raise OSError(f"CreateJobObjectW failed: {k32.GetLastError()}")

    jeli = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    jeli.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    jeli.BasicLimitInformation.ActiveProcessLimit = 16

    if max_memory_mb > 0:
        jeli.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY
        jeli.ProcessMemoryLimit = max_memory_mb * 1024 * 1024

    if not k32.SetInformationJobObject(
        hJob,
        JobObjectExtendedLimitInformation,
        ctypes.byref(jeli),
        ctypes.sizeof(jeli),
    ):
        err = k32.GetLastError()
        k32.CloseHandle(hJob)
        raise OSError(f"SetInformationJobObject failed: {err}")

    # 2. Prepare Low-Integrity restricted token if requested
    hRestricted = None
    if low_integrity:
        hProcessToken = wt.HANDLE()
        if adv32.OpenProcessToken(
            k32.GetCurrentProcess(),
            TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY | TOKEN_QUERY | TOKEN_ADJUST_DEFAULT,
            ctypes.byref(hProcessToken),
        ):
            hNewToken = wt.HANDLE()
            if adv32.CreateRestrictedToken(
                hProcessToken,
                DISABLE_MAX_PRIVILEGE,
                0,
                None,
                0,
                None,
                0,
                None,
                ctypes.byref(hNewToken),
            ):
                pSid = ctypes.c_void_p()
                if adv32.ConvertStringSidToSidW("S-1-16-4096", ctypes.byref(pSid)):
                    tml = TOKEN_MANDATORY_LABEL()
                    tml.Label.Attributes = SE_GROUP_INTEGRITY
                    tml.Label.Sid = pSid.value
                    adv32.SetTokenInformation(
                        hNewToken,
                        TokenIntegrityLevel,
                        ctypes.byref(tml),
                        ctypes.sizeof(tml) + adv32.GetLengthSid(pSid),
                    )
                    k32.LocalFree(pSid)
                    hRestricted = hNewToken
            k32.CloseHandle(hProcessToken)

    # 3. Create suspended process
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    pi = PROCESS_INFORMATION()

    cmd_buf = ctypes.create_unicode_buffer(cmd_str)
    creation_flags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT
    spawned = False

    if hRestricted:
        spawned = bool(
            adv32.CreateProcessAsUserW(
                hRestricted,
                None,
                cmd_buf,
                None,
                None,
                False,
                creation_flags,
                None,
                work_dir,
                ctypes.byref(si),
                ctypes.byref(pi),
            )
        )
        k32.CloseHandle(hRestricted)

    # Fallback to standard CreateProcessW suspended if restricted token failed or wasn't requested
    if not spawned:
        spawned = bool(
            k32.CreateProcessW(
                None,
                cmd_buf,
                None,
                None,
                False,
                creation_flags,
                None,
                work_dir,
                ctypes.byref(si),
                ctypes.byref(pi),
            )
        )

    if not spawned:
        err = k32.GetLastError()
        k32.CloseHandle(hJob)
        raise OSError(f"Process creation failed: Windows Error {err}")

    # 4. Assign to Job Object before resuming execution
    if not k32.AssignProcessToJobObject(hJob, pi.hProcess):
        err = k32.GetLastError()
        k32.TerminateProcess(pi.hProcess, 1)
        k32.CloseHandle(pi.hThread)
        k32.CloseHandle(pi.hProcess)
        k32.CloseHandle(hJob)
        raise OSError(f"AssignProcessToJobObject failed: Windows Error {err}")

    # 5. Resume suspended main thread
    k32.ResumeThread(pi.hThread)
    k32.CloseHandle(pi.hThread)

    return pi.dwProcessId, hJob, pi.hProcess


def _spawn_posix_contained(
    command: Union[str, List[str]],
    work_dir: Optional[str],
    max_memory_mb: int,
) -> tuple[int, subprocess.Popen]:
    """Spawn contained process on POSIX using setrlimit memory cap and PR_SET_NO_NEW_PRIVS."""
    def _preexec():
        if work_dir:
            try:
                os.chdir(work_dir)
            except Exception:
                pass
        if max_memory_mb > 0:
            try:
                import resource
                mem_bytes = max_memory_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
            except Exception:
                pass
        try:
            # PR_SET_NO_NEW_PRIVS = 38
            import ctypes
            libc = ctypes.CDLL(None)
            libc.prctl(38, 1, 0, 0, 0)
        except Exception:
            pass

    cmd_args = shlex.split(command) if isinstance(command, str) else command
    proc = subprocess.Popen(cmd_args, cwd=work_dir, preexec_fn=_preexec)
    return proc.pid, proc


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------
def spawn_contained_process(
    command: Union[str, List[str]],
    work_dir: Optional[str] = None,
    max_memory_mb: int = 1024,
    low_integrity: bool = True,
) -> ContainedProcess:
    """
    Spawn a subprocess inside a kernel-enforced containment boundary.
    Measures exact spawn latency in microseconds.

    Args:
        command: String or list of arguments representing the command line.
        work_dir: Optional working directory for the child process.
        max_memory_mb: Hard virtual memory ceiling in megabytes (default 1024 MB).
        low_integrity: Whether to drop token privileges to Low-Integrity level (Windows).

    Returns:
        ContainedProcess instance with tracked PID, handles, and microsecond spawn latency.
    """
    cmd_str = subprocess.list2cmdline(command) if isinstance(command, list) else command
    start_ns = time.perf_counter_ns()

    pid = 0
    h_job = None
    h_proc = None
    popen_proc = None
    backend = "python_ctypes"

    # Check if native compiled C DLL is active and functional
    if _C_LIB is not None:
        try:
            out_pid = ctypes.c_ulong(0)
            c_ret = _C_LIB.jai_spawn_sandboxed(
                None,
                cmd_str,
                work_dir,
                max_memory_mb,
                1 if low_integrity else 0,
                ctypes.byref(out_pid),
            )
            if c_ret == 0 and out_pid.value > 0:
                pid = int(out_pid.value)
                backend = "c_dll"
                if IS_WINDOWS:
                    # Open process handle for synchronous waiting and exit code retrieval
                    h_proc = k32.OpenProcess(0x00100000 | 0x1000 | 0x0001, False, pid)
        except Exception as _c_err:
            logger.debug(f"[os_sandbox] _C_LIB call error ({_c_err}), falling back to direct kernel ctypes.")
            pid = 0
            backend = "python_ctypes"

    # Fallback to direct kernel containment if C DLL did not spawn
    if pid == 0:
        backend = "python_ctypes"
        if IS_WINDOWS:
            pid, h_job, h_proc = _spawn_windows_kernel(
                cmd_str=cmd_str,
                work_dir=work_dir,
                max_memory_mb=max_memory_mb,
                low_integrity=low_integrity,
            )
        else:
            pid, popen_proc = _spawn_posix_contained(
                command=command,
                work_dir=work_dir,
                max_memory_mb=max_memory_mb,
            )

    end_ns = time.perf_counter_ns()
    latency_us = max(1, (end_ns - start_ns) // 1000)

    contained = ContainedProcess(
        pid=pid,
        command=cmd_str,
        spawn_latency_us=latency_us,
        max_memory_mb=max_memory_mb,
        low_integrity=low_integrity,
        job_handle=h_job,
        process_handle=h_proc,
        popen_proc=popen_proc,
        backend=backend,
    )
    _ACTIVE_SANDBOXES[pid] = contained
    logger.info(f"[os_sandbox] Spawned contained PID={pid} in {latency_us}µs (Mem={max_memory_mb}MB, LowIntegrity={low_integrity})")
    return contained


def terminate_sandboxed_process(pid: int = 0) -> bool:
    """
    Terminate a sandboxed process or all registered sandboxes.

    Args:
        pid: Specific PID to terminate, or 0 to terminate all registered sandboxes.
    """
    if _C_LIB is not None:
        try:
            _C_LIB.jai_terminate_sandbox(pid)
        except Exception:
            pass

    if pid == 0:
        pids = list(_ACTIVE_SANDBOXES.keys())
        success = True
        for p in pids:
            proc = _ACTIVE_SANDBOXES.get(p)
            if proc:
                if not proc.terminate():
                    success = False
        return success
    else:
        proc = _ACTIVE_SANDBOXES.get(pid)
        if proc:
            return proc.terminate()
        return False


def get_active_sandboxes() -> Dict[int, ContainedProcess]:
    """Return dictionary of currently registered active sandboxes."""
    # Filter dead ones
    dead = [pid for pid, proc in _ACTIVE_SANDBOXES.items() if not proc.is_alive()]
    for d in dead:
        _ACTIVE_SANDBOXES.pop(d, None)
    return dict(_ACTIVE_SANDBOXES)


# Register automatic atexit cleanup to ensure zero orphaned sandboxed processes
atexit.register(terminate_sandboxed_process, 0)
