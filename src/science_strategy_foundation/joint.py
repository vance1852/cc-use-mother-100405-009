"""区域创新联合投入与履约服务。

在基础服务之上登记合作章程版本、成员资格、真实资源、里程碑、承诺、
会签、履约、扰动与成果权益，并保证：

- 同一人才、设备、资金只登记一次，所有承诺从统一额度池中扣减，
  各地不能把同一位专家、同一台设备重复计入自己的配套承诺；
- 承诺经全体成员会签后才占用可分配额度；
- 成员上限按聚合口径校验，拆分承诺不能绕过；
- 同一资源跨计划冲突时按已冻结的优先规则裁定；
- 人员离任、资金延期、设备停机、部分履约和成员退出只重算未来义务，
  已完成的投入与成果分配不得回写；
- 支持按历史日期复原当时有效的章程版本与承诺。
"""

from __future__ import annotations

import functools
import json
import math
from datetime import date
from typing import Any

from .audit import append_event, canonical_json
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .service import DomainService

RESOURCE_KINDS = frozenset({"talent_hour", "equipment_slot", "fund"})
MEMBER_ROLES = frozenset({"lead", "participant"})
DISRUPTION_KINDS = frozenset({"personnel_departure", "fund_delay", "equipment_downtime", "member_exit"})
CLOSE_REASONS = frozenset({"completed", "partial_fulfillment", "cancelled"})
RULE_KEYS = frozenset({"milestone_due_date", "commitment_created_at", "member_joined_at"})
CAP_COLUMN = {"talent_hour": "hour_cap", "equipment_slot": "slot_cap", "fund": "fund_cap"}
DISRUPTION_RESOURCE_KIND = {
    "personnel_departure": "talent_hour",
    "fund_delay": "fund",
    "equipment_downtime": "equipment_slot",
}
DEFAULT_PRIORITY_ORDER = (
    {"key": "milestone_due_date", "direction": "asc"},
    {"key": "commitment_created_at", "direction": "asc"},
)
EPSILON = 1e-9


def _check_date(value: str, field: str) -> str:
    value = str(value).strip()
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期") from None
    return value


def _check_quantity(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} 必须是正数") from None
    if math.isnan(number) or math.isinf(number) or number <= 0:
        raise ValidationError(f"{field} 必须是正数")
    return round(number, 6)


def _check_cap(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} 必须是非负数值") from None
    if math.isnan(number) or math.isinf(number) or number < 0:
        raise ValidationError(f"{field} 必须是非负数值")
    return round(number, 6)


def _check_delta(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} 必须是非零数值") from None
    if math.isnan(number) or math.isinf(number) or number == 0:
        raise ValidationError(f"{field} 必须是非零数值")
    return round(number, 6)


