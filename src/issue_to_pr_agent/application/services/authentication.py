from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timezone
import hashlib
import hmac
import json

from ...domain.entities import AuthSubjectType, AuthenticatedPrincipal
from ...shared.exceptions import PolicyError


def issue_bearer_token(
    *,
    secret: str,
    subject: str,
    actor: str,
    subject_type: AuthSubjectType = AuthSubjectType.USER,
    issuer: str | None = None,
    team: str | None = None,
    groups: list[str] | None = None,
    scopes: list[str] | None = None,
    tenant_ids: list[str] | None = None,
    expires_in_seconds: int = 3600,
    now: datetime | None = None,
) -> str:
    issued_at = int((now or datetime.now(timezone.utc)).timestamp())
    payload = {
        "sub": subject,
        "actor": actor,
        "subject_type": subject_type.value,
        "iat": issued_at,
        "exp": issued_at + expires_in_seconds,
    }
    if issuer:
        payload["iss"] = issuer
    if team:
        payload["team"] = team
    if groups:
        payload["groups"] = [item for item in groups if item]
    if scopes:
        payload["scopes"] = [item for item in scopes if item]
    if tenant_ids:
        payload["tenant_ids"] = [item for item in tenant_ids if item]
    return _encode_token(payload=payload, secret=secret)


def authenticate_bearer_token(
    token: str,
    *,
    secret: str,
    expected_issuer: str | None = None,
    now: datetime | None = None,
) -> AuthenticatedPrincipal:
    payload = _decode_token(token=token, secret=secret)
    subject = _required_string(payload, "sub")
    actor = _required_string(payload, "actor")
    issuer = _optional_string(payload.get("iss"))
    if expected_issuer is not None and issuer != expected_issuer:
        raise PolicyError("Bearer token issuer is invalid.")

    current_time = now or datetime.now(timezone.utc)
    expires_at_epoch = _required_int(payload, "exp")
    if current_time.timestamp() >= expires_at_epoch:
        raise PolicyError("Bearer token has expired.")

    subject_type_raw = _optional_string(payload.get("subject_type")) or AuthSubjectType.USER.value
    subject_type = AuthSubjectType(subject_type_raw)
    team = _optional_string(payload.get("team"))
    groups = _dedupe_strings([team, *_string_list(payload.get("groups"))])
    scopes = _dedupe_strings(_string_list(payload.get("scopes")))
    tenant_ids = _dedupe_strings(_string_list(payload.get("tenant_ids")))
    issued_at = _epoch_to_iso(_optional_int(payload.get("iat")))
    expires_at = _epoch_to_iso(expires_at_epoch)
    return AuthenticatedPrincipal(
        subject=subject,
        actor=actor,
        subject_type=subject_type,
        issuer=issuer,
        team=team,
        groups=groups,
        scopes=scopes,
        tenant_ids=tenant_ids,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _encode_token(*, payload: dict[str, object], secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    header_segment = _b64encode_json(header)
    payload_segment = _b64encode_json(payload)
    signing_input = f"{header_segment}.{payload_segment}".encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_segment}.{payload_segment}.{_b64encode_bytes(signature)}"


def _decode_token(*, token: str, secret: str) -> dict[str, object]:
    parts = token.split(".")
    if len(parts) != 3:
        raise PolicyError("Bearer token format is invalid.")
    header_segment, payload_segment, signature_segment = parts
    signing_input = f"{header_segment}.{payload_segment}".encode("utf-8")
    expected_signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    actual_signature = _b64decode_bytes(signature_segment)
    if not hmac.compare_digest(actual_signature, expected_signature):
        raise PolicyError("Bearer token signature is invalid.")
    header = _decode_json_segment(header_segment)
    if _optional_string(header.get("alg")) != "HS256":
        raise PolicyError("Bearer token algorithm is invalid.")
    payload = _decode_json_segment(payload_segment)
    return payload


def _b64encode_json(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64encode_bytes(encoded)


def _decode_json_segment(segment: str) -> dict[str, object]:
    try:
        payload = json.loads(_b64decode_bytes(segment).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyError("Bearer token payload is invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise PolicyError("Bearer token payload must be a JSON object.")
    return payload


def _b64encode_bytes(payload: bytes) -> str:
    return urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _b64decode_bytes(payload: str) -> bytes:
    padding = "=" * (-len(payload) % 4)
    try:
        return urlsafe_b64decode(payload + padding)
    except (ValueError, TypeError) as exc:
        raise PolicyError("Bearer token encoding is invalid.") from exc


def _required_string(payload: dict[str, object], field_name: str) -> str:
    value = _optional_string(payload.get(field_name))
    if value is None:
        raise PolicyError(f"Bearer token is missing required claim: {field_name}.")
    return value


def _required_int(payload: dict[str, object], field_name: str) -> int:
    value = _optional_int(payload.get(field_name))
    if value is None:
        raise PolicyError(f"Bearer token is missing required claim: {field_name}.")
    return value


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _epoch_to_iso(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _dedupe_strings(values: list[str | None]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value is None:
            continue
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered
