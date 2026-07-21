"""Run approved Serverless Devs FC operations with repository environment values."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REQUIRED_DEPLOYMENT_VARIABLES = (
    "DASHSCOPE_API_KEY",
    "OPENROUTER_API_KEY",
    "PRAXIS_OPERATOR_TOKEN",
    "WEBHOOK_SIGNING_SECRET",
    "DEDUP_WINDOW_SECONDS",
    "MAX_WEBHOOK_BODY_BYTES",
    "MAX_APPROVAL_BODY_BYTES",
    "QWEN_BASE_URL",
    "QWENCLOUD_MODELS",
    "OPENROUTER_MODELS",
    "PRIMARY_MODEL",
    "FAST_MODEL",
    "OPENROUTER_FAST_MODEL",
    "PRAXIS_DEMO_TARGET_URL",
    "PRAXIS_DEMO_TARGET_TOKEN",
    "FC_EXECUTION_ROLE_ARN",
    "MEMORY_BACKEND",
    "MEMORY_SIMILARITY_THRESHOLD",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "TABLESTORE_ENDPOINT",
    "TABLESTORE_INSTANCE",
)
BOOTSTRAP_TARGET_VARIABLES = ("PRAXIS_DEMO_TARGET_TOKEN",)
BOOTSTRAP_TARGET_PASSTHROUGH_VARIABLES = frozenset(
    {
        "ALIBABA_CLOUD_ACCESS_KEY_ID",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
        "ALIBABA_CLOUD_SECURITY_TOKEN",
        "APPDATA",
        "COMSPEC",
        "HOME",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
        "serverless_devs_config_home",
    }
)
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
INSTANCE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
SENSITIVE_NAME_PARTS = ("KEY", "SECRET", "TOKEN", "PASSWORD")
SAFE_ERROR_CODES = (
    "AccessDenied",
    "InvalidAccessKeyId",
    "InvalidSecurityToken",
    "Throttling",
    "ResourceNotFound",
    "ServiceUnavailable",
)
ALLOWED_QWENCLOUD_SMOKE_MODELS = (
    "qwen3.8-max-preview",
    "qwen3.7-max",
    "qwen3-max",
    "qwen-plus",
)
COMMAND_DEADLINE_SECONDS: Mapping[str, float] = {
    "bootstrap-target-verify": 120.0,
    "bootstrap-target-deploy": 600.0,
    "verify": 120.0,
    "deploy": 600.0,
    "instances": 60.0,
    "target-instances": 60.0,
    "smoke": 120.0,
    "memory-smoke": 120.0,
}
TERMINATION_GRACE_SECONDS = 5.0
TIMEOUT_EXIT_CODE = 124
_WINDOWS_CONTAINMENT_ERROR = "Windows process containment failed"
_WINDOWS_CLEANUP_ERROR = "Windows process cleanup failed"


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: str
    timed_out: bool = False


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key.isidentifier():
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def _child_environment(
    repository: Path,
    base: Mapping[str, str] | None = None,
    *,
    required_variables: tuple[str, ...] = REQUIRED_DEPLOYMENT_VARIABLES,
) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    environment.update(_read_dotenv(repository / ".env"))

    missing = [
        name
        for name in required_variables
        if not environment.get(name, "").strip()
    ]
    if missing:
        raise RuntimeError(
            "Missing required deployment variables: " + ", ".join(missing)
        )
    return environment


def _bootstrap_target_environment(
    repository: Path,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the least-privilege environment for the isolated target deploy."""

    source = _child_environment(
        repository,
        base,
        required_variables=BOOTSTRAP_TARGET_VARIABLES,
    )
    allowed_names = {
        *BOOTSTRAP_TARGET_VARIABLES,
        *BOOTSTRAP_TARGET_PASSTHROUGH_VARIABLES,
    }
    allowed_names_casefolded = {name.casefold() for name in allowed_names}
    return {
        name: value
        for name, value in source.items()
        if name.casefold() in allowed_names_casefolded
    }


