"""Authentication & authorization primitives.

Phase 2a ships the mechanics (JWT, password hashing, dependencies). The user lookup in
:func:`get_current_user` is a placeholder that trusts token claims; phase 2b replaces it
with a database lookup and fills :data:`ROLE_PERMISSIONS` from the spec.

Permission strings are dotted (``service_requests.read``); a trailing ``.*`` grants a
whole namespace and ``*`` grants everything.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import uuid4

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash

from app.core.config import get_settings
from app.core.errors import AuthenticationError, PermissionDeniedError

# --- passwords -----------------------------------------------------------------------

_password_hash = PasswordHash.recommended()  # argon2id


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _password_hash.verify(password, password_hash)


def verify_and_update_password(password: str, password_hash: str) -> tuple[bool, str | None]:
    """Verify and return a new hash when the stored one uses outdated parameters."""
    return _password_hash.verify_and_update(password, password_hash)


# --- tokens --------------------------------------------------------------------------


def create_access_token(
    subject: str | int,
    *,
    roles: Iterable[str] = (),
    email: str | None = None,
    ttl_minutes: int | None = None,
    extra_claims: Mapping[str, Any] | None = None,
) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    ttl = ttl_minutes if ttl_minutes is not None else settings.jwt_ttl_minutes
    claims: dict[str, Any] = {
        "sub": str(subject),
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=ttl),
        "jti": uuid4().hex,
        "roles": sorted(set(roles)),
    }
    if email is not None:
        claims["email"] = email
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(
        claims, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm
    )


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        return jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "exp", "iat"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("The access token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("The access token is invalid.") from exc


# --- principals & permissions --------------------------------------------------------

#: role -> granted permissions. Phase 2b fills this from the spec's role matrix.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset({"*"}),
}


def permissions_for_roles(roles: Iterable[str]) -> frozenset[str]:
    granted: set[str] = set()
    for role in roles:
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(granted)


def permission_matches(granted: str, required: str) -> bool:
    if granted in ("*", required):
        return True
    if granted.endswith(".*"):
        return required.startswith(granted[:-1])
    return False


@dataclass(frozen=True, slots=True)
class CurrentUser:
    id: str
    email: str | None = None
    roles: frozenset[str] = field(default_factory=frozenset)
    permissions: frozenset[str] = field(default_factory=frozenset)

    def has_permission(self, permission: str) -> bool:
        return any(permission_matches(granted, permission) for granted in self.permissions)

    def has_role(self, role: str) -> bool:
        return role in self.roles


_bearer = HTTPBearer(auto_error=False, description="JWT access token from POST /auth/login.")


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> CurrentUser:
    """Resolve the caller from the ``Authorization: Bearer`` header (401 otherwise).

    PLACEHOLDER (phase 2a): builds the principal from token claims only.
    Phase 2b: load the user row (active flag, roles) from the database.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Missing bearer token.")
    claims = decode_access_token(credentials.credentials)
    roles = frozenset(str(role) for role in claims.get("roles", []))
    return CurrentUser(
        id=str(claims["sub"]),
        email=claims.get("email"),
        roles=roles,
        permissions=permissions_for_roles(roles),
    )


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def require_permission(
    *permissions: str, any_of: bool = False
) -> Callable[..., Coroutine[Any, Any, CurrentUser]]:
    """Dependency factory: 401 without a user, 403 unless the user holds the permission(s).

    ``require_permission("a", "b")`` needs both; ``any_of=True`` needs at least one.
    The dependency returns the :class:`CurrentUser`, so it can replace ``CurrentUserDep``.
    """

    async def dependency(user: CurrentUserDep) -> CurrentUser:
        held = [user.has_permission(p) for p in permissions]
        if not permissions or (any(held) if any_of else all(held)):
            return user
        missing = [p for p, ok in zip(permissions, held, strict=True) if not ok]
        raise PermissionDeniedError(
            "You do not have permission to perform this action.",
            requiredPermissions=list(permissions) if any_of else missing,
        )

    dependency.__name__ = f"require_permission({', '.join(permissions)})"
    return dependency
