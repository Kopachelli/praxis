import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import scripts.fc as fc
from app.config import ACCEPTED_QWENCLOUD_MODELS

from scripts.fc import (
    ALLOWED_QWENCLOUD_SMOKE_MODELS,
    REQUIRED_DEPLOYMENT_VARIABLES,
    _bootstrap_target_environment,
    _child_environment,
    _command,
    _find_serverless_cli,
    _read_dotenv,
    _redact_output,
    _safe_summary,
    _safe_smoke_proof,
    _safe_memory_smoke_proof,
    _secret_free_environment,
    _template_for_action,
    _trace_log_directory,
)


@pytest.fixture
def fake_windows_containment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Popen test doubles on the direct cleanup path on Windows."""

    if os.name != "nt":
        return
    fake_job = object()
    monkeypatch.setattr(fc, "_create_windows_job", lambda: fake_job)
    monkeypatch.setattr(
        fc,
        "_assign_process_to_windows_job",
        lambda _job, _process: None,
    )
    monkeypatch.setattr(fc, "_close_windows_job", lambda _job: None)
    monkeypatch.setattr(
        fc,
        "_terminate_windows_job_tree",
        lambda process, _job, *, grace_seconds: fc._terminate_direct_child(
            process,
            grace_seconds=grace_seconds,
        ),
    )


def test_smoke_model_allowlist_matches_runtime_configuration() -> None:
    assert ALLOWED_QWENCLOUD_SMOKE_MODELS == ACCEPTED_QWENCLOUD_MODELS


def test_manifests_keep_exactly_one_active_non_idle_instance_per_function() -> None:
    manifest = (Path(__file__).parents[1] / "deploy" / "s.yaml").read_text(
        encoding="utf-8"
    )
    target, controller = manifest.split("  praxis:\n", 1)

    active_capacity = (
        "      provisionConfig:\n"
        "        alwaysAllocateCPU: true\n"
        "        alwaysAllocateGPU: false\n"
        "        defaultTarget: 1\n"
        "        scheduledActions: []\n"
        "        targetTrackingPolicies: []\n"
    )
    for function in (target, controller):
        assert "      instanceConcurrency: 1\n" in function
        assert (
            "      concurrencyConfig:\n"
            "        reservedConcurrency: 1\n"
        ) in function
        assert active_capacity in function
        assert "      scalingConfig:\n" not in function


def test_controller_manifest_uses_dedicated_role_and_tablestore_runtime_config() -> None:
    manifest = (Path(__file__).parents[1] / "deploy" / "s.yaml").read_text(
        encoding="utf-8"
    )
    target, controller = manifest.split("  praxis:\n", 1)

    assert "role: ${env('FC_EXECUTION_ROLE_ARN')}" in controller
    assert "role: ${env('FC_EXECUTION_ROLE_ARN')}" not in target
    for name in (
        "MEMORY_BACKEND",
        "MEMORY_SIMILARITY_THRESHOLD",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIM",
        "TABLESTORE_ENDPOINT",
        "TABLESTORE_INSTANCE",
    ):
        assert f"{name}: ${{env('{name}')}}" in controller
    assert "ALIBABA_CLOUD_ACCESS_KEY_ID:" not in controller
    assert "ALIBABA_CLOUD_ACCESS_KEY_SECRET:" not in controller
    assert "ALIBABA_CLOUD_SECURITY_TOKEN:" not in controller


def test_target_bootstrap_manifest_matches_final_isolated_target_resource() -> None:
    repository = Path(__file__).parents[1]
    final_manifest = (repository / "deploy" / "s.yaml").read_text(encoding="utf-8")
    bootstrap_manifest = (repository / "deploy" / "target.s.yaml").read_text(
        encoding="utf-8"
    )

    final_target = final_manifest.split("resources:\n", 1)[1].split(
        "\n  praxis:\n",
        1,
    )[0]
    bootstrap_target = bootstrap_manifest.split("resources:\n", 1)[1]

    assert bootstrap_target.strip() == final_target.strip()
    assert "        APP_ENV: production\n" in bootstrap_target


def test_approval_body_limit_is_part_of_the_fc_deployment_contract() -> None:
    repository = Path(__file__).parents[1]
    example_environment = (repository / ".env.example").read_text(encoding="utf-8")
    manifest = (repository / "deploy" / "s.yaml").read_text(encoding="utf-8")
    controller = manifest.split("  praxis:\n", 1)[1]

    assert "MAX_APPROVAL_BODY_BYTES=16384" in example_environment
    assert "MAX_APPROVAL_BODY_BYTES" in REQUIRED_DEPLOYMENT_VARIABLES
    assert (
        "MAX_APPROVAL_BODY_BYTES: ${env('MAX_APPROVAL_BODY_BYTES')}" in controller
    )


def test_operator_token_is_part_of_the_controller_deployment_contract() -> None:
    repository = Path(__file__).parents[1]
    manifest = (repository / "deploy" / "s.yaml").read_text(encoding="utf-8")
    controller = manifest.split("  praxis:\n", 1)[1]
    target = manifest.split("  praxis:\n", 1)[0]

    assert "PRAXIS_OPERATOR_TOKEN" in REQUIRED_DEPLOYMENT_VARIABLES
    assert (
        "PRAXIS_OPERATOR_TOKEN: ${env('PRAXIS_OPERATOR_TOKEN')}" in controller
    )
    assert "PRAXIS_OPERATOR_TOKEN" not in target


def test_read_dotenv_handles_quotes_and_inline_comments(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PLAIN=value\nQUOTED='value # retained'\nCOMMENTED=value # removed\n",
        encoding="utf-8",
    )

    assert _read_dotenv(env_file) == {
        "PLAIN": "value",
        "QUOTED": "value # retained",
        "COMMENTED": "value",
    }


def test_child_environment_prefers_repository_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(f"{name}=from-file" for name in REQUIRED_DEPLOYMENT_VARIABLES),
        encoding="utf-8",
    )

    environment = _child_environment(
        tmp_path,
        {"PRIMARY_MODEL": "from-process"},
    )

    assert environment["PRIMARY_MODEL"] == "from-file"
    assert all(environment[name] for name in REQUIRED_DEPLOYMENT_VARIABLES)


def test_child_environment_rotated_provider_keys_override_stale_process_values(
    tmp_path: Path,
) -> None:
    file_values = {
        name: f"file-{name.lower()}" for name in REQUIRED_DEPLOYMENT_VARIABLES
    }
    file_values["DASHSCOPE_API_KEY"] = "rotated-dashscope-key"
    file_values["OPENROUTER_API_KEY"] = "rotated-openrouter-key"
    (tmp_path / ".env").write_text(
        "\n".join(f"{name}={value}" for name, value in file_values.items()),
        encoding="utf-8",
    )

    environment = _child_environment(
        tmp_path,
        {
            "DASHSCOPE_API_KEY": "stale-dashscope-key",
            "OPENROUTER_API_KEY": "stale-openrouter-key",
        },
    )

    assert environment["DASHSCOPE_API_KEY"] == "rotated-dashscope-key"
    assert environment["OPENROUTER_API_KEY"] == "rotated-openrouter-key"


def test_child_environment_reports_names_not_values(tmp_path: Path) -> None:
    secret_sentinel = "must-not-appear"
    (tmp_path / ".env").write_text(
        f"DASHSCOPE_API_KEY={secret_sentinel}\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError) as caught:
        _child_environment(tmp_path, {})

    rendered = str(caught.value)
    assert "OPENROUTER_API_KEY" in rendered
    assert secret_sentinel not in rendered


def test_final_environment_requires_target_url_but_bootstrap_requires_only_token(
    tmp_path: Path,
) -> None:
    token = "bootstrap-secret-sentinel"
    (tmp_path / ".env").write_text(
        f"PRAXIS_DEMO_TARGET_TOKEN={token}\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="PRAXIS_DEMO_TARGET_URL") as captured:
        _child_environment(tmp_path, {})

    bootstrap = _bootstrap_target_environment(tmp_path, {})

    assert bootstrap["PRAXIS_DEMO_TARGET_TOKEN"] == token
    assert token not in str(captured.value)


def test_bootstrap_environment_keeps_only_launcher_and_deploy_credentials(
    tmp_path: Path,
) -> None:
    token = "bootstrap-secret-sentinel"
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                f"PRAXIS_DEMO_TARGET_TOKEN={token}",
                "DASHSCOPE_API_KEY=provider-secret-must-not-pass",
                "OPENROUTER_API_KEY=fallback-secret-must-not-pass",
                "WEBHOOK_SIGNING_SECRET=webhook-secret-must-not-pass",
                "TABLESTORE_ENDPOINT=https://unneeded.example.com",
                "TABLESTORE_INSTANCE=unneeded-instance",
            )
        ),
        encoding="utf-8",
    )
    base = {
        "PATH": "launcher-path",
        "APPDATA": "serverless-config-home",
        "ALIBABA_CLOUD_ACCESS_KEY_ID": "deploy-key-id",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "deploy-key-secret",
        "ALIBABA_CLOUD_SECURITY_TOKEN": "deploy-security-token",
        "UNRELATED": "must-not-pass",
    }

    environment = _bootstrap_target_environment(tmp_path, base)

    assert environment == {
        "PATH": "launcher-path",
        "APPDATA": "serverless-config-home",
        "ALIBABA_CLOUD_ACCESS_KEY_ID": "deploy-key-id",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "deploy-key-secret",
        "ALIBABA_CLOUD_SECURITY_TOKEN": "deploy-security-token",
        "PRAXIS_DEMO_TARGET_TOKEN": token,
    }


@pytest.mark.parametrize(
    ("path_results", "expected"),
    (
        ({"s": r"C:\\tools\\s.exe", "s.cmd": r"C:\\tools\\s.cmd"}, r"C:\\tools\\s.exe"),
        ({"s": None, "s.cmd": r"C:\\tools\\s.cmd"}, r"C:\\tools\\s.cmd"),
    ),
)
def test_find_serverless_cli_prefers_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_results: dict[str, str | None],
    expected: str,
) -> None:
    npm_dir = tmp_path / "npm"
    npm_dir.mkdir()
    (npm_dir / "s.cmd").write_text("fallback", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(fc.shutil, "which", path_results.get)

    assert _find_serverless_cli() == expected


def test_find_serverless_cli_uses_existing_per_user_npm_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    npm_dir = tmp_path / "npm"
    npm_dir.mkdir()
    npm_executable = npm_dir / "s.cmd"
    npm_executable.write_text("fallback", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(fc.shutil, "which", lambda _name: None)

    assert _find_serverless_cli() == str(npm_executable)


def test_find_serverless_cli_reports_missing_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    npm_dir = tmp_path / "npm"
    npm_dir.mkdir()
    (npm_dir / "s.cmd").mkdir()
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(fc.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="not installed or not on PATH"):
        _find_serverless_cli()


def test_only_approved_serverless_actions_are_constructed(tmp_path: Path) -> None:
    template = tmp_path / "s.yaml"

    assert _command("verify", "s", template) == ["s", "verify", "-t", str(template)]
    assert _command("deploy", "s", template) == [
        "s",
        "deploy",
        "-t",
        str(template),
        "--use-local",
        "-y",
    ]
    assert _command("bootstrap-target-verify", "s", template) == [
        "s",
        "verify",
        "-t",
        str(template),
    ]
    assert _command("bootstrap-target-deploy", "s", template) == [
        "s",
        "deploy",
        "-t",
        str(template),
        "--use-local",
        "-y",
    ]
    assert _command("instances", "s", template) == [
        "s",
        "instance",
        "list",
        "-t",
        str(template),
        "-o",
        "json",
        "--silent",
    ]
    assert _command("target-instances", "s", template) == [
        "s",
        "instance",
        "list",
        "-t",
        str(template),
        "-o",
        "json",
        "--silent",
    ]
    assert _command("smoke", "s", template, "c-safe-instance") == [
        "s",
        "instance",
        "exec",
        "-t",
        str(template),
        "--instance-id",
        "c-safe-instance",
        "--shell",
        "/bin/bash",
        "--cmd",
        "cd /code && /var/fc/lang/python3.10/bin/python3 -m app.m0_smoke",
    ]
    memory_command = _command("memory-smoke", "s", template, "c-safe-instance")
    assert memory_command[:8] == [
        "s",
        "instance",
        "exec",
        "-t",
        str(template),
        "--instance-id",
        "c-safe-instance",
        "--shell",
    ]
    assert "build_memory_backend" in memory_command[-1]
    assert "ensure_ready" in memory_command[-1]
    assert "ALIBABA_CLOUD_ACCESS_KEY" not in memory_command[-1]
    with pytest.raises(RuntimeError, match="valid --instance-id"):
        _command("smoke", "s", template, "bad instance")
    with pytest.raises(RuntimeError, match="Unsupported"):
        _command("preview", "s", template)


def test_redaction_removes_all_sensitive_environment_values() -> None:
    environment = {
        "DASHSCOPE_API_KEY": "dashscope-secret-sentinel",
        "OPENROUTER_API_KEY": "openrouter-secret-sentinel",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "ram-secret-sentinel",
        "PRIMARY_MODEL": "qwen3.7-max",
    }
    output = " ".join(environment.values())

    redacted = _redact_output(output, environment)

    assert "dashscope-secret-sentinel" not in redacted
    assert "openrouter-secret-sentinel" not in redacted
    assert "ram-secret-sentinel" not in redacted
    assert "qwen3.7-max" in redacted


def test_deploy_summary_is_allowlisted() -> None:
    raw = """
