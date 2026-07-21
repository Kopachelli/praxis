import json
from collections.abc import Mapping
from typing import Any

import pytest

from scripts import fc_role


class FakeCloudError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message)
        self.code = code


class FakeGateway:
    def __init__(self, account_id: str, instance: str) -> None:
        self.account_id = account_id
        self.instance = instance
        self.role: dict[str, Any] | None = None
        self.policy: dict[str, Any] | None = None
        self.attached = False
        self.extra_policies: list[dict[str, Any]] = []
        self.list_metadata: dict[str, Any] = {}
        self.actions: list[str] = []

    def call(self, action: str, query: Mapping[str, str]) -> dict[str, Any]:
        self.actions.append(action)
        if action == "GetRole":
            if self.role is None:
                raise FakeCloudError("EntityNotExist.Role")
            return {"body": {"Role": self.role}}
        if action == "CreateRole":
            self.role = {
                "Arn": f"acs:ram::{self.account_id}:role/{fc_role.ROLE_NAME}",
                "AssumeRolePolicyDocument": query["AssumeRolePolicyDocument"],
            }
            return {"body": {"Role": self.role}}
        if action == "GetPolicy":
            if self.policy is None:
                raise FakeCloudError("EntityNotExist.Policy")
            return {"body": {"Policy": self.policy}}
        if action == "CreatePolicy":
            self.policy = {
                "DefaultVersion": "v1",
                "DefaultPolicyVersion": {
                    "VersionId": "v1",
                    "PolicyDocument": query["PolicyDocument"],
                },
            }
            return {"body": {"Policy": self.policy}}
        if action == "GetPolicyVersion":
            assert self.policy is not None
            return {
                "body": {
                    "PolicyVersion": {
                        "PolicyDocument": self.policy["DefaultPolicyVersion"][
                            "PolicyDocument"
                        ]
                    }
                }
            }
        if action == "ListPoliciesForRole":
            expected = (
                [{"PolicyName": fc_role.POLICY_NAME, "PolicyType": "Custom"}]
                if self.attached
                else []
            )
            items = [*expected, *self.extra_policies]
            return {
                "body": {
                    "Policies": {"Policy": items},
                    **self.list_metadata,
                }
            }
        if action == "AttachPolicyToRole":
            self.attached = True
            return {"body": {}}
        raise AssertionError(f"unexpected action {action}")


def test_policy_is_exact_table_scoped_and_runtime_only() -> None:
    policy = fc_role._policy_document("1234567890123456", "praxis")
    statement = policy["Statement"][0]

    assert statement["Action"] == list(fc_role.TABLESTORE_ACTIONS)
    assert statement["Resource"] == [
        "acs:ots:ap-southeast-1:1234567890123456:instance/praxis/table/praxis_memory"
    ]
    assert not any("Create" in action or "Delete" in action for action in statement["Action"])


def test_trust_is_function_compute_only() -> None:
    assert fc_role._trust_document() == {
        "Statement": [
            {
                "Action": "sts:AssumeRole",
                "Effect": "Allow",
                "Principal": {"Service": ["fc.aliyuncs.com"]},
            }
        ],
        "Version": "1",
    }


def test_ensure_creates_verifies_and_attaches_dedicated_role() -> None:
    account_id = "1234567890123456"
    gateway = FakeGateway(account_id, "praxis")

    result = fc_role._ensure(gateway, account_id, "praxis")

    assert result == {
        "role_exists": True,
        "policy_exists": True,
        "policy_attached": True,
        "attached_policies_expected": [
            {
                "policy_type": "Custom",
                "policy_name": fc_role.POLICY_NAME,
            }
        ],
        "attached_policies_actual": [
            {
                "policy_type": "Custom",
                "policy_name": fc_role.POLICY_NAME,
            }
        ],
        "policy_attachment_set_matches": True,
        "unexpected_policy_attachments": False,
        "configuration_matches": True,
        "role_arn": f"acs:ram::{account_id}:role/{fc_role.ROLE_NAME}",
    }
    assert "CreateRole" in gateway.actions
    assert "CreatePolicy" in gateway.actions
    assert "AttachPolicyToRole" in gateway.actions


