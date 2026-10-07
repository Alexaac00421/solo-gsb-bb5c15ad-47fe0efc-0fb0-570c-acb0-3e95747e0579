"""电子档案销毁裁决服务 - FastAPI 入口。"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import credentials as cred
from . import db
from .db import close_pool, open_pool
from .schemas import (
    ArchiveIn,
    ArchiveOut,
    DestroyIn,
    ReleaseIn,
    SealIn,
    SealOut,
)

app = FastAPI(
    title="电子档案销毁裁决服务",
    version="1.0.0",
    description=(
        "档案登记 / 案件封存与解除 / 批量销毁裁决。"
        "按服务端裁决时刻逐件返回结果, 整批一次提交, 保存不可变凭据。"
    ),
)


class DomainError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.details = details


@app.exception_handler(DomainError)
async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
    body: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.details:
        body["details"] = exc.details
    return JSONResponse(status_code=exc.status, content=body)


@app.exception_handler(psycopg.Error)
async def pg_error_handler(_: Request, exc: psycopg.Error) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"code": "database_error", "message": str(exc).splitlines()[0]},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"code": "invalid_request", "message": "请求参数不合法",
                 "details": {"errors": jsonable_encoder(exc.errors())}},
    )


@app.on_event("startup")
def _startup() -> None:
    open_pool()


@app.on_event("shutdown")
def _shutdown() -> None:
    close_pool()


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> dict[str, str]:
    with db.pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 档案登记
# ---------------------------------------------------------------------------
@app.post("/v1/archives", status_code=201, response_model=ArchiveOut)
def register_archive(body: ArchiveIn) -> ArchiveOut:
    with db.pool.connection() as conn, conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO archives (archive_no, retain_until)
                VALUES (%s, %s)
                ON CONFLICT (archive_no) DO NOTHING
                RETURNING archive_no, retain_until, status, created_at
                """,
                (body.archive_no, body.retain_until),
            )
            row = cur.fetchone()
            if row is None:
                raise DomainError(409, "archive_exists", "档案编号已存在",
                                  {"archive_no": body.archive_no})
    return _archive_out(row, seals=[])


