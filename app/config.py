import os


class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql://archive:archive@localhost:5432/archive",
    )
    schema_path: str = os.getenv(
        "SCHEMA_PATH", "/app/db/init/01_schema.sql"
    )
    db_connect_timeout: int = int(os.getenv("DB_CONNECT_TIMEOUT", "10"))
    db_pool_min: int = int(os.getenv("DB_POOL_MIN", "1"))
    db_pool_max: int = int(os.getenv("DB_POOL_MAX", "10"))


settings = Settings()