def test_ensure_is_read_before_write_and_idempotent() -> None:
    account_id = "1234567890123456"
    gateway = FakeGateway(account_id, "praxis")
    fc_role._ensure(gateway, account_id, "praxis")
    gateway.actions.clear()

    result = fc_role._ensure(gateway, account_id, "praxis")

    assert result["configuration_matches"] is True
    assert "CreateRole" not in gateway.actions
    assert "CreatePolicy" not in gateway.actions
    assert "AttachPolicyToRole" not in gateway.actions


def test_existing_policy_drift_fails_before_mutation() -> None:
    account_id = "1234567890123456"
    gateway = FakeGateway(account_id, "praxis")
    fc_role._ensure(gateway, account_id, "praxis")
    assert gateway.policy is not None
    gateway.policy["DefaultPolicyVersion"]["PolicyDocument"] = json.dumps(
        {"Version": "1", "Statement": []}
    )
    gateway.actions.clear()

    with pytest.raises(fc_role.ConfigurationDrift, match="runtime_policy_document_mismatch"):
        fc_role._ensure(gateway, account_id, "praxis")

    assert "CreateRole" not in gateway.actions
    assert "CreatePolicy" not in gateway.actions
    assert "AttachPolicyToRole" not in gateway.actions


@pytest.mark.parametrize(
    ("policy_type", "policy_name"),
    (
        ("System", "AdministratorAccess"),
        ("Custom", "UnexpectedRuntimeAccess"),
    ),
)
def test_inspect_rejects_any_unexpected_policy_attachment(
    policy_type: str,
    policy_name: str,
) -> None:
    account_id = "1234567890123456"
    gateway = FakeGateway(account_id, "praxis")
    fc_role._ensure(gateway, account_id, "praxis")
    gateway.extra_policies = [
        {
            "PolicyName": policy_name,
            "PolicyType": policy_type,
            "Description": "credential-like-response-field-must-not-escape",
        }
    ]

    result = fc_role._inspect(gateway, account_id, "praxis")

    actual = {
        (item["policy_type"], item["policy_name"])
        for item in result["attached_policies_actual"]
    }
    assert result["policy_attached"] is True
    assert result["policy_attachment_set_matches"] is False
    assert result["unexpected_policy_attachments"] is True
    assert result["configuration_matches"] is False
    assert actual == {
        ("Custom", fc_role.POLICY_NAME),
        (policy_type, policy_name),
    }
    assert "credential-like-response-field-must-not-escape" not in repr(result)


def test_ensure_refuses_to_mutate_role_with_unexpected_policy_attachment() -> None:
    account_id = "1234567890123456"
    gateway = FakeGateway(account_id, "praxis")
    fc_role._ensure(gateway, account_id, "praxis")
    gateway.extra_policies = [
        {"PolicyName": "AdministratorAccess", "PolicyType": "System"}
    ]
    gateway.actions.clear()

    with pytest.raises(
        fc_role.ConfigurationDrift,
        match="unexpected_role_policy_attachment",
    ):
        fc_role._ensure(gateway, account_id, "praxis")

    assert gateway.actions == ["GetRole", "GetPolicy", "ListPoliciesForRole"]


def test_inspect_operation_reports_failed_verification_for_extra_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = "1234567890123456"
    gateway = FakeGateway(account_id, "praxis")
    fc_role._ensure(gateway, account_id, "praxis")
    gateway.extra_policies = [
        {"PolicyName": "UnexpectedRuntimeAccess", "PolicyType": "Custom"}
    ]
    monkeypatch.setattr(
        fc_role,
        "_environment",
        lambda: {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "local-test-id",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "local-test-secret",
            "TABLESTORE_INSTANCE": "praxis",
        },
    )
    monkeypatch.setattr(
        fc_role,
        "_caller_account_id",
        lambda _credentials: account_id,
    )
    monkeypatch.setattr(fc_role, "_TeaRamGateway", lambda _credentials: gateway)

    result = fc_role._operate("inspect")

    assert result["ok"] is False
    assert result["configuration_matches"] is False
    assert result["policy_attachment_set_matches"] is False
    assert "local-test-secret" not in repr(result)


