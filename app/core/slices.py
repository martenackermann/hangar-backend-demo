"""SliceRegistry + slice discovery (SPEC §2.2/§2.4).

**Canonical extension mechanism**: every package ``app/modules/<slice>/`` defines
``register(reg: SliceRegistry) -> None`` in its ``__init__.py``; core discovers the
packages with ``pkgutil``, imports their ``models.py`` (so ``Base.metadata`` is complete
for Alembic/``create_all`` even without an app) and calls ``register`` exactly once.
Nothing else is auto-discovered — routers, actions, guards, subscribers, templates,
tasks, seeders, exporters and search providers are all registered explicitly.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from typing import Any

import structlog
from fastapi import APIRouter

from app.core import actions as actions_module
from app.core import events as events_module
from app.core.actions import ActionBody, ActionHandler, Resource
from app.core.enums import Trigger
from app.core.events import Phase
from app.core.notifications.registry import NotificationTemplate, templates
from app.core.search import SearchProvider, register_search_provider
from app.core.seed import framework as seed_framework
from app.core.statemachine import Authorizer, AuthorizerFn, Guard, GuardFn, authorizers, guards

log = structlog.get_logger(__name__)

MODULES_PACKAGE = "app.modules"
_RESOURCE_ENTITY = {
    "service_requests": "service_request",
    "lease_returns": "lease_return",
    "assets": "asset",
}


@dataclass
class SliceRegistry:
    """Handed to each slice's ``register(reg)``; every registration is tagged with ``owner``."""

    owner: str
    routers: list[APIRouter] = field(default_factory=list)

    # --- HTTP -----------------------------------------------------------------------------
    def include_router(self, router: APIRouter) -> None:
        """Mounted under ``/api/v1``. Give it its own ``prefix``/``tags``."""
        self.routers.append(router)

    # --- workflow ---------------------------------------------------------------------------
    def register_action(
        self,
        *,
        resource: Resource,
        name: str,
        permission: str | tuple[str, ...] | None = None,
        payload_model: type[ActionBody] | None = None,
        handler: ActionHandler | None = None,
        bulk: bool | None = None,
        via_endpoint: bool | None = None,
    ) -> None:
        actions_module.actions.register(
            resource=resource,
            name=name,
            permission=permission,
            payload_model=payload_model,
            handler=handler,
            bulk=bulk,
            owner=self.owner,
            via_endpoint=via_endpoint,
        )

    def register_guard(
        self,
        entity_type: str,
        action: str | Collection[str],
        fn: GuardFn,
        *,
        code: str,
        triggers: Collection[Trigger] | None = None,
        needs_payload: bool = False,
    ) -> None:
        """``fn(session, entity, actor, payload) -> GuardFailure | None`` for core-defined
        transitions (``entity_type`` = ``service_request`` | ``lease_return`` | ``asset``)."""
        entity = _RESOURCE_ENTITY.get(entity_type, entity_type)
        guards.add(
            entity,
            action,
            Guard(
                code,
                fn,
                frozenset(triggers) if triggers is not None else None,
                needs_payload,
                self.owner,
            ),
        )

    def register_authorizer(
        self,
        resource: str,
        action: str | Collection[str],
        fn: AuthorizerFn,
        *,
        code: str = "forbidden",
        message: str = "You are not allowed to perform this action on this record.",
    ) -> None:
        """``fn(session, entity, actor) -> bool`` (SPEC_RESOLUTIONS #2): ``False`` hides the
        action from ``availableActions``; calling it returns ``403 <code>``."""
        entity = _RESOURCE_ENTITY.get(resource, resource)
        authorizers.add(entity, action, Authorizer(fn, code, message, self.owner))

    def subscribe(
        self,
        event_type: type,
        handler: Callable[..., Awaitable[None]],
        phase: Phase = "after_commit",
    ) -> None:
        events_module.subscribe(event_type, handler, phase=phase)

    # --- mail / jobs / search ---------------------------------------------------------------
    def register_template(self, template: NotificationTemplate) -> None:
        templates.register(template)

    def register_task(
        self,
        fn: Callable[..., Any],
        *,
        name: str,
        queue: str = "default",
        lock: str | None = None,
        queueing_lock: str | None = None,
        retry: Any = None,
        pass_context: bool = False,
        periodic: str | None = None,
    ) -> Any:
        from app.jobs.app import register_task

        return register_task(
            fn,
            name=name,
            queue=queue,
            lock=lock,
            queueing_lock=queueing_lock,
            retry=retry,
            pass_context=pass_context,
            periodic=periodic,
        )

    def register_search_provider(self, fn: SearchProvider) -> None:
        register_search_provider(fn)

    # --- seed -------------------------------------------------------------------------------
    def register_seeder(self, order: int, fn: seed_framework.Seeder) -> None:
        seed_framework.register_seeder(order, fn, f"{self.owner}:{getattr(fn, '__name__', 'seed')}")

    def register_seed_exporter(self, fn: seed_framework.SeedExporter) -> None:
        seed_framework.register_seed_exporter(
            fn, f"{self.owner}:{getattr(fn, '__name__', 'export')}"
        )

    def register_demo_truncate(self, tables: list[str] | tuple[str, ...]) -> None:
        seed_framework.register_demo_truncate(tables)


# --- discovery --------------------------------------------------------------------------------

_loaded: dict[str, SliceRegistry] = {}
_models_imported = False


def slice_packages() -> list[str]:
    package = importlib.import_module(MODULES_PACKAGE)
    return sorted(
        info.name
        for info in pkgutil.iter_modules(package.__path__)
        if info.ispkg and not info.name.startswith("_")
    )


def owner_of(package: str) -> str:
    """``h1_requests`` → ``h1``."""
    return package.split("_", 1)[0]


def import_models() -> None:
    """Import core models and every ``app/modules/*/models.py``."""
    global _models_imported
    if _models_imported:
        return
    importlib.import_module("app.core.models")
    for package in slice_packages():
        name = f"{MODULES_PACKAGE}.{package}.models"
        if importlib.util.find_spec(name) is not None:
            importlib.import_module(name)
    _models_imported = True


def load_slices() -> dict[str, SliceRegistry]:
    """Bootstrap core registrations and call every slice's ``register`` once (idempotent)."""
    from app.core.bootstrap import register_core

    register_core()
    import_models()
    for package in slice_packages():
        if package in _loaded:
            continue
        module = importlib.import_module(f"{MODULES_PACKAGE}.{package}")
        registry = SliceRegistry(owner=owner_of(package))
        register = getattr(module, "register", None)
        if callable(register):
            register(registry)
        _loaded[package] = registry
        log.debug("slice_loaded", slice=package, routers=len(registry.routers))
    return dict(_loaded)


def slice_routers() -> list[APIRouter]:
    return [router for registry in load_slices().values() for router in registry.routers]
