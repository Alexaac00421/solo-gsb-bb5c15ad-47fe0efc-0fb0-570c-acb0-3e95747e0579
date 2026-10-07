"""销毁凭据: 规范化载荷 + SHA-256 哈希链。

凭据设计目标是"可复查、不可篡改":
  * 每件销毁结果有独立 item 凭据;
  * 批次凭据链在前一批次凭据之上 (创世批次前链为 GENESIS);
  * 全部结果以 append-only 表 + 数据库触发器禁止 UPDATE/DELETE;
  * GET /v1/destruction/requests/{request_no}/verify 可重新计算并比对。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

GENESIS = "GENESIS"


def canonical_json(obj: Any) -> str:
    """确定的 JSON 规范化: 键排序、无空白、不转义非 ASCII。"""
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_fingerprint(archive_numbers: list[str]) -> str:
    """幂等请求内容指纹: 去重并排序后的档案编号集合。"""
    return sha256_hex(canonical_json(sorted(set(archive_numbers))))


def build_item_obj(
    *,
    request_no: str,
    position: int,
    archive_no: str,
    destroyed: bool,
    reason: str,
    retain_until: str | None,
    sealed_by_cases: list[str],
    prior_request_no: str | None,
    adjudicated_at: str,
) -> dict[str, Any]:
    return {
        "request_no": request_no,
        "position": position,
        "archive_no": archive_no,
        "destroyed": destroyed,
        "reason": reason,
        "retain_until": retain_until,
        "sealed_by_cases": sealed_by_cases,
        "prior_request_no": prior_request_no,
        "adjudicated_at": adjudicated_at,
    }


def item_credential(item_obj: dict[str, Any]) -> str:
    return sha256_hex("item:" + canonical_json(item_obj))


def build_batch_obj(
    *,
    request_no: str,
    archive_numbers: list[str],
    adjudicated_at: str,
    destroyed_count: int,
    not_destroyed_count: int,
    item_credentials: list[str],
    prev_credential: str,
) -> dict[str, Any]:
    return {
        "request_no": request_no,
        "archive_numbers": archive_numbers,
        "adjudicated_at": adjudicated_at,
        "destroyed_count": destroyed_count,
        "not_destroyed_count": not_destroyed_count,
        "items": item_credentials,
        "prev_credential": prev_credential,
    }


def batch_credential(batch_obj: dict[str, Any]) -> str:
    return sha256_hex("batch:" + canonical_json(batch_obj))
