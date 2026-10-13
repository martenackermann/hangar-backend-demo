"""User administration (API.md §5.5; SPEC_RESOLUTIONS #22)."""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import hash_password
from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationProblem
from app.core.events import UserDeactivated, emit
from app.core.models import Site, User

from ..schemas import UserCreate, UserPatch

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,63}$")


async def get_user(session: AsyncSession, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found.")
    return user


async def _home_site(session: AsyncSession, site_id: int | None) -> Site | None:
    if site_id is None:
        return None
    site = await session.get(Site, site_id)
    if site is None:
        raise ValidationProblem({"homeSiteId": ["Unknown site."]})
    return site


async def create_user(session: AsyncSession, body: UserCreate) -> User:
    email = body.email.strip().lower()
    if not _EMAIL.match(email):
        raise ValidationProblem({"email": ["Enter a valid email address."]})
    exists = await session.scalar(select(User.id).where(func.lower(User.email) == email))
    if exists is not None:
        raise ConflictError(
            "A user with this email already exists.",
            code="duplicate",
            errors={"email": ["A user with this email already exists."]},
        )
    site = await _home_site(session, body.home_site_id)
    user = User(
        email=email,
        first_name=body.first_name.strip(),
        last_name=body.last_name.strip(),
        role=body.role,
        home_site=site,
        home_site_id=site.id if site else None,
        password_hash=hash_password(body.password),
        is_active=True,
        is_demo_persona=False,
    )
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def update_user(session: AsyncSession, user: User, body: UserPatch, actor: User) -> User:
    values = body.model_dump(exclude_unset=True, by_alias=False)
    errors = {
        name: ["This field cannot be empty."]
        for name, key in (("firstName", "first_name"), ("lastName", "last_name"), ("role", "role"))
        if key in values and values[key] is None
    }
    if errors:
        raise ValidationProblem(errors)
    if "role" in values and values["role"] != user.role:
        if user.id == actor.id:
            raise ConflictError("You cannot change your own role.")
        if user.is_demo_persona:
            raise ConflictError("Demo personas keep their role so the demo stories stay intact.")
        user.role = values["role"]
    if "home_site_id" in values:
        site = await _home_site(session, values["home_site_id"])
        user.home_site = site
        user.home_site_id = site.id if site else None
    for name in ("first_name", "last_name"):
        if name in values:
            setattr(user, name, values[name].strip())
    await session.flush()
    await session.refresh(user)
    return user


async def set_active(session: AsyncSession, user: User, active: bool, actor: User) -> User:
    if not active and user.id == actor.id:
        raise ConflictError("You cannot deactivate your own account.")
    if not active and user.is_demo_persona and get_settings().demo_mode:
        # Anyone can sign in as a persona on a public demo: locking one out (or every admin)
        # would break the shared demo until the next reset.
        raise ConflictError("Demo personas stay active so the demo stays usable for everyone.")
    was_active = user.is_active
    user.is_active = active
    await session.flush()
    if was_active and not active:
        await emit(session, UserDeactivated(user.id))
    await session.refresh(user)
    return user


async def reset_password(session: AsyncSession, user: User, new_password: str) -> None:
    if user.is_demo_persona:
        raise ConflictError("Demo personas always use the demo password; it cannot be changed.")
    user.password_hash = hash_password(new_password)
    await session.flush()
