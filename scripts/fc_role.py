"""Provision Praxis's least-privilege FC execution role without leaking SDK output.

This is deployment tooling, not application runtime. It emits only an allowlisted
JSON envelope and never prints Alibaba SDK exception messages or response bodies.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.agent.memory import MEMORY_TABLE  # noqa: E402

REGION = "ap-southeast-1"
RAM_ENDPOINT = "ram.aliyuncs.com"
STS_ENDPOINT = "sts.ap-southeast-1.aliyuncs.com"
ROLE_NAME = "praxis-fc-tablestore-role"
POLICY_NAME = "PraxisFcTablestoreRuntime"
ROLE_DESCRIPTION = "Praxis FC runtime access to its exact Tablestore memory table"
POLICY_DESCRIPTION = "Least-privilege Praxis memory runtime operations"
TABLESTORE_ACTIONS = (
    "ots:DescribeTable",
    "ots:ListSearchIndex",
    "ots:DescribeSearchIndex",
    "ots:PutRow",
    "ots:Search",
)
_SAFE_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")
_SAFE_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
_SAFE_POLICY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_POLICY_TYPES = frozenset({"Custom", "System"})
_EXPECTED_POLICY_IDENTITIES = frozenset({("Custom", POLICY_NAME)})
_INVALID_POLICY_IDENTITY = ("Invalid", "<redacted>")


@dataclass(frozen=True, slots=True)
class Credentials:
    access_key_id: str = field(repr=False)
    access_key_secret: str = field(repr=False)
    security_token: str = field(default="", repr=False)


class RamGateway(Protocol):
    def call(self, action: str, query: Mapping[str, str]) -> dict[str, Any]: ...


class ConfigurationDrift(RuntimeError):
    """A dedicated role or policy exists but differs from the accepted contract."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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


def _environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    values = dict(os.environ if base is None else base)
    values.update(_read_dotenv(REPOSITORY_ROOT / ".env"))
    required = (
        "ALIBABA_CLOUD_ACCESS_KEY_ID",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
        "TABLESTORE_INSTANCE",
    )
    missing = [name for name in required if not values.get(name, "").strip()]
    if missing:
        raise RuntimeError("Missing required deployment variables: " + ", ".join(missing))
    instance = values["TABLESTORE_INSTANCE"].strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", instance):
        raise ValueError("TABLESTORE_INSTANCE must be a lowercase Alibaba instance name")
    return values


def _trust_document() -> dict[str, Any]:
    return {
        "Statement": [
            {
                "Action": "sts:AssumeRole",
                "Effect": "Allow",
                "Principal": {"Service": ["fc.aliyuncs.com"]},
            }
        ],
        "Version": "1",
    }


