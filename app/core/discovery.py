"""Module auto-discovery.

Every package under ``app/modules/`` is a feature module. Well-known submodules are
imported automatically, so feature slices never edit shared wiring:

=================  ==============================================================
``models.py``      imported before ``create_all`` / Alembic autogenerate
``router.py``      must export ``router: APIRouter``; mounted under ``/api/v1``
``jobs.py``        must export ``blueprint: procrastinate.Blueprint``; tasks are
                   registered on the job app under the namespace ``<module>``
``subscribers.py`` imported for its side effects (``@on_transition`` handlers)
``seed.py``        may export ``async def seed(session)`` and ``SEED_ORDER: int``
=================  ==============================================================

Packages whose name starts with ``_`` are ignored. A missing submodule is fine; an
import error inside an existing one is raised (never swallowed).
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from functools import cache
from types import ModuleType

MODULES_PACKAGE = "app.modules"


@cache
def module_names() -> tuple[str, ...]:
    package = importlib.import_module(MODULES_PACKAGE)
    return tuple(
        sorted(
            info.name
            for info in pkgutil.iter_modules(package.__path__)
            if info.ispkg and not info.name.startswith("_")
        )
    )


def import_submodule(module: str, submodule: str) -> ModuleType | None:
    name = f"{MODULES_PACKAGE}.{module}.{submodule}"
    if importlib.util.find_spec(name) is None:
        return None
    return importlib.import_module(name)


def discover(submodule: str) -> list[tuple[str, ModuleType]]:
    """Import ``app.modules.<module>.<submodule>`` for every module that has it."""
    found: list[tuple[str, ModuleType]] = []
    for module in module_names():
        imported = import_submodule(module, submodule)
        if imported is not None:
            found.append((module, imported))
    return found


def import_models() -> list[str]:
    """Import every ``models.py`` so ``Base.metadata`` is complete."""
    return [name for name, _ in discover("models")]


def import_subscribers() -> list[str]:
    return [name for name, _ in discover("subscribers")]