@app.get("/v1/archives/{archive_no}", response_model=ArchiveOut)
def get_archive(archive_no: str) -> ArchiveOut:
    with db.pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.archive_no, a.retain_until, a.status, a.created_at,
                   b.adjudicated_at AS destroyed_at,
                   b.request_no       AS destroyed_request_no
              FROM archives a
              LEFT JOIN destruction_items di
                ON di.archive_no = a.archive_no AND di.destroyed
              LEFT JOIN destruction_batches b ON b.id = di.batch_id
             WHERE a.archive_no = %s
            """,
            (archive_no,),
        )
        row = cur.fetchone()
        if row is None:
            raise DomainError(404, "archive_not_found", "档案不存在",
                              {"archive_no": archive_no})
        cur.execute(
            """
            SELECT case_no, sealed_at, released_at, status
              FROM seals WHERE archive_no = %s AND status = 'active'
             ORDER BY case_no
            """,
            (archive_no,),
        )
        seals = [SealOut(**r) for r in cur.fetchall()]
    return _archive_out(row, seals=seals)


def _archive_out(row: dict, seals: list[SealOut]) -> ArchiveOut:
    return ArchiveOut(
        archive_no=row["archive_no"],
        retain_until=row["retain_until"],
        status=row["status"],
        created_at=row["created_at"],
        destroyed_at=row.get("destroyed_at"),
        destroyed_request_no=row.get("destroyed_request_no"),
        active_seals=seals,
    )


# ---------------------------------------------------------------------------
# 封存 / 解除封存
# ---------------------------------------------------------------------------
@app.post("/v1/seals", status_code=201, response_model=SealOut)
def create_seal(body: SealIn) -> SealOut:
    """案件对档案发起封存。

    与销毁事务互斥: 先锁档案行; 若销毁已先提交, 档案为 destroyed -> 409 conflict。
    """
    with db.pool.connection() as conn, conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM archives WHERE archive_no = %s FOR UPDATE",
                (body.archive_no,),
            )
            arch = cur.fetchone()
            if arch is None:
                raise DomainError(404, "archive_not_found", "档案不存在",
                                  {"archive_no": body.archive_no})
            if arch["status"] == "destroyed":
                raise DomainError(409, "archive_destroyed",
                                  "档案已销毁, 不得再封存",
                                  {"archive_no": body.archive_no})
            cur.execute(
                """
                INSERT INTO seals (archive_no, case_no)
                VALUES (%s, %s)
                ON CONFLICT (archive_no, case_no) WHERE status = 'active'
                DO NOTHING
                RETURNING case_no, sealed_at, released_at, status
                """,
                (body.archive_no, body.case_no),
            )
            row = cur.fetchone()
            if row is None:
                raise DomainError(409, "seal_exists",
                                  "该案件对该档案的封存已生效",
                                  {"archive_no": body.archive_no, "case_no": body.case_no})
    return SealOut(**row)


@app.post("/v1/seals/release", response_model=SealOut)
def release_seal(body: ReleaseIn) -> SealOut:
    """解除某一案件对档案的封存; 不影响其他案件的封存。"""
    with db.pool.connection() as conn, conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM archives WHERE archive_no = %s FOR UPDATE",
                (body.archive_no,),
            )
            arch = cur.fetchone()
            if arch is None:
                raise DomainError(404, "archive_not_found", "档案不存在",
                                  {"archive_no": body.archive_no})
            if arch["status"] == "destroyed":
                raise DomainError(409, "archive_destroyed",
                                  "档案已销毁, 封存状态不得变更",
                                  {"archive_no": body.archive_no})
            cur.execute(
                """
                UPDATE seals
                   SET status = 'released', released_at = now()
                 WHERE archive_no = %s AND case_no = %s AND status = 'active'
                RETURNING case_no, sealed_at, released_at, status
                """,
                (body.archive_no, body.case_no),
            )
            row = cur.fetchone()
            if row is None:
                raise DomainError(404, "active_seal_not_found",
                                  "该案件对该档案没有生效中的封存",
                                  {"archive_no": body.archive_no, "case_no": body.case_no})
    return SealOut(**row)


# ---------------------------------------------------------------------------
# 批量销毁裁决
# ---------------------------------------------------------------------------
DESTROY_LOCK_XID = 8801  # 事务级咨询锁: 串行化哈希链取链头


@app.post("/v1/destruction/requests", response_model=None)
def submit_destruction(body: DestroyIn):
    numbers = body.archive_numbers  # 已去重、保序
    fingerprint = cred.request_fingerprint(numbers)
    claim_token = str(uuid.uuid4())

    with db.pool.connection() as conn, conn.transaction():
        with conn.cursor() as cur:
            # 1) 幂等占位。同 request_no 并发: 第二个事务在此阻塞至第一事务提交
            cur.execute(
                """
                INSERT INTO destruction_batches
                    (request_no, archive_numbers, fingerprint,
                     claim_token, status, prev_credential)
                VALUES (%s, %s, %s, %s, 'processing', %s)
                ON CONFLICT (request_no) DO NOTHING
                """,
                (body.request_no, numbers, fingerprint, claim_token,
                 cred.GENESIS),
            )

            cur.execute(
                """
                SELECT id, seq, request_no, archive_numbers, fingerprint, claim_token,
                       status, prev_credential
                  FROM destruction_batches
                 WHERE request_no = %s
                 FOR UPDATE
                """,
                (body.request_no,),
            )
            batch = cur.fetchone()
            if batch is None:  # 极端情况下再取一次
                raise DomainError(409, "request_conflict",
                                  "请求号正在被处理, 请稍后重试",
                                  {"request_no": body.request_no})

            if batch["status"] == "completed":
                if batch["fingerprint"] != fingerprint:
                    raise DomainError(
                        409, "request_content_conflict",
                        "相同请求号已用于不同的档案编号集合",
                        {
                            "request_no": body.request_no,
                            "existing_archive_numbers": batch["archive_numbers"],
                        },
                    )
                return _load_completed(conn, batch["id"], replayed=True)

            if batch["claim_token"] != claim_token:
                # 前一个处理者崩溃留下 processing 占位行
                raise DomainError(
                    409, "request_processing",
                    "该请求号处于未决状态 (前次处理可能中断), 请更换请求号或联系管理员",
                    {"request_no": body.request_no},
                )

            # 2) 按排序锁定整批档案行 —— 与封存/解除事务互斥, 防死锁
            ordered = sorted(numbers)
            cur.execute(
                "SELECT archive_no, retain_until, status "
                "FROM unnest(%s::text[]) AS t(archive_no) "
                "JOIN archives a USING (archive_no) ORDER BY a.archive_no FOR UPDATE OF a",
                (ordered,),
            )
            found_rows = cur.fetchall()
            found = {r["archive_no"]: r for r in found_rows}
            missing = [n for n in ordered if n not in found]
            if missing:
                # 档案不存在 -> 整个事务回滚 (含幂等占位), 不留任何部分销毁
                raise DomainError(
                    404, "archive_not_found",
                    "批次中存在不存在的档案, 整批未处理",
                    {"missing_archive_numbers": missing},
                )

            # 3) 一次性快照全部相关生效封存 (行锁已保证封存事务无法并发提交)
            cur.execute(
                """
                SELECT archive_no, array_agg(case_no ORDER BY case_no) AS cases
                  FROM seals
                 WHERE status = 'active' AND archive_no = ANY(%s)
                 GROUP BY archive_no
                """,
                (ordered,),
            )
            active_seals = {r["archive_no"]: list(r["cases"]) for r in cur.fetchall()}

            # 4) 服务端裁决时刻
            cur.execute("SELECT now() AS now")
            now = cur.fetchone()["now"]

            # 5) 串行化哈希链: 取上一完成批次的凭据, 并按完成顺序分配链序号
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (DESTROY_LOCK_XID,))
            cur.execute(
                """
                SELECT credential, seq
                  FROM destruction_batches
                 WHERE status = 'completed'
                 ORDER BY seq DESC
                 LIMIT 1
                """
            )
            head = cur.fetchone()
            prev_credential = head["credential"] if head else cred.GENESIS
            chain_seq = (head["seq"] + 1) if head else 1

            # 6) 逐件裁决
            item_credentials: list[str] = []
            results_payload: list[dict] = []
            destroyed_count = 0
            not_destroyed_count = 0

            for position, archive_no in enumerate(numbers, start=1):
                arch = found[archive_no]
                cases = active_seals.get(archive_no, [])
                prior_request_no = None

                if arch["status"] == "destroyed":
                    destroyed = False
                    reason = "already_destroyed"
                elif arch["retain_until"] > now:
                    destroyed = False
                    reason = "not_due"
                elif cases:
                    destroyed = False
                    reason = "sealed"
                else:
                    destroyed = True
                    reason = "destroyed"

                if destroyed:
                    cur.execute(
                        """
                        UPDATE archives SET status = 'destroyed'
                         WHERE archive_no = %s AND status = 'active'
                        """,
                        (archive_no,),
                    )
                    if cur.rowcount != 1:
                        raise DomainError(500, "internal_error",
                                          "裁决状态更新失败")
                    destroyed_count += 1
                else:
                    not_destroyed_count += 1
                    if reason == "already_destroyed":
                        cur.execute(
                            """
                            SELECT b.request_no
                              FROM destruction_items di
                              JOIN destruction_batches b ON b.id = di.batch_id
                             WHERE di.archive_no = %s AND di.destroyed
                             LIMIT 1
                            """,
                            (archive_no,),
                        )
                        prow = cur.fetchone()
                        prior_request_no = prow["request_no"] if prow else None

                item_obj = cred.build_item_obj(
                    request_no=body.request_no,
                    position=position,
                    archive_no=archive_no,
                    destroyed=destroyed,
                    reason=reason,
                    retain_until=arch["retain_until"].isoformat(),
                    sealed_by_cases=cases,
                    prior_request_no=prior_request_no,
                    adjudicated_at=now.isoformat(),
                )
                ic = cred.item_credential(item_obj)
                item_credentials.append(ic)

                cur.execute(
                    """
                    INSERT INTO destruction_items
                        (batch_id, archive_no, position, destroyed, reason,
                         retain_until, sealed_by_cases, prior_request_no,
                         item_credential)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        batch["id"], archive_no, position, destroyed, reason,
                        arch["retain_until"], cases, prior_request_no, ic,
                    ),
                )

                results_payload.append(
                    {
                        "position": position,
                        "archive_no": archive_no,
                        "destroyed": destroyed,
                        "reason": reason,
                        "retain_until": arch["retain_until"].isoformat(),
                        "sealed_by_cases": cases,
                        "prior_request_no": prior_request_no,
                        "item_credential": ic,
                    }
                )

            # 7) 批次凭据 (哈希链) 并一次提交
            batch_obj = cred.build_batch_obj(
                request_no=body.request_no,
                archive_numbers=numbers,
                adjudicated_at=now.isoformat(),
                destroyed_count=destroyed_count,
                not_destroyed_count=not_destroyed_count,
                item_credentials=item_credentials,
                prev_credential=prev_credential,
            )
            batch_cred = cred.batch_credential(batch_obj)
            canonical = cred.canonical_json(batch_obj)

            cur.execute(
                """
                UPDATE destruction_batches
                   SET status = 'completed',
                       seq = %s,
                       adjudicated_at = %s,
                       destroyed_count = %s,
                       not_destroyed_count = %s,
                       prev_credential = %s,
                       credential = %s,
                       canonical_payload = %s
                 WHERE id = %s AND status = 'processing'
                """,
                (chain_seq, now, destroyed_count, not_destroyed_count,
                 prev_credential, batch_cred, canonical, batch["id"]),
            )
            if cur.rowcount != 1:
                raise DomainError(500, "internal_error", "批次完结失败")

            return {
                "request_no": body.request_no,
                "status": "completed",
                "replayed": False,
                "adjudicated_at": now.isoformat(),
                "destroyed_count": destroyed_count,
                "not_destroyed_count": not_destroyed_count,
                "results": results_payload,
                "credential": batch_cred,
                "prev_credential": prev_credential,
                "seq": chain_seq,
            }