def _find_serverless_cli() -> str:
    executable = shutil.which("s") or shutil.which("s.cmd")
    if executable is not None:
        return executable

    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        npm_executable = Path(appdata) / "npm" / "s.cmd"
        if npm_executable.is_file():
            return str(npm_executable)

    raise RuntimeError("Serverless Devs CLI `s` is not installed or not on PATH")


def _command(
    action: str,
    executable: str,
    template: Path,
    instance_id: str | None = None,
) -> list[str]:
    common = ["-t", str(template)]
    if action in {"verify", "bootstrap-target-verify"}:
        return [executable, "verify", *common]
    if action in {"deploy", "bootstrap-target-deploy"}:
        return [executable, "deploy", *common, "--use-local", "-y"]
    if action in {"instances", "target-instances"}:
        return [executable, "instance", "list", *common, "-o", "json", "--silent"]
    if action in {"smoke", "memory-smoke"}:
        if not instance_id or not re.fullmatch(r"[A-Za-z0-9_-]+", instance_id):
            raise RuntimeError(f"{action} requires a valid --instance-id")
        remote_command = (
            "cd /code && /var/fc/lang/python3.10/bin/python3 -m app.m0_smoke"
            if action == "smoke"
            else (
                "cd /code && /var/fc/lang/python3.10/bin/python3 -c \""
                "import asyncio,json; "
                "from app.config import Settings; "
                "from app.agent.memory import build_memory_backend; "
                "s=Settings.from_env(); b=build_memory_backend(s); "
                "asyncio.run(b.ensure_ready()); "
                "print(json.dumps({'ok':True,'backend':b.name,"
                "'embedding_model':s.embedding_model,"
                "'embedding_dimension':s.embedding_dim}))\""
            )
        )
        return [
            executable,
            "instance",
            "exec",
            *common,
            "--instance-id",
            instance_id,
            "--shell",
            "/bin/bash",
            "--cmd",
            remote_command,
        ]
    raise RuntimeError(f"Unsupported Serverless Devs action: {action}")


def _redact_output(output: str, environment: Mapping[str, str]) -> str:
    redacted = output
    sensitive_values = {
        value
        for name, value in environment.items()
        if any(part in name.upper() for part in SENSITIVE_NAME_PARTS)
        and len(value) >= 4
    }
    for value in sorted(sensitive_values, key=len, reverse=True):
        redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def _safe_summary(action: str, output: str, returncode: int) -> dict[str, object]:
    clean = ANSI_ESCAPE.sub("", output)
    summary: dict[str, object] = {
        "action": action,
        "ok": returncode == 0,
        "exit_code": returncode,
    }
    if action in {"deploy", "bootstrap-target-deploy"} and returncode == 0:
        region = re.search(r"(?m)^region:\s*([a-z0-9-]+)\s*$", clean)
        if region:
            summary["region"] = region.group(1)
        function_names = re.findall(
            r"(?m)^functionName:\s*([A-Za-z0-9_-]+)\s*$", clean
        )
        function_name = (
            "praxis-api" if "praxis-api" in function_names else function_names[0]
            if function_names
            else None
        )
        if function_name:
            summary["function_name"] = function_name
        urls = re.findall(
            r"(?m)^\s*system_url:\s*(https://[A-Za-z0-9.-]+\.fcapp\.run)\s*$",
            clean,
        )
        if urls:
            controller_url = next(
                (
                    url
                    for url in urls
                    if function_name
                    and re.match(rf"https://{re.escape(function_name)}(?:-|\.)", url)
                ),
                urls[-1],
            )
            summary["url"] = controller_url
    elif action in {"instances", "target-instances"} and returncode == 0:
        instance_ids = _safe_instance_ids(clean)
        if instance_ids is None:
            summary.update(
                {
                    "ok": False,
                    "exit_code": 1,
                    "reason": "malformed_instance_list_output",
                }
            )
        else:
            summary["instance_ids"] = instance_ids
    elif returncode != 0:
        summary["error_codes"] = [
            code for code in SAFE_ERROR_CODES if code.lower() in clean.lower()
        ]
        status_codes = sorted(
            set(re.findall(r"\b(?:HTTP|statusCode)\D{0,4}([45]\d\d)\b", clean, re.I))
        )
        if status_codes:
            summary["http_statuses"] = status_codes
    return summary


