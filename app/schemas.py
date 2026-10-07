from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class ArchiveIn(BaseModel):
    archive_no: str = Field(min_length=1, max_length=200)
    retain_until: datetime = Field(
        ..., description="保留截止时间, 带时区的 ISO-8601 时间, 如 2026-01-01T00:00:00Z"
    )

    @field_validator("archive_no")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("archive_no must not be blank")
        return v

    @field_validator("retain_until")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("retain_until must be timezone-aware (e.g. append 'Z')")
        return v


class ArchiveOut(BaseModel):
    archive_no: str
    retain_until: datetime
    status: str
    created_at: datetime
    destroyed_at: datetime | None = None
    destroyed_request_no: str | None = None
    active_seals: list["SealOut"] = []


class SealIn(BaseModel):
    archive_no: str = Field(min_length=1, max_length=200)
    case_no: str = Field(min_length=1, max_length=200)

    @field_validator("archive_no", "case_no")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class SealOut(BaseModel):
    case_no: str
    sealed_at: datetime
    released_at: datetime | None = None
    status: str


class ReleaseIn(BaseModel):
    archive_no: str = Field(min_length=1, max_length=200)
    case_no: str = Field(min_length=1, max_length=200)

    @field_validator("archive_no", "case_no")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class DestroyIn(BaseModel):
    request_no: str = Field(min_length=1, max_length=200)
    archive_numbers: list[str] = Field(min_length=1)

    @field_validator("request_no")
    @classmethod
    def _strip_req(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("request_no must not be blank")
        return v

    @field_validator("archive_numbers")
    @classmethod
    def _norm(cls, values: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for raw in values:
            v = raw.strip()
            if not v:
                raise ValueError("archive number must not be blank")
            if len(v) > 200:
                raise ValueError("archive number too long")
            if v not in seen:
                seen.add(v)
                ordered.append(v)
        if len(ordered) > 10000:
            raise ValueError("at most 10000 archive numbers per batch")
        return ordered


class ItemOut(BaseModel):
    position: int
    archive_no: str
    destroyed: bool
    reason: str
    retain_until: datetime | None = None
    sealed_by_cases: list[str] = []
    prior_request_no: str | None = None
    item_credential: str


class DestroyOut(BaseModel):
    request_no: str
    status: str
    replayed: bool
    adjudicated_at: datetime
    destroyed_count: int
    not_destroyed_count: int
    results: list[ItemOut]
    credential: str
    prev_credential: str
    seq: int


class VerifyOut(BaseModel):
    request_no: str
    valid: bool
    checks: dict[str, bool]
    seq: int
    prev_credential: str
    expected_prev: str | None = None
    stored_credential: str
    recomputed_credential: str
    adjudicated_at: datetime
    results: list[ItemOut]


class ErrorOut(BaseModel):
    code: str
    message: str
    details: dict | None = None


# SealOut 在 ArchiveOut 之后定义, 触发前向引用解析
ArchiveOut.model_rebuild()
