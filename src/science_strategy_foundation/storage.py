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
CREATE TABLE IF NOT EXISTS charter_versions (
    charter_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    title TEXT NOT NULL,
    terms_json TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (charter_id, version)
);
CREATE TABLE IF NOT EXISTS members (
    member_id TEXT PRIMARY KEY REFERENCES organizations(organization_id),
    charter_id TEXT NOT NULL,
    role TEXT NOT NULL,
    fund_cap REAL NOT NULL CHECK(fund_cap >= 0),
    hour_cap REAL NOT NULL CHECK(hour_cap >= 0),
    slot_cap REAL NOT NULL CHECK(slot_cap >= 0),
    joined_at TEXT NOT NULL,
    exited_at TEXT,
    exit_effective_from TEXT
);
CREATE TABLE IF NOT EXISTS resources (
    resource_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('talent_hour','equipment_slot','fund')),
    unique_key TEXT NOT NULL,
    owner_member_id TEXT NOT NULL REFERENCES members(member_id),
    label TEXT NOT NULL,
    capacity REAL NOT NULL CHECK(capacity > 0),
    conditions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(kind, unique_key)
);
CREATE TABLE IF NOT EXISTS milestones (
    milestone_id TEXT PRIMARY KEY,
    charter_id TEXT NOT NULL,
    title TEXT NOT NULL,
    due_date TEXT NOT NULL,
    requirements_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS priority_rulesets (
    ruleset_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK(version >= 1),
    rules_json TEXT NOT NULL,
    frozen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL REFERENCES members(member_id),
    milestone_id TEXT NOT NULL REFERENCES milestones(milestone_id),
    lines_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','active','closed')),
    created_at TEXT NOT NULL,
    activated_at TEXT,
    closed_at TEXT,
    close_reason TEXT
);
CREATE TABLE IF NOT EXISTS countersignatures (
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    signer_member_id TEXT NOT NULL REFERENCES members(member_id),
    signed_at TEXT NOT NULL,
    PRIMARY KEY (commitment_id, signer_member_id)
);
CREATE TABLE IF NOT EXISTS fulfillments (
    fulfillment_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    resource_id TEXT NOT NULL REFERENCES resources(resource_id),
    milestone_id TEXT NOT NULL REFERENCES milestones(milestone_id),
    member_id TEXT NOT NULL REFERENCES members(member_id),
    quantity REAL NOT NULL CHECK(quantity > 0),
    occurred_on TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_disruptions (
    disruption_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    resource_id TEXT REFERENCES resources(resource_id),
    member_id TEXT REFERENCES members(member_id),
    capacity_delta REAL,
    effective_from TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcome_distributions (
    distribution_id TEXT PRIMARY KEY,
    milestone_id TEXT NOT NULL UNIQUE REFERENCES milestones(milestone_id),
    allocations_json TEXT NOT NULL,
    decided_on TEXT NOT NULL,
    created_at TEXT NOT NULL
);
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
