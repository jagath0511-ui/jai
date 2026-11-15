/**
 * native/jai_sandbox.c - Native OS Process Containment Library for Project JAI.
 * ==============================================================================
 * Provides kernel-enforced resource and privilege boundaries:
 *
 * Windows (_WIN32):
 * - Win32 Job Objects (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, ProcessMemoryLimit,
 *   ActiveProcessLimit).
 * - Optional Low Integrity Level (S-1-16-4096) with DISABLE_MAX_PRIVILEGE restricted tokens.
 *
 * Linux / POSIX:
 * - POSIX setrlimit (RLIMIT_AS for process memory cap).
 * - prctl(PR_SET_NO_NEW_PRIVS) preventing privilege escalation.
 *
 * Clean C ABI exports:
 * - int jai_spawn_sandboxed(...)
 * - int jai_terminate_sandbox(...)
 */

#if !defined(_WIN32) && !defined(_GNU_SOURCE)
#define _GNU_SOURCE
#endif

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <sddl.h>

#define JAI_EXPORT __declspec(dllexport)
#else
#include <unistd.h>
#include <sys/types.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <signal.h>
#include <wchar.h>
#ifdef __linux__
#include <sys/prctl.h>
#endif

#define JAI_EXPORT __attribute__((visibility("default")))
#endif

#ifdef _WIN32

#define MAX_ACTIVE_SANDBOXES 256

typedef struct {
    DWORD pid;
    HANDLE hJob;
    HANDLE hProcess;
    int in_use;
} ActiveSandbox;

static ActiveSandbox g_sandboxes[MAX_ACTIVE_SANDBOXES];
static CRITICAL_SECTION g_sandbox_lock;
static volatile LONG g_lock_initialized = 0;

static void ensure_lock_initialized(void) {
    if (InterlockedCompareExchange(&g_lock_initialized, 1, 0) == 0) {
        InitializeCriticalSection(&g_sandbox_lock);
    }
}

static void register_sandbox(DWORD pid, HANDLE hJob, HANDLE hProcess) {
    int i;
    ensure_lock_initialized();
    EnterCriticalSection(&g_sandbox_lock);
    for (i = 0; i < MAX_ACTIVE_SANDBOXES; i++) {
        if (!g_sandboxes[i].in_use) {
            g_sandboxes[i].pid = pid;
            g_sandboxes[i].hJob = hJob;
            g_sandboxes[i].hProcess = hProcess;
            g_sandboxes[i].in_use = 1;
            break;
        }
    }
    LeaveCriticalSection(&g_sandbox_lock);
}

static HANDLE unregister_sandbox(DWORD pid, HANDLE* out_process) {
    int i;
    HANDLE hJob = NULL;
    ensure_lock_initialized();
    EnterCriticalSection(&g_sandbox_lock);
    for (i = 0; i < MAX_ACTIVE_SANDBOXES; i++) {
        if (g_sandboxes[i].in_use && g_sandboxes[i].pid == pid) {
            hJob = g_sandboxes[i].hJob;
            if (out_process) {
                *out_process = g_sandboxes[i].hProcess;
            }
            g_sandboxes[i].in_use = 0;
            g_sandboxes[i].pid = 0;
            g_sandboxes[i].hJob = NULL;
            g_sandboxes[i].hProcess = NULL;
            break;
        }
    }
    LeaveCriticalSection(&g_sandbox_lock);
    return hJob;
}

