"""Transition maps (SR, lease return, asset), core guards and the TransitionService."""

from app.core.transitions import guards as _core_guards  # noqa: F401 - registers core guards
from app.core.transitions.asset_map import ASSET_TRANSITIONS
from app.core.transitions.lease_map import LEASE_TRANSITIONS, restored_status
from app.core.transitions.service import (
    CHAIN_MAIL_ACTIONS,
    MAPS,
    TransitionResult,
    TransitionService,
    check_version,
    entity_type_of,
    load_for_update,
    transition_service,
)
from app.core.transitions.sr_map import SR_TRANSITIONS

__all__ = [
    "ASSET_TRANSITIONS",
    "CHAIN_MAIL_ACTIONS",
    "LEASE_TRANSITIONS",
    "MAPS",
    "SR_TRANSITIONS",
    "TransitionResult",
    "TransitionService",
    "check_version",
    "entity_type_of",
    "load_for_update",
    "restored_status",
    "transition_service",
]