def _safe_instance_ids(output: str) -> list[str] | None:
    """Parse the documented function-to-instances Serverless Devs envelope."""

    def reject_nonstandard_constant(_value: str) -> None:
        raise ValueError("non-standard JSON constant")

    def reject_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    try:
        payload = json.loads(
            output,
            parse_constant=reject_nonstandard_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        return None

    if not isinstance(payload, Mapping) or len(payload) != 1:
        return None
    function_name, envelope = next(iter(payload.items()))
    if (
        not isinstance(function_name, str)
        or INSTANCE_ID.fullmatch(function_name) is None
        or not isinstance(envelope, Mapping)
        or set(envelope) != {"instances"}
    ):
        return None
    instances = envelope.get("instances")
    if not isinstance(instances, list):
        return None

    instance_ids: list[str] = []
    for item in instances:
        if not isinstance(item, Mapping):
            return None
        instance_id = item.get("instanceId")
        if (
            not isinstance(instance_id, str)
            or INSTANCE_ID.fullmatch(instance_id) is None
        ):
            return None
        instance_ids.append(instance_id)
    return sorted(set(instance_ids))


def _safe_smoke_proof(output: str) -> dict[str, str] | None:
    """Return only allowlisted fields from a valid inner FC smoke proof."""

    clean = ANSI_ESCAPE.sub("", output)
    for line in clean.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{") or not candidate.endswith("}"):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(payload, dict)
            and payload.get("ok") is True
            and payload.get("provider") == "qwencloud"
            and payload.get("model") in ALLOWED_QWENCLOUD_SMOKE_MODELS
        ):
            return {
                "provider": payload["provider"],
                "model": payload["model"],
            }
    return None


def _safe_memory_smoke_proof(output: str) -> dict[str, object] | None:
    """Return only the accepted deployed Tablestore schema proof fields."""

    clean = ANSI_ESCAPE.sub("", output)
    for line in clean.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{") or not candidate.endswith("}"):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(payload, dict)
            and payload.get("ok") is True
            and payload.get("backend") == "tablestore"
            and payload.get("embedding_model") == "text-embedding-v4"
            and payload.get("embedding_dimension") == 1024
        ):
            return {
                "backend": "tablestore",
                "embedding_model": "text-embedding-v4",
                "embedding_dimension": 1024,
            }
    return None


def _trace_log_directory(environment: Mapping[str, str], trace_id: str) -> Path:
    config_home = Path(
        environment.get("serverless_devs_config_home", str(Path.home()))
    ).resolve()
    log_root = (config_home / ".s" / "logs").resolve()
    target = (log_root / trace_id).resolve()
    if target.parent != log_root:
        raise RuntimeError("Refusing unsafe Serverless Devs trace-log path")
    return target


def _secret_free_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if base is None else base
    return {
        name: value
        for name, value in source.items()
        if not any(part in name.upper() for part in SENSITIVE_NAME_PARTS)
    }


def _template_for_action(repository: Path, action: str) -> Path:
    if action in {"bootstrap-target-verify", "bootstrap-target-deploy"}:
        return repository / "deploy" / "target.s.yaml"
    if action == "target-instances":
        return repository / "deploy" / "target-instance.s.yaml"
    if action in {"instances", "smoke", "memory-smoke"}:
        return repository / "deploy" / "instance.s.yaml"
    return repository / "deploy" / "s.yaml"


def _terminate_child(
    process: subprocess.Popen[str],
    *,
    grace_seconds: float | None = None,
) -> None:
    """Stop the child's isolated process group, then close inherited pipes."""

    grace = TERMINATION_GRACE_SECONDS if grace_seconds is None else grace_seconds
    pid = getattr(process, "pid", None)
    if isinstance(pid, int) and pid > 0:
        if os.name != "nt":
            _terminate_posix_process_group(process, pid=pid, grace_seconds=grace)
            return

    # Windows production children are contained by a kill-on-close Job Object
    # before this fallback can be reached. Keep the direct path for defensive
    # callers and test doubles that expose no stable process identity.
    _terminate_direct_child(process, grace_seconds=grace)