class JointService:
    """协调联合投入的承诺、额度、履约与权益规则。"""

    def __init__(self, foundation: DomainService) -> None:
        self.foundation = foundation
        self.database = foundation.database

    # ---- 基础工具 ----

    def _now(self) -> str:
        return self.foundation.now_text()

    def _today(self) -> str:
        return self._now()[:10]

    def _actor(self, connection, actor_id: str) -> Actor:
        return self.foundation.authenticate(connection, actor_id)

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _require_member_actor(self, actor: Actor, member_id: str) -> None:
        if actor.role == "admin":
            return
        if actor.role == "operator" and actor.organization_id == member_id:
            return
        raise PermissionDenied("只能代表本成员机构执行该动作")

    def _member_row(self, connection, member_id: str):
        row = connection.execute("SELECT * FROM members WHERE member_id=?", (member_id,)).fetchone()
        if row is None:
            raise NotFoundError("成员不存在")
        return row

    def _active_member(self, connection, member_id: str):
        row = self._member_row(connection, member_id)
        if row["exited_at"] is not None:
            raise ConflictError("成员已退出")
        return row

    def _resource_row(self, connection, resource_id: str):
        row = connection.execute("SELECT * FROM resources WHERE resource_id=?", (resource_id,)).fetchone()
        if row is None:
            raise NotFoundError("资源不存在")
        return row

    def _milestone_row(self, connection, milestone_id: str):
        row = connection.execute("SELECT * FROM milestones WHERE milestone_id=?", (milestone_id,)).fetchone()
        if row is None:
            raise NotFoundError("里程碑不存在")
        return row

    def _commitment_row(self, connection, commitment_id: str):
        row = connection.execute("SELECT * FROM commitments WHERE commitment_id=?", (commitment_id,)).fetchone()
        if row is None:
            raise NotFoundError("承诺不存在")
        return row

    # ---- 额度与占用 ----

    def _effective_capacity(self, connection, resource_id: str, on_date: str) -> float:
        """资源在某日期的有效额度：基础额度加上该日期前生效的扰动调整。"""

        row = self._resource_row(connection, resource_id)
        delta = connection.execute(
            "SELECT COALESCE(SUM(capacity_delta),0) AS total FROM resource_disruptions "
            "WHERE resource_id=? AND effective_from<=?",
            (resource_id, on_date),
        ).fetchone()["total"]
        return round(min(row["capacity"], max(0.0, row["capacity"] + delta)), 6)

    def _active_commitment_rows(self, connection, on_date: str, member_id: str | None = None):
        """复原某日期仍然生效的承诺：已会签生效且在该日期前未关闭。"""

        query = ("SELECT * FROM commitments WHERE activated_at IS NOT NULL "
                 "AND substr(activated_at,1,10)<=? "
                 "AND (closed_at IS NULL OR substr(closed_at,1,10)>?)")
        parameters: list[Any] = [on_date, on_date]
        if member_id:
            query += " AND member_id=?"
            parameters.append(member_id)
        query += " ORDER BY commitment_id"
        return connection.execute(query, parameters).fetchall()

    def _allocated_quantity(self, connection, resource_id: str, on_date: str,
                            exclude_member: str | None = None) -> float:
        total = 0.0
        for row in self._active_commitment_rows(connection, on_date):
            if exclude_member and row["member_id"] == exclude_member:
                continue
            for line in json.loads(row["lines_json"]):
                if line["resource_id"] == resource_id:
                    total += line["quantity"]
        return round(total, 6)

    def _member_usage(self, connection, member_id: str, on_date: str) -> dict[str, float]:
        """成员在某日期的聚合占用，按资源类别汇总，拆分承诺不能绕过。"""

        usage = {kind: 0.0 for kind in RESOURCE_KINDS}
        for row in self._active_commitment_rows(connection, on_date, member_id):
            for line in json.loads(row["lines_json"]):
                kind = self._resource_row(connection, line["resource_id"])["kind"]
                usage[kind] += line["quantity"]
        return {kind: round(total, 6) for kind, total in usage.items()}

    # ---- 优先规则裁定 ----

    def _priority_order(self, connection) -> tuple[str, list[dict[str, str]]]:
        row = connection.execute(
            "SELECT * FROM priority_rulesets ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return "default", [dict(rule) for rule in DEFAULT_PRIORITY_ORDER]
        return row["ruleset_id"], json.loads(row["rules_json"])["order"]

    def _priority_compare(self, connection, order: list[dict[str, str]]):
        milestones = {row["milestone_id"]: row for row in connection.execute("SELECT * FROM milestones")}
        members = {row["member_id"]: row for row in connection.execute("SELECT * FROM members")}

        def key_of(row, key: str) -> str:
            if key == "milestone_due_date":
                return milestones[row["milestone_id"]]["due_date"]
            if key == "member_joined_at":
                return members[row["member_id"]]["joined_at"]
            return row["created_at"]

        def compare(left, right) -> int:
            for rule in order:
                former, latter = key_of(left, rule["key"]), key_of(right, rule["key"])
                if former == latter:
                    continue
                result = -1 if former < latter else 1
                return result if rule.get("direction", "asc") == "asc" else -result
            return 0

        return compare

    def _adjudicate(self, connection, resource_id: str, candidate, on_date: str) -> dict[str, Any]:
        """按冻结的优先规则裁定同一资源上的跨计划冲突。"""

        capacity = self._effective_capacity(connection, resource_id, on_date)
        ruleset_id, order = self._priority_order(connection)
        competitors = [row for row in self._active_commitment_rows(connection, on_date)
                       if any(line["resource_id"] == resource_id for line in json.loads(row["lines_json"]))]
        competitors.append(candidate)
        competitors.sort(key=functools.cmp_to_key(self._priority_compare(connection, order)))
        remaining = capacity
        winners, losers = [], []
        for row in competitors:
            quantity = sum(line["quantity"] for line in json.loads(row["lines_json"])
                           if line["resource_id"] == resource_id)
            if quantity <= remaining + EPSILON:
                winners.append(row["commitment_id"])
                remaining -= quantity
            else:
                losers.append(row["commitment_id"])
        rank = competitors.index(candidate) + 1
        return {"ruleset_id": ruleset_id, "capacity": capacity, "rank": rank,
                "winners": winners, "losers": losers}

    def _activate_commitment(self, connection, commitment, actor_id: str) -> None:
        """会签完成后生效：聚合校验成员上限与资源额度，然后占用额度。"""

        member = self._active_member(connection, commitment["member_id"])
        today = self._today()
        lines = json.loads(commitment["lines_json"])
        usage = self._member_usage(connection, member["member_id"], today)
        for line in lines:
            kind = self._resource_row(connection, line["resource_id"])["kind"]
            usage[kind] += line["quantity"]
        for kind, total in usage.items():
            cap = member[CAP_COLUMN[kind]]
            if total > cap + EPSILON:
                raise ConflictError(f"成员 {kind} 投入合计 {total} 超出上限 {cap}，拆分承诺不能绕过上限")
        for line in lines:
            resource_id = line["resource_id"]
            allocated = self._allocated_quantity(connection, resource_id, today)
            capacity = self._effective_capacity(connection, resource_id, today)
            if allocated + line["quantity"] > capacity + EPSILON:
                ruling = self._adjudicate(connection, resource_id, commitment, today)
                raise ConflictError(
                    f"资源 {resource_id} 可分配额度不足（剩余 {round(capacity - allocated, 6)}），"
                    f"按冻结优先规则 {ruling['ruleset_id']} 裁定本承诺排序第 {ruling['rank']} 位，不能生效"
                )
        now = self._now()
        connection.execute("UPDATE commitments SET status='active', activated_at=? WHERE commitment_id=?",
                           (now, commitment["commitment_id"]))
        append_event(connection, actor_id=actor_id, action="joint.commitment_activated",
                     resource_type="commitment", resource_id=commitment["commitment_id"],
                     detail={"member_id": commitment["member_id"],
                             "milestone_id": commitment["milestone_id"]},
                     occurred_at=now)

    # ---- 章程与成员 ----

    def register_charter_version(self, *, request_id: str, actor_id: str, charter_id: str,
                                 version: int, title: str, terms: dict[str, Any],
                                 effective_from: str) -> WriteReceipt:
        """登记合作章程版本，版本递增且生效日期不回溯，历史版本可复原。"""

        payload = {"actor_id": actor_id, "charter_id": charter_id, "version": version,
                   "title": title, "terms": terms, "effective_from": effective_from}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.foundation.require_roles(actor, "admin")
            charter_id = self.foundation.check_identifier(charter_id, "charter_id")
            try:
                version = int(version)
            except (TypeError, ValueError):
                raise ValidationError("version 必须是正整数") from None
            if version < 1:
                raise ValidationError("version 必须是正整数")
            title = self._text(title, "title")
            if not isinstance(terms, dict) or not terms:
                raise ValidationError("terms 必须是非空对象")
            effective_from = _check_date(effective_from, "effective_from")

            def create() -> tuple[str, str, dict[str, Any]]:
                previous = connection.execute(
                    "SELECT MAX(version) AS version, MAX(effective_from) AS effective_from "
                    "FROM charter_versions WHERE charter_id=?", (charter_id,)).fetchone()
                if previous["version"] is not None:
                    if version <= previous["version"]:
                        raise ConflictError("章程版本必须递增")
                    if effective_from < previous["effective_from"]:
                        raise ValidationError("新版本生效日期不能早于已有版本")
                connection.execute(
                    "INSERT INTO charter_versions(charter_id,version,title,terms_json,effective_from,registered_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (charter_id, version, title, canonical_json(terms), effective_from, self._now()))
                append_event(connection, actor_id=actor_id, action="joint.charter_version_registered",
                             resource_type="charter_version", resource_id=f"{charter_id}@{version}",
                             detail={"charter_id": charter_id, "version": version,
                                     "effective_from": effective_from},
                             occurred_at=self._now())
                return "charter_version", f"{charter_id}@{version}", {"charter_id": charter_id, "version": version}

            return self.foundation.idempotent(connection, request_id=request_id,
                                              action="joint.register_charter_version",
                                              payload=payload, create=create)

    def admit_member(self, *, request_id: str, actor_id: str, member_id: str, charter_id: str,
                     role: str, fund_cap: float, hour_cap: float, slot_cap: float,
                     joined_at: str) -> WriteReceipt:
        """登记成员资格与投入上限，上限按成员聚合执行。"""

        payload = {"actor_id": actor_id, "member_id": member_id, "charter_id": charter_id,
                   "role": role, "fund_cap": fund_cap, "hour_cap": hour_cap,
                   "slot_cap": slot_cap, "joined_at": joined_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.foundation.require_roles(actor, "admin")
            member_id = self.foundation.check_identifier(member_id, "member_id")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (member_id,)).fetchone() is None:
                raise NotFoundError("成员机构未在基础服务登记")
            if connection.execute("SELECT 1 FROM charter_versions WHERE charter_id=?",
                                  (charter_id,)).fetchone() is None:
                raise NotFoundError("合作章程不存在")
            if role not in MEMBER_ROLES:
                raise ValidationError("role 不在允许范围内")
            fund_cap = _check_cap(fund_cap, "fund_cap")
            hour_cap = _check_cap(hour_cap, "hour_cap")
            slot_cap = _check_cap(slot_cap, "slot_cap")
            joined_at = _check_date(joined_at, "joined_at")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO members(member_id,charter_id,role,fund_cap,hour_cap,slot_cap,joined_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (member_id, charter_id, role, fund_cap, hour_cap, slot_cap, joined_at))
                except Exception as exc:
                    raise ConflictError("成员已经存在") from exc
                append_event(connection, actor_id=actor_id, action="joint.member_admitted",
                             resource_type="member", resource_id=member_id,
                             detail={"charter_id": charter_id, "role": role,
                                     "fund_cap": fund_cap, "hour_cap": hour_cap, "slot_cap": slot_cap},
                             occurred_at=self._now())
                return "member", member_id, {"member_id": member_id}

            return self.foundation.idempotent(connection, request_id=request_id,
                                              action="joint.admit_member", payload=payload, create=create)

    # ---- 资源、里程碑与优先规则 ----

    def register_resource(self, *, request_id: str, actor_id: str, resource_id: str, kind: str,
                          unique_key: str, owner_member_id: str, label: str, capacity: float,
                          conditions: dict[str, Any] | None = None) -> WriteReceipt:
        """登记真实资源，同一类别同一标识全局唯一，防止重复计入。"""

        payload = {"actor_id": actor_id, "resource_id": resource_id, "kind": kind,
                   "unique_key": unique_key, "owner_member_id": owner_member_id,
                   "label": label, "capacity": capacity, "conditions": conditions}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if kind not in RESOURCE_KINDS:
                raise ValidationError("资源类别无效")
            self._active_member(connection, owner_member_id)
            self._require_member_actor(actor, owner_member_id)
            resource_id = self.foundation.check_identifier(resource_id, "resource_id")
            unique_key = self._text(unique_key, "unique_key", 120)
            label = self._text(label, "label")
            capacity = _check_quantity(capacity, "capacity")
            if conditions is None:
                conditions = {}
            if not isinstance(conditions, dict):
                raise ValidationError("conditions 必须是对象")

            def create() -> tuple[str, str, dict[str, Any]]:
                duplicate = connection.execute(
                    "SELECT resource_id FROM resources WHERE kind=? AND unique_key=?",
                    (kind, unique_key)).fetchone()
                if duplicate:
                    raise ConflictError(f"同一资源已登记为 {duplicate['resource_id']}，不能重复计入")
                connection.execute(
                    "INSERT INTO resources(resource_id,kind,unique_key,owner_member_id,label,capacity,"
                    "conditions_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (resource_id, kind, unique_key, owner_member_id, label, capacity,
                     canonical_json(conditions), self._now()))
                append_event(connection, actor_id=actor_id, action="joint.resource_registered",
                             resource_type="resource", resource_id=resource_id,
                             detail={"kind": kind, "unique_key": unique_key,
                                     "owner_member_id": owner_member_id, "capacity": capacity},
                             occurred_at=self._now())
                return "resource", resource_id, {"resource_id": resource_id}

            return self.foundation.idempotent(connection, request_id=request_id,
                                              action="joint.register_resource", payload=payload, create=create)

    def define_milestone(self, *, request_id: str, actor_id: str, milestone_id: str,
                         charter_id: str, title: str, due_date: str,
                         requirements: list[dict[str, Any]]) -> WriteReceipt:
        """登记里程碑及其资源需求，需求可指定具体资源或资源类别。"""

        payload = {"actor_id": actor_id, "milestone_id": milestone_id, "charter_id": charter_id,
                   "title": title, "due_date": due_date, "requirements": requirements}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.foundation.require_roles(actor, "admin")
            milestone_id = self.foundation.check_identifier(milestone_id, "milestone_id")
            if connection.execute("SELECT 1 FROM charter_versions WHERE charter_id=?",
                                  (charter_id,)).fetchone() is None:
                raise NotFoundError("合作章程不存在")
            title = self._text(title, "title")
            due_date = _check_date(due_date, "due_date")
            if not isinstance(requirements, list) or not requirements:
                raise ValidationError("requirements 必须是非空列表")
            normalized = []
            seen = set()
            for item in requirements:
                if not isinstance(item, dict):
                    raise ValidationError("requirements 元素必须是对象")
                quantity = _check_quantity(item.get("quantity"), "requirements.quantity")
                resource_id = item.get("resource_id")
                kind = item.get("kind")
                if resource_id and kind:
                    raise ValidationError("同一需求不能同时指定资源与类别")
                if resource_id:
                    resource = self._resource_row(connection, resource_id)
                    key = ("resource", resource["resource_id"])
                    normalized.append({"resource_id": resource["resource_id"],
                                       "kind": resource["kind"], "quantity": quantity})
                elif kind:
                    if kind not in RESOURCE_KINDS:
                        raise ValidationError("需求类别无效")
                    key = ("kind", kind)
                    normalized.append({"kind": kind, "quantity": quantity})
                else:
                    raise ValidationError("需求必须指定资源或类别")
                if key in seen:
                    raise ValidationError("需求不能重复")
                seen.add(key)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO milestones(milestone_id,charter_id,title,due_date,requirements_json,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (milestone_id, charter_id, title, due_date,
                         canonical_json(normalized), self._now()))
                except Exception as exc:
                    raise ConflictError("里程碑已经存在") from exc
                append_event(connection, actor_id=actor_id, action="joint.milestone_defined",
                             resource_type="milestone", resource_id=milestone_id,
                             detail={"charter_id": charter_id, "due_date": due_date,
                                     "requirements": normalized},
                             occurred_at=self._now())
                return "milestone", milestone_id, {"milestone_id": milestone_id}

            return self.foundation.idempotent(connection, request_id=request_id,
                                              action="joint.define_milestone", payload=payload, create=create)

    def freeze_priority_rules(self, *, request_id: str, actor_id: str, ruleset_id: str,
                              version: int, rules: dict[str, Any]) -> WriteReceipt:
        """冻结一版优先规则，冻结后不可更改，冲突裁定以最高版本为准。"""

        payload = {"actor_id": actor_id, "ruleset_id": ruleset_id, "version": version, "rules": rules}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.foundation.require_roles(actor, "admin")
            ruleset_id = self.foundation.check_identifier(ruleset_id, "ruleset_id")
            try:
                version = int(version)
            except (TypeError, ValueError):
                raise ValidationError("version 必须是正整数") from None
            if version < 1:
                raise ValidationError("version 必须是正整数")
            if not isinstance(rules, dict) or not isinstance(rules.get("order"), list) or not rules["order"]:
                raise ValidationError("rules.order 必须是非空列表")
            for rule in rules["order"]:
                if not isinstance(rule, dict) or rule.get("key") not in RULE_KEYS:
                    raise ValidationError("优先规则键无效")
                if rule.get("direction", "asc") not in ("asc", "desc"):
                    raise ValidationError("优先规则方向无效")

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM priority_rulesets WHERE ruleset_id=?",
                                      (ruleset_id,)).fetchone():
                    raise ConflictError("优先规则已冻结，不能更改")
                current = connection.execute(
                    "SELECT COALESCE(MAX(version),0) AS version FROM priority_rulesets").fetchone()["version"]
                if version <= current:
                    raise ConflictError("优先规则版本必须递增")
                connection.execute(
                    "INSERT INTO priority_rulesets(ruleset_id,version,rules_json,frozen_at) VALUES(?,?,?,?)",
                    (ruleset_id, version, canonical_json(rules), self._now()))
                append_event(connection, actor_id=actor_id, action="joint.priority_rules_frozen",
                             resource_type="priority_ruleset", resource_id=ruleset_id,
                             detail={"version": version, "rules": rules},
                             occurred_at=self._now())
                return "priority_ruleset", ruleset_id, {"ruleset_id": ruleset_id, "version": version}

            return self.foundation.idempotent(connection, request_id=request_id,
                                              action="joint.freeze_priority_rules",
                                              payload=payload, create=create)

    # ---- 承诺与会签 ----

    def draft_commitment(self, *, request_id: str, actor_id: str, commitment_id: str,
                         member_id: str, milestone_id: str,
                         lines: list[dict[str, Any]]) -> WriteReceipt:
        """登记承诺草稿，草稿不占用可分配额度。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "member_id": member_id,
                   "milestone_id": milestone_id, "lines": lines}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._active_member(connection, member_id)
            self._require_member_actor(actor, member_id)
            self._milestone_row(connection, milestone_id)
            commitment_id = self.foundation.check_identifier(commitment_id, "commitment_id")
            if not isinstance(lines, list) or not lines:
                raise ValidationError("lines 必须是非空列表")
            normalized = []
            seen = set()
            for item in lines:
                if not isinstance(item, dict):
                    raise ValidationError("lines 元素必须是对象")
                resource = self._resource_row(connection, item.get("resource_id"))
                if resource["resource_id"] in seen:
                    raise ValidationError("同一承诺中资源不能重复")
                seen.add(resource["resource_id"])
                quantity = _check_quantity(item.get("quantity"), "lines.quantity")
                purpose = item.get("purpose")
                conditions = json.loads(resource["conditions_json"])
                allowed = conditions.get("allowed_purposes")
                if allowed and purpose not in allowed:
                    raise ValidationError("资金用途不在该资源允许范围内")
                line = {"resource_id": resource["resource_id"], "quantity": quantity}
                if purpose is not None:
                    line["purpose"] = self._text(str(purpose), "lines.purpose", 120)
                normalized.append(line)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO commitments(commitment_id,member_id,milestone_id,lines_json,status,created_at) "
                        "VALUES(?,?,?,?, 'draft', ?)",
                        (commitment_id, member_id, milestone_id,
                         canonical_json(normalized), self._now()))
                except Exception as exc:
                    raise ConflictError("承诺编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="joint.commitment_drafted",
                             resource_type="commitment", resource_id=commitment_id,
                             detail={"member_id": member_id, "milestone_id": milestone_id,
                                     "lines": normalized},
                             occurred_at=self._now())
                return "commitment", commitment_id, {"commitment_id": commitment_id, "status": "draft"}

            return self.foundation.idempotent(connection, request_id=request_id,
                                              action="joint.draft_commitment", payload=payload, create=create)

    def countersign_commitment(self, *, request_id: str, actor_id: str, commitment_id: str,
                               signer_member_id: str) -> WriteReceipt:
        """成员会签承诺，全体成员会签完成后承诺才占用可分配额度。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id,
                   "signer_member_id": signer_member_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            commitment = self._commitment_row(connection, commitment_id)
            if commitment["status"] != "draft":
                raise ConflictError("承诺已生效或已关闭，不能再会签")
            self._active_member(connection, signer_member_id)
            self._require_member_actor(actor, signer_member_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute(
                        "SELECT 1 FROM countersignatures WHERE commitment_id=? AND signer_member_id=?",
                        (commitment_id, signer_member_id)).fetchone():
                    raise ConflictError("该成员已会签")
                now = self._now()
                connection.execute(
                    "INSERT INTO countersignatures(commitment_id,signer_member_id,signed_at) VALUES(?,?,?)",
                    (commitment_id, signer_member_id, now))
                append_event(connection, actor_id=actor_id, action="joint.commitment_countersigned",
                             resource_type="countersignature",
                             resource_id=f"{commitment_id}:{signer_member_id}",
                             detail={"commitment_id": commitment_id, "signer_member_id": signer_member_id},
                             occurred_at=now)
                active_members = {row["member_id"] for row in connection.execute(
                    "SELECT member_id FROM members WHERE exited_at IS NULL")}
                signed = {row["signer_member_id"] for row in connection.execute(
                    "SELECT signer_member_id FROM countersignatures WHERE commitment_id=?",
                    (commitment_id,))}
                activated = False
                if active_members <= signed:
                    self._activate_commitment(connection, commitment, actor_id)
                    activated = True
                return ("countersignature", f"{commitment_id}:{signer_member_id}",
                        {"commitment_id": commitment_id, "activated": activated})

            return self.foundation.idempotent(connection, request_id=request_id,
                                              action="joint.countersign_commitment",
                                              payload=payload, create=create)

    def close_commitment(self, *, request_id: str, actor_id: str, commitment_id: str,
                         reason: str, effective_from: str) -> WriteReceipt:
        """关闭承诺，只重算未来义务，已登记的履约不受影响。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id,
                   "reason": reason, "effective_from": effective_from}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if reason not in CLOSE_REASONS:
                raise ValidationError("关闭原因无效")
            commitment = self._commitment_row(connection, commitment_id)
            if commitment["status"] == "closed":
                raise ConflictError("承诺已关闭")
            self._require_member_actor(actor, commitment["member_id"])
            effective_from = _check_date(effective_from, "effective_from")
            if effective_from < self._today():
                raise ValidationError("关闭只重算未来义务，生效日期不能早于今天")
            if commitment["status"] == "draft" and reason != "cancelled":
                raise ConflictError("未生效的承诺只能取消")
            if commitment["status"] == "active" and reason == "cancelled":
                raise ConflictError("已生效的承诺不能取消，请按完成或部分履约关闭")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE commitments SET status='closed', closed_at=?, close_reason=? "
                    "WHERE commitment_id=?",
                    (effective_from, reason, commitment_id))
                append_event(connection, actor_id=actor_id, action="joint.commitment_closed",
                             resource_type="commitment", resource_id=commitment_id,
                             detail={"reason": reason, "effective_from": effective_from},
                             occurred_at=self._now())
                return "commitment_closure", commitment_id, {"commitment_id": commitment_id}

            return self.foundation.idempotent(connection, request_id=request_id,
                                              action="joint.close_commitment", payload=payload, create=create)

    # ---- 履约、扰动与成果权益 ----

    def record_fulfillment(self, *, request_id: str, actor_id: str, fulfillment_id: str,
                           commitment_id: str, resource_id: str, quantity: float,
                           occurred_on: str) -> WriteReceipt:
        """登记已完成的实际投入，登记后不可更改。"""

        payload = {"actor_id": actor_id, "fulfillment_id": fulfillment_id,
                   "commitment_id": commitment_id, "resource_id": resource_id,
                   "quantity": quantity, "occurred_on": occurred_on}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            commitment = self._commitment_row(connection, commitment_id)
            self._require_member_actor(actor, commitment["member_id"])
            fulfillment_id = self.foundation.check_identifier(fulfillment_id, "fulfillment_id")
            self._resource_row(connection, resource_id)
            quantity = _check_quantity(quantity, "quantity")
            occurred_on = _check_date(occurred_on, "occurred_on")
            if occurred_on > self._today():
                raise ValidationError("履约日期不能晚于今天")
            line = next((item for item in json.loads(commitment["lines_json"])
                         if item["resource_id"] == resource_id), None)
            if line is None:
                raise ValidationError("承诺不包含该资源")
            if commitment["status"] == "draft":
                raise ConflictError("承诺尚未会签生效")
            if commitment["status"] == "closed" and occurred_on >= commitment["closed_at"][:10]:
                raise ConflictError("承诺已关闭，不能补记关闭后的投入")

            def create() -> tuple[str, str, dict[str, Any]]:
                delivered = connection.execute(
                    "SELECT COALESCE(SUM(quantity),0) AS total FROM fulfillments "
                    "WHERE commitment_id=? AND resource_id=?",
                    (commitment_id, resource_id)).fetchone()["total"]
                if delivered + quantity > line["quantity"] + EPSILON:
                    raise ConflictError("累计履约不能超过承诺数量")
                connection.execute(
                    "INSERT INTO fulfillments(fulfillment_id,commitment_id,resource_id,milestone_id,"
                    "member_id,quantity,occurred_on,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (fulfillment_id, commitment_id, resource_id, commitment["milestone_id"],
                     commitment["member_id"], quantity, occurred_on, self._now()))
                append_event(connection, actor_id=actor_id, action="joint.fulfillment_recorded",
                             resource_type="fulfillment", resource_id=fulfillment_id,
                             detail={"commitment_id": commitment_id, "resource_id": resource_id,
                                     "quantity": quantity, "occurred_on": occurred_on},
                             occurred_at=self._now())
                return "fulfillment", fulfillment_id, {"fulfillment_id": fulfillment_id}

            return self.foundation.idempotent(connection, request_id=request_id,
                                              action="joint.record_fulfillment",
                                              payload=payload, create=create)

    def record_disruption(self, *, request_id: str, actor_id: str, disruption_id: str,
                          kind: str, effective_from: str, resource_id: str | None = None,
                          member_id: str | None = None, capacity_delta: float | None = None,
                          detail: dict[str, Any] | None = None) -> WriteReceipt:
        """登记人员离任、资金延期、设备停机或成员退出，只重算未来义务。"""

        payload = {"actor_id": actor_id, "disruption_id": disruption_id, "kind": kind,
                   "effective_from": effective_from, "resource_id": resource_id,
                   "member_id": member_id, "capacity_delta": capacity_delta, "detail": detail}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if kind not in DISRUPTION_KINDS:
                raise ValidationError("扰动类型无效")
            disruption_id = self.foundation.check_identifier(disruption_id, "disruption_id")
            effective_from = _check_date(effective_from, "effective_from")
            if effective_from < self._today():
                raise ValidationError("扰动只重算未来义务，生效日期不能早于今天")
            if detail is None:
                detail = {}
            if not isinstance(detail, dict):
                raise ValidationError("detail 必须是对象")
            if kind == "member_exit":
                if not member_id:
                    raise ValidationError("member_exit 必须指定 member_id")
                self._active_member(connection, member_id)
                self._require_member_actor(actor, member_id)
                resource_id = None
                capacity_delta = None
            else:
                if not resource_id:
                    raise ValidationError(f"{kind} 必须指定 resource_id")
                resource = self._resource_row(connection, resource_id)
                if resource["kind"] != DISRUPTION_RESOURCE_KIND[kind]:
                    raise ValidationError("扰动类型与资源类别不匹配")
                capacity_delta = _check_delta(capacity_delta, "capacity_delta")
                self._require_member_actor(actor, resource["owner_member_id"])
                member_id = None

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                connection.execute(
                    "INSERT INTO resource_disruptions(disruption_id,kind,resource_id,member_id,"
                    "capacity_delta,effective_from,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (disruption_id, kind, resource_id, member_id, capacity_delta,
                     effective_from, canonical_json(detail), now))
                if kind == "member_exit":
                    connection.execute(
                        "UPDATE members SET exited_at=?, exit_effective_from=? WHERE member_id=?",
                        (now, effective_from, member_id))
                    for row in connection.execute(
                            "SELECT * FROM commitments WHERE member_id=? AND status IN ('draft','active')",
                            (member_id,)).fetchall():
                        connection.execute(
                            "UPDATE commitments SET status='closed', closed_at=?, "
                            "close_reason='member_exit' WHERE commitment_id=?",
                            (effective_from, row["commitment_id"]))
                        append_event(connection, actor_id=actor_id, action="joint.commitment_closed",
                                     resource_type="commitment", resource_id=row["commitment_id"],
                                     detail={"reason": "member_exit", "effective_from": effective_from},
                                     occurred_at=now)
                append_event(connection, actor_id=actor_id, action="joint.disruption_recorded",
                             resource_type="disruption", resource_id=disruption_id,
                             detail={"kind": kind, "resource_id": resource_id, "member_id": member_id,
                                     "capacity_delta": capacity_delta, "effective_from": effective_from},
                             occurred_at=now)
                return "disruption", disruption_id, {"disruption_id": disruption_id}

            return self.foundation.idempotent(connection, request_id=request_id,
                                              action="joint.record_disruption",
                                              payload=payload, create=create)

    def distribute_outcome(self, *, request_id: str, actor_id: str, distribution_id: str,
                           milestone_id: str, allocations: list[dict[str, Any]],
                           decided_on: str) -> WriteReceipt:
        """登记里程碑成果权益分配，每个里程碑只分配一次，登记后不可回写。"""

        payload = {"actor_id": actor_id, "distribution_id": distribution_id,
                   "milestone_id": milestone_id, "allocations": allocations, "decided_on": decided_on}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self.foundation.require_roles(actor, "admin")
            self._milestone_row(connection, milestone_id)
            distribution_id = self.foundation.check_identifier(distribution_id, "distribution_id")
            decided_on = _check_date(decided_on, "decided_on")
            if decided_on > self._today():
                raise ValidationError("分配决定日期不能晚于今天")
            if not isinstance(allocations, list) or not allocations:
                raise ValidationError("allocations 必须是非空列表")
            normalized = []
            total = 0.0
            for item in allocations:
                if not isinstance(item, dict):
                    raise ValidationError("allocations 元素必须是对象")
                member = self._member_row(connection, item.get("member_id"))
                share = _check_quantity(item.get("share"), "allocations.share")
                if share > 1:
                    raise ValidationError("单个成员份额不能超过 1")
                if (member["exited_at"] is not None
                        and decided_on >= member["exit_effective_from"]):
                    raise ConflictError("退出成员不参与其退出后决定的成果分配")
                total += share
                normalized.append({"member_id": member["member_id"], "share": share})
            if abs(total - 1.0) > 1e-6:
                raise ValidationError("成果权益份额合计必须等于 1")

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM outcome_distributions WHERE milestone_id=?",
                                      (milestone_id,)).fetchone():
                    raise ConflictError("该里程碑的成果权益已分配，不能回写")
                connection.execute(
                    "INSERT INTO outcome_distributions(distribution_id,milestone_id,allocations_json,"
                    "decided_on,created_at) VALUES(?,?,?,?,?)",
                    (distribution_id, milestone_id, canonical_json(normalized),
                     decided_on, self._now()))
                append_event(connection, actor_id=actor_id, action="joint.outcome_distributed",
                             resource_type="distribution", resource_id=distribution_id,
                             detail={"milestone_id": milestone_id, "allocations": normalized,
                                     "decided_on": decided_on},
                             occurred_at=self._now())
                return "distribution", distribution_id, {"distribution_id": distribution_id}

            return self.foundation.idempotent(connection, request_id=request_id,
                                              action="joint.distribute_outcome",
                                              payload=payload, create=create)

    # ---- 查询 ----

    def _fulfilled_quantity(self, connection, milestone_id: str,
                            requirement: dict[str, Any], as_of: str) -> float:
        total = 0.0
        for row in connection.execute(
                "SELECT resource_id, quantity FROM fulfillments WHERE milestone_id=? AND occurred_on<=?",
                (milestone_id, as_of)):
            if requirement.get("resource_id"):
                if row["resource_id"] != requirement["resource_id"]:
                    continue
            else:
                resource = self._resource_row(connection, row["resource_id"])
                if resource["kind"] != requirement["kind"]:
                    continue
            total += row["quantity"]
        return round(total, 6)

    def _readiness(self, connection, milestone, as_of: str,
                   exclude_member: str | None = None,
                   include_rows: tuple = ()) -> dict[str, Any]:
        """核对里程碑在某日期的真实资源保障，可排除或补入承诺用于情景演算。"""

        requirements = json.loads(milestone["requirements_json"])
        commitments = [row for row in self._active_commitment_rows(connection, as_of)
                       if row["milestone_id"] == milestone["milestone_id"]
                       and (exclude_member is None or row["member_id"] != exclude_member)]
        commitments.extend(row for row in include_rows
                           if row["milestone_id"] == milestone["milestone_id"])
        items = []
        for requirement in requirements:
            committed = 0.0
            for row in commitments:
                for line in json.loads(row["lines_json"]):
                    if requirement.get("resource_id"):
                        if line["resource_id"] != requirement["resource_id"]:
                            continue
                    else:
                        resource = self._resource_row(connection, line["resource_id"])
                        if resource["kind"] != requirement["kind"]:
                            continue
                    committed += line["quantity"]
            fulfilled = self._fulfilled_quantity(connection, milestone["milestone_id"],
                                                 requirement, as_of)
            gap = max(0.0, round(requirement["quantity"] - committed, 6))
            item: dict[str, Any] = {"kind": requirement["kind"],
                                    "required": requirement["quantity"],
                                    "committed": round(committed, 6),
                                    "fulfilled": fulfilled, "gap": gap}
            if requirement.get("resource_id"):
                item["resource_id"] = requirement["resource_id"]
                resource_ids = [requirement["resource_id"]]
            else:
                resource_ids = [row["resource_id"] for row in connection.execute(
                    "SELECT resource_id FROM resources WHERE kind=?", (requirement["kind"],))]
            capacity = sum(self._effective_capacity(connection, rid, as_of) for rid in resource_ids)
            allocated = sum(self._allocated_quantity(connection, rid, as_of, exclude_member)
                            for rid in resource_ids)
            item["capacity"] = round(capacity, 6)
            item["allocated"] = round(allocated, 6)
            item["available"] = round(capacity - allocated, 6)
            item["over_allocated"] = allocated > capacity + EPSILON
            items.append(item)
        suggestions = []
        gap_kinds = {item["kind"] for item in items if item["gap"] > EPSILON}
        if gap_kinds:
            for member in connection.execute(
                    "SELECT * FROM members WHERE exited_at IS NULL ORDER BY member_id"):
                if exclude_member and member["member_id"] == exclude_member:
                    continue
                usage = self._member_usage(connection, member["member_id"], as_of)
                for kind in sorted(gap_kinds):
                    headroom = member[CAP_COLUMN[kind]] - usage[kind]
                    if headroom > EPSILON:
                        suggestions.append({"member_id": member["member_id"], "kind": kind,
                                            "headroom": round(headroom, 6)})
            suggestions.sort(key=lambda entry: (-entry["headroom"], entry["member_id"]))
        return {"milestone_id": milestone["milestone_id"], "title": milestone["title"],
                "due_date": milestone["due_date"], "as_of": as_of,
                "ready": all(item["gap"] <= EPSILON for item in items),
                "items": items, "suggestions": suggestions}

    def milestone_readiness(self, milestone_id: str, as_of: str | None = None) -> dict[str, Any]:
        """理事会视图：里程碑是否具备真实资源、缺口由谁补足。"""

        connection = self.database.connection
        as_of = _check_date(as_of, "as_of") if as_of else self._today()
        milestone = self._milestone_row(connection, milestone_id)
        return self._readiness(connection, milestone, as_of)

    def member_position(self, member_id: str, as_of: str | None = None) -> dict[str, Any]:
        """成员视图：上限、占用、余量、已履约与当前生效承诺。"""

        connection = self.database.connection
        as_of = _check_date(as_of, "as_of") if as_of else self._today()
        member = self._member_row(connection, member_id)
        usage = self._member_usage(connection, member_id, as_of)
        fulfilled = {kind: 0.0 for kind in RESOURCE_KINDS}
        for row in connection.execute(
                "SELECT resource_id, quantity FROM fulfillments WHERE member_id=? AND occurred_on<=?",
                (member_id, as_of)):
            kind = self._resource_row(connection, row["resource_id"])["kind"]
            fulfilled[kind] = round(fulfilled[kind] + row["quantity"], 6)
        caps = {kind: member[CAP_COLUMN[kind]] for kind in RESOURCE_KINDS}
        return {"member_id": member_id, "role": member["role"],
                "status": "exited" if member["exited_at"] is not None else "active",
                "exit_effective_from": member["exit_effective_from"], "as_of": as_of,
                "caps": caps, "committed": usage,
                "headroom": {kind: round(caps[kind] - usage[kind], 6) for kind in RESOURCE_KINDS},
                "fulfilled": fulfilled,
                "active_commitments": [self._commitment_dict(row) for row in
                                       self._active_commitment_rows(connection, as_of, member_id)]}

    def exit_impact(self, member_id: str, as_of: str | None = None) -> dict[str, Any]:
        """演算或复盘成员退出：未来义务释放、里程碑缺口与既有权益不变。"""

        connection = self.database.connection
        as_of = _check_date(as_of, "as_of") if as_of else self._today()
        member = self._member_row(connection, member_id)
        exited = member["exited_at"] is not None
        effective = member["exit_effective_from"] if exited else as_of
        if exited:
            rows = connection.execute(
                "SELECT * FROM commitments WHERE member_id=? AND activated_at IS NOT NULL "
                "AND substr(activated_at,1,10)<=? AND (closed_at IS NULL OR substr(closed_at,1,10)>=?) "
                "ORDER BY commitment_id",
                (member_id, effective, effective)).fetchall()
        else:
            rows = self._active_commitment_rows(connection, effective, member_id)
        released = []
        milestone_ids = set()
        for row in rows:
            for line in json.loads(row["lines_json"]):
                delivered = connection.execute(
                    "SELECT COALESCE(SUM(quantity),0) AS total FROM fulfillments "
                    "WHERE commitment_id=? AND resource_id=? AND occurred_on<?",
                    (row["commitment_id"], line["resource_id"], effective)).fetchone()["total"]
                remaining = round(line["quantity"] - delivered, 6)
                if remaining > EPSILON:
                    released.append({"commitment_id": row["commitment_id"],
                                     "milestone_id": row["milestone_id"],
                                     "resource_id": line["resource_id"], "quantity": remaining})
                    milestone_ids.add(row["milestone_id"])
        affected = []
        for milestone_id in sorted(milestone_ids):
            milestone = self._milestone_row(connection, milestone_id)
            if exited:
                baseline = self._readiness(connection, milestone, effective, include_rows=tuple(rows))
                actual = self._readiness(connection, milestone, effective)
            else:
                baseline = self._readiness(connection, milestone, effective)
                actual = self._readiness(connection, milestone, effective, exclude_member=member_id)
            gaps = []
            for base_item, actual_item in zip(baseline["items"], actual["items"]):
                increase = round(actual_item["gap"] - base_item["gap"], 6)
                if increase > EPSILON:
                    gaps.append({"kind": actual_item["kind"],
                                 "resource_id": actual_item.get("resource_id"),
                                 "gap_increase": increase, "gap_after": actual_item["gap"]})
            affected.append({"milestone_id": milestone_id,
                             "ready_without_member": actual["ready"], "gaps": gaps})
        distributions = []
        for row in connection.execute("SELECT * FROM outcome_distributions ORDER BY decided_on"):
            for allocation in json.loads(row["allocations_json"]):
                if allocation["member_id"] == member_id:
                    distributions.append({"distribution_id": row["distribution_id"],
                                          "milestone_id": row["milestone_id"],
                                          "share": allocation["share"],
                                          "decided_on": row["decided_on"]})
        charter = self._charter_at_row(connection, member["charter_id"], effective)
        policy = "lapse"
        if charter is not None:
            policy = json.loads(charter["terms_json"]).get("exit_share_policy", "lapse")
        return {"member_id": member_id, "exited": exited,
                "exit_effective_from": member["exit_effective_from"] if exited else None,
                "as_of": as_of, "released_future_obligations": released,
                "affected_milestones": affected,
                "completed_distributions_unchanged": distributions,
                "future_share_policy": policy}

    def _charter_at_row(self, connection, charter_id: str, on_date: str):
        return connection.execute(
            "SELECT * FROM charter_versions WHERE charter_id=? AND effective_from<=? "
            "ORDER BY effective_from DESC, version DESC LIMIT 1",
            (charter_id, on_date)).fetchone()

    def charter_at(self, charter_id: str, on_date: str) -> dict[str, Any]:
        """按历史日期复原当时有效的合作章程版本。"""

        on_date = _check_date(on_date, "on_date")
        row = self._charter_at_row(self.database.connection, charter_id, on_date)
        if row is None:
            raise NotFoundError("该日期没有生效的章程版本")
        return {"charter_id": row["charter_id"], "version": row["version"],
                "title": row["title"], "terms": json.loads(row["terms_json"]),
                "effective_from": row["effective_from"]}

    def _commitment_dict(self, row) -> dict[str, Any]:
        return {"commitment_id": row["commitment_id"], "member_id": row["member_id"],
                "milestone_id": row["milestone_id"], "lines": json.loads(row["lines_json"]),
                "activated_at": row["activated_at"]}

    def commitments_at(self, on_date: str, member_id: str | None = None) -> list[dict[str, Any]]:
        """按历史日期复原当时生效的承诺。"""

        on_date = _check_date(on_date, "on_date")
        return [self._commitment_dict(row)
                for row in self._active_commitment_rows(self.database.connection, on_date, member_id)]

    def resource_capacity(self, resource_id: str, as_of: str | None = None) -> dict[str, Any]:
        """资源视图：基础额度、扰动调整、已占用与剩余可分配额度。"""

        connection = self.database.connection
        as_of = _check_date(as_of, "as_of") if as_of else self._today()
        resource = self._resource_row(connection, resource_id)
        effective = self._effective_capacity(connection, resource_id, as_of)
        allocated = self._allocated_quantity(connection, resource_id, as_of)
        disruptions = [{"disruption_id": row["disruption_id"], "kind": row["kind"],
                        "effective_from": row["effective_from"],
                        "capacity_delta": row["capacity_delta"]}
                       for row in connection.execute(
                           "SELECT * FROM resource_disruptions WHERE resource_id=? "
                           "ORDER BY effective_from, disruption_id", (resource_id,))]
        return {"resource_id": resource_id, "kind": resource["kind"],
                "unique_key": resource["unique_key"],
                "owner_member_id": resource["owner_member_id"],
                "conditions": json.loads(resource["conditions_json"]),
                "as_of": as_of, "base_capacity": resource["capacity"],
                "effective_capacity": effective, "allocated": allocated,
                "available": round(effective - allocated, 6),
                "over_allocated": allocated > effective + EPSILON,
                "disruptions": disruptions}
