"""Base schema classes shared by every module.

``ApiModel`` makes the JSON contract camelCase while Python code stays snake_case:
fields are *serialized* by alias (``serialNumber``) and *accepted* by alias or by name.
``from_attributes`` lets routers return ORM objects directly.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    # validate_by_name + validate_by_alias == pydantic's former ``populate_by_name=True``
    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        from_attributes=True,
    )


class JobAccepted(ApiModel):
    """Body of a ``202 Accepted`` response for long-running work. Poll ``GET /jobs/{jobId}``."""

    job_id: int = Field(description="Background job id; poll GET /api/v1/jobs/{jobId}.")
    status_url: str = Field(description="Relative URL of the job status resource.")

    @classmethod
    def for_job(cls, job_id: int) -> JobAccepted:
        return cls(job_id=job_id, status_url=f"/api/v1/jobs/{job_id}")


class ActionIn(ApiModel):
    """Default body of ``POST /<resource>/{id}/actions/{action}`` (extend per action)."""

    comment: str | None = Field(default=None, max_length=2000)


class BulkActionIn(ApiModel):
    """Body of ``POST /<resource>/bulk/{action}``."""

    ids: list[int] = Field(min_length=1, max_length=500)
    comment: str | None = Field(default=None, max_length=2000)


class BulkFailure(ApiModel):
    id: int | str
    status: int
    title: str
    detail: str | None = None


class BulkResult(ApiModel):
    """Result of ``POST /<resource>/bulk/{action}``: per-item outcome, never all-or-nothing."""

    succeeded: list[int | str] = Field(default_factory=list)
    failed: list[BulkFailure] = Field(default_factory=list)
