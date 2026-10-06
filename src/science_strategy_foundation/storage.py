"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jv_platforms (
    platform_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jv_charter_versions (
    charter_id TEXT PRIMARY KEY,
    platform_id TEXT NOT NULL REFERENCES jv_platforms(platform_id),
    version_no INTEGER NOT NULL,
    priority_rules_json TEXT NOT NULL,
    caps_json TEXT NOT NULL,
    outcome_redistribution TEXT NOT NULL,
    content_json TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    status TEXT NOT NULL CHECK(status IN ('effective','superseded')),
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    UNIQUE(platform_id, version_no)
);
CREATE TABLE IF NOT EXISTS jv_memberships (
    membership_id TEXT PRIMARY KEY,
    platform_id TEXT NOT NULL REFERENCES jv_platforms(platform_id),
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    member_role TEXT NOT NULL CHECK(member_role IN ('lead','partner','observer')),
    member_order INTEGER NOT NULL,
    local_conditions_json TEXT NOT NULL,
    joined_on TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','exited')),
    exited_on TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(platform_id, organization_id)
);
CREATE TABLE IF NOT EXISTS jv_persons (
    person_id TEXT PRIMARY KEY,
    platform_id TEXT NOT NULL REFERENCES jv_platforms(platform_id),
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    person_code TEXT NOT NULL,
    display_name TEXT NOT NULL,
    title TEXT NOT NULL,
    capacity_hours_monthly INTEGER NOT NULL CHECK(capacity_hours_monthly > 0),
    status TEXT NOT NULL CHECK(status IN ('active','departed')),
    departed_on TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(platform_id, person_code)
);
CREATE TABLE IF NOT EXISTS jv_equipment (
    equipment_id TEXT PRIMARY KEY,
    platform_id TEXT NOT NULL REFERENCES jv_platforms(platform_id),
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    equipment_code TEXT NOT NULL,
    display_name TEXT NOT NULL,
    monthly_capacity_hours INTEGER NOT NULL CHECK(monthly_capacity_hours > 0),
    status TEXT NOT NULL CHECK(status IN ('active','down')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(platform_id, equipment_code)
);
CREATE TABLE IF NOT EXISTS jv_fund_sources (
    source_id TEXT PRIMARY KEY,
    platform_id TEXT NOT NULL REFERENCES jv_platforms(platform_id),
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    source_code TEXT NOT NULL,
    display_name TEXT NOT NULL,
    total_amount INTEGER NOT NULL CHECK(total_amount > 0),
    currency TEXT NOT NULL,
    restrictions_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(platform_id, source_code)
);
CREATE TABLE IF NOT EXISTS jv_plans (
    plan_id TEXT PRIMARY KEY,
    platform_id TEXT NOT NULL REFERENCES jv_platforms(platform_id),
    name TEXT NOT NULL,
    priority_rank INTEGER NOT NULL,
    lead_organization_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jv_milestones (
    milestone_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES jv_plans(plan_id),
    milestone_code TEXT NOT NULL,
    name TEXT NOT NULL,
    due_date TEXT NOT NULL,
    requirements_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planned','completed')),
    completed_on TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, milestone_code)
);
CREATE TABLE IF NOT EXISTS jv_commitments (
    commitment_id TEXT PRIMARY KEY,
    platform_id TEXT NOT NULL REFERENCES jv_platforms(platform_id),
    plan_id TEXT NOT NULL,
    milestone_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    resource_type TEXT NOT NULL CHECK(resource_type IN ('person_hours','equipment','funding')),
    resource_id TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK(qty > 0),
    window_start TEXT,
    window_end TEXT,
    tranches_json TEXT,
    period_key TEXT NOT NULL,
    required_signoffs_json TEXT NOT NULL,
    submitted_at TEXT,
    charter_id TEXT,
    signed_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('draft','awaiting_signoff','active','blocked','released','completed','withdrawn')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jv_signoffs (
    commitment_id TEXT NOT NULL REFERENCES jv_commitments(commitment_id),
    organization_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY(commitment_id, organization_id)
);
CREATE TABLE IF NOT EXISTS jv_lifecycle_events (
    lifecycle_event_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES jv_commitments(commitment_id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    effective_on TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL

);
CREATE INDEX IF NOT EXISTS idx_jv_lifecycle_commitment ON jv_lifecycle_events(commitment_id);
CREATE TABLE IF NOT EXISTS jv_allocations (
    allocation_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES jv_commitments(commitment_id),
    kind TEXT NOT NULL CHECK(kind IN ('grant','backfill','release')),
    qty INTEGER NOT NULL CHECK(qty >= 0),
    effective_on TEXT NOT NULL,
    reference_event_id TEXT,
    detail_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jv_performances (
    performance_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES jv_commitments(commitment_id),
    qty INTEGER NOT NULL CHECK(qty > 0),
    occurred_on TEXT NOT NULL,
    ref_type TEXT,
    ref_key TEXT,
    detail_json TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jv_resource_events (
    event_id TEXT PRIMARY KEY,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('departed','downtime')),
    effective_start TEXT NOT NULL,
    effective_end TEXT,
    note TEXT,
    occurred_on TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jv_tranche_events (
    tranche_event_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES jv_commitments(commitment_id),
    tranch_no INTEGER NOT NULL CHECK(tranch_no >= 1),
    amount INTEGER NOT NULL CHECK(amount > 0),
    event_type TEXT NOT NULL CHECK(event_type IN ('scheduled','delayed','received')),
    event_date TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jv_outcome_policies (
    policy_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES jv_plans(plan_id),
    version_no INTEGER NOT NULL,
    shares_json TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    change_note TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, version_no)
);
CREATE TABLE IF NOT EXISTS jv_outcome_awards (
    award_id TEXT PRIMARY KEY,
    milestone_id TEXT NOT NULL UNIQUE,
    plan_id TEXT NOT NULL,
    shares_json TEXT NOT NULL,
    landing_conditions_json TEXT NOT NULL,
    basis_json TEXT NOT NULL,
    finalized_on TEXT NOT NULL,
    finalized_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jv_commitments_resource ON jv_commitments(resource_type, resource_id, period_key);
CREATE INDEX IF NOT EXISTS idx_jv_allocations_commitment ON jv_allocations(commitment_id);
CREATE INDEX IF NOT EXISTS idx_jv_performances_commitment ON jv_performances(commitment_id);
CREATE INDEX IF NOT EXISTS idx_jv_tranches_commitment ON jv_tranche_events(commitment_id);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
