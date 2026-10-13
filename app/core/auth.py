"""Authentication: argon2 passwords, HS256 JWTs, DB-backed ``current_user`` and ``require()``.

Token claims: ``sub`` (user id), ``role``, ``iat``, ``exp`` (API.md §1.1). The user row is
loaded on every request, so deactivation and role changes take effect immediately.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
import structlog
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash

from app.core.config import get_settings
from app.core.context import current_actor_id
from app.core.db import SessionDep
from app.core.errors import AuthenticationError, ForbiddenError
from app.core.models import User
from app.core.permissions import has_permission, permissions_for

# --- passwords -----------------------------------------------------------------------------

_password_hash = PasswordHash.recommended()  # argon2id

#: Pre-computed hash used to keep login timing uniform for unknown emails.
_DUMMY_HASH: str | None = None


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _password_hash.verify(password, password_hash)
    except Exception:  # noqa: BLE001 - malformed hash = no match
        return False


def dummy_verify(password: str) -> None:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("dummy-password-for-timing")
    verify_password(password, _DUMMY_HASH)


# --- tokens --------------------------------------------------------------------------------


def create_access_token(user: User) -> tuple[str, datetime]:
    settings = get_settings()
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=settings.jwt_ttl_minutes)
    claims = {"sub": str(user.id), "role": str(user.role), "iat": now, "exp": expires}
    token = jwt.encode(
        claims, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    return token, expires


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        return jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "exp"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("The access token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("The access token is invalid.") from exc


# --- dependencies --------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False, description="JWT from POST /auth/login.")


async def get_current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """The authenticated user (401 without/with an invalid token, 403 when deactivated)."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Missing bearer token.")
    claims = decode_access_token(credentials.credentials)
    try:
        user_id = int(claims["sub"])
    except (TypeError, ValueError) as exc:
        raise AuthenticationError("The access token is invalid.") from exc
    user = await session.get(User, user_id)
    if user is None:
        raise AuthenticationError("The access token is invalid.")
    if not user.is_active:
        raise ForbiddenError("This account is deactivated.", code="account_inactive")
    current_actor_id.set(user.id)  # default actor for notify() / logs (#14)
    structlog.contextvars.bind_contextvars(user_id=user.id)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def user_permissions(user: User) -> frozenset[str]:
    return permissions_for(user.role)


def can(user: User | None, permission: str | tuple[str, ...] | None) -> bool:
    """``True`` for ``None`` users only when no permission is required (system actors)."""
    if user is None:
        return permission is None
    return has_permission(user.role, permission)


def ensure(user: User, permission: str | tuple[str, ...]) -> None:
    if not can(user, permission):
        keys = [permission] if isinstance(permission, str) else list(permission)
        raise ForbiddenError(
            "You do not have permission to perform this action.", requiredPermissions=keys
        )


def require(*permissions: str, any_of: bool = False) -> Callable[..., Coroutine[Any, Any, User]]:
    """Dependency factory: 401 unauthenticated, 403 ``forbidden`` unless the user holds the
    permission(s) — all of them, or at least one with ``any_of=True``. Returns the user::

        @router.get("", dependencies=[Depends(require("sr.read"))])
        async def handler(user: Annotated[User, Depends(require("sr.update"))]): ...
    """

    async def dependency(user: CurrentUser) -> User:
        held = [can(user, p) for p in permissions]
        if not permissions or (any(held) if any_of else all(held)):
            return user
        missing = (
            list(permissions)
            if any_of
            else [p for p, ok in zip(permissions, held, strict=True) if not ok]
        )
        raise ForbiddenError(
            "You do not have permission to perform this action.", requiredPermissions=missing
        )

    dependency.__name__ = f"require_{'_or_'.join(p.replace('.', '_') for p in permissions)}"
    return dependency