def _policy_document(account_id: str, instance: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d{6,32}", account_id):
        raise ValueError("Alibaba account identity was not valid")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", instance):
        raise ValueError("TABLESTORE_INSTANCE must be lowercase")
    resource = (
        f"acs:ots:{REGION}:{account_id}:instance/{instance}/table/{MEMORY_TABLE}"
    )
    return {
        "Statement": [
            {
                "Action": list(TABLESTORE_ACTIONS),
                "Effect": "Allow",
                "Resource": [resource],
            }
        ],
        "Version": "1",
    }


def _document_text(document: Mapping[str, Any]) -> str:
    return json.dumps(document, separators=(",", ":"), sort_keys=True)


def _parsed_document(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(unquote(value))
    except (json.JSONDecodeError, UnicodeError):
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _response_body(response: Mapping[str, Any]) -> dict[str, Any]:
    body = response.get("body", response.get("Body", response))
    return dict(body) if isinstance(body, Mapping) else {}


def _error_code(error: BaseException) -> str | None:
    candidates: list[Any] = []
    for name in ("code", "error_code"):
        candidates.append(getattr(error, name, None))
    for name in ("get_code", "get_error_code"):
        getter = getattr(error, name, None)
        if callable(getter):
            try:
                candidates.append(getter())
            except Exception:
                pass
    data = getattr(error, "data", None)
    if isinstance(data, Mapping):
        candidates.extend((data.get("Code"), data.get("code")))
    for candidate in candidates:
        if isinstance(candidate, str) and _SAFE_CODE.fullmatch(candidate):
            return candidate
    return None


def _is_code(error: BaseException, *codes: str) -> bool:
    return _error_code(error) in codes


class _TeaRamGateway:
    def __init__(self, credentials: Credentials) -> None:
        from alibabacloud_tea_openapi.client import Client
        from alibabacloud_tea_openapi.models import Config

        self._client = Client(
            Config(
                access_key_id=credentials.access_key_id,
                access_key_secret=credentials.access_key_secret,
                security_token=credentials.security_token or None,
                endpoint=RAM_ENDPOINT,
                protocol="HTTPS",
                region_id=REGION,
                connect_timeout=5_000,
                read_timeout=10_000,
            )
        )

    def call(self, action: str, query: Mapping[str, str]) -> dict[str, Any]:
        from alibabacloud_tea_openapi import utils_models
        from darabonba.runtime import RuntimeOptions

        params = utils_models.Params(
            action=action,
            version="2015-05-01",
            protocol="HTTPS",
            pathname="/",
            method="POST",
            auth_type="AK",
            style="RPC",
            req_body_type="formData",
            body_type="json",
        )
        request = utils_models.OpenApiRequest(query=dict(query))
        runtime = RuntimeOptions(
            autoretry=False,
            max_attempts=1,
            connect_timeout=5_000,
            read_timeout=10_000,
        )
        response = self._client.call_api(params, request, runtime)
        return dict(response) if isinstance(response, Mapping) else {}


def _caller_account_id(credentials: Credentials) -> str:
    from alibabacloud_sts20150401.client import Client
    from alibabacloud_tea_openapi.models import Config
    from darabonba.runtime import RuntimeOptions

    client = Client(
        Config(
            access_key_id=credentials.access_key_id,
            access_key_secret=credentials.access_key_secret,
            security_token=credentials.security_token or None,
            endpoint=STS_ENDPOINT,
            protocol="HTTPS",
            region_id=REGION,
            connect_timeout=5_000,
            read_timeout=10_000,
        )
    )
    response = client.get_caller_identity_with_options(
        RuntimeOptions(
            autoretry=False,
            max_attempts=1,
            connect_timeout=5_000,
            read_timeout=10_000,
        )
    )
    account_id = getattr(getattr(response, "body", None), "account_id", None)
    if not isinstance(account_id, str) or not re.fullmatch(r"\d{6,32}", account_id):
        raise RuntimeError("Alibaba caller identity did not include a valid account ID")
    return account_id


def _get_or_none(
    gateway: RamGateway,
    action: str,
    query: Mapping[str, str],
    missing_code: str,
) -> dict[str, Any] | None:
    try:
        return _response_body(gateway.call(action, query))
    except Exception as exc:
        if _is_code(exc, missing_code):
            return None
        raise


def _role(gateway: RamGateway) -> dict[str, Any] | None:
    body = _get_or_none(
        gateway,
        "GetRole",
        {"RoleName": ROLE_NAME},
        "EntityNotExist.Role",
    )
    if body is None:
        return None
    value = body.get("Role")
    return dict(value) if isinstance(value, Mapping) else {}


def _policy(gateway: RamGateway) -> dict[str, Any] | None:
    body = _get_or_none(
        gateway,
        "GetPolicy",
        {"PolicyType": "Custom", "PolicyName": POLICY_NAME},
        "EntityNotExist.Policy",
    )
    if body is None:
        return None
    value = body.get("Policy")
    return dict(value) if isinstance(value, Mapping) else {}


def _policy_version_document(
    gateway: RamGateway,
    version_id: str,
) -> dict[str, Any] | None:
    body = _response_body(
        gateway.call(
            "GetPolicyVersion",
            {
                "PolicyType": "Custom",
                "PolicyName": POLICY_NAME,
                "VersionId": version_id,
            },
        )
    )
    version = body.get("PolicyVersion")
    if not isinstance(version, Mapping):
        return None
    return _parsed_document(
        version.get("PolicyDocument", version.get("Document"))
    )


def _default_policy_document(
    gateway: RamGateway,
    policy: Mapping[str, Any],
) -> dict[str, Any] | None:
    embedded = policy.get("DefaultPolicyVersion")
    if isinstance(embedded, Mapping):
        document = _parsed_document(
            embedded.get("PolicyDocument", embedded.get("Document"))
        )
        if document is not None:
            return document
        version_id = embedded.get("VersionId")
    else:
        version_id = policy.get(
            "DefaultVersion",
            policy.get("DefaultVersionId"),
        )
    if not isinstance(version_id, str) or not version_id:
        return None
    return _policy_version_document(gateway, version_id)


def _normalized_policy_identity(value: Any) -> tuple[str, str]:
    """Return only the non-secret identity fields needed for exact comparison."""

    if not isinstance(value, Mapping):
        return _INVALID_POLICY_IDENTITY
    policy_type = value.get("PolicyType")
    policy_name = value.get("PolicyName")
    if (
        not isinstance(policy_type, str)
        or policy_type not in _POLICY_TYPES
        or not isinstance(policy_name, str)
        or not _SAFE_POLICY_NAME.fullmatch(policy_name)
    ):
        return _INVALID_POLICY_IDENTITY
    return policy_type, policy_name


def _attached_policy_identities(gateway: RamGateway) -> frozenset[tuple[str, str]]:
    body = _response_body(
        gateway.call("ListPoliciesForRole", {"RoleName": ROLE_NAME})
    )
    # RAM 2015-05-01 currently documents ListPoliciesForRole as an
    # unpaginated operation. If the service ever returns truncation metadata,
    # treating the first page as complete could hide a privileged attachment;
    # fail the exact-set comparison instead of guessing an undocumented cursor
    # contract.
    is_truncated = body.get("IsTruncated")
    marker = body.get("Marker")
    if is_truncated not in (None, False) or marker not in (None, ""):
        return frozenset({_INVALID_POLICY_IDENTITY})
    policies = body.get("Policies", {})
    if isinstance(policies, Mapping):
        policies = policies.get("Policy", [])
    if isinstance(policies, Mapping):
        policies = [policies]
    if not isinstance(policies, list):
        return frozenset({_INVALID_POLICY_IDENTITY})
    return frozenset(_normalized_policy_identity(item) for item in policies)


def _policy_identity_diagnostics(
    identities: frozenset[tuple[str, str]],
) -> list[dict[str, str]]:
    return [
        {"policy_type": policy_type, "policy_name": policy_name}
        for policy_type, policy_name in sorted(identities)
    ]


def _attachment_diagnostics(
    actual: frozenset[tuple[str, str]],
) -> dict[str, Any]:
    return {
        "attached_policies_expected": _policy_identity_diagnostics(
            _EXPECTED_POLICY_IDENTITIES
        ),
        "attached_policies_actual": _policy_identity_diagnostics(actual),
        "policy_attachment_set_matches": actual == _EXPECTED_POLICY_IDENTITIES,
        "unexpected_policy_attachments": bool(
            actual - _EXPECTED_POLICY_IDENTITIES
        ),
    }


def _inspect(
    gateway: RamGateway,
    account_id: str,
    instance: str,
) -> dict[str, Any]:
    expected_role_arn = f"acs:ram::{account_id}:role/{ROLE_NAME}"
    expected_trust = _trust_document()
    expected_policy = _policy_document(account_id, instance)

    role = _role(gateway)
    policy = _policy(gateway)
    role_exists = role is not None
    policy_exists = policy is not None

    if role_exists:
        if role.get("Arn") != expected_role_arn:
            raise ConfigurationDrift("role_arn_mismatch")
        if _parsed_document(role.get("AssumeRolePolicyDocument")) != expected_trust:
            raise ConfigurationDrift("role_trust_policy_mismatch")

    if policy_exists:
        document = _default_policy_document(gateway, policy)
        if document is None:
            raise ConfigurationDrift("policy_default_version_missing")
        if document != expected_policy:
            raise ConfigurationDrift("runtime_policy_document_mismatch")

    attached_identities = (
        _attached_policy_identities(gateway) if role_exists else frozenset()
    )
    attachment_diagnostics = _attachment_diagnostics(attached_identities)
    policy_attached = bool(
        role_exists
        and policy_exists
        and _EXPECTED_POLICY_IDENTITIES.issubset(attached_identities)
    )
    result: dict[str, Any] = {
        "role_exists": role_exists,
        "policy_exists": policy_exists,
        "policy_attached": policy_attached,
        **attachment_diagnostics,
        "configuration_matches": (
            role_exists
            and policy_exists
            and attachment_diagnostics["policy_attachment_set_matches"]
        ),
    }
    if role_exists:
        result["role_arn"] = expected_role_arn
    return result


def _ensure(
    gateway: RamGateway,
    account_id: str,
    instance: str,
) -> dict[str, Any]:
    before = _inspect(gateway, account_id, instance)
    if before["unexpected_policy_attachments"]:
        raise ConfigurationDrift("unexpected_role_policy_attachment")
    if not before["role_exists"]:
        try:
            gateway.call(
                "CreateRole",
                {
                    "RoleName": ROLE_NAME,
                    "Description": ROLE_DESCRIPTION,
                    "AssumeRolePolicyDocument": _document_text(_trust_document()),
                },
            )
        except Exception as exc:
            if not _is_code(exc, "EntityAlreadyExists.Role"):
                raise
        # Re-read after both success and a create race.
        role = _role(gateway)
        if role is None:
            raise RuntimeError("Role creation did not become visible")

    if not before["policy_exists"]:
        try:
            gateway.call(
                "CreatePolicy",
                {
                    "PolicyName": POLICY_NAME,
                    "Description": POLICY_DESCRIPTION,
                    "PolicyDocument": _document_text(
                        _policy_document(account_id, instance)
                    ),
                },
            )
        except Exception as exc:
            if not _is_code(exc, "EntityAlreadyExists.Policy"):
                raise
        policy = _policy(gateway)
        if policy is None:
            raise RuntimeError("Policy creation did not become visible")

    middle = _inspect(gateway, account_id, instance)
    if middle["unexpected_policy_attachments"]:
        raise ConfigurationDrift("unexpected_role_policy_attachment")
    if not middle["policy_attached"]:
        try:
            gateway.call(
                "AttachPolicyToRole",
                {
                    "PolicyType": "Custom",
                    "PolicyName": POLICY_NAME,
                    "RoleName": ROLE_NAME,
                },
            )
        except Exception as exc:
            if not _is_code(exc, "EntityAlreadyExists.Role.Policy"):
                raise

    result = _inspect(gateway, account_id, instance)
    if not result["configuration_matches"]:
        raise RuntimeError("Dedicated role did not reach the required configuration")
    return result


def _operate(action: str) -> dict[str, Any]:
    env = _environment()
    credentials = Credentials(
        env["ALIBABA_CLOUD_ACCESS_KEY_ID"],
        env["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
        env.get("ALIBABA_CLOUD_SECURITY_TOKEN", ""),
    )
    account_id = _caller_account_id(credentials)
    gateway = _TeaRamGateway(credentials)
    instance = env["TABLESTORE_INSTANCE"].strip()
    result = (
        _inspect(gateway, account_id, instance)
        if action == "inspect"
        else _ensure(gateway, account_id, instance)
    )
    return {
        "action": action,
        "ok": result["configuration_matches"] is True,
        **result,
    }


def _safe_execute(
    operation: Callable[[], dict[str, Any]],
    *,
    failure_reason: str = "fc_role_operation_failed",
) -> dict[str, Any]:
    # Alibaba SDK failures can include signed request URLs. Discard every byte the
    # SDK writes and return only stable, allowlisted metadata.
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            return operation()
    except ConfigurationDrift as exc:
        return {"ok": False, "reason": exc.reason}
    except Exception as exc:
        error_type = type(exc).__name__
        result: dict[str, Any] = {
            "ok": False,
            "reason": failure_reason,
            "error_type": error_type if _SAFE_TYPE.fullmatch(error_type) else "CloudError",
        }
        code = _error_code(exc)
        if code is not None:
            result["error_code"] = code
        return result


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or create Praxis's dedicated FC Tablestore role safely."
    )
    parser.add_argument("action", choices=("inspect", "ensure"))
    args = parser.parse_args(argv)
    output = _safe_execute(lambda: _operate(args.action))
    print(json.dumps(output, sort_keys=True))
    return 0 if output.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(run())
