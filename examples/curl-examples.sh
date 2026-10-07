#!/usr/bin/env bash
# 电子档案销毁裁决服务 - curl 调用示例
# 前置: docker compose up -d --build
set -euo pipefail
BASE="${BASE_URL:-http://localhost:8080}"

# 1) 登记档案 (A 已到期, B 未到期)
curl -sS -X POST "$BASE/v1/archives" -H 'Content-Type: application/json' \
  -d '{"archive_no":"ARC-A","retain_until":"2020-01-01T00:00:00Z"}'; echo
curl -sS -X POST "$BASE/v1/archives" -H 'Content-Type: application/json' \
  -d '{"archive_no":"ARC-B","retain_until":"2099-01-01T00:00:00Z"}'; echo

# 2) 案件 C-100 / C-200 分别封存 ARC-A
curl -sS -X POST "$BASE/v1/seals" -H 'Content-Type: application/json' \
  -d '{"archive_no":"ARC-A","case_no":"C-100"}'; echo
curl -sS -X POST "$BASE/v1/seals" -H 'Content-Type: application/json' \
  -d '{"archive_no":"ARC-A","case_no":"C-200"}'; echo

# 3) 批量销毁裁决: A 被封存, B 未到期
curl -sS -X POST "$BASE/v1/destruction/requests" \
  -H 'Content-Type: application/json' \
  -d '{"request_no":"REQ-001","archive_numbers":["ARC-A","ARC-B"]}'; echo

# 4) 只解除 C-100 (C-200 仍生效)
curl -sS -X POST "$BASE/v1/seals/release" -H 'Content-Type: application/json' \
  -d '{"archive_no":"ARC-A","case_no":"C-100"}'; echo

# 5) 同请求号同内容重试 -> replayed=true
curl -sS -X POST "$BASE/v1/destruction/requests" \
  -H 'Content-Type: application/json' \
  -d '{"request_no":"REQ-001","archive_numbers":["ARC-A","ARC-B"]}'; echo

# 6) 查询与凭据校验
curl -sS "$BASE/v1/destruction/requests/REQ-001" | python3 -m json.tool
curl -sS "$BASE/v1/destruction/requests/REQ-001/verify" | python3 -m json.tool
