#!/usr/bin/env python3
"""电子档案销毁裁决服务 - 完整调用示例 (仅依赖标准库)。

用法:
    1. docker compose up -d --build
    2. python3 examples/demo.py
       (可用 BASE_URL=http://host:8080 python3 examples/demo.py 覆盖地址)

演示流程:
    A. 登记两件档案 (一件已到期 / 一件未到期)
    B. 案件 C-100 对 A 发起封存, 案件 C-200 也封存 A
    C. 批量销毁 [A, B]: A 因有效封存未销毁, B 未到期未销毁
    D. 解除 C-100 的封存 (C-200 仍然生效), 再次申请 -> 仍被 C-200 阻止
    E. 解除 C-200, 再次申请 -> A 被销毁
    F. 相同请求号同内容重试 -> replayed=true; 换内容 -> 409
    G. 对已销毁档案再封存 -> 409; 历史凭据仍可查询/校验
    H. 批次中含不存在的档案 -> 整批 404, 不留部分销毁
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.getenv("BASE_URL", "http://localhost:8080")


def call(method: str, path: str, body=None, expect_fail: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode())
            print(f"{method} {path} -> {resp.status}")
            return payload
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        print(f"{method} {path} -> {e.code}: {detail}")
        if not expect_fail:
            sys.exit(1)
        return json.loads(detail)


def show(label: str, value):
    print(f"\n=== {label} ===")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> None:
    # A. 登记档案 (示例日期固定, A 早已到期, B 到 2099 年)
    call("POST", "/v1/archives",
         {"archive_no": "ARC-A", "retain_until": "2020-01-01T00:00:00Z"})
    call("POST", "/v1/archives",
         {"archive_no": "ARC-B", "retain_until": "2099-01-01T00:00:00Z"})
    show("查询 ARC-A", call("GET", "/v1/archives/ARC-A"))

    # B. 两个案件分别封存 A
    call("POST", "/v1/seals", {"archive_no": "ARC-A", "case_no": "C-100"})
    call("POST", "/v1/seals", {"archive_no": "ARC-A", "case_no": "C-200"})

    # C. 首次批量裁决: 两件都不能销毁
    r1 = call("POST", "/v1/destruction/requests",
              {"request_no": "REQ-001", "archive_numbers": ["ARC-A", "ARC-B"]})
    show("REQ-001 裁决结果", r1)

    # D. 只解除 C-100, C-200 继续阻止销毁
    call("POST", "/v1/seals/release",
         {"archive_no": "ARC-A", "case_no": "C-100"})
    r2 = call("POST", "/v1/destruction/requests",
              {"request_no": "REQ-002", "archive_numbers": ["ARC-A"]})
    show("REQ-002: 解除一个案件不影响其他案件", r2)

    # E. 解除 C-200 后 A 可销毁
    call("POST", "/v1/seals/release",
         {"archive_no": "ARC-A", "case_no": "C-200"})
    r3 = call("POST", "/v1/destruction/requests",
              {"request_no": "REQ-003", "archive_numbers": ["ARC-A"]})
    show("REQ-003: A 被销毁", r3)

    # F. 幂等: 同号同内容 -> replayed; 换内容 -> 409
    replay = call("POST", "/v1/destruction/requests",
                  {"request_no": "REQ-001",
                   "archive_numbers": ["ARC-A", "ARC-B"]})
    assert replay["replayed"] is True
    show("REQ-001 同内容重试 (回放原结果)", {"replayed": replay["replayed"],
        "credential": replay["credential"]})
    call("POST", "/v1/destruction/requests",
         {"request_no": "REQ-001", "archive_numbers": ["ARC-A"]},
         expect_fail=True)

    # G. 已销毁档案不得再封存; 历史凭据仍可查询、校验
    call("POST", "/v1/seals",
         {"archive_no": "ARC-A", "case_no": "C-300"}, expect_fail=True)
    verify = call("GET", "/v1/destruction/requests/REQ-003/verify")
    show("REQ-003 凭据校验", {"valid": verify["valid"], "checks": verify["checks"]})

    # H. 不存在的档案: 整批报错, 无部分销毁
    call("POST", "/v1/destruction/requests",
         {"request_no": "REQ-004",
          "archive_numbers": ["ARC-B", "ARC-NOPE"]}, expect_fail=True)
    # ARC-B 依然存在且未被销毁
    show("整批失败后 ARC-B 仍为 active", call("GET", "/v1/archives/ARC-B"))

    print("\n全部演示完成。")


if __name__ == "__main__":
    main()