def test_malformed_attachment_is_redacted_and_fails_closed() -> None:
    account_id = "1234567890123456"
    gateway = FakeGateway(account_id, "praxis")
    fc_role._ensure(gateway, account_id, "praxis")
    sentinel = "credential-and-signature-must-not-escape"
    gateway.extra_policies = [
        {"PolicyName": f"invalid {sentinel}", "PolicyType": "System"}
    ]

    result = fc_role._inspect(gateway, account_id, "praxis")

    assert result["configuration_matches"] is False
    assert {
        "policy_type": "Invalid",
        "policy_name": "<redacted>",
    } in result["attached_policies_actual"]
    assert sentinel not in repr(result)


@pytest.mark.parametrize(
    "metadata",
    (
        {"IsTruncated": True, "Marker": "next-page"},
        {"IsTruncated": "true"},
        {"Marker": "unexpected-cursor"},
        {"Marker": {"opaque": "malformed-marker-secret"}},
    ),
)
def test_undocumented_attachment_truncation_fails_closed(
    metadata: dict[str, Any],
) -> None:
    account_id = "1234567890123456"
    gateway = FakeGateway(account_id, "praxis")
    fc_role._ensure(gateway, account_id, "praxis")
    gateway.list_metadata = metadata

    result = fc_role._inspect(gateway, account_id, "praxis")

    assert result["configuration_matches"] is False
    assert result["policy_attachment_set_matches"] is False
    assert result["unexpected_policy_attachments"] is True
    assert result["attached_policies_actual"] == [
        {"policy_type": "Invalid", "policy_name": "<redacted>"}
    ]
    assert "next-page" not in repr(result)
    assert "unexpected-cursor" not in repr(result)
    assert "malformed-marker-secret" not in repr(result)


def test_explicit_untruncated_attachment_response_remains_valid() -> None:
    account_id = "1234567890123456"
    gateway = FakeGateway(account_id, "praxis")
    fc_role._ensure(gateway, account_id, "praxis")
    gateway.list_metadata = {"IsTruncated": False, "Marker": ""}

    result = fc_role._inspect(gateway, account_id, "praxis")

    assert result["configuration_matches"] is True
    assert result["policy_attachment_set_matches"] is True


def test_safe_execute_discards_sdk_output_and_exception_message(capsys: pytest.CaptureFixture[str]) -> None:
    sentinel = "credential-and-signature-must-not-escape"

    def unsafe_operation() -> dict[str, Any]:
        print(sentinel)
        raise FakeCloudError("SignatureDoesNotMatch", sentinel)

    result = fc_role._safe_execute(unsafe_operation)
    captured = capsys.readouterr()

    assert result == {
        "ok": False,
        "reason": "fc_role_operation_failed",
        "error_type": "FakeCloudError",
        "error_code": "SignatureDoesNotMatch",
    }
    assert sentinel not in captured.out
    assert sentinel not in captured.err
    assert sentinel not in json.dumps(result)


def test_environment_reports_names_not_values(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = "must-not-be-rendered"
    monkeypatch.setattr(fc_role, "REPOSITORY_ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        f"ALIBABA_CLOUD_ACCESS_KEY_ID={sentinel}\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError) as caught:
        fc_role._environment({})

    assert "ALIBABA_CLOUD_ACCESS_KEY_SECRET" in str(caught.value)
    assert "TABLESTORE_INSTANCE" in str(caught.value)
    assert sentinel not in str(caught.value)
