"""Permission keys and the role matrix — exactly SPEC §3.2.

``require("sr.read")`` (``app.core.auth``) enforces them server-side; ``GET /me`` lists the
caller's keys so the frontend can hide what it cannot do.
"""

from __future__ import annotations

from app.core.enums import Role

V, T, D, A = Role.VIEWER, Role.TECHNICIAN, Role.DISPATCHER, Role.ADMIN
_ALL = frozenset({V, T, D, A})
_OPS = frozenset({D, A})

#: key -> (label, roles granted)
PERMISSIONS: dict[str, tuple[str, frozenset[Role]]] = {
    "sr.read": ("Read service requests and the pipeline", _ALL),
    "sr.update": ("Edit service request fields and region override", _OPS),
    "sr.comms": ("Send SR mails, pause/resume, hardware hold, VIP release", _OPS),
    "sr.manage": ("Cancel, reopen and complete service requests manually", _OPS),
    "sr.export": ("Export service requests", _OPS),
    "lease.read": ("Read lease returns", _ALL),
    "lease.manage": ("Lease return actions, edits and comments", _OPS),
    "lease.export": ("Export lease returns", _OPS),
    "contacts.update": ("Edit contacts", _OPS),
    "comms.read": ("Read mail log, dispatch runs and templates", frozenset({V, D, A})),
    "comms.dispatch": ("Run the mail dispatch", _OPS),
    "comms.settings": ("Edit dispatch settings", _OPS),
    "staging.scan": ("Installation place scan station", frozenset({T, D, A})),
    "staging.personalize": ("Personalization (claim, assign, finish)", _OPS),
    "staging.close": ("Order closing", _OPS),
    "fulfilment.pick": ("Technician picking", _OPS),
    "fulfilment.ship": ("Parcel packing and shipments", _OPS),
    "appointments.read": ("Read calendars and appointments", _ALL),
    "appointments.manage": ("Sync, book, link and reschedule appointments", _OPS),
    "tours.read": ("Read all tours", _ALL),
    "tours.plan": ("Plan tours", _OPS),
    "tours.execute_own": ("Run own tours and deployments", frozenset({T, D, A})),
    "tours.execute_any": ("Run deployments on any tour", _OPS),
    "stock.read": ("Read stock, call-offs, assets and deactivations", _ALL),
    "stock.recalculate": ("Trigger stock recalculation", _OPS),
    "stock.buffers": ("Edit stock buffers", _OPS),
    "calloff.manage": ("Manage call-off orders", _OPS),
    "assets.update": ("Correct assets, mark missing/found", _OPS),
    "assets.deactivate": ("Upload deactivations", _OPS),
    "assets.export": ("Export assets and stock", _OPS),
    "inventory.read": ("Read inventories and reports", _ALL),
    "inventory.scan": ("Scan into an open inventory", frozenset({T, D, A})),
    "inventory.manage": ("Open, close and cancel inventories", _OPS),
    "imports.read": ("Read imports", frozenset({V, D, A})),
    "imports.create": ("Upload and cancel imports", _OPS),
    "master.read": ("Read master data", _ALL),
    "master.manage": ("Edit master data", frozenset({A})),
    "users.read": ("List users", _ALL),
    "users.manage": ("Administer users", frozenset({A})),
    "dashboard.read": ("Read dashboard KPIs", _ALL),
    "demo.reset": ("Reset demo data", frozenset({A})),
}

ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    role: frozenset(key for key, (_, roles) in PERMISSIONS.items() if role in roles)
    for role in Role
}


def permissions_for(role: Role | str) -> frozenset[str]:
    return ROLE_PERMISSIONS[Role(role)]


def has_permission(role: Role | str, permission: str | tuple[str, ...] | None) -> bool:
    """``permission`` may be a single key, a tuple (any of) or ``None`` (no requirement)."""
    if permission is None:
        return True
    granted = permissions_for(role)
    keys = (permission,) if isinstance(permission, str) else permission
    return any(key in granted for key in keys)


def is_known(permission: str) -> bool:
    return permission in PERMISSIONS