def _terminate_posix_process_group(
    process: subprocess.Popen[str],
    *,
    pid: int,
    grace_seconds: float,
) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _drain_or_close_process(process, grace_seconds=grace_seconds)


def _create_windows_job() -> object:
    """Create one unnamed, non-inheritable kill-on-close Job Object."""

    import ctypes
    from ctypes import wintypes

    class JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class IoCounters(ctypes.Structure):
        _fields_ = (
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        )

    class JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", JobObjectBasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(ctypes.get_last_error(), "job creation failed")
    try:
        information = JobObjectExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            handle,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise OSError(ctypes.get_last_error(), "job configuration failed")
    except BaseException:
        kernel32.CloseHandle(handle)
        raise
    return handle


def _assign_process_to_windows_job(
    job_handle: object,
    process: subprocess.Popen[str],
) -> None:
    """Assign the exact Popen process handle without reopening a reusable PID."""

    import ctypes
    from ctypes import wintypes

    raw_process_handle = getattr(process, "_handle", None)
    try:
        process_handle = int(raw_process_handle)
    except (TypeError, ValueError):
        process_handle = 0
    if process_handle <= 0:
        raise OSError(6, "stable process handle is unavailable")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    if not kernel32.AssignProcessToJobObject(
        job_handle,
        wintypes.HANDLE(process_handle),
    ):
        raise OSError(ctypes.get_last_error(), "job assignment failed")


def _terminate_windows_job(job_handle: object) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    if not kernel32.TerminateJobObject(job_handle, TIMEOUT_EXIT_CODE):
        raise OSError(ctypes.get_last_error(), "job termination failed")


