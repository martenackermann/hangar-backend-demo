"""Packed key/value cells ``[Key: value],[Key: value]`` (SPEC §7.4.1, preserved on purpose).

Grammar::

    packed := "" | pair ( "," pair )*
    pair   := "[" key ":" SP? value "]"

Algorithm: trim; empty → ``{}``; must start with ``[`` and end with ``]`` else
``packed_format``; strip the outer brackets; split on the literal ``],[``; split each part
on the **first** ``:``; trim key and value; keys match case-insensitively; unknown key →
warning ``packed_unknown_key``; same key twice → ``packed_duplicate_key``; required key
missing → ``packed_missing_key``; placeholder values (``N/A``, ``-``, empty) → ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .issues import Issue
from .spec import PackedKey
from .values import is_null

SEPARATOR = "],["


@dataclass(slots=True)
class PackedResult:
    values: dict[str, str | None] = field(default_factory=dict)  # PackedKey.name → value
    present: set[str] = field(default_factory=set)  # PackedKey.name of keys present
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)


def parse_packed(
    raw: str | None, column: str, keys: tuple[PackedKey, ...], *, check_required: bool = True
) -> PackedResult:
    result = PackedResult()
    text = (raw or "").strip()
    if not text:
        if check_required:
            _missing(result, column, keys)
        return result
    if not (text.startswith("[") and text.endswith("]")):
        result.issues.append(
            Issue.error(
                "packed_format",
                "Packed values must look like [Key: value],[Key: value].",
                column,
                raw,
            )
        )
        return result
    by_key = {k.key.casefold(): k for k in keys}
    for part in text[1:-1].split(SEPARATOR):
        if ":" not in part:
            result.issues.append(
                Issue.error(
                    "packed_format",
                    f"'[{part}]' is not a [Key: value] pair.",
                    column,
                    raw,
                )
            )
            continue
        key_text, value_text = part.split(":", 1)
        key_text = key_text.strip()
        spec = by_key.get(key_text.casefold())
        if spec is None:
            result.issues.append(
                Issue.warning(
                    "packed_unknown_key",
                    f"Unknown key '{key_text}' was ignored.",
                    f"{column}.{key_text}",
                    value_text.strip(),
                )
            )
            continue
        if spec.name in result.present:
            result.issues.append(
                Issue.error(
                    "packed_duplicate_key",
                    f"Key '{spec.key}' appears more than once.",
                    f"{column}.{spec.key}",
                    value_text.strip(),
                )
            )
            continue
        result.present.add(spec.name)
        value = value_text.strip()
        result.values[spec.name] = None if is_null(value) else value
    if check_required and result.ok:
        _missing(result, column, keys)
    return result


def _missing(result: PackedResult, column: str, keys: tuple[PackedKey, ...]) -> None:
    for spec in keys:
        if spec.required and result.values.get(spec.name) is None:
            result.issues.append(
                Issue.error(
                    "packed_missing_key",
                    f"Required key '{spec.key}' is missing.",
                    f"{column}.{spec.key}",
                )
            )


def build_packed(pairs: list[tuple[str, str]]) -> str:
    """Inverse of :func:`parse_packed` (used by sample generation and tests)."""
    return ",".join(f"[{key}: {value}]" for key, value in pairs)
