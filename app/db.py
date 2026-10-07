"""连接池管理 + 启动时幂等建表。"""
from __future__ import annotations

import os
import time

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

pool: ConnectionPool | None = None


def _bootstrap_schema() -> None:
    """等待数据库就绪并幂等执行结构脚本 (docker-entrypoint-initdb.d 之外的双保险)。"""
    deadline = time.monotonic() + 60
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(
                settings.database_url,
                autocommit=True,
                connect_timeout=settings.db_connect_timeout,
            ) as conn:
                path = settings.schema_path
                if not os.path.exists(path):
                    # 本地开发 (非容器) 回退到仓库相对路径
                    path = os.path.join(
                        os.path.dirname(os.path.dirname(__file__)),
                        "db",
                        "init",
                        "01_schema.sql",
                    )
                with open(path, "r", encoding="utf-8") as fh:
                    conn.execute(fh.read())
            return
        except psycopg.OperationalError as exc:  # 数据库可能还在启动
            last_err = exc
            time.sleep(1)
    raise RuntimeError(f"database not ready: {last_err}")


def open_pool() -> None:
    global pool
    _bootstrap_schema()
    pool = ConnectionPool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        kwargs={"row_factory": dict_row},
        open=True,
    )


def close_pool() -> None:
    global pool
    if pool is not None:
        pool.close()
        pool = None