def _windows_job_active_processes(job_handle: object) -> int:
    import ctypes
    from ctypes import wintypes

    class JobObjectBasicAccountingInformation(ctypes.Structure):
        _fields_ = (
            ("TotalUserTime", wintypes.LARGE_INTEGER),
            ("TotalKernelTime", wintypes.LARGE_INTEGER),
            ("ThisPeriodTotalUserTime", wintypes.LARGE_INTEGER),
            ("ThisPeriodTotalKernelTime", wintypes.LARGE_INTEGER),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    information = JobObjectBasicAccountingInformation()
    if not kernel32.QueryInformationJobObject(
        job_handle,
        1,
        ctypes.byref(information),
        ctypes.sizeof(information),
        None,
    ):
        raise OSError(ctypes.get_last_error(), "job state query failed")
    return int(information.ActiveProcesses)


def _close_windows_job(job_handle: object) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    if not kernel32.CloseHandle(job_handle):
        raise OSError(ctypes.get_last_error(), "job close failed")


def _terminate_windows_job_tree(
    process: subprocess.Popen[str],
    job_handle: object,
    *,
    grace_seconds: float,
) -> None:
    """Terminate one contained tree and verify the job is empty before return."""

    deadline = time.monotonic() + grace_seconds
    cleanup_failed = False
    try:
        _terminate_windows_job(job_handle)
        while _windows_job_active_processes(job_handle):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Windows Job Object did not become empty")
            time.sleep(min(0.01, remaining))
    except Exception:
        cleanup_failed = True
    try:
        # KILL_ON_JOB_CLOSE is the final fail-safe even when termination or
        # accounting queries fail.
        _close_windows_job(job_handle)
    except Exception:
        cleanup_failed = True

    try:
        _drain_or_close_process_until(process, deadline=deadline)
    except Exception:
        cleanup_failed = True
    try:
        parent_running = process.poll() is None
    except Exception:
        parent_running = True
    if cleanup_failed or parent_running:
        raise RuntimeError(_WINDOWS_CLEANUP_ERROR)


def _abort_uncontained_windows_process(
    process: subprocess.Popen[str],
    job_handle: object,
    *,
    grace_seconds: float,
) -> None:
    """Kill the exact parent handle when Job Object assignment fails."""

    deadline = time.monotonic() + grace_seconds
    cleanup_failed = False
    try:
        if process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass
    except Exception:
        cleanup_failed = True
    try:
        _drain_or_close_process_until(process, deadline=deadline)
    except Exception:
        cleanup_failed = True
    finally:
        try:
            _close_windows_job(job_handle)
        except Exception:
            cleanup_failed = True
    try:
        parent_running = process.poll() is None
    except Exception:
        parent_running = True
    if cleanup_failed or parent_running:
        raise RuntimeError(_WINDOWS_CLEANUP_ERROR)


def _terminate_direct_child(
    process: subprocess.Popen[str],
    *,
    grace_seconds: float,
) -> None:
    """Fallback for non-Popen test doubles that expose no process identity."""

    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.communicate(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            return
        try:
            process.communicate(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            for stream_name in ("stdin", "stdout", "stderr"):
                stream = getattr(process, stream_name, None)
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            try:
                process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                pass


def _drain_or_close_process(
    process: subprocess.Popen[str],
    *,
    grace_seconds: float,
) -> None:
    try:
        process.communicate(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(process, stream_name, None)
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass


def _drain_or_close_process_until(
    process: subprocess.Popen[str],
    *,
    deadline: float,
) -> None:
    """Drain a terminated Windows process within one shared deadline."""

    try:
        process.communicate(timeout=max(0.0, deadline - time.monotonic()))
        return
    except subprocess.TimeoutExpired:
        pass
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(process, stream_name, None)
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        pass


def _process_group_options() -> dict[str, object]:
    """Return platform-specific Popen options for isolated tree cleanup."""

    if os.name == "nt":
        return {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        }
    return {"start_new_session": True}


def _run_bounded_command(
    command: list[str],
    *,
    working_directory: Path,
    environment: Mapping[str, str],
    deadline_seconds: float,
) -> _CommandResult:
    windows_job: object | None = None
    if os.name == "nt":
        try:
            windows_job = _create_windows_job()
        except Exception:
            raise RuntimeError(_WINDOWS_CONTAINMENT_ERROR) from None
    try:
        process = subprocess.Popen(
            command,
            cwd=working_directory,
            env=dict(environment),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_process_group_options(),
        )
    except BaseException:
        if windows_job is not None:
            try:
                _close_windows_job(windows_job)
            except Exception:
                pass
        raise

    if windows_job is not None:
        try:
            _assign_process_to_windows_job(windows_job, process)
        except Exception:
            try:
                _abort_uncontained_windows_process(
                    process,
                    windows_job,
                    grace_seconds=TERMINATION_GRACE_SECONDS,
                )
            except Exception:
                pass
            raise RuntimeError(_WINDOWS_CONTAINMENT_ERROR) from None
    try:
        stdout, _ = process.communicate(timeout=deadline_seconds)
    except subprocess.TimeoutExpired:
        if windows_job is not None:
            job_to_close = windows_job
            windows_job = None
            try:
                _terminate_windows_job_tree(
                    process,
                    job_to_close,
                    grace_seconds=TERMINATION_GRACE_SECONDS,
                )
            except Exception:
                raise RuntimeError(_WINDOWS_CLEANUP_ERROR) from None
        else:
            _terminate_child(process)
        return _CommandResult(
            returncode=TIMEOUT_EXIT_CODE,
            stdout="",
            timed_out=True,
        )
    except BaseException:
        if windows_job is not None:
            job_to_close = windows_job
            windows_job = None
            try:
                _terminate_windows_job_tree(
                    process,
                    job_to_close,
                    grace_seconds=TERMINATION_GRACE_SECONDS,
                )
            except Exception:
                raise RuntimeError(_WINDOWS_CLEANUP_ERROR) from None
        else:
            _terminate_child(process)
        raise
    if windows_job is not None:
        try:
            _close_windows_job(windows_job)
        except Exception:
            # If closing the kill-on-close handle itself fails, make an
            # explicit group-termination attempt before surfacing the fixed
            # cleanup error. A second close is best-effort handle hygiene.
            try:
                _terminate_windows_job(windows_job)
            except Exception:
                pass
            try:
                _close_windows_job(windows_job)
            except Exception:
                pass
            raise RuntimeError(_WINDOWS_CLEANUP_ERROR) from None
    return _CommandResult(
        returncode=process.returncode,
        stdout=stdout or "",
    )


def _timeout_summary(action: str, deadline_seconds: float) -> dict[str, object]:
    summary: dict[str, object] = {
        "action": action,
        "ok": False,
        "exit_code": TIMEOUT_EXIT_CODE,
        "timed_out": True,
        "deadline_seconds": deadline_seconds,
    }
    if action in {"deploy", "bootstrap-target-deploy"}:
        summary.update(
            {
                "release_state": "unknown",
                "retry_safe": False,
                "required_action": "reconcile_remote_state_before_retry",
            }
        )
    return summary


def run(action: str, instance_id: str | None = None) -> int:
    repository = Path(__file__).resolve().parents[1]
    template = _template_for_action(repository, action)
    if not template.is_file():
        raise RuntimeError("deploy/s.yaml is missing")
    if action in {"deploy", "bootstrap-target-deploy"} and not (
        repository / "python"
    ).is_dir():
        raise RuntimeError(
            "FC dependency bundle is missing; run scripts/build_fc_dependencies.py"
        )

    if action in {"instances", "target-instances", "smoke", "memory-smoke"}:
        environment = _secret_free_environment()
    elif action in {"bootstrap-target-verify", "bootstrap-target-deploy"}:
        environment = _bootstrap_target_environment(repository)
    else:
        environment = _child_environment(repository)
    trace_id = f"praxis-{uuid.uuid4().hex}"
    environment["serverless_devs_traceid"] = trace_id
    command = _command(action, _find_serverless_cli(), template, instance_id)
    trace_log = _trace_log_directory(environment, trace_id)
    secret_free_workdir = repository / ".fc-cli-work"
    if action in {"instances", "target-instances", "smoke", "memory-smoke"}:
        secret_free_workdir.mkdir(exist_ok=True)
    working_directory = (
        secret_free_workdir
        if action in {"instances", "target-instances", "smoke", "memory-smoke"}
        else repository
    )
    deadline_seconds = COMMAND_DEADLINE_SECONDS[action]
    try:
        completed = _run_bounded_command(
            command,
            working_directory=working_directory,
            environment=environment,
            deadline_seconds=deadline_seconds,
        )
        if completed.timed_out:
            print(json.dumps(_timeout_summary(action, deadline_seconds)))
            result_code = TIMEOUT_EXIT_CODE
        elif action in {"smoke", "memory-smoke"}:
            proof = (
                _safe_smoke_proof(completed.stdout)
                if action == "smoke"
                else _safe_memory_smoke_proof(completed.stdout)
            )
            result_code = completed.returncode or (0 if proof is not None else 1)
            summary: dict[str, object] = {
                "action": (
                    "smoke_transport"
                    if action == "smoke"
                    else "memory_smoke_transport"
                ),
                "ok": result_code == 0,
                "exit_code": result_code,
            }
            if result_code == 0 and proof is not None:
                summary.update(proof)
            print(json.dumps(summary))
        else:
            redacted = _redact_output(completed.stdout, environment)
            summary = _safe_summary(action, redacted, completed.returncode)
            print(json.dumps(summary))
            result_code = int(summary["exit_code"])
    finally:
        if trace_log.is_dir():
            shutil.rmtree(trace_log)
    return result_code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run secret-safe Serverless Devs operations for Praxis."
    )
    parser.add_argument(
        "action",
        choices=(
            "bootstrap-target-verify",
            "bootstrap-target-deploy",
            "verify",
            "deploy",
            "instances",
            "target-instances",
            "smoke",
            "memory-smoke",
        ),
    )
    parser.add_argument("--instance-id")
    args = parser.parse_args()
    try:
        return run(args.action, args.instance_id)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