region: ap-southeast-1
environmentVariables:
  DASHSCOPE_API_KEY: should-never-be-copied
functionName: praxis-api
url:
  system_url: https://praxis-api-example.ap-southeast-1.fcapp.run
"""

    assert _safe_summary("deploy", raw, 0) == {
        "action": "deploy",
        "ok": True,
        "exit_code": 0,
        "region": "ap-southeast-1",
        "function_name": "praxis-api",
        "url": "https://praxis-api-example.ap-southeast-1.fcapp.run",
    }


def test_bootstrap_deploy_summary_exposes_only_target_identity_and_url() -> None:
    secret_sentinel = "target-token-must-not-escape"
    raw = f"""
region: ap-southeast-1
environmentVariables:
  PRAXIS_DEMO_TARGET_TOKEN: {secret_sentinel}
functionName: praxis-demo-target
url:
  system_url: https://praxis-demo-target-example.ap-southeast-1.fcapp.run
"""

    summary = _safe_summary("bootstrap-target-deploy", raw, 0)

    assert summary == {
        "action": "bootstrap-target-deploy",
        "ok": True,
        "exit_code": 0,
        "region": "ap-southeast-1",
        "function_name": "praxis-demo-target",
        "url": "https://praxis-demo-target-example.ap-southeast-1.fcapp.run",
    }
    assert secret_sentinel not in repr(summary)


@pytest.mark.parametrize(
    ("raw", "expected_ids"),
    (
        ('{"praxis-api":{"instances":[]}}', []),
        (
            '{"praxis-api":{"instances":['
            '{"instanceId":"c-second"},{"instanceId":"c-first"},'
            '{"instanceId":"c-first"}]}}',
            ["c-first", "c-second"],
        ),
    ),
)
def test_instance_list_summary_accepts_recognized_json_shapes(
    raw: str,
    expected_ids: list[str],
) -> None:
    assert _safe_summary("instances", raw, 0) == {
        "action": "instances",
        "ok": True,
        "exit_code": 0,
        "instance_ids": expected_ids,
    }


@pytest.mark.parametrize(
    "raw",
    (
        "not-json",
        "{}",
        '{"instances":"not-a-list"}',
        '{"error":{"instanceId":"c-fake"}}',
        '{"instances":[]}',
        '{"instances":[],"broken":{"instances":"not-a-list"}}',
        '{"instances":"not-a-list","instances":[]}',
        '{"praxis-api":{"instances":[]},"metadata":NaN}',
        (
            '{"praxis-api":{"instances":[]},'
            '"broken":{"instances":"not-a-list"}}'
        ),
        "[" * 1_100 + "[]" + "]" * 1_100,
    ),
)
def test_instance_list_summary_rejects_malformed_zero_exit_output(raw: str) -> None:
    assert _safe_summary("instances", raw, 0) == {
        "action": "instances",
        "ok": False,
        "exit_code": 1,
        "reason": "malformed_instance_list_output",
    }


def test_instance_list_run_promotes_malformed_zero_exit_to_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_windows_containment: None,
) -> None:
    class MalformedSuccess:
        returncode = 0

        def communicate(self, *, timeout: float | None = None) -> tuple[str, None]:
            assert timeout == fc.COMMAND_DEADLINE_SECONDS["instances"]
            return "successful transport without JSON", None

    monkeypatch.setattr(fc, "_find_serverless_cli", lambda: "s")
    monkeypatch.setattr(
        fc,
        "_secret_free_environment",
        lambda: {"serverless_devs_config_home": str(tmp_path)},
    )
    monkeypatch.setattr(
        fc.subprocess,
        "Popen",
        lambda *_args, **_kwargs: MalformedSuccess(),
    )

    assert fc.run("instances") == 1
    assert json.loads(capsys.readouterr().out) == {
        "action": "instances",
        "ok": False,
        "exit_code": 1,
        "reason": "malformed_instance_list_output",
    }


def test_smoke_proof_returns_only_allowlisted_fields() -> None:
    sensitive_sentinel = "remote-response-body-must-not-leak"
    raw = (
        "transport banner\n"
        '{"ok":true,"provider":"qwencloud","model":"qwen3.7-max",'
        f'"reason":"{sensitive_sentinel}"}}\n'
    )

    proof = _safe_smoke_proof(raw)

    assert proof == {"provider": "qwencloud", "model": "qwen3.7-max"}
    assert sensitive_sentinel not in repr(proof)


def test_memory_smoke_proof_returns_only_allowlisted_fields() -> None:
    sentinel = "schema-response-must-not-leak"
    raw = (
        "remote banner\n"
        '{"ok":true,"backend":"tablestore",'
        '"embedding_model":"text-embedding-v4",'
        f'"embedding_dimension":1024,"detail":"{sentinel}"}}\n'
    )

    proof = _safe_memory_smoke_proof(raw)

    assert proof == {
        "backend": "tablestore",
        "embedding_model": "text-embedding-v4",
        "embedding_dimension": 1024,
    }
    assert sentinel not in repr(proof)


@pytest.mark.parametrize(
    "raw",
    (
        '{"ok":false,"provider":"qwencloud","model":"qwen3.7-max"}',
        '{"ok":1,"provider":"qwencloud","model":"qwen3.7-max"}',
        '{"ok":true,"provider":"openrouter","model":"qwen3.7-max"}',
        '{"ok":true,"provider":"qwencloud","model":"not-allowed"}',
        '{"ok":true,"provider":"qwencloud","model":"qwen3.7-max"',
        "transport completed without a proof",
    ),
)
def test_smoke_proof_rejects_invalid_or_missing_inner_proof(raw: str) -> None:
    assert _safe_smoke_proof(raw) is None


@pytest.mark.parametrize(
    ("transport_code", "inner_output", "expected_code"),
    (
        (
            0,
            '{"ok":true,"provider":"qwencloud","model":"qwen3.7-max"}\n',
            0,
        ),
        (
            0,
            '{"ok":false,"provider":"qwencloud","model":"qwen3.7-max"}\n',
            1,
        ),
        (0, "{malformed-json}\n", 1),
        (0, "transport only\n", 1),
        (
            7,
            '{"ok":true,"provider":"qwencloud","model":"qwen3.7-max"}\n',
            7,
        ),
    ),
)
def test_smoke_run_gates_transport_on_inner_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_windows_containment: None,
    transport_code: int,
    inner_output: str,
    expected_code: int,
) -> None:
    captured_call: dict[str, object] = {}

    class FakeProcess:
        returncode = transport_code

        def communicate(self, *, timeout: float | None = None) -> tuple[str, None]:
            captured_call["timeout"] = timeout
            return inner_output, None

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        captured_call["command"] = command
        captured_call["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(fc, "_find_serverless_cli", lambda: "s")
    monkeypatch.setattr(
        fc,
        "_secret_free_environment",
        lambda: {"serverless_devs_config_home": str(tmp_path)},
    )
    monkeypatch.setattr(fc.subprocess, "Popen", fake_popen)

    result = fc.run("smoke", "c-safe-instance")
    rendered = capsys.readouterr().out
    summary = json.loads(rendered)

    assert result == expected_code
    assert summary["ok"] is (expected_code == 0)
    assert summary["exit_code"] == expected_code
    assert captured_call["kwargs"]["shell"] is False
    assert captured_call["kwargs"]["stderr"] is subprocess.STDOUT
    assert captured_call["kwargs"]["stdout"] is subprocess.PIPE
    assert captured_call["timeout"] == fc.COMMAND_DEADLINE_SECONDS["smoke"]
    assert inner_output.strip() not in rendered
    if expected_code == 0:
        assert summary["provider"] == "qwencloud"
        assert summary["model"] == "qwen3.7-max"
    else:
        assert "provider" not in summary
        assert "model" not in summary


def test_memory_smoke_runs_from_secret_free_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_windows_containment: None,
) -> None:
    captured_call: dict[str, object] = {}
    inner_output = (
        '{"ok":true,"backend":"tablestore",'
        '"embedding_model":"text-embedding-v4",'
        '"embedding_dimension":1024}\n'
    )

    class FakeProcess:
        returncode = 0

        def communicate(self, *, timeout: float | None = None) -> tuple[str, None]:
            captured_call["timeout"] = timeout
            return inner_output, None

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        captured_call["command"] = command
        captured_call["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(fc, "_find_serverless_cli", lambda: "s")
    monkeypatch.setattr(
        fc,
        "_secret_free_environment",
        lambda: {"serverless_devs_config_home": str(tmp_path)},
    )
    monkeypatch.setattr(fc.subprocess, "Popen", fake_popen)

    result = fc.run("memory-smoke", "c-safe-instance")
    summary = json.loads(capsys.readouterr().out)
    repository = Path(fc.__file__).resolve().parents[1]
    expected_workdir = repository / ".fc-cli-work"

    assert result == 0
    assert summary == {
        "action": "memory_smoke_transport",
        "ok": True,
        "exit_code": 0,
        "backend": "tablestore",
        "embedding_model": "text-embedding-v4",
        "embedding_dimension": 1024,
    }
    assert captured_call["kwargs"]["cwd"] == expected_workdir
    assert captured_call["kwargs"]["cwd"] != repository
    assert expected_workdir.is_dir()
    assert captured_call["kwargs"]["shell"] is False
    assert captured_call["timeout"] == fc.COMMAND_DEADLINE_SECONDS["memory-smoke"]


def test_trace_log_directory_is_scoped_to_serverless_logs(tmp_path: Path) -> None:
    target = _trace_log_directory(
        {"serverless_devs_config_home": str(tmp_path)},
        "praxis-safe-trace",
    )

    assert target == tmp_path / ".s" / "logs" / "praxis-safe-trace"


def test_target_instance_action_selects_isolated_target_template(
    tmp_path: Path,
) -> None:
    assert _template_for_action(tmp_path, "instances") == (
        tmp_path / "deploy" / "instance.s.yaml"
    )
    assert _template_for_action(tmp_path, "target-instances") == (
        tmp_path / "deploy" / "target-instance.s.yaml"
    )
    assert _template_for_action(tmp_path, "bootstrap-target-verify") == (
        tmp_path / "deploy" / "target.s.yaml"
    )
    assert _template_for_action(tmp_path, "bootstrap-target-deploy") == (
        tmp_path / "deploy" / "target.s.yaml"
    )


def test_secret_free_environment_removes_credentials() -> None:
    environment = _secret_free_environment(
        {
            "PATH": "safe",
            "DASHSCOPE_API_KEY": "secret",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "secret",
            "SESSION_TOKEN": "secret",
        }
    )

    assert environment == {"PATH": "safe"}


def test_serverless_actions_have_fixed_command_specific_deadlines() -> None:
    assert fc.COMMAND_DEADLINE_SECONDS == {
        "bootstrap-target-verify": 120.0,
        "bootstrap-target-deploy": 600.0,
        "verify": 120.0,
        "deploy": 600.0,
        "instances": 60.0,
        "target-instances": 60.0,
        "smoke": 120.0,
        "memory-smoke": 120.0,
    }


def test_child_processes_start_in_an_isolated_platform_group() -> None:
    options = fc._process_group_options()

    if os.name == "nt":
        assert options == {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }
    else:
        assert options == {"start_new_session": True}


def test_hung_child_is_terminated_and_timeout_output_is_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_windows_containment: None,
) -> None:
    secret_sentinel = "hung-child-output-must-not-escape"

    class HungProcess:
        returncode: int | None = None
        terminated = False
        killed = False
        communicate_timeouts: list[float | None] = []

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def communicate(self, *, timeout: float | None = None) -> tuple[str, None]:
            self.communicate_timeouts.append(timeout)
            if len(self.communicate_timeouts) == 1:
                raise subprocess.TimeoutExpired("s", timeout or 0)
            self.returncode = -15
            return secret_sentinel, None

    process = HungProcess()
    monkeypatch.setattr(fc.subprocess, "Popen", lambda *_args, **_kwargs: process)

    result = fc._run_bounded_command(
        ["s", "verify"],
        working_directory=tmp_path,
        environment={"PATH": "safe"},
        deadline_seconds=0.01,
    )

    assert result == fc._CommandResult(
        returncode=fc.TIMEOUT_EXIT_CODE,
        stdout="",
        timed_out=True,
    )
    assert process.terminated is True
    assert process.killed is False
    assert process.communicate_timeouts == [0.01, fc.TERMINATION_GRACE_SECONDS]
    assert secret_sentinel not in repr(result)


def test_termination_escalates_to_kill_when_child_ignores_grace_period(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_windows_containment: None,
) -> None:
    class UnresponsiveProcess:
        returncode: int | None = None
        terminated = False
        killed = False
        waited: float | None = None
        communicate_timeouts: list[float | None] = []

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def wait(self, *, timeout: float | None = None) -> int:
            self.waited = timeout
            self.returncode = -9
            return self.returncode

        def communicate(self, *, timeout: float | None = None) -> tuple[str, None]:
            self.communicate_timeouts.append(timeout)
            raise subprocess.TimeoutExpired("s", timeout or 0)

    process = UnresponsiveProcess()
    monkeypatch.setattr(fc.subprocess, "Popen", lambda *_args, **_kwargs: process)

    result = fc._run_bounded_command(
        ["s", "deploy"],
        working_directory=tmp_path,
        environment={"PATH": "safe"},
        deadline_seconds=0.01,
    )

    assert result.timed_out is True
    assert process.terminated is True
    assert process.killed is True
    assert process.waited == fc.TERMINATION_GRACE_SECONDS
    assert process.communicate_timeouts == [
        0.01,
        fc.TERMINATION_GRACE_SECONDS,
        fc.TERMINATION_GRACE_SECONDS,
    ]


def _process_is_running(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists.
                return False
            raise OSError(error, "OpenProcess failed")
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                raise OSError(ctypes.get_last_error(), "GetExitCodeProcess failed")
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _force_terminate_test_process(pid: int) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
        ]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x0001, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:
                return
            raise OSError(error, "OpenProcess failed during test cleanup")
        try:
            if not kernel32.TerminateProcess(handle, 1):
                raise OSError(
                    ctypes.get_last_error(),
                    "TerminateProcess failed during test cleanup",
                )
        finally:
            kernel32.CloseHandle(handle)
        return
    try:
        os.kill(pid, fc.signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_timeout_terminates_real_parent_and_descendant_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_pid_file = tmp_path / "processes.pid"
    child_program = "import time; time.sleep(60)"
    parent_program = (
        "import os,pathlib,subprocess,sys,time;"
        "kwargs=({'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP} "
        "if sys.platform=='win32' else {});"
        f"child=subprocess.Popen([sys.executable,'-c',{child_program!r}],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL,**kwargs);"
        f"pathlib.Path({str(process_pid_file)!r}).write_text("
        "f'{os.getpid()}:{child.pid}',encoding='ascii');"
        "time.sleep(60)"
    )
    monkeypatch.setattr(fc, "TERMINATION_GRACE_SECONDS", 0.5)
    parent_pid: int | None = None
    child_pid: int | None = None

    try:
        started = time.monotonic()
        result = fc._run_bounded_command(
            [sys.executable, "-c", parent_program],
            working_directory=tmp_path,
            environment=dict(os.environ),
            deadline_seconds=2.0,
        )
        elapsed = time.monotonic() - started
        assert result.timed_out is True
        assert elapsed < 5.0
        assert process_pid_file.is_file()
        parent_pid, child_pid = map(
            int,
            process_pid_file.read_text(encoding="ascii").split(":"),
        )

        deadline = time.monotonic() + 5.0
        while (
            _process_is_running(parent_pid) or _process_is_running(child_pid)
        ) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _process_is_running(parent_pid) is False
        assert _process_is_running(child_pid) is False
    finally:
        for process_id in (child_pid, parent_pid):
            if process_id is not None and _process_is_running(process_id):
                _force_terminate_test_process(process_id)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
def test_windows_job_terminates_descendant_spawned_after_timeout_cleanup_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_file = tmp_path / "release-late-child"
    process_pid_file = tmp_path / "late-processes.pid"
    child_program = "import time; time.sleep(60)"
    parent_program = "\n".join(
        (
            "import os, pathlib, subprocess, sys, time",
            f"release = pathlib.Path({str(release_file)!r})",
            "while not release.exists():",
            "    time.sleep(0.005)",
            "child = subprocess.Popen(",
            f"    [sys.executable, '-c', {child_program!r}],",
            "    stdin=subprocess.DEVNULL,",
            "    stdout=subprocess.DEVNULL,",
            "    stderr=subprocess.DEVNULL,",
            "    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,",
            ")",
            f"pid_path = pathlib.Path({str(process_pid_file)!r})",
            "pid_temp = pid_path.with_suffix('.tmp')",
            "pid_temp.write_text(f'{os.getpid()}:{child.pid}', encoding='ascii')",
            "pid_temp.replace(pid_path)",
            "time.sleep(60)",
        )
    )
    original_terminate = fc._terminate_windows_job_tree
    monkeypatch.setattr(fc, "TERMINATION_GRACE_SECONDS", 1.0)

    def terminate_after_late_spawn(
        process: subprocess.Popen[str],
        job_handle: object,
        *,
        grace_seconds: float,
    ) -> None:
        release_file.write_text("release", encoding="ascii")
        deadline = time.monotonic() + 2.0
        while not process_pid_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        original_terminate(
            process,
            job_handle,
            grace_seconds=grace_seconds,
        )

    monkeypatch.setattr(fc, "_terminate_windows_job_tree", terminate_after_late_spawn)
    parent_pid: int | None = None
    child_pid: int | None = None
    try:
        started = time.monotonic()
        result = fc._run_bounded_command(
            [sys.executable, "-c", parent_program],
            working_directory=tmp_path,
            environment=dict(os.environ),
            deadline_seconds=0.1,
        )
        elapsed = time.monotonic() - started

        assert result.timed_out is True
        assert elapsed < 5.0
        assert process_pid_file.is_file()
        parent_pid, child_pid = map(
            int,
            process_pid_file.read_text(encoding="ascii").split(":"),
        )
        assert _process_is_running(parent_pid) is False
        assert _process_is_running(child_pid) is False
    finally:
        for process_id in (child_pid, parent_pid):
            if process_id is not None and _process_is_running(process_id):
                _force_terminate_test_process(process_id)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
def test_windows_job_assignment_failure_kills_exact_parent_and_is_fixed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[str]] = []

    def capturing_popen(*args: object, **kwargs: object) -> subprocess.Popen[str]:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    def fail_assignment(_job: object, _process: subprocess.Popen[str]) -> None:
        raise OSError("forced assignment failure")

    monkeypatch.setattr(fc.subprocess, "Popen", capturing_popen)
    monkeypatch.setattr(fc, "_assign_process_to_windows_job", fail_assignment)
    monkeypatch.setattr(fc, "TERMINATION_GRACE_SECONDS", 0.5)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="^Windows process containment failed$"):
        fc._run_bounded_command(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            working_directory=tmp_path,
            environment=dict(os.environ),
            deadline_seconds=1.0,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert len(processes) == 1
    assert processes[0].poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
def test_windows_job_termination_error_closes_job_and_kills_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[str]] = []

    def capturing_popen(*args: object, **kwargs: object) -> subprocess.Popen[str]:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(fc.subprocess, "Popen", capturing_popen)
    monkeypatch.setattr(
        fc,
        "_terminate_windows_job",
        lambda _job: (_ for _ in ()).throw(OSError("forced termination failure")),
    )
    monkeypatch.setattr(fc, "TERMINATION_GRACE_SECONDS", 0.5)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="^Windows process cleanup failed$"):
        fc._run_bounded_command(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            working_directory=tmp_path,
            environment=dict(os.environ),
            deadline_seconds=0.05,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert len(processes) == 1
    assert processes[0].poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
def test_windows_job_close_failure_attempts_explicit_termination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = object()
    close_calls: list[object] = []
    termination_calls: list[object] = []

    class CompletedProcess:
        returncode = 0

        def communicate(self, *, timeout: float | None = None) -> tuple[str, None]:
            return "", None

    def fail_close(job_handle: object) -> None:
        close_calls.append(job_handle)
        raise OSError("forced close failure")

    monkeypatch.setattr(fc, "_create_windows_job", lambda: job)
    monkeypatch.setattr(
        fc,
        "_assign_process_to_windows_job",
        lambda _job, _process: None,
    )
    monkeypatch.setattr(fc, "_close_windows_job", fail_close)
    monkeypatch.setattr(
        fc,
        "_terminate_windows_job",
        lambda job_handle: termination_calls.append(job_handle),
    )
    monkeypatch.setattr(
        fc.subprocess,
        "Popen",
        lambda *_args, **_kwargs: CompletedProcess(),
    )

    with pytest.raises(RuntimeError, match="^Windows process cleanup failed$"):
        fc._run_bounded_command(
            ["s", "verify"],
            working_directory=tmp_path,
            environment={"PATH": "safe"},
            deadline_seconds=0.01,
        )

    assert close_calls == [job, job]
    assert termination_calls == [job]


@pytest.mark.parametrize("returncode", (0, 7))
def test_trace_cleanup_runs_after_normal_success_and_child_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_windows_containment: None,
    returncode: int,
) -> None:
    trace_paths: list[Path] = []

    class CompletedProcess:
        def __init__(self) -> None:
            self.returncode = returncode

        def communicate(self, *, timeout: float | None = None) -> tuple[str, None]:
            assert timeout == fc.COMMAND_DEADLINE_SECONDS["instances"]
            return (
                '{"praxis-api":{"instances":['
                '{"instanceId":"c-safe-instance"}]}}',
                None,
            )

    def fake_popen(_command: list[str], **kwargs: object) -> CompletedProcess:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        trace_path = (
            tmp_path
            / ".s"
            / "logs"
            / str(environment["serverless_devs_traceid"])
        )
        trace_path.mkdir(parents=True)
        trace_paths.append(trace_path)
        return CompletedProcess()

    monkeypatch.setattr(fc, "_find_serverless_cli", lambda: "s")
    monkeypatch.setattr(
        fc,
        "_secret_free_environment",
        lambda: {"serverless_devs_config_home": str(tmp_path)},
    )
    monkeypatch.setattr(fc.subprocess, "Popen", fake_popen)

    assert fc.run("instances") == returncode
    json.loads(capsys.readouterr().out)
    assert len(trace_paths) == 1
    assert trace_paths[0].exists() is False


def test_trace_cleanup_runs_when_child_start_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_windows_containment: None,
) -> None:
    trace_paths: list[Path] = []

    def failed_popen(_command: list[str], **kwargs: object) -> None:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        trace_path = (
            tmp_path
            / ".s"
            / "logs"
            / str(environment["serverless_devs_traceid"])
        )
        trace_path.mkdir(parents=True)
        trace_paths.append(trace_path)
        raise OSError("safe test launch failure")

    monkeypatch.setattr(fc, "_find_serverless_cli", lambda: "s")
    monkeypatch.setattr(
        fc,
        "_secret_free_environment",
        lambda: {"serverless_devs_config_home": str(tmp_path)},
    )
    monkeypatch.setattr(fc.subprocess, "Popen", failed_popen)

    with pytest.raises(OSError, match="safe test launch failure"):
        fc.run("instances")

    assert len(trace_paths) == 1
    assert trace_paths[0].exists() is False


def test_trace_cleanup_runs_after_keyboard_interrupt_and_terminates_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_windows_containment: None,
) -> None:
    trace_paths: list[Path] = []

    class InterruptedProcess:
        returncode: int | None = None
        interrupted = False
        terminated = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True

        def communicate(self, *, timeout: float | None = None) -> tuple[str, None]:
            if not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            self.returncode = -15
            return "", None

    process = InterruptedProcess()

    def fake_popen(_command: list[str], **kwargs: object) -> InterruptedProcess:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        trace_path = (
            tmp_path
            / ".s"
            / "logs"
            / str(environment["serverless_devs_traceid"])
        )
        trace_path.mkdir(parents=True)
        trace_paths.append(trace_path)
        return process

    monkeypatch.setattr(fc, "_find_serverless_cli", lambda: "s")
    monkeypatch.setattr(
        fc,
        "_secret_free_environment",
        lambda: {"serverless_devs_config_home": str(tmp_path)},
    )
    monkeypatch.setattr(fc.subprocess, "Popen", fake_popen)

    with pytest.raises(KeyboardInterrupt):
        fc.run("instances")

    assert process.terminated is True
    assert len(trace_paths) == 1
    assert trace_paths[0].exists() is False


def test_deploy_timeout_is_secret_safe_unknown_release_state_and_scrubs_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_windows_containment: None,
) -> None:
    secret_sentinel = "partial-deploy-secret-must-not-escape"
    trace_paths: list[Path] = []

    class TimedOutDeploy:
        returncode: int | None = None
        terminated = False
        first_communication = True

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True

        def communicate(self, *, timeout: float | None = None) -> tuple[str, None]:
            if self.first_communication:
                self.first_communication = False
                raise subprocess.TimeoutExpired(
                    "s deploy",
                    timeout or 0,
                    output=secret_sentinel,
                )
            self.returncode = -15
            return secret_sentinel, None

    process = TimedOutDeploy()

    def fake_popen(_command: list[str], **kwargs: object) -> TimedOutDeploy:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        trace_path = (
            tmp_path
            / ".s"
            / "logs"
            / str(environment["serverless_devs_traceid"])
        )
        trace_path.mkdir(parents=True)
        (trace_path / "trace.log").write_text(secret_sentinel, encoding="utf-8")
        trace_paths.append(trace_path)
        return process

    monkeypatch.setattr(fc, "_find_serverless_cli", lambda: "s")
    monkeypatch.setattr(
        fc,
        "_child_environment",
        lambda _repository: {
            "serverless_devs_config_home": str(tmp_path),
            "DASHSCOPE_API_KEY": secret_sentinel,
        },
    )
    monkeypatch.setattr(fc.subprocess, "Popen", fake_popen)

    assert fc.run("deploy") == fc.TIMEOUT_EXIT_CODE
    rendered = capsys.readouterr().out
    summary = json.loads(rendered)

    assert summary == {
        "action": "deploy",
        "ok": False,
        "exit_code": 124,
        "timed_out": True,
        "deadline_seconds": 600.0,
        "release_state": "unknown",
        "retry_safe": False,
        "required_action": "reconcile_remote_state_before_retry",
    }
    assert secret_sentinel not in rendered
    assert process.terminated is True
    assert len(trace_paths) == 1
    assert trace_paths[0].exists() is False


def test_read_only_timeout_does_not_claim_unknown_release_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_windows_containment: None,
) -> None:
    class TimedOutRead:
        returncode: int | None = None
        first_communication = True

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            return None

        def communicate(self, *, timeout: float | None = None) -> tuple[str, None]:
            if self.first_communication:
                self.first_communication = False
                raise subprocess.TimeoutExpired("s instance list", timeout or 0)
            self.returncode = -15
            return "", None

    monkeypatch.setattr(fc, "_find_serverless_cli", lambda: "s")
    monkeypatch.setattr(
        fc,
        "_secret_free_environment",
        lambda: {"serverless_devs_config_home": str(tmp_path)},
    )
    monkeypatch.setattr(
        fc.subprocess,
        "Popen",
        lambda *_args, **_kwargs: TimedOutRead(),
    )

    assert fc.run("instances") == fc.TIMEOUT_EXIT_CODE
    summary = json.loads(capsys.readouterr().out)

    assert summary["timed_out"] is True
    assert summary["deadline_seconds"] == 60.0
    assert "release_state" not in summary
    assert "retry_safe" not in summary
    assert "required_action" not in summary
