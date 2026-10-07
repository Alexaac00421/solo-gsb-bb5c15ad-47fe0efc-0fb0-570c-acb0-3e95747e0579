-- 电子档案销毁裁决服务 - 数据库结构 (幂等, 可重复执行)

-- 档案主表
CREATE TABLE IF NOT EXISTS archives (
    id           BIGSERIAL PRIMARY KEY,
    archive_no   TEXT NOT NULL UNIQUE,
    retain_until TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','destroyed')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 封存记录: 每个 (档案, 案件) 至多有一条生效封存; 解除后保留历史行 (released 状态)
CREATE TABLE IF NOT EXISTS seals (
    id           BIGSERIAL PRIMARY KEY,
    archive_no   TEXT NOT NULL REFERENCES archives(archive_no),
    case_no      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','released')),
    sealed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at  TIMESTAMPTZ
);

-- 部分唯一索引: 同一案件对同一档案不能重复生效封存
CREATE UNIQUE INDEX IF NOT EXISTS uq_seal_active
    ON seals (archive_no, case_no) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_seals_active ON seals (archive_no) WHERE status = 'active';

-- 销毁批次 (幂等键)。seq 为哈希链序号, 在批次完成时 (串行点) 分配
CREATE TABLE IF NOT EXISTS destruction_batches (
    id                 BIGSERIAL PRIMARY KEY,
    seq                BIGINT UNIQUE,
    request_no         TEXT NOT NULL UNIQUE,
    archive_numbers    TEXT[] NOT NULL,
    fingerprint        TEXT NOT NULL,
    claim_token        TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'processing'
                         CHECK (status IN ('processing','completed')),
    adjudicated_at     TIMESTAMPTZ,
    destroyed_count    INT,
    not_destroyed_count INT,
    prev_credential    TEXT NOT NULL,
    credential         TEXT,
    canonical_payload  TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 逐件销毁结果 (不可变凭据载体, 只增不改不删)
CREATE TABLE IF NOT EXISTS destruction_items (
    id              BIGSERIAL PRIMARY KEY,
    batch_id        BIGINT NOT NULL REFERENCES destruction_batches(id),
    archive_no      TEXT NOT NULL,
    position        INT NOT NULL,
    destroyed       BOOLEAN NOT NULL,
    reason          TEXT NOT NULL CHECK (reason IN ('destroyed','not_due','sealed','already_destroyed')),
    retain_until    TIMESTAMPTZ,
    sealed_by_cases TEXT[] NOT NULL DEFAULT '{}',
    prior_request_no TEXT,
    item_credential TEXT NOT NULL,
    UNIQUE (batch_id, position)
);
CREATE INDEX IF NOT EXISTS idx_items_archive ON destruction_items (archive_no);
CREATE INDEX IF NOT EXISTS idx_items_batch ON destruction_items (batch_id);

-- 销毁批次序号 (占位插入不再占用; 完成时在串行点按 max+1 分配链序号)
-- 兼容升级: 旧库中 seq 可能为 NOT NULL
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'destruction_batches' AND column_name = 'seq'
           AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE destruction_batches ALTER COLUMN seq DROP NOT NULL;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 不变式触发器:
--   archives:           destroyed 终态, 禁止任何 UPDATE / DELETE
--   seals:              仅允许 active -> released; 禁止 DELETE
--   destruction_batches: 仅允许 processing -> completed 一次; 不允许 DELETE
--   destruction_items:  完全禁止 UPDATE / DELETE
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION enforce_immutability() RETURNS trigger AS $$
BEGIN
    IF TG_TABLE_NAME = 'archives' THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'archives are append-only and status is terminal';
        END IF;
        IF OLD.status = 'destroyed' THEN
            RAISE EXCEPTION 'destroyed archive % is immutable', OLD.archive_no;
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'seals' THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'seal history must not be deleted';
        END IF;
        IF NOT (OLD.status = 'active' AND NEW.status = 'released'
                AND NEW.released_at IS NOT NULL) THEN
            RAISE EXCEPTION 'only active -> released transition is allowed';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'destruction_batches' THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'destruction batches are immutable';
        END IF;
        IF NOT (OLD.status = 'processing' AND NEW.status = 'completed'
                AND NEW.credential IS NOT NULL) THEN
            RAISE EXCEPTION 'only processing -> completed transition is allowed';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'destruction_items' THEN
        RAISE EXCEPTION 'destruction items are immutable';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_archives_immutable ON archives;
CREATE TRIGGER trg_archives_immutable
    BEFORE UPDATE OR DELETE ON archives
    FOR EACH ROW EXECUTE FUNCTION enforce_immutability();

DROP TRIGGER IF EXISTS trg_seals_immutable ON seals;
CREATE TRIGGER trg_seals_immutable
    BEFORE UPDATE OR DELETE ON seals
    FOR EACH ROW EXECUTE FUNCTION enforce_immutability();

DROP TRIGGER IF EXISTS trg_batches_immutable ON destruction_batches;
CREATE TRIGGER trg_batches_immutable
    BEFORE UPDATE OR DELETE ON destruction_batches
    FOR EACH ROW EXECUTE FUNCTION enforce_immutability();

DROP TRIGGER IF EXISTS trg_items_immutable ON destruction_items;
CREATE TRIGGER trg_items_immutable
    BEFORE UPDATE OR DELETE ON destruction_items
    FOR EACH ROW EXECUTE FUNCTION enforce_immutability();

-- 语句级: 禁止 TRUNCATE 绕过行级不可变约束
CREATE OR REPLACE FUNCTION forbid_truncate() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'table % is append-only; TRUNCATE is forbidden', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_no_truncate_archives ON archives;
CREATE TRIGGER trg_no_truncate_archives BEFORE TRUNCATE ON archives
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_truncate();
DROP TRIGGER IF EXISTS trg_no_truncate_seals ON seals;
CREATE TRIGGER trg_no_truncate_seals BEFORE TRUNCATE ON seals
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_truncate();
DROP TRIGGER IF EXISTS trg_no_truncate_batches ON destruction_batches;
CREATE TRIGGER trg_no_truncate_batches BEFORE TRUNCATE ON destruction_batches
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_truncate();
DROP TRIGGER IF EXISTS trg_no_truncate_items ON destruction_items;
CREATE TRIGGER trg_no_truncate_items BEFORE TRUNCATE ON destruction_items
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_truncate();