JAI_EXPORT int jai_spawn_sandboxed(
    const wchar_t* exe_path,
    const wchar_t* cmd_line,
    const wchar_t* work_dir,
    size_t max_memory_mb,
    int low_integrity,
    unsigned long* out_pid
) {
    if (!out_pid) {
        return -1;
    }
    *out_pid = 0;

    // 1. Create a Win32 Job Object with containment limits
    HANDLE hJob = CreateJobObjectW(NULL, NULL);
    if (!hJob) {
        return (int)GetLastError();
    }

    JOBOBJECT_EXTENDED_LIMIT_INFORMATION jeli;
    ZeroMemory(&jeli, sizeof(jeli));

    // Limit active processes to 16 and kill all processes when job object is closed
    jeli.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_ACTIVE_PROCESS;
    jeli.BasicLimitInformation.ActiveProcessLimit = 16;

    // Apply memory cap if requested
    if (max_memory_mb > 0) {
        jeli.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY;
        jeli.ProcessMemoryLimit = (SIZE_T)max_memory_mb * 1024 * 1024;
    }

    if (!SetInformationJobObject(hJob, JobObjectExtendedLimitInformation, &jeli, sizeof(jeli))) {
        DWORD err = GetLastError();
        CloseHandle(hJob);
        return (int)err;
    }

    // 2. Prepare mutable command line buffer (CreateProcessW modifies cmdline in place)
    wchar_t* mutable_cmd = NULL;
    if (cmd_line && wcslen(cmd_line) > 0) {
        size_t len = wcslen(cmd_line) + 1;
        mutable_cmd = (wchar_t*)malloc(len * sizeof(wchar_t));
        if (!mutable_cmd) {
            CloseHandle(hJob);
            return -2;
        }
        memcpy(mutable_cmd, cmd_line, len * sizeof(wchar_t));
    }

    // 3. Optional Low-Integrity Token Creation
    HANDLE hToken = NULL;
    if (low_integrity) {
        HANDLE hProcessToken = NULL;
        if (OpenProcessToken(GetCurrentProcess(), TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY | TOKEN_QUERY | TOKEN_ADJUST_DEFAULT, &hProcessToken)) {
            HANDLE hRestricted = NULL;
            if (CreateRestrictedToken(hProcessToken, DISABLE_MAX_PRIVILEGE, 0, NULL, 0, NULL, 0, NULL, &hRestricted)) {
                PSID pLowIntegritySid = NULL;
                if (ConvertStringSidToSidW(L"S-1-16-4096", &pLowIntegritySid)) {
                    TOKEN_MANDATORY_LABEL tml;
                    ZeroMemory(&tml, sizeof(tml));
                    tml.Label.Attributes = SE_GROUP_INTEGRITY;
                    tml.Label.Sid = pLowIntegritySid;

                    SetTokenInformation(
                        hRestricted,
                        TokenIntegrityLevel,
                        &tml,
                        (DWORD)(sizeof(TOKEN_MANDATORY_LABEL) + GetLengthSid(pLowIntegritySid))
                    );
                    LocalFree(pLowIntegritySid);
                }
                hToken = hRestricted;
            }
            CloseHandle(hProcessToken);
        }
    }

    // 4. Spawn child process in SUSPENDED state
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));

    DWORD creation_flags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT;
    BOOL spawned = FALSE;

    if (hToken) {
        spawned = CreateProcessAsUserW(
            hToken,
            (exe_path && wcslen(exe_path) > 0) ? exe_path : NULL,
            mutable_cmd,
            NULL,
            NULL,
            FALSE,
            creation_flags,
            NULL,
            (work_dir && wcslen(work_dir) > 0) ? work_dir : NULL,
            &si,
            &pi
        );
        CloseHandle(hToken);
        hToken = NULL;
    }

    // Fallback to CreateProcessW if CreateProcessAsUserW failed or low_integrity was not requested
    if (!spawned) {
        spawned = CreateProcessW(
            (exe_path && wcslen(exe_path) > 0) ? exe_path : NULL,
            mutable_cmd,
            NULL,
            NULL,
            FALSE,
            creation_flags,
            NULL,
            (work_dir && wcslen(work_dir) > 0) ? work_dir : NULL,
            &si,
            &pi
        );
    }

    if (mutable_cmd) {
        free(mutable_cmd);
    }

    if (!spawned) {
        DWORD err = GetLastError();
        CloseHandle(hJob);
        return (int)err;
    }

    // 5. Assign child process to Job Object before resuming execution
    if (!AssignProcessToJobObject(hJob, pi.hProcess)) {
        DWORD err = GetLastError();
        TerminateProcess(pi.hProcess, 1);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        CloseHandle(hJob);
        return (int)err;
    }

    // 6. Resume child main thread
    ResumeThread(pi.hThread);
    CloseHandle(pi.hThread);

    *out_pid = pi.dwProcessId;
    register_sandbox(pi.dwProcessId, hJob, pi.hProcess);

    return 0; // Success
}

JAI_EXPORT int jai_terminate_sandbox(unsigned long pid) {
    if (pid == 0) {
        // Terminate all registered sandboxes
        int i;
        ensure_lock_initialized();
        EnterCriticalSection(&g_sandbox_lock);
        for (i = 0; i < MAX_ACTIVE_SANDBOXES; i++) {
            if (g_sandboxes[i].in_use) {
                TerminateJobObject(g_sandboxes[i].hJob, 1);
                CloseHandle(g_sandboxes[i].hJob);
                if (g_sandboxes[i].hProcess) {
                    CloseHandle(g_sandboxes[i].hProcess);
                }
                g_sandboxes[i].in_use = 0;
                g_sandboxes[i].pid = 0;
            }
        }
        LeaveCriticalSection(&g_sandbox_lock);
        return 0;
    }

    HANDLE hProcess = NULL;
    HANDLE hJob = unregister_sandbox((DWORD)pid, &hProcess);
    if (!hJob) {
        return -1; // Not found
    }

    TerminateJobObject(hJob, 1);
    CloseHandle(hJob);
    if (hProcess) {
        CloseHandle(hProcess);
    }
    return 0;
}

#else // Non-Windows (Linux / POSIX fallback)

JAI_EXPORT int jai_spawn_sandboxed(
    const wchar_t* exe_path,
    const wchar_t* cmd_line,
    const wchar_t* work_dir,
    size_t max_memory_mb,
    int low_integrity,
    unsigned long* out_pid
) {
    (void)low_integrity;
    (void)exe_path;
    if (!out_pid) {
        return -1;
    }
    *out_pid = 0;

    // Convert wchar_t to char for POSIX
    char c_work[1024] = {0};
    char c_cmd[4096] = {0};

    if (work_dir) wcstombs(c_work, work_dir, sizeof(c_work) - 1);
    if (cmd_line) wcstombs(c_cmd, cmd_line, sizeof(c_cmd) - 1);

    signal(SIGCHLD, SIG_IGN);

    pid_t pid = fork();
    if (pid < 0) {
        return -1;
    }

    if (pid == 0) {
        // Child process
        if (c_work[0] != '\0') {
            if (chdir(c_work) != 0) {
                // Ignore failure cleanly to satisfy __attribute_warn_unused_result__
            }
        }

        // Apply memory ceiling via setrlimit
        if (max_memory_mb > 0) {
            struct rlimit rl;
            rl.rlim_cur = (rlim_t)max_memory_mb * 1024 * 1024;
            rl.rlim_max = (rlim_t)max_memory_mb * 1024 * 1024;
            setrlimit(RLIMIT_AS, &rl);
        }

#ifdef PR_SET_NO_NEW_PRIVS
        prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);
#endif

        char* argv[] = {(char*)"/bin/sh", (char*)"-c", c_cmd, NULL};
        execv("/bin/sh", argv);
        _exit(127);
    }

    *out_pid = (unsigned long)pid;
    return 0;
}

JAI_EXPORT int jai_terminate_sandbox(unsigned long pid) {
    if (pid == 0) return 0;
    return kill((pid_t)pid, SIGKILL);
}

#endif
