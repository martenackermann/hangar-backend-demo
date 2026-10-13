"""``/auth/*`` and ``/me`` (API.md §3.1–3.2)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import Field
from sqlalchemy import func, select

from app.core.auth import (
    CurrentUser,
    create_access_token,
    dummy_verify,
    user_permissions,
    verify_password,
)
from app.core.config import get_settings
from app.core.db import SessionDep, commit
from app.core.enums import Role, label
from app.core.errors import AuthenticationError, ForbiddenError, NotFoundError, problem_responses
from app.core.models import User
from app.core.ratelimit import login_limiter
from app.core.read_models.refs import site_ref
from app.core.schemas import ApiInput, ApiModel, ListResponse, SiteRef
from app.core.seed.reference import PERSONAS
from app.core.timeutil import now_utc

router = APIRouter(tags=["auth"])


class LoginRequest(ApiInput):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class PersonaLoginRequest(ApiInput):
    email: str = Field(min_length=3, max_length=254)


class Me(ApiModel):
    id: int
    email: str
    first_name: str
    last_name: str
    display_name: str
    role: Role
    permissions: list[str]
    home_site: SiteRef | None
    is_demo_persona: bool


class LoginResponse(ApiModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 - token type, not a secret
    expires_at: datetime
    user: Me


class Persona(ApiModel):
    email: str
    display_name: str
    role: Role
    role_label: str
    description: str
    home_site: SiteRef | None
    group: Literal["primary", "more_technicians"]
    initials: str


def build_me(user: User) -> Me:
    return Me(
        id=user.id,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        display_name=user.display_name,
        role=user.role,
        permissions=sorted(user_permissions(user)),
        home_site=site_ref(user.home_site),
        is_demo_persona=user.is_demo_persona,
    )


async def _login(session: SessionDep, user: User) -> LoginResponse:
    if not user.is_active:
        raise ForbiddenError("This account is deactivated.", code="account_inactive")
    token, expires = create_access_token(user)
    user.last_login_at = now_utc()
    await commit(session)
    return LoginResponse(access_token=token, expires_at=expires, user=build_me(user))


async def _by_email(session: SessionDep, email: str) -> User | None:
    return (
        await session.scalars(select(User).where(func.lower(User.email) == email.strip().lower()))
    ).first()


_LOGIN_THROTTLED = "Too many failed login attempts. Try again in a few minutes."


def client_ip(request: Request) -> str:
    """Peer address (uvicorn resolves ``X-Forwarded-For`` only from ``FORWARDED_ALLOW_IPS``)."""
    return request.client.host if request.client else "unknown"


@router.post(
    "/auth/login",
    response_model=LoginResponse,
    summary="Log in with email and password",
    responses=problem_responses(401, 403, 422, 429),
)
async def login(body: LoginRequest, request: Request, session: SessionDep) -> LoginResponse:
    settings = get_settings()
    window = settings.login_rate_limit_window_seconds
    email = body.email.strip().lower()
    ip = client_ip(request)
    ip_key, account_key = ("login-ip", ip), ("login", ip, email)
    # Throttled before the (deliberately slow) argon2 check; only failures count.
    login_limiter.check(
        ip_key, limit=settings.login_rate_limit_per_ip, window=window, message=_LOGIN_THROTTLED
    )
    login_limiter.check(
        account_key,
        limit=settings.login_rate_limit_per_account,
        window=window,
        message=_LOGIN_THROTTLED,
    )
    user = await _by_email(session, email)
    # Demo personas share the published demo password: outside demo mode they cannot log in.
    if user is None or (user.is_demo_persona and not settings.demo_mode):
        dummy_verify(body.password)
        user = None
    elif not verify_password(body.password, user.password_hash):
        user = None
    if user is None:
        login_limiter.hit(ip_key, window=window)
        login_limiter.hit(account_key, window=window)
        raise AuthenticationError("Email or password is incorrect.", code="invalid_credentials")
    login_limiter.reset(account_key)
    return await _login(session, user)


@router.post(
    "/auth/logout", status_code=204, summary="Log out (stateless)", responses=problem_responses(401)
)
async def logout(_user: CurrentUser) -> Response:
    return Response(status_code=204)


@router.get(
    "/auth/personas",
    response_model=ListResponse[Persona],
    summary="Demo personas (demo mode only)",
    responses=problem_responses(404),
)
async def personas(session: SessionDep) -> ListResponse[Persona]:
    if not get_settings().demo_mode:
        raise NotFoundError("Not available outside demo mode.")
    users = (
        await session.scalars(
            select(User)
            .where(User.is_demo_persona, User.is_active)
            .order_by(User.persona_order, User.id)
        )
    ).all()
    stories = {p.email: p for p in PERSONAS}
    data: list[Persona] = []
    for user in users:
        story = stories.get(user.email)
        data.append(
            Persona(
                email=user.email,
                display_name=user.display_name,
                role=user.role,
                role_label=label(user.role),
                description=story.description if story else label(user.role),
                home_site=site_ref(user.home_site),
                group="primary"
                if (story is None or story.group == "primary")
                else "more_technicians",
                initials=user.initials,
            )
        )
    return ListResponse[Persona](data=data)


@router.post(
    "/auth/persona-login",
    response_model=LoginResponse,
    summary="Log in as a demo persona (demo mode only)",
    responses=problem_responses(403, 404, 422, 429),
)
async def persona_login(
    body: PersonaLoginRequest, request: Request, session: SessionDep
) -> LoginResponse:
    settings = get_settings()
    if not settings.demo_mode:
        raise NotFoundError("Not available outside demo mode.")
    key, window = ("persona", client_ip(request)), settings.login_rate_limit_window_seconds
    login_limiter.check(
        key,
        limit=settings.persona_login_rate_limit_per_ip,
        window=window,
        message="Too many persona logins. Try again in a few minutes.",
    )
    login_limiter.hit(key, window=window)
    user = await _by_email(session, body.email)
    if user is None or not user.is_demo_persona:
        raise ForbiddenError("Not a demo persona.")
    return await _login(session, user)


@router.get(
    "/me",
    response_model=Me,
    summary="The current user",
    tags=["auth"],
    responses=problem_responses(401),
)
async def me(user: CurrentUser) -> Me:
    return build_me(user)
