"""端到端测试: 对真实 uvicorn 服务 + PostgreSQL 验证全部业务规则。

用法:
    DATABASE_URL=postgresql://archive@/tmp:5433/archive python3 tests/e2e_test.py
"""
import os
import sys
import threading
import time

import httpx
import psycopg
import uvicorn

os.environ.setdefault("DATABASE_URL", "postgresql://archive@/tmp:5433/archive")
os.environ.setdefault("SCHEMA_PATH", "/workspace/db/init/01_schema.sql")

sys.path.insert(0, "/workspace")

from app.main import app  # noqa: E402
from app import credentials as cred  # noqa: E402

BASE = "http://127.0.0.1:8099"
DBURL = os.environ["DATABASE_URL"]
PASS = 0
FAIL = 0


def check(name: str, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def reset_db():
    sql = open(os.environ["SCHEMA_PATH"]).read()
    with psycopg.connect(DBURL, autocommit=True) as conn:
        # 先应用 (可能升级的) 结构; 再以 owner 身份临时禁用用户触发器清空数据
        conn.execute(sql)
        for t in ("destruction_items", "destruction_batches", "seals", "archives"):
            conn.execute(f"ALTER TABLE {t} DISABLE TRIGGER USER")
        conn.execute("TRUNCATE destruction_items, destruction_batches, seals, archives")
        for t in ("destruction_items", "destruction_batches", "seals", "archives"):
            conn.execute(f"ALTER TABLE {t} ENABLE TRIGGER USER")


def main():
    reset_db()
    config = uvicorn.Config(app, host="127.0.0.1", port=8099, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            if httpx.get(BASE + "/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.2)

    c = httpx.Client(base_url=BASE, timeout=30, trust_env=False)

    print("[1] 登记档案")
    r = c.post("/v1/archives", json={"archive_no": "A", "retain_until": "2020-01-01T00:00:00Z"})
    check("register A 201", r.status_code == 201, r.text)
    r = c.post("/v1/archives", json={"archive_no": "B", "retain_until": "2099-01-01T00:00:00Z"})
    check("register B 201", r.status_code == 201, r.text)
    r = c.post("/v1/archives", json={"archive_no": "A", "retain_until": "2020-01-01T00:00:00Z"})
    check("duplicate register 409", r.status_code == 409, r.text)
    r = c.post("/v1/archives", json={"archive_no": "C", "retain_until": "2020-01-01T00:00:00"})
    check("naive datetime rejected 422", r.status_code == 422, r.text)
    r = c.get("/v1/archives/A")
    check("get archive active", r.status_code == 200 and r.json()["status"] == "active", r.text)
    r = c.get("/v1/archives/NOPE")
    check("get missing archive 404", r.status_code == 404)

    print("[2] 封存: 不存在 / 重复")
    r = c.post("/v1/seals", json={"archive_no": "NOPE", "case_no": "X1"})
    check("seal missing archive 404", r.status_code == 404)
    r = c.post("/v1/seals", json={"archive_no": "A", "case_no": "X1"})
    check("seal X1->A 201", r.status_code == 201, r.text)
    r = c.post("/v1/seals", json={"archive_no": "A", "case_no": "X1"})
    check("duplicate seal 409", r.status_code == 409)
    r = c.post("/v1/seals", json={"archive_no": "A", "case_no": "X2"})
    check("seal X2->A 201", r.status_code == 201)
    r = c.get("/v1/archives/A")
    check("two active seals", [s["case_no"] for s in r.json()["active_seals"]] == ["X1", "X2"], r.text)

    print("[3] 首批裁决: A sealed, B not_due")
    r = c.post("/v1/destruction/requests", json={"request_no": "R1", "archive_numbers": ["A", "B"]})
    j = r.json()
    check("R1 200", r.status_code == 200, r.text)
    check("R1 counts 0/2", j["destroyed_count"] == 0 and j["not_destroyed_count"] == 2, j)
    check("R1 A sealed by X1,X2",
          j["results"][0]["reason"] == "sealed" and j["results"][0]["sealed_by_cases"] == ["X1", "X2"], j["results"][0])
    check("R1 B not_due", j["results"][1]["reason"] == "not_due", j["results"][1])
    check("R1 prev GENESIS", j["prev_credential"] == "GENESIS" and j["seq"] == 1, j)
    check("R1 64-hex cred", len(j["credential"]) == 64, j["credential"])
    r = c.get("/v1/archives/A")
    check("A still active", r.json()["status"] == "active")

    print("[4] 不存在档案: 整批报错, 不留部分销毁")
    r = c.post("/v1/destruction/requests", json={"request_no": "R2", "archive_numbers": ["B", "GHOST"]})
    check("missing -> 404", r.status_code == 404 and "GHOST" in r.text, r.text)
    with psycopg.connect(DBURL) as conn:
        n = conn.execute("SELECT count(*) FROM destruction_batches WHERE request_no='R2'").fetchone()[0]
        ni = conn.execute("SELECT count(*) FROM destruction_items i JOIN destruction_batches b ON b.id=i.batch_id WHERE b.request_no='R2'").fetchone()[0]
    check("R2 placeholder rolled back", n == 0 and ni == 0, f"{n},{ni}")
    r = c.post("/v1/destruction/requests", json={"request_no": "R2", "archive_numbers": ["B"]})
    check("R2 reused after rollback works", r.status_code == 200 and r.json()["results"][0]["reason"] == "not_due", r.text)

    print("[5] 解除一个案件不影响其他案件")
    r = c.post("/v1/seals/release", json={"archive_no": "A", "case_no": "X1"})
    check("release X1 200", r.status_code == 200 and r.json()["status"] == "released", r.text)
    r = c.post("/v1/seals", json={"archive_no": "A", "case_no": "X1"})
    check("re-seal after release allowed", r.status_code == 201, r.text)
    c.post("/v1/seals/release", json={"archive_no": "A", "case_no": "X1"})
    r = c.post("/v1/seals/release", json={"archive_no": "A", "case_no": "X1"})
    check("double release 404", r.status_code == 404)
    r = c.post("/v1/destruction/requests", json={"request_no": "R3", "archive_numbers": ["A"]})
    check("R3 still sealed by X2", r.json()["results"][0]["reason"] == "sealed"
          and r.json()["results"][0]["sealed_by_cases"] == ["X2"], r.text)

    print("[6] 解除 X2 后销毁 A")
    r = c.post("/v1/seals/release", json={"archive_no": "A", "case_no": "X2"})
    check("release X2 200", r.status_code == 200)
    r1cred = c.get("/v1/destruction/requests/R1").json()["credential"]
    r3cred = c.get("/v1/destruction/requests/R3").json()["credential"]
    r = c.post("/v1/destruction/requests", json={"request_no": "R4", "archive_numbers": ["A", "B"]})
    j = r.json()
    check("R4 A destroyed", j["results"][0]["destroyed"] is True and j["results"][0]["reason"] == "destroyed", j)
    check("R4 B not_due", j["results"][1]["destroyed"] is False, j)
    check("R4 counts 1/1", j["destroyed_count"] == 1 and j["not_destroyed_count"] == 1)
    check("R4 chain links R3 credential", j["prev_credential"] == r3cred and r3cred != r1cred,
          (j["prev_credential"], r3cred))
    r = c.get("/v1/archives/A")
    check("A destroyed terminal", r.json()["status"] == "destroyed" and r.json()["destroyed_request_no"] == "R4", r.text)

    print("[7] 已销毁终态: 再封存/解除/销毁受限")
    r = c.post("/v1/seals", json={"archive_no": "A", "case_no": "Y"})
    check("seal destroyed 409", r.status_code == 409)
    r = c.post("/v1/seals/release", json={"archive_no": "A", "case_no": "X1"})
    check("release on destroyed 409", r.status_code == 409)
    r = c.post("/v1/destruction/requests", json={"request_no": "R5", "archive_numbers": ["A"]})
    j = r.json()
    check("already_destroyed + prior ref", j["results"][0]["reason"] == "already_destroyed"
          and j["results"][0]["prior_request_no"] == "R4", j)

    print("[8] 幂等: 同号同内容回放, 换内容冲突")
    r1 = c.post("/v1/destruction/requests", json={"request_no": "R1", "archive_numbers": ["A", "B"]})
    j = r1.json()
    check("replay 200 replayed", r1.status_code == 200 and j["replayed"] is True, r1.text)
    check("replay same credential & frozen results", j["results"][0]["reason"] == "sealed", j)
    r2 = c.post("/v1/destruction/requests", json={"request_no": "R1", "archive_numbers": ["B"]})
    check("same no different content 409", r2.status_code == 409 and r2.json()["code"] == "request_content_conflict", r2.text)
    r3 = c.get("/v1/destruction/requests/R1")
    check("GET original result", r3.json()["credential"] == j["credential"] and r3.json()["replayed"] is False)
    r4 = c.get("/v1/destruction/requests/UNKNOWN")
    check("GET unknown 404", r4.status_code == 404)

    print("[9] 凭据校验")
    r = c.get("/v1/destruction/requests/R4/verify")
    j = r.json()
    check("verify R4 valid", j["valid"] is True and all(j["checks"].values()), j["checks"])
    # 篡改数据库 -> 校验失败
    with psycopg.connect(DBURL, autocommit=True) as conn:
        conn.execute("ALTER TABLE destruction_items DISABLE TRIGGER trg_items_immutable")
        conn.execute("UPDATE destruction_items SET reason='not_due' WHERE archive_no='A'")
        conn.execute("ALTER TABLE destruction_items ENABLE TRIGGER trg_items_immutable")
    r = c.get("/v1/destruction/requests/R4/verify")
    j = r.json()
    check("tamper detected", j["valid"] is False and j["checks"]["all_item_credentials"] is False, j["checks"])
    # 恢复
    with psycopg.connect(DBURL, autocommit=True) as conn:
        conn.execute("ALTER TABLE destruction_items DISABLE TRIGGER trg_items_immutable")
        conn.execute("UPDATE destruction_items SET reason='destroyed' WHERE archive_no='A' AND batch_id=(SELECT id FROM destruction_batches WHERE request_no='R4')")
        conn.execute("ALTER TABLE destruction_items ENABLE TRIGGER trg_items_immutable")

    print("[10] 不可变触发器")
    with psycopg.connect(DBURL) as conn:
        try:
            conn.execute("DELETE FROM destruction_items WHERE archive_no='A'")
            conn.commit()
            ok = False
        except psycopg.Error as e:
            ok = "immutable" in str(e)
            conn.rollback()
    check("item DELETE blocked by trigger", ok)
    with psycopg.connect(DBURL) as conn:
        try:
            conn.execute("DELETE FROM seals WHERE case_no='X1'")
            conn.commit()
            ok = False
        except psycopg.Error:
            ok = True
            conn.rollback()
    check("seal DELETE blocked", ok)
    with psycopg.connect(DBURL) as conn:
        try:
            conn.execute("TRUNCATE destruction_items")
            conn.commit()
            ok = False
        except psycopg.Error as e:
            ok = "append-only" in str(e)
            conn.rollback()
    check("TRUNCATE blocked", ok)
    with psycopg.connect(DBURL) as conn:
        try:
            conn.execute("TRUNCATE archives, seals CASCADE")
            conn.commit()
            ok = False
        except psycopg.Error:
            ok = True
            conn.rollback()
    check("TRUNCATE archives cascade also blocked", ok)

    print("[11] 并发(真重叠): 封存事务持锁先提交 -> 阻止等待中的销毁")
    c.post("/v1/archives", json={"archive_no": "P1", "retain_until": "2020-01-01T00:00:00Z"})
    hold11 = threading.Event()
    release11 = threading.Event()

    def seal_holds_lock():
        with psycopg.connect(DBURL) as conn:
            conn.execute("SELECT status FROM archives WHERE archive_no='P1' FOR UPDATE")
            hold11.set()
            release11.wait(timeout=10)
            # 销毁请求正在行锁上等待; 此处封存先提交
            conn.execute("INSERT INTO seals (archive_no, case_no) VALUES ('P1','CASE-CONC')")
            conn.commit()

    th = threading.Thread(target=seal_holds_lock)
    th.start()
    hold11.wait()
    resp11 = {}

    def destroy_waiter():
        resp11["r"] = c.post(
            "/v1/destruction/requests",
            json={"request_no": "RC1", "archive_numbers": ["P1"]},
        )

    dw = threading.Thread(target=destroy_waiter)
    dw.start()
    time.sleep(0.5)
    check("destroy waits behind seal txn", "r" not in resp11)
    release11.set()
    th.join(5)
    dw.join(15)
    j11 = resp11["r"].json()
    check("seal committed first blocks destruction",
          resp11["r"].status_code == 200 and j11["results"][0]["reason"] == "sealed"
          and "CASE-CONC" in j11["results"][0]["sealed_by_cases"], j11)

    print("[12] 并发(真重叠): 销毁事务持锁先提交 -> 等待中的封存 409")
    c.post("/v1/archives", json={"archive_no": "P2", "retain_until": "2020-01-01T00:00:00Z"})
    barrier2 = threading.Barrier(2)
    done = threading.Event()

    def destroy_first():
        # 直接用 HTTP, 服务端事务内部锁 P2 后停住
        with psycopg.connect(DBURL) as conn:
            conn.execute("SELECT status FROM archives WHERE archive_no='P2' FOR UPDATE")
            barrier2.wait()
            conn.execute("UPDATE archives SET status='destroyed' WHERE archive_no='P2'")
            conn.execute("""INSERT INTO destruction_batches
                (seq, request_no, archive_numbers, fingerprint, claim_token, status,
                 adjudicated_at, destroyed_count, not_destroyed_count, prev_credential,
                 credential, canonical_payload)
                VALUES (9001,'RC-RAW',ARRAY['P2']::text[],
                       'x','t','completed',now(),1,0,'GENESIS','c','p')""")
            conn.commit()
            done.set()

    th2 = threading.Thread(target=destroy_first)
    th2.start()
    barrier2.wait()
    time.sleep(0.5)  # 等销毁先提交
    th2.join(timeout=5)
    r = c.post("/v1/seals", json={"archive_no": "P2", "case_no": "LATE"})
    check("destruction committed first -> seal 409", r.status_code == 409
          and r.json()["code"] == "archive_destroyed", r.text)

    print("[13] 行锁真并发: HTTP 销毁持锁期间封存等待, 按提交顺序")
    c.post("/v1/archives", json={"archive_no": "P3", "retain_until": "2020-01-01T00:00:00Z"})
    hold = threading.Event()
    release = threading.Event()

    def slow_destroy_raw():
        with psycopg.connect(DBURL) as conn:
            conn.execute("SELECT status FROM archives WHERE archive_no='P3' FOR UPDATE")
            hold.set()
            release.wait(timeout=10)
            conn.execute("UPDATE archives SET status='destroyed' WHERE archive_no='P3'")
            conn.commit()

    th3 = threading.Thread(target=slow_destroy_raw)
    th3.start()
    hold.wait()
    t0 = time.time()
    seal_resp = {}

    def seal_waiter():
        seal_resp["r"] = c.post("/v1/seals", json={"archive_no": "P3", "case_no": "WAIT"})

    sw = threading.Thread(target=seal_waiter)
    sw.start()
    time.sleep(0.5)
    check("seal waits while destroy txn holds lock", time.time() - t0 > 0.4 and "r" not in seal_resp)
    release.set()
    th3.join(5); sw.join(15)
    check("seal after destroy commit = 409", seal_resp["r"].status_code == 409, seal_resp.get("r"))

    print("[14] 并发完成顺序与占位顺序相反时, 哈希链按完成顺序连续")
    c.post("/v1/archives", json={"archive_no": "Q1", "retain_until": "2020-01-01T00:00:00Z"})
    c.post("/v1/archives", json={"archive_no": "Q2", "retain_until": "2020-01-01T00:00:00Z"})
    go14 = threading.Event()
    done14 = threading.Event()
    results14 = {}

    def slow_batch(request_no, archive_no, delay):
        with psycopg.connect(DBURL) as conn:
            with conn.transaction():
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO destruction_batches
                        (request_no, archive_numbers, fingerprint, claim_token,
                         status, prev_credential)
                    VALUES (%s, %s, %s, %s, 'processing', 'GENESIS')
                    RETURNING id
                    """,
                    (request_no, [archive_no],
                     cred.request_fingerprint([archive_no]),
                     f"tok-{request_no}"),
                )
                bid = cur.fetchone()[0]
                cur.execute("SELECT now()")
                ts = cur.fetchone()[0]
                cur.execute("SELECT retain_until FROM archives WHERE archive_no=%s FOR UPDATE",
                            (archive_no,))
                ru = cur.fetchone()[0]
                iobj = cred.build_item_obj(
                    request_no=request_no, position=1, archive_no=archive_no,
                    destroyed=True, reason="destroyed", retain_until=ru.isoformat(),
                    sealed_by_cases=[], prior_request_no=None,
                    adjudicated_at=ts.isoformat())
                ic = cred.item_credential(iobj)
                go14.wait(timeout=10)
                time.sleep(delay)  # 控制完成顺序: SLOW 先占位但后完成
                cur.execute("SELECT pg_advisory_xact_lock(8801)")
                cur.execute(
                    "SELECT credential, seq FROM destruction_batches "
                    "WHERE status='completed' ORDER BY seq DESC LIMIT 1")
                h = cur.fetchone()
                prev = h[0] if h else cred.GENESIS
                nseq = (h[1] + 1) if h else 1
                bobj = cred.build_batch_obj(
                    request_no=request_no, archive_numbers=[archive_no],
                    adjudicated_at=ts.isoformat(), destroyed_count=1,
                    not_destroyed_count=0, item_credentials=[ic],
                    prev_credential=prev)
                bc = cred.batch_credential(bobj)
                cur.execute(
                    """INSERT INTO destruction_items
                       (batch_id, archive_no, position, destroyed, reason,
                        retain_until, sealed_by_cases, prior_request_no, item_credential)
                       VALUES (%s,%s,1,true,'destroyed',%s,'{}',NULL,%s)""",
                    (bid, archive_no, ru, ic))
                cur.execute(
                    """UPDATE archives SET status='destroyed' WHERE archive_no=%s""",
                    (archive_no,))
                cur.execute(
                    """UPDATE destruction_batches SET status='completed', seq=%s,
                       adjudicated_at=%s, destroyed_count=1, not_destroyed_count=0,
                       prev_credential=%s, credential=%s, canonical_payload=%s
                       WHERE id=%s""",
                    (nseq, ts, prev, bc, cred.canonical_json(bobj), bid))
                results14[request_no] = (nseq, bc)
            done14.set()

    t_slow = threading.Thread(target=slow_batch, args=("SLOW", "Q1", 1.0))
    t_fast = threading.Thread(target=slow_batch, args=("FAST", "Q2", 0.0))
    t_slow.start()
    time.sleep(0.3)   # SLOW 先完成占位
    t_fast.start()
    time.sleep(0.3)   # FAST 后占位
    go14.set()
    t_fast.join(10)
    t_slow.join(10)
    check("both concurrent batches completed", set(results14) == {"FAST", "SLOW"}, results14)
    # FAST 后占位却先完成 -> seq 更小; SLOW 链接 FAST
    check("FAST completed first (seq smaller)",
          results14["FAST"][0] < results14["SLOW"][0], results14)
    rv_fast = c.get("/v1/destruction/requests/FAST/verify").json()
    rv_slow = c.get("/v1/destruction/requests/SLOW/verify").json()
    check("FAST verify valid chain", rv_fast["valid"], rv_fast["checks"])
    check("SLOW verify valid chain", rv_slow["valid"], rv_slow["checks"])
    check("SLOW prev links FAST credential",
          rv_slow["prev_credential"] == rv_fast["stored_credential"],
          (rv_slow["prev_credential"], rv_fast["stored_credential"]))

    server.should_exit = True
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
