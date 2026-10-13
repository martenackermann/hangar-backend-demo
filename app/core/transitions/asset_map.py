"""Asset movement map — SPEC §4.3 (+ SPEC_RESOLUTIONS #24, #49).

Used by :class:`app.core.assets.AssetService` (the only writer of asset status/location)
and published in ``GET /meta/enums.transitions.asset``. Target status/location rules
that depend on parameters (``mark_found``, ``deactivate``, ``correct`` …) live in
AssetService; ``to=None`` here means "computed".
"""

from __future__ import annotations

from app.core.enums import DEACTIVATED, AssetAction, AssetStatus, Trigger, label
from app.core.statemachine import NEW, Transition, TransitionMap

A = AssetStatus
U, J, IM, S, Y = Trigger.USER, Trigger.JOB, Trigger.IMPORT, Trigger.SYNC, Trigger.SYSTEM


def _set(*statuses: AssetStatus | str) -> frozenset[str]:
    return frozenset(str(s) for s in statuses)


_ALL = frozenset(str(s) for s in AssetStatus)
#: SPEC_RESOLUTIONS #49: status sync may create from ∅ and never touches workflow/deactivated.
_SYNC_FROM = (
    _ALL - _set(A.ASSIGNED, A.PREPARED, A.PROVIDED) - frozenset(map(str, DEACTIVATED))
) | frozenset({NEW})


def _t(
    action: AssetAction,
    from_: frozenset[str],
    to: str | None,
    triggers: frozenset[Trigger],
    *,
    permission: str | None = None,
    owner: str,
    bulk: bool = False,
    internal: bool = True,
    destructive: bool = False,
    requires_comment: bool = False,
) -> Transition:
    return Transition(
        action.value,
        from_,
        to,
        permission,
        triggers,
        label(action),
        owner,
        bulk=bulk,
        internal=internal,
        destructive=destructive,
        requires_comment=requires_comment,
    )


ASSET_TRANSITIONS = TransitionMap(
    "asset",
    [
        _t(
            AssetAction.RECEIVE,
            frozenset({NEW}) | _set(A.AVAILABLE),
            A.AVAILABLE,
            frozenset({IM}),
            owner="h2",
        ),
        _t(AssetAction.PLACE, _set(A.AVAILABLE), A.AVAILABLE, frozenset({U}), owner="h3"),
        _t(AssetAction.ASSIGN, _set(A.AVAILABLE), A.ASSIGNED, frozenset({U}), owner="h3"),
        _t(AssetAction.UNASSIGN, _set(A.ASSIGNED), A.AVAILABLE, frozenset({U, Y}), owner="h3"),
        _t(AssetAction.PREPARE, _set(A.ASSIGNED), A.PREPARED, frozenset({Y}), owner="h3"),
        _t(AssetAction.PROVIDE, _set(A.PREPARED), A.PROVIDED, frozenset({Y}), owner="h3"),
        _t(AssetAction.RETURN_TO_HUB, _set(A.PROVIDED), A.PREPARED, frozenset({Y, S}), owner="h4"),
        _t(
            AssetAction.RELEASE,
            _set(A.ASSIGNED, A.PREPARED, A.PROVIDED),
            A.AVAILABLE,
            frozenset({Y, IM}),
            owner="h1",
        ),
        _t(AssetAction.DEPLOY, _set(A.PROVIDED), A.IN_USE, frozenset({Y}), owner="h4"),
        _t(AssetAction.REGISTER_UNKNOWN, frozenset({NEW}), A.IN_USE, frozenset({U}), owner="h4"),
        _t(AssetAction.COLLECT, _set(A.IN_USE, A.MISSING), A.COLLECTED, frozenset({Y}), owner="h4"),
        _t(AssetAction.SYNC_IN_USE, _SYNC_FROM, A.IN_USE, frozenset({IM}), owner="h2"),
        _t(AssetAction.SYNC_COLLECTED, _SYNC_FROM, A.COLLECTED, frozenset({IM}), owner="h2"),
        _t(
            AssetAction.MARK_MISSING,
            _set(A.AVAILABLE, A.COLLECTED),
            A.MISSING,
            frozenset({U, Y}),
            permission="assets.update",
            owner="h5",
            bulk=True,
            internal=False,
        ),
        _t(
            AssetAction.MARK_FOUND,
            _set(A.MISSING),
            None,
            frozenset({U, Y}),
            permission="assets.update",
            owner="h5",
            internal=False,
        ),
        _t(
            AssetAction.INVENTORY_RELOCATE,
            _set(A.AVAILABLE, A.COLLECTED, A.MISSING),
            None,
            frozenset({U, Y}),
            owner="h5",
        ),
        _t(
            AssetAction.DEACTIVATE,
            _set(A.AVAILABLE, A.IN_USE, A.COLLECTED, A.MISSING),
            None,
            frozenset({U}),
            permission="assets.deactivate",
            owner="h5",
            destructive=True,
        ),
        _t(
            AssetAction.CORRECT,
            _ALL,
            None,
            frozenset({U}),
            permission="assets.update",
            owner="h5",
            bulk=True,
            internal=False,
            requires_comment=True,
        ),
    ],
)
