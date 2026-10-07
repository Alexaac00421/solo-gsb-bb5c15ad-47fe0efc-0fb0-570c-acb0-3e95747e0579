# 电子档案销毁裁决服务

Python 3.11 + FastAPI + PostgreSQL 16 实现。支持档案登记、案件封存/解除、
批量销毁裁决，按**服务端裁决时刻**逐件给出结果，整批一次提交，并保存
**只增不改不删、带哈希链的不可变凭据**。

## 一键启动

```bash
docker compose up -d --build
```

- API 地址: <http://localhost:8080>
- Swagger 文档: <http://localhost:8080/docs>
- OpenAPI: <http://localhost:8080/openapi.json>
- 健康检查: `GET http://localhost:8080/health`
- PostgreSQL: `localhost:5432`（库/用户/密码均为 `archive`）

停止与清空数据：

```bash
docker compose down          # 停止
docker compose down -v       # 停止并删除数据库卷（清空全部数据）
```

数据库结构通过两条路径初始化（内容为同一份幂等脚本，可重复执行）：

1. 首次创建数据卷时由 Postgres 官方镜像执行 `db/init/01_schema.sql`；
2. API 启动时等待数据库就绪后再幂等执行同一脚本（双保险）。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://archive:archive@localhost:5432/archive` | PostgreSQL 连接串；Compose 中指向 `db` 服务 |
| `SCHEMA_PATH` | `/app/db/init/01_schema.sql` | 结构脚本路径（应用启动时幂等执行） |
| `DB_CONNECT_TIMEOUT` | `10` | 启动等待数据库的单次连接超时（秒） |
| `DB_POOL_MIN` / `DB_POOL_MAX` | `1` / `10` | 连接池大小 |

修改端口映射：编辑 `docker-compose.yml` 中 `api.ports`（如 `"9000:8080"`）。

## 业务规则

- 档案登记后处于 `active` 状态，带保留截止时间（`retain_until`，必须带时区）。
- 不同案件可对**同一档案分别封存、分别解除**；解除一个案件不影响其他案件。
- 销毁裁决条件（仅以下全部满足才销毁）：
  1. 裁决时刻（数据库 `now()`）已到/过保留截止时间；
  2. 不存在任何案件的生效封存。
- 每件返回：`destroyed` 与 `reason`：
  - `destroyed`：本次已销毁；
  - `not_due`：裁决时刻未到期；
  - `sealed`：存在有效封存案件，`sealed_by_cases` 同时给出全部生效案件号；
  - `already_destroyed`：此前已被销毁（幂等情形），`prior_request_no` 指向原销毁请求。
- **整批一次提交**：所有件在同一个数据库事务内裁决、状态变更与凭据写入；
  批次中只要有一件档案不存在 → 整批 `404` 回滚，不留任何部分销毁，请求号可重试。
- **并发按提交顺序定结果**：封存/解除/销毁均先以 `SELECT ... FOR UPDATE`
  锁定档案行——封存事务先提交则裁决看到封存而阻止销毁；销毁事务先提交，
  则后到的封存/解除读到 `destroyed` 终态并返回 `409 archive_destroyed`。
- 已销毁档案为终态：不能再封存、解除或再次销毁；历史结果与凭据永久可查。
- **幂等**：
  - 相同 `request_no` + 相同档案集合重试 → 返回原结果，`replayed=true`；
  - 相同 `request_no` + 不同档案集合 → `409 request_content_conflict`；
  - 幂等内容指纹为「去重并排序后的档案编号集合」的 SHA-256。

### 不可变凭据

- 逐件结果与批次结果写入 append-only 表，PostgreSQL 触发器禁止
  `UPDATE`/`DELETE`；已销毁档案行同样禁止修改/删除。
- 每件结果含独立 SHA-256 凭据；批次凭据包含全部件凭据，且链接上一完成批次
  的凭据形成哈希链（创世批次前链为 `GENESIS`）。
- `GET /v1/destruction/requests/{request_no}/verify` 用存储数据重算哈希，
  返回件级、批次级、链链接与规范化载荷四项校验结果，供事后复查。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/archives` | 登记档案（重复编号 `409`） |
| `GET` | `/v1/archives/{archive_no}` | 查询档案、状态与生效封存列表 |
| `POST` | `/v1/seals` | 案件发起封存（档案已销毁 `409`；重复封存 `409`） |
| `POST` | `/v1/seals/release` | 解除该案件封存（其他案件不受影响） |
| `POST` | `/v1/destruction/requests` | 批量销毁裁决（幂等键 `request_no`） |
| `GET` | `/v1/destruction/requests/{request_no}` | 查询历史裁决结果 |
| `GET` | `/v1/destruction/requests/{request_no}/verify` | 重算并校验凭据 |
| `GET` | `/health` | 健康检查 |

错误响应统一格式：

```json
{ "code": "sealed", "message": "...", "details": { } }
```

## 调用示例

启动后任选其一：

```bash
# Python（仅标准库），含完整业务断言与打印
python3 examples/demo.py

# curl 脚本
bash examples/curl-examples.sh
```

典型销毁请求：

```bash
curl -sS -X POST http://localhost:8080/v1/destruction/requests \
  -H 'Content-Type: application/json' \
  -d '{"request_no":"REQ-001","archive_numbers":["ARC-A","ARC-B"]}'
```

响应（裁决时刻 A 已到期但被两个案件封存，B 未到期）：

```json
{
  "request_no": "REQ-001",
  "status": "completed",
  "replayed": false,
  "adjudicated_at": "2026-10-07T08:00:00+00:00",
  "destroyed_count": 0,
  "not_destroyed_count": 2,
  "results": [
    {"position": 1, "archive_no": "ARC-A", "destroyed": false,
     "reason": "sealed", "sealed_by_cases": ["C-100", "C-200"],
     "item_credential": "..."},
    {"position": 2, "archive_no": "ARC-B", "destroyed": false,
     "reason": "not_due", "sealed_by_cases": [],
     "item_credential": "..."}
  ],
  "credential": "…批次哈希链凭据…",
  "prev_credential": "GENESIS",
  "seq": 1
}
```

## 本地开发（不使用 Docker 运行应用，可选）

```bash
pip install -r requirements.txt
# 自行准备 PostgreSQL 16 并执行 db/init/01_schema.sql
export DATABASE_URL=postgresql://user:pass@localhost:5432/archive
uvicorn app.main:app --reload --port 8080
```

## 项目结构

```
app/
  main.py         # HTTP API 与裁决事务
  db.py           # 连接池、等待数据库、幂等建表
  credentials.py  # 规范化载荷 / SHA-256 / 哈希链
  schemas.py      # 请求/响应模型
  config.py       # 环境变量配置
db/init/01_schema.sql  # 表、索引、不可变触发器
examples/demo.py       # 标准库 Python 端到端示例
examples/curl-examples.sh
Dockerfile / docker-compose.yml
```