def _load_completed(conn, batch_id: int, replayed: bool) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT request_no, status, adjudicated_at, destroyed_count,
                   not_destroyed_count, credential, prev_credential, seq,
                   archive_numbers
              FROM destruction_batches WHERE id = %s
            """,
            (batch_id,),
        )
        b = cur.fetchone()
        cur.execute(
            """
            SELECT position, archive_no, destroyed, reason, retain_until,
                   sealed_by_cases, prior_request_no, item_credential
              FROM destruction_items WHERE batch_id = %s ORDER BY position
            """,
            (batch_id,),
        )
        results = []
        for r in cur.fetchall():
            results.append(
                {
                    "position": r["position"],
                    "archive_no": r["archive_no"],
                    "destroyed": r["destroyed"],
                    "reason": r["reason"],
                    "retain_until": r["retain_until"].isoformat()
                    if r["retain_until"] is not None else None,
                    "sealed_by_cases": list(r["sealed_by_cases"] or []),
                    "prior_request_no": r["prior_request_no"],
                    "item_credential": r["item_credential"],
                }
            )
    return {
        "request_no": b["request_no"],
        "status": b["status"],
        "replayed": replayed,
        "adjudicated_at": b["adjudicated_at"].isoformat(),
        "destroyed_count": b["destroyed_count"],
        "not_destroyed_count": b["not_destroyed_count"],
        "results": results,
        "credential": b["credential"],
        "prev_credential": b["prev_credential"],
        "seq": b["seq"],
    }


@app.get("/v1/destruction/requests/{request_no}")
def get_destruction(request_no: str):
    with db.pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, status FROM destruction_batches WHERE request_no = %s",
            (request_no,),
        )
        b = cur.fetchone()
        if b is None:
            raise DomainError(404, "request_not_found", "销毁请求不存在",
                              {"request_no": request_no})
        if b["status"] != "completed":
            raise DomainError(409, "request_processing", "请求尚未完成裁决",
                              {"request_no": request_no})
        return _load_completed(conn, b["id"], replayed=False)


@app.get("/v1/destruction/requests/{request_no}/verify")
def verify_destruction(request_no: str):
    """重算哈希并与存储凭据比对, 供复查。"""
    with db.pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, request_no, status, seq, credential, prev_credential,
                   canonical_payload, adjudicated_at, archive_numbers
              FROM destruction_batches WHERE request_no = %s
            """,
            (request_no,),
        )
        b = cur.fetchone()
        if b is None:
            raise DomainError(404, "request_not_found", "销毁请求不存在",
                              {"request_no": request_no})
        if b["status"] != "completed":
            raise DomainError(409, "request_processing", "请求尚未完成裁决",
                              {"request_no": request_no})

        cur.execute(
            """
            SELECT position, archive_no, destroyed, reason, retain_until,
                   sealed_by_cases, prior_request_no, item_credential
              FROM destruction_items WHERE batch_id = %s ORDER BY position
            """,
            (b["id"],),
        )
        items = cur.fetchall()

        cur.execute(
            """
            SELECT credential FROM destruction_batches
             WHERE status = 'completed' AND seq < %s
             ORDER BY seq DESC LIMIT 1
            """,
            (b["seq"],),
        )
        prev = cur.fetchone()
        expected_prev = prev["credential"] if prev else cred.GENESIS

    item_creds: list[str] = []
    item_checks: dict[str, bool] = {}
    results_payload: list[dict] = []
    destroyed_count = 0

    for r in items:
        item_obj = cred.build_item_obj(
            request_no=request_no,
            position=r["position"],
            archive_no=r["archive_no"],
            destroyed=r["destroyed"],
            reason=r["reason"],
            retain_until=r["retain_until"].isoformat()
            if r["retain_until"] is not None else None,
            sealed_by_cases=list(r["sealed_by_cases"] or []),
            prior_request_no=r["prior_request_no"],
            adjudicated_at=b["adjudicated_at"].isoformat(),
        )
        recomputed = cred.item_credential(item_obj)
        item_creds.append(recomputed)
        item_checks[r["archive_no"]] = (recomputed == r["item_credential"])
        if r["destroyed"]:
            destroyed_count += 1
        results_payload.append(
            {
                "position": r["position"],
                "archive_no": r["archive_no"],
                "destroyed": r["destroyed"],
                "reason": r["reason"],
                "retain_until": r["retain_until"].isoformat()
                if r["retain_until"] is not None else None,
                "sealed_by_cases": list(r["sealed_by_cases"] or []),
                "prior_request_no": r["prior_request_no"],
                "item_credential": r["item_credential"],
            }
        )

    batch_obj = cred.build_batch_obj(
        request_no=request_no,
        archive_numbers=list(b["archive_numbers"]),
        adjudicated_at=b["adjudicated_at"].isoformat(),
        destroyed_count=destroyed_count,
        not_destroyed_count=len(items) - destroyed_count,
        item_credentials=item_creds,
        prev_credential=expected_prev,
    )
    recomputed_batch = cred.batch_credential(batch_obj)
    canonical_matches = cred.canonical_json(batch_obj) == b["canonical_payload"]
    chain_ok = expected_prev == b["prev_credential"]
    batch_ok = recomputed_batch == b["credential"]
    all_items_ok = all(item_checks.values()) and len(item_checks) == len(items)

    valid = batch_ok and chain_ok and all_items_ok and canonical_matches

    return {
        "request_no": request_no,
        "valid": valid,
        "checks": {
            "batch_credential": batch_ok,
            "hash_chain_link": chain_ok,
            "all_item_credentials": all_items_ok,
            "canonical_payload": canonical_matches,
        },
        "seq": b["seq"],
        "prev_credential": b["prev_credential"],
        "expected_prev": expected_prev,
        "stored_credential": b["credential"],
        "recomputed_credential": recomputed_batch,
        "adjudicated_at": b["adjudicated_at"].isoformat(),
        "results": results_payload,
    }
