"""区域创新联合投入与履约服务。

在基础服务（组织、操作者、场所、审计链、幂等回执）之上实现交叉学科平台的
联合投入登记、会签占用、冲突裁定、履约追踪、退出重算与成果权益固化。

关键约束：
- 合作章程按版本冻结，承诺会签完成时快照生效章程与优先规则；
- 承诺只有完成全部会签后才占用可分配额度；
- 同一资源跨计划冲突时，按承诺冻结的优先规则（计划优先级、会签时间）裁定；
- 可分配额度按机构净占用额汇总核算，拆分承诺不能绕过机构上限；
- 人员离任、设备停机/恢复、资金延期、成员退出只重算当前及未来义务，
  既有履约与已固化的成果分配永不回写。
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any, Callable, Iterable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .storage import Database

RESOURCE_TYPES = ("person_hours", "equipment", "funding")
REDISTRIBUTION_RULES = ("pro_rata", "lead", "lapse")
CAP_KEYS = {
    "person_hours": "person_hours_monthly",
    "equipment": "equipment_hours_monthly",
    "funding": "funding_total",
}
_WRITE_ROLES = ("admin", "operator")
_CONFIG_ROLES = ("admin",)


def today_of(clock: Clock) -> date:
    return clock.now().date()


def period_of(value: date | str) -> str:
    if isinstance(value, date):
        return value.strftime("%Y-%m")
    return value[:7]


def parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def parse_period(value: str, field: str) -> str:
    text = str(value).strip()
    try:
        date.fromisoformat(text + "-01")
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} 必须是 YYYY-MM 月份") from exc
    return text


class JointVentureService:
    """登记联合投入并维护会签、占用、履约与权益账。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础工具

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _today(self) -> date:
        return today_of(self.clock)

    def _idempotent(self, conn, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]):
        from .models import WriteReceipt

        payload_hash = digest(payload)
        row = conn.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True,
                                json.loads(row["response_json"]))
        resource_type, resource_id, response = create()
        conn.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False, response)

    def _actor(self, conn, actor_id: str):
        row = conn.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require_roles(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _platform(self, conn, platform_id: str):
        row = conn.execute("SELECT * FROM jv_platforms WHERE platform_id=?", (platform_id,)).fetchone()
        if row is None:
            raise NotFoundError("平台不存在")
        return row

    def _active_membership(self, conn, platform_id: str, organization_id: str, on: date | None = None):
        row = conn.execute(
            "SELECT * FROM jv_memberships WHERE platform_id=? AND organization_id=?",
            (platform_id, organization_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("机构不是该平台成员")
        on = on or self._today()
        if parse_date(row["joined_on"], "joined_on") > on:
            raise ValidationError("该日期机构尚未加入平台")
        if row["status"] == "exited" and parse_date(row["exited_on"], "exited_on") <= on:
            raise ValidationError("机构已退出平台")
        return row

    def _charter_on(self, conn, platform_id: str, on: date):
        row = conn.execute(
            "SELECT * FROM jv_charter_versions WHERE platform_id=? AND effective_from<=? "
            "ORDER BY version_no DESC LIMIT 1",
            (platform_id, on.isoformat()),
        ).fetchone()
        if row is None:
            raise ValidationError("该日期尚无生效的合作章程")
        return row

    def _audit(self, conn, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(conn, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _lifecycle(self, conn, *, commitment_id: str, from_status: str, to_status: str,
                   effective_on: date, reason: str, detail: dict[str, Any] | None = None) -> None:
        conn.execute(
            "INSERT INTO jv_lifecycle_events(lifecycle_event_id,commitment_id,from_status,to_status,"
            "effective_on,reason,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, commitment_id, from_status, to_status, effective_on.isoformat(),
             reason, canonical_json(detail or {}), self._now()),
        )
        conn.execute("UPDATE jv_commitments SET status=? WHERE commitment_id=?",
                     (to_status, commitment_id))

    # ------------------------------------------------------------------ 平台与成员

    def create_platform(self, *, request_id: str, actor_id: str, platform_id: str, name: str):
        payload = {"actor_id": actor_id, "platform_id": platform_id, "name": name}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_WRITE_ROLES)
            name = str(name).strip()
            if not name:
                raise ValidationError("name 不能为空")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO jv_platforms(platform_id,name,created_by,created_at) VALUES(?,?,?,?)",
                        (platform_id, name, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("平台编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="jv.platform_created",
                            resource_type="jv_platform", resource_id=platform_id, detail={"name": name})
                return "jv_platform", platform_id, {"platform_id": platform_id}

            return self._idempotent(conn, request_id=request_id, action="jv.create_platform",
                                    payload=payload, create=create)

    def register_membership(self, *, request_id: str, actor_id: str, platform_id: str,
                            organization_id: str, member_role: str, member_order: int,
                            local_conditions: dict[str, Any] | None = None, joined_on: str | None = None):
        payload = {"actor_id": actor_id, "platform_id": platform_id,
                   "organization_id": organization_id, "member_role": member_role,
                   "member_order": member_order, "local_conditions": local_conditions or {},
                   "joined_on": joined_on}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_CONFIG_ROLES)
            self._platform(conn, platform_id)
            org = conn.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                               (organization_id,)).fetchone()
            if org is None:
                raise NotFoundError("组织不存在")
            if member_role not in ("lead", "partner", "observer"):
                raise ValidationError("member_role 必须是 lead/partner/observer")
            member_order = int(member_order)
            if member_order < 0:
                raise ValidationError("member_order 不能为负")
            if not isinstance(local_conditions or {}, dict):
                raise ValidationError("local_conditions 必须是对象")
            joined = parse_date(joined_on, "joined_on") if joined_on else self._today()

            def create():
                try:
                    conn.execute(
                        "INSERT INTO jv_memberships(platform_id,organization_id,member_role,member_order,"
                        "local_conditions_json,joined_on,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,'active',?,?)",
                        (platform_id, organization_id, member_role, member_order,
                         canonical_json(local_conditions or {}), joined.isoformat(),
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该机构已是平台成员或字段冲突") from exc
                self._audit(conn, actor_id=actor_id, action="jv.member_registered",
                            resource_type="jv_membership", resource_id=organization_id,
                            detail={"platform_id": platform_id, "member_role": member_role})
                return "jv_membership", f"{platform_id}:{organization_id}", {
                    "platform_id": platform_id, "organization_id": organization_id}

            return self._idempotent(conn, request_id=request_id, action="jv.register_membership",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 章程版本

    def publish_charter(self, *, request_id: str, actor_id: str, platform_id: str,
                        priority_rules: dict[str, Any], caps: dict[str, int],
                        outcome_redistribution: str, content: dict[str, Any] | None = None,
                        effective_from: str | None = None):
        """发布新版本章程；新版本生效时旧版本自动截止。"""

        payload = {"actor_id": actor_id, "platform_id": platform_id,
                   "priority_rules": priority_rules, "caps": caps,
                   "outcome_redistribution": outcome_redistribution, "content": content or {},
                   "effective_from": effective_from}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_CONFIG_ROLES)
            self._platform(conn, platform_id)
            if not isinstance(priority_rules, dict) or priority_rules.get("type") != "plan_rank":
                raise ValidationError("priority_rules 必须声明 type=plan_rank 的冻结优先规则")
            if not isinstance(caps, dict):
                raise ValidationError("caps 必须是对象")
            clean_caps: dict[str, int] = {}
            for key, value in caps.items():
                if key not in CAP_KEYS.values():
                    raise ValidationError(f"未知上限规则 {key}")
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValidationError(f"{key} 必须是非负整数")
                clean_caps[key] = value
            if outcome_redistribution not in REDISTRIBUTION_RULES:
                raise ValidationError("outcome_redistribution 必须是 pro_rata/lead/lapse")
            effective = parse_date(effective_from, "effective_from") if effective_from else self._today()
            previous = conn.execute(
                "SELECT * FROM jv_charter_versions WHERE platform_id=? ORDER BY version_no DESC LIMIT 1",
                (platform_id,)).fetchone()
            version_no = (previous["version_no"] + 1) if previous else 1
            if previous and effective <= parse_date(previous["effective_from"], "effective_from"):
                raise ValidationError("新版本生效日期必须晚于既往版本")

            def create():
                charter_id = uuid.uuid4().hex
                if previous:
                    conn.execute(
                        "UPDATE jv_charter_versions SET status='superseded', effective_to=? WHERE charter_id=?",
                        (effective.isoformat(), previous["charter_id"]),
                    )
                conn.execute(
                    "INSERT INTO jv_charter_versions(charter_id,platform_id,version_no,priority_rules_json,"
                    "caps_json,outcome_redistribution,content_json,effective_from,effective_to,status,"
                    "published_by,published_at) VALUES(?,?,?,?,?,?,?,?,?, 'effective', ?,?)",
                    (charter_id, platform_id, version_no, canonical_json(priority_rules),
                     canonical_json(clean_caps), outcome_redistribution, canonical_json(content or {}),
                     effective.isoformat(), None, actor_id, self._now()),
                )
                self._audit(conn, actor_id=actor_id, action="jv.charter_published",
                            resource_type="jv_charter", resource_id=charter_id,
                            detail={"platform_id": platform_id, "version_no": version_no,
                                    "effective_from": effective.isoformat()})
                return "jv_charter", charter_id, {
                    "charter_id": charter_id, "platform_id": platform_id, "version_no": version_no,
                    "effective_from": effective.isoformat()}

            return self._idempotent(conn, request_id=request_id, action="jv.publish_charter",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 资源台账

    def register_person(self, *, request_id: str, actor_id: str, platform_id: str,
                        organization_id: str, person_code: str, display_name: str,
                        title: str, capacity_hours_monthly: int):
        payload = {"actor_id": actor_id, "platform_id": platform_id,
                   "organization_id": organization_id, "person_code": person_code}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_WRITE_ROLES)
            self._active_membership(conn, platform_id, organization_id)
            if actor["organization_id"] != organization_id and actor["role"] != "admin":
                raise PermissionDenied("只能为本机构登记人才")
            capacity = int(capacity_hours_monthly)
            if capacity <= 0:
                raise ValidationError("capacity_hours_monthly 必须为正整数")

            def create():
                person_id = uuid.uuid4().hex
                try:
                    conn.execute(
                        "INSERT INTO jv_persons(person_id,platform_id,organization_id,person_code,"
                        "display_name,title,capacity_hours_monthly,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?, 'active', ?,?)",
                        (person_id, platform_id, organization_id, person_code, display_name,
                         title, capacity, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("人才编号已存在") from exc
                self._audit(conn, actor_id=actor_id, action="jv.person_registered",
                            resource_type="jv_person", resource_id=person_id,
                            detail={"platform_id": platform_id, "person_code": person_code})
                return "jv_person", person_id, {"person_id": person_id}

            return self._idempotent(conn, request_id=request_id, action="jv.register_person",
                                    payload=payload, create=create)

    def register_equipment(self, *, request_id: str, actor_id: str, platform_id: str,
                           organization_id: str, equipment_code: str, display_name: str,
                           monthly_capacity_hours: int):
        payload = {"actor_id": actor_id, "platform_id": platform_id,
                   "organization_id": organization_id, "equipment_code": equipment_code}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_WRITE_ROLES)
            self._active_membership(conn, platform_id, organization_id)
            if actor["organization_id"] != organization_id and actor["role"] != "admin":
                raise PermissionDenied("只能为本机构登记设备")
            capacity = int(monthly_capacity_hours)
            if capacity <= 0:
                raise ValidationError("monthly_capacity_hours 必须为正整数")

            def create():
                equipment_id = uuid.uuid4().hex
                try:
                    conn.execute(
                        "INSERT INTO jv_equipment(equipment_id,platform_id,organization_id,equipment_code,"
                        "display_name,monthly_capacity_hours,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?, 'active', ?,?)",
                        (equipment_id, platform_id, organization_id, equipment_code,
                         display_name, capacity, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("设备编号已存在") from exc
                self._audit(conn, actor_id=actor_id, action="jv.equipment_registered",
                            resource_type="jv_equipment", resource_id=equipment_id,
                            detail={"platform_id": platform_id, "equipment_code": equipment_code})
                return "jv_equipment", equipment_id, {"equipment_id": equipment_id}

            return self._idempotent(conn, request_id=request_id, action="jv.register_equipment",
                                    payload=payload, create=create)

    def register_fund_source(self, *, request_id: str, actor_id: str, platform_id: str,
                             organization_id: str, source_code: str, display_name: str,
                             total_amount: int, currency: str,
                             restrictions: dict[str, Any] | None = None):
        """登记企业/地方资金来源及其用途限制与成果落地条件。"""

        payload = {"actor_id": actor_id, "platform_id": platform_id,
                   "organization_id": organization_id, "source_code": source_code,
                   "total_amount": total_amount, "restrictions": restrictions or {}}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_WRITE_ROLES)
            self._active_membership(conn, platform_id, organization_id)
            if actor["organization_id"] != organization_id and actor["role"] != "admin":
                raise PermissionDenied("只能为本机构登记资金来源")
            total = int(total_amount)
            if total <= 0:
                raise ValidationError("total_amount 必须为正整数（最小货币单位）")
            if not isinstance(restrictions or {}, dict):
                raise ValidationError("restrictions 必须是对象")

            def create():
                source_id = uuid.uuid4().hex
                try:
                    conn.execute(
                        "INSERT INTO jv_fund_sources(source_id,platform_id,organization_id,source_code,"
                        "display_name,total_amount,currency,restrictions_json,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (source_id, platform_id, organization_id, source_code, display_name,
                         total, currency, canonical_json(restrictions or {}), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("资金来源编号已存在") from exc
                self._audit(conn, actor_id=actor_id, action="jv.fund_source_registered",
                            resource_type="jv_fund_source", resource_id=source_id,
                            detail={"platform_id": platform_id, "source_code": source_code,
                                    "total_amount": total})
                return "jv_fund_source", source_id, {"source_id": source_id}

            return self._idempotent(conn, request_id=request_id, action="jv.register_fund_source",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 计划与里程碑

    def create_plan(self, *, request_id: str, actor_id: str, platform_id: str, plan_id: str,
                    name: str, priority_rank: int, lead_organization_id: str):
        payload = {"actor_id": actor_id, "platform_id": platform_id, "plan_id": plan_id,
                   "priority_rank": priority_rank, "lead_organization_id": lead_organization_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_CONFIG_ROLES)
            self._platform(conn, platform_id)
            self._active_membership(conn, platform_id, lead_organization_id)
            rank = int(priority_rank)
            if rank < 0:
                raise ValidationError("priority_rank 不能为负")
            clash = conn.execute("SELECT 1 FROM jv_plans WHERE platform_id=? AND priority_rank=?",
                                 (platform_id, rank)).fetchone()
            if clash:
                raise ConflictError("平台内计划优先级不能重复")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO jv_plans(plan_id,platform_id,name,priority_rank,"
                        "lead_organization_id,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (plan_id, platform_id, name, rank, lead_organization_id, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("计划编号已存在") from exc
                self._audit(conn, actor_id=actor_id, action="jv.plan_created",
                            resource_type="jv_plan", resource_id=plan_id,
                            detail={"platform_id": platform_id, "priority_rank": rank})
                return "jv_plan", plan_id, {"plan_id": plan_id}

            return self._idempotent(conn, request_id=request_id, action="jv.create_plan",
                                    payload=payload, create=create)

    def create_milestone(self, *, request_id: str, actor_id: str, plan_id: str, milestone_code: str,
                         name: str, due_date: str, requirements: list[dict[str, Any]]):
        """登记里程碑的真实资源需求：人才工时/设备时段/资金（可指定具体资源与月份）。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id, "milestone_code": milestone_code,
                   "due_date": due_date, "requirements": requirements}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_WRITE_ROLES)
            plan = conn.execute("SELECT * FROM jv_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan is None:
                raise NotFoundError("计划不存在")
            due = parse_date(due_date, "due_date")
            clean_requirements = self._clean_requirements(requirements)

            def create():
                milestone_id = uuid.uuid4().hex
                try:
                    conn.execute(
                        "INSERT INTO jv_milestones(milestone_id,plan_id,milestone_code,name,due_date,"
                        "requirements_json,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?, 'planned', ?,?)",
                        (milestone_id, plan_id, milestone_code, name, due.isoformat(),
                         canonical_json(clean_requirements), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("里程碑编号已存在") from exc
                self._audit(conn, actor_id=actor_id, action="jv.milestone_created",
                            resource_type="jv_milestone", resource_id=milestone_id,
                            detail={"plan_id": plan_id, "milestone_code": milestone_code})
                return "jv_milestone", milestone_id, {"milestone_id": milestone_id}

            return self._idempotent(conn, request_id=request_id, action="jv.create_milestone",
                                    payload=payload, create=create)

    def _clean_requirements(self, requirements: Any) -> list[dict[str, Any]]:
        if not isinstance(requirements, list) or not requirements:
            raise ValidationError("requirements 必须是非空数组")
        cleaned = []
        for item in requirements:
            if not isinstance(item, dict):
                raise ValidationError("每个资源需求必须是对象")
            rtype = item.get("resource_type")
            if rtype not in RESOURCE_TYPES:
                raise ValidationError("resource_type 必须是 person_hours/equipment/funding")
            qty = int(item.get("qty", 0))
            if qty <= 0:
                raise ValidationError("需求数量必须为正整数")
            period = item.get("period_key")
            entry = {"resource_type": rtype, "qty": qty}
            if item.get("resource_id"):
                entry["resource_id"] = str(item["resource_id"])
            if period:
                entry["period_key"] = parse_period(period, "period_key")
            else:
                entry["period_key"] = None
            cleaned.append(entry)
        return cleaned

    def complete_milestone(self, *, request_id: str, actor_id: str, milestone_id: str,
                           completed_on: str | None = None):
        payload = {"actor_id": actor_id, "milestone_id": milestone_id, "completed_on": completed_on}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_WRITE_ROLES)
            milestone = conn.execute("SELECT * FROM jv_milestones WHERE milestone_id=?",
                                     (milestone_id,)).fetchone()
            if milestone is None:
                raise NotFoundError("里程碑不存在")
            if milestone["status"] == "completed":
                raise ConflictError("里程碑已标记完成，不可回写")
            on = parse_date(completed_on, "completed_on") if completed_on else self._today()

            def create():
                conn.execute(
                    "UPDATE jv_milestones SET status='completed', completed_on=? WHERE milestone_id=?",
                    (on.isoformat(), milestone_id),
                )
                self._audit(conn, actor_id=actor_id, action="jv.milestone_completed",
                            resource_type="jv_milestone", resource_id=milestone_id,
                            detail={"completed_on": on.isoformat()})
                return "jv_milestone", milestone_id, {"milestone_id": milestone_id, "completed_on": on.isoformat()}

            return self._idempotent(conn, request_id=request_id, action="jv.complete_milestone",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 承诺与会签

    def register_commitment(self, *, request_id: str, actor_id: str, platform_id: str, plan_id: str,
                            milestone_id: str, organization_id: str, resource_type: str,
                            resource_id: str, qty: int, period_key: str,
                            window_start: str | None = None, window_end: str | None = None,
                            tranches: list[dict[str, Any]] | None = None,
                            required_signoffs: list[str] | None = None):
        """登记一条配套承诺（草稿）。草稿和会签中承诺不占用任何可分配额度。"""

        payload = {"actor_id": actor_id, "platform_id": platform_id, "plan_id": plan_id,
                   "milestone_id": milestone_id, "organization_id": organization_id,
                   "resource_type": resource_type, "resource_id": resource_id, "qty": qty,
                   "period_key": period_key, "tranches": tranches,
                   "required_signoffs": required_signoffs}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_WRITE_ROLES)
            self._platform(conn, platform_id)
            self._active_membership(conn, platform_id, organization_id)
            plan = conn.execute("SELECT * FROM jv_plans WHERE plan_id=? AND platform_id=?",
                                (plan_id, platform_id)).fetchone()
            if plan is None:
                raise NotFoundError("计划不存在或不属于该平台")
            milestone = conn.execute(
                "SELECT * FROM jv_milestones WHERE milestone_id=? AND plan_id=?",
                (milestone_id, plan_id)).fetchone()
            if milestone is None:
                raise NotFoundError("里程碑不存在或不属于该计划")
            if resource_type not in RESOURCE_TYPES:
                raise ValidationError("resource_type 必须是 person_hours/equipment/funding")
            qty = int(qty)
            if qty <= 0:
                raise ValidationError("qty 必须为正整数")
            period = parse_period(period_key, "period_key")
            start = parse_date(window_start, "window_start") if window_start else None
            end = parse_date(window_end, "window_end") if window_end else None
            if start and end and start > end:
                raise ValidationError("时间窗口起始不能晚于结束")
            owner_id = self._validate_resource(conn, platform_id, resource_type, resource_id, qty, period)
            # 资金只能承诺本机构来源；人才/设备允许跨机构重复主张，以便会签后由
            # 冻结优先规则对同一资源的多重计入统一裁定，避免直接汇总高估能力。
            if resource_type == "funding" and owner_id != organization_id:
                raise ValidationError("只能承诺本机构登记的资金来源")
            clean_tranches = self._clean_tranches(resource_type, qty, tranches)
            signoffs = required_signoffs
            if signoffs is None:
                signoffs = sorted({organization_id, plan["lead_organization_id"]})
            if not isinstance(signoffs, list) or not signoffs or organization_id not in signoffs:
                raise ValidationError("required_signoffs 必须包含承诺机构")
            for org in signoffs:
                self._active_membership(conn, platform_id, org)

            def create():
                commitment_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO jv_commitments(commitment_id,platform_id,plan_id,milestone_id,"
                    "organization_id,resource_type,resource_id,qty,window_start,window_end,"
                    "tranches_json,period_key,required_signoffs_json,status,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?, 'draft', ?,?)",
                    (commitment_id, platform_id, plan_id, milestone_id, organization_id,
                     resource_type, resource_id, qty,
                     start.isoformat() if start else None, end.isoformat() if end else None,
                     canonical_json(clean_tranches) if clean_tranches else None, period,
                     canonical_json(signoffs), actor_id, self._now()),
                )
                self._lifecycle(conn, commitment_id=commitment_id, from_status="", to_status="draft",
                                effective_on=self._today(), reason="registered")
                for tranch in clean_tranches:
                    conn.execute(
                        "INSERT INTO jv_tranche_events(tranche_event_id,commitment_id,tranch_no,amount,"
                        "event_type,event_date,recorded_by,created_at) VALUES(?,?,?,?,'scheduled',?,?,?)",
                        (uuid.uuid4().hex, commitment_id, tranch["tranch_no"], tranch["amount"],
                         tranch["due_date"], actor_id, self._now()),
                    )
                self._audit(conn, actor_id=actor_id, action="jv.commitment_registered",
                            resource_type="jv_commitment", resource_id=commitment_id,
                            detail={"plan_id": plan_id, "resource_type": resource_type,
                                    "qty": qty, "period_key": period})
                return "jv_commitment", commitment_id, {"commitment_id": commitment_id}

            return self._idempotent(conn, request_id=request_id, action="jv.register_commitment",
                                    payload=payload, create=create)

    def _validate_resource(self, conn, platform_id: str, resource_type: str,
                           resource_id: str, qty: int, period: str) -> str:
        if resource_type == "person_hours":
            row = conn.execute(
                "SELECT * FROM jv_persons WHERE person_id=? AND platform_id=?",
                (resource_id, platform_id)).fetchone()
            if row is None:
                raise NotFoundError("人才不存在")
            if row["status"] != "active":
                raise ValidationError("该人才已离任，不能再计入承诺")
            if qty > row["capacity_hours_monthly"]:
                raise ValidationError("承诺工时超过该人才月度可投入总工时")
            return row["organization_id"]
        if resource_type == "equipment":
            row = conn.execute(
                "SELECT * FROM jv_equipment WHERE equipment_id=? AND platform_id=?",
                (resource_id, platform_id)).fetchone()
            if row is None:
                raise NotFoundError("设备不存在")
            if row["status"] != "active":
                raise ValidationError("设备处于停机状态，不能再计入承诺")
            if qty > row["monthly_capacity_hours"]:
                raise ValidationError("承诺时段超过该设备月度可用总时段")
            return row["organization_id"]
        row = conn.execute(
            "SELECT * FROM jv_fund_sources WHERE source_id=? AND platform_id=?",
            (resource_id, platform_id)).fetchone()
        if row is None:
            raise NotFoundError("资金来源不存在")
        return row["organization_id"]

    def _clean_tranches(self, resource_type: str, qty: int, tranches: Any) -> list[dict[str, Any]]:
        if tranches is None:
            return []
        if resource_type != "funding":
            raise ValidationError("只有资金承诺可以登记分期")
        if not isinstance(tranches, list) or not tranches:
            raise ValidationError("tranches 必须是非空数组")
        cleaned = []
        total = 0
        seen = set()
        for item in tranches:
            no = int(item["tranch_no"])
            if no < 1 or no in seen:
                raise ValidationError("分期编号无效或重复")
            seen.add(no)
            amount = int(item["amount"])
            if amount <= 0:
                raise ValidationError("分期金额必须为正整数")
            due = parse_date(item["due_date"], "tranch.due_date")
            cleaned.append({"tranch_no": no, "amount": amount, "due_date": due.isoformat()})
            total += amount
        if total != qty:
            raise ValidationError("分期金额合计必须等于承诺金额")
        cleaned.sort(key=lambda item: item["tranch_no"])
        return cleaned

    def submit_commitment(self, *, actor_id: str, commitment_id: str):
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_WRITE_ROLES)
            row = self._commitment(conn, commitment_id)
            if row["status"] != "draft":
                raise ConflictError("只有草稿承诺可以提交会签")
            if actor["organization_id"] != row["organization_id"] and actor["role"] != "admin":
                raise PermissionDenied("只能由承诺机构提交会签")
            conn.execute("UPDATE jv_commitments SET submitted_at=? WHERE commitment_id=?",
                         (self._today().isoformat(), commitment_id))
            self._lifecycle(conn, commitment_id=commitment_id, from_status="draft",
                            to_status="awaiting_signoff", effective_on=self._today(),
                            reason="submitted")
            self._audit(conn, actor_id=actor_id, action="jv.commitment_submitted",
                        resource_type="jv_commitment", resource_id=commitment_id, detail={})
            return {"commitment_id": commitment_id, "status": "awaiting_signoff"}

    def withdraw_commitment(self, *, request_id: str, actor_id: str, commitment_id: str):
        payload = {"actor_id": actor_id, "commitment_id": commitment_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_WRITE_ROLES)
            row = self._commitment(conn, commitment_id)
            if row["status"] not in ("draft", "awaiting_signoff"):
                raise ConflictError("只有未占用额度的承诺可以撤回")
            if actor["organization_id"] != row["organization_id"] and actor["role"] != "admin":
                raise PermissionDenied("只能撤回本机构的承诺")

            def create():
                self._lifecycle(conn, commitment_id=commitment_id, from_status=row["status"],
                                to_status="withdrawn", effective_on=self._today(), reason="withdrawn")
                self._audit(conn, actor_id=actor_id, action="jv.commitment_withdrawn",
                            resource_type="jv_commitment", resource_id=commitment_id, detail={})
                return "jv_commitment", commitment_id, {"commitment_id": commitment_id}

            return self._idempotent(conn, request_id=request_id, action="jv.withdraw_commitment",
                                    payload=payload, create=create)

    def sign_commitment(self, *, actor_id: str, commitment_id: str, note: str | None = None):
        """登记一方会签；最后一方会签完成时冻结章程并裁定额度占用。"""

        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_WRITE_ROLES)
            row = self._commitment(conn, commitment_id)
            if row["status"] != "awaiting_signoff":
                raise ConflictError("只有会签中的承诺可以补签")
            required = self._json(row["required_signoffs_json"])
            if actor["organization_id"] not in required:
                raise PermissionDenied("本机构不在会签方清单内")
            exists = conn.execute(
                "SELECT 1 FROM jv_signoffs WHERE commitment_id=? AND organization_id=?",
                (commitment_id, actor["organization_id"])).fetchone()
            if exists:
                raise ConflictError("本机构已完成会签")
            conn.execute(
                "INSERT INTO jv_signoffs(commitment_id,organization_id,actor_id,signed_at,note) "
                "VALUES(?,?,?,?,?)",
                (commitment_id, actor["organization_id"], actor_id, self._today().isoformat(), note),
            )
            signed = {r["organization_id"] for r in conn.execute(
                "SELECT organization_id FROM jv_signoffs WHERE commitment_id=?", (commitment_id,))}
            self._audit(conn, actor_id=actor_id, action="jv.commitment_signed",
                        resource_type="jv_commitment", resource_id=commitment_id,
                        detail={"organization_id": actor["organization_id"]})
            if set(required) <= signed:
                return self._activate(conn, row, actor_id)
            return {"commitment_id": commitment_id, "status": "awaiting_signoff",
                    "signed": sorted(signed), "required": sorted(required)}

    def _activate(self, conn, row, actor_id: str) -> dict[str, Any]:
        on = self._today()
        if row["period_key"] < period_of(on):
            raise ValidationError("承诺月份已经过去，不能占用历史额度，请重新登记")
        charter = self._charter_on(conn, row["platform_id"], on)
        conn.execute(
            "UPDATE jv_commitments SET charter_id=?, signed_at=? WHERE commitment_id=?",
            (charter["charter_id"], on.isoformat(), row["commitment_id"]),
        )
        # 先进入在保状态，使裁定把这条新承诺纳入容量与上限分配。
        self._lifecycle(conn, commitment_id=row["commitment_id"], from_status="awaiting_signoff",
                        to_status="blocked", effective_on=on, reason="signoff_completed")
        fresh = self._commitment(conn, row["commitment_id"])
        resolution = self._adjudicate(conn, row["platform_id"], row["resource_type"], on,
                                      trigger="signoff", trigger_commitment=row["commitment_id"])
        winner = next((item for item in resolution if item["commitment_id"] == row["commitment_id"]), None)
        self._audit(conn, actor_id=actor_id, action="jv.commitment_activated",
                    resource_type="jv_commitment", resource_id=row["commitment_id"],
                    detail={"charter_id": charter["charter_id"], "resolution": resolution})
        return {"commitment_id": row["commitment_id"], "status": winner["status"] if winner else "blocked",
                "charter_id": charter["charter_id"], "frozen_priority_rules": self._json(charter["priority_rules_json"]),
                "resolution": resolution}

    # ------------------------------------------------------------------ 额度裁定

    def _commitment(self, conn, commitment_id: str):
        row = conn.execute("SELECT * FROM jv_commitments WHERE commitment_id=?",
                           (commitment_id,)).fetchone()
        if row is None:
            raise NotFoundError("承诺不存在")
        return row

    @staticmethod
    def _json(value: str | None) -> Any:
        import json

        return json.loads(value) if value else None

    def _net_granted(self, conn, commitment_id: str, as_of: date | None = None) -> int:
        as_of = as_of or self._today()
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE kind WHEN 'release' THEN -qty ELSE qty END),0) AS total "
            "FROM jv_allocations WHERE commitment_id=? AND effective_on<=?",
            (commitment_id, as_of.isoformat())).fetchone()
        return int(row["total"])

    def _performed(self, conn, commitment_id: str, as_of: date | None = None) -> int:
        as_of = as_of or self._today()
        row = conn.execute(
            "SELECT COALESCE(SUM(qty),0) AS total FROM jv_performances "
            "WHERE commitment_id=? AND occurred_on<=?",
            (commitment_id, as_of.isoformat())).fetchone()
        return int(row["total"])

    def _downtime_reduction(self, conn, equipment_id: str, period: str, on: date) -> int:
        period_start = parse_period(period, "period_key") and date.fromisoformat(period + "-01")
        if period_start.month == 12:
            next_month = date(period_start.year + 1, 1, 1)
        else:
            next_month = date(period_start.year, period_start.month + 1, 1)
        period_days = (next_month - period_start).days
        capacity = conn.execute(
            "SELECT monthly_capacity_hours FROM jv_equipment WHERE equipment_id=?",
            (equipment_id,)).fetchone()["monthly_capacity_hours"]
        reduction_days = 0
        for event in conn.execute(
                "SELECT * FROM jv_resource_events WHERE resource_type='equipment' AND resource_id=? "
                "AND event_type='downtime' ORDER BY effective_start", (equipment_id,)):
            start = parse_date(event["effective_start"], "effective_start")
            end_raw = event["effective_end"]
            end = parse_date(end_raw, "effective_end") if end_raw else None
            if end is not None and end < start:
                continue
            overlap_start = max(start, period_start)
            overlap_end = min(end or next_month, next_month)
            if overlap_start < overlap_end:
                reduction_days += (overlap_end - overlap_start).days
        reduction_days = min(reduction_days, period_days)
        return capacity * reduction_days // period_days

    def _adjudicate(self, conn, platform_id: str, resource_type: str, on: date, *,
                    trigger: str, trigger_commitment: str | None = None) -> list[dict[str, Any]]:
        """按冻结优先规则对当前及未来月份的在保承诺重算净占用额。

        排序键固定为：计划优先级升序、会签完成时间升序、承诺编号兜底。
        人才/设备按（资源，月份）裁定，资金按来源累计到账/到期计划跨月裁定；
        资源容量与机构上限同时构成约束。净占用额变化只生成当日生效的
        backfill/release 流水，且释放后不得低于已履约部分（历史不回写）。
        """

        current_period = period_of(on)
        charter = self._charter_on(conn, platform_id, on)
        caps = self._json(charter["caps_json"])
        cap_key = CAP_KEYS[resource_type]
        org_cap = caps.get(cap_key)

        rows = conn.execute(
            "SELECT c.*, p.priority_rank FROM jv_commitments c JOIN jv_plans p ON c.plan_id=p.plan_id "
            "WHERE c.platform_id=? AND c.resource_type=? AND c.status IN ('active','blocked') "
            "AND c.period_key>=? ORDER BY p.priority_rank, c.signed_at, c.commitment_id",
            (platform_id, resource_type, current_period)).fetchall()
        if resource_type == "funding":
            # 资金必须按月份顺序消费累计可用量，再在同月内按优先级裁定。
            rows = sorted(rows, key=lambda r: (r["period_key"], r["priority_rank"],
                                               r["signed_at"], r["commitment_id"]))

        resource_capacity: dict[tuple, int] = {}
        if resource_type == "person_hours":
            for person in conn.execute(
                    "SELECT * FROM jv_persons WHERE platform_id=? AND status='active'",
                    (platform_id,)):
                for r in rows:
                    if r["resource_id"] == person["person_id"]:
                        resource_capacity[(person["person_id"], r["period_key"])] = \
                            person["capacity_hours_monthly"]
        elif resource_type == "equipment":
            # 停机设备仍保留容量，只是按停机事件折算受影响月份的可用时段。
            for equipment in conn.execute("SELECT * FROM jv_equipment WHERE platform_id=?",
                                          (platform_id,)):
                for r in rows:
                    if r["resource_id"] == equipment["equipment_id"]:
                        reduction = self._downtime_reduction(conn, equipment["equipment_id"],
                                                             r["period_key"], on)
                        resource_capacity[(equipment["equipment_id"], r["period_key"])] = \
                            max(0, equipment["monthly_capacity_hours"] - reduction)
        else:
            source_totals = {s["source_id"]: s["total_amount"]
                             for s in conn.execute("SELECT source_id,total_amount FROM jv_fund_sources "
                                                  "WHERE platform_id=?", (platform_id,))}
            for source in conn.execute("SELECT * FROM jv_fund_sources WHERE platform_id=?", (platform_id,)):
                periods = sorted({r["period_key"] for r in rows if r["resource_id"] == source["source_id"]})
                for period in periods:
                    resource_capacity[(source["source_id"], period)] = min(
                        source_totals[source["source_id"]],
                        self._fund_supply_through(conn, source["source_id"], period))

        resource_used: dict[tuple, int] = {}
        fund_used_total: dict[str, int] = {}
        org_remaining: dict[tuple, int] = {}
        in_batch = {r["commitment_id"] for r in rows}
        resolution: list[dict[str, Any]] = []
        for r in rows:
            bucket = (r["resource_id"], r["period_key"])
            org_bucket = (r["organization_id"], "*" if resource_type == "funding" else r["period_key"])
            if org_cap is not None and org_bucket not in org_remaining:
                used = self._org_cap_used(conn, platform_id, r["organization_id"],
                                          resource_type, r["period_key"], exclude=in_batch, as_of=on)
                org_remaining[org_bucket] = max(0, org_cap - used)
            if resource_type == "funding":
                consumed = fund_used_total.get(r["resource_id"], 0)
                available = resource_capacity.get(bucket, 0) - consumed
            else:
                available = resource_capacity.get(bucket, 0) - resource_used.get(bucket, 0)
            if org_cap is not None:
                available = min(available, org_remaining[org_bucket])
            # 已履约部分是历史事实，任何重算都不能把净占用额压到它以下。
            floor = self._performed(conn, r["commitment_id"], on)
            new_net = min(r["qty"], max(floor, available))
            current_net = self._net_granted(conn, r["commitment_id"], on)
            if new_net > current_net:
                self._insert_allocation(conn, commitment_id=r["commitment_id"], kind="backfill",
                                        qty=new_net - current_net, effective_on=on,
                                        detail={"reason": trigger,
                                                "trigger_commitment": trigger_commitment})
            elif new_net < current_net:
                self._insert_allocation(conn, commitment_id=r["commitment_id"], kind="release",
                                        qty=current_net - new_net, effective_on=on,
                                        detail={"reason": trigger,
                                                "trigger_commitment": trigger_commitment})
            if resource_type == "funding":
                fund_used_total[r["resource_id"]] = consumed + new_net
            else:
                resource_used[bucket] = resource_used.get(bucket, 0) + new_net
            if org_cap is not None:
                org_remaining[org_bucket] = org_remaining.get(org_bucket, 0) - new_net
            new_status = "active" if new_net > 0 else "blocked"
            if new_status != r["status"]:
                self._lifecycle(conn, commitment_id=r["commitment_id"], from_status=r["status"],
                                to_status=new_status, effective_on=on, reason=trigger,
                                detail={"net_granted": new_net})
            resolution.append({"commitment_id": r["commitment_id"], "plan_id": r["plan_id"],
                               "priority_rank": r["priority_rank"], "qty": r["qty"],
                               "net_granted": new_net, "status": new_status,
                               "organization_id": r["organization_id"]})
        if resource_type != "funding":
            resolution.sort(key=lambda item: (item["priority_rank"], item["commitment_id"]))
        return resolution

    def _fund_supply_through(self, conn, source_id: str, period: str) -> int:
        """资金来源到指定月份月末的累计供给额。

        已会签占用的承诺才计入：有分期的按最新分期状态（received/delayed/scheduled），
        未登记分期的承诺视为在其承诺月份全额排期。
        """

        period_end = self._period_end(period)
        total = 0
        commitments = conn.execute(
            "SELECT * FROM jv_commitments WHERE resource_type='funding' AND resource_id=? "
            "AND status IN ('active','blocked')", (source_id,)).fetchall()
        for commitment in commitments:
            latest = {}
            for event in conn.execute(
                    "SELECT * FROM jv_tranche_events WHERE commitment_id=? ORDER BY rowid",
                    (commitment["commitment_id"],)):
                latest[event["tranch_no"]] = event
            if latest:
                for event in latest.values():
                    if parse_date(event["event_date"], "event_date") <= period_end:
                        total += event["amount"]
            elif self._period_end(commitment["period_key"]) <= period_end:
                total += commitment["qty"]
        return total

    @staticmethod
    def _period_end(period: str) -> date:
        start = date.fromisoformat(period + "-01")
        if start.month == 12:
            return date(start.year + 1, 1, 1)
        return date(start.year, start.month + 1, 1)

    def _org_cap_used(self, conn, platform_id: str, organization_id: str, resource_type: str,
                      period_key: str, *, exclude: set[str], as_of: date) -> int:
        """汇总机构在某容量桶内的净占用额；按全额逐笔汇总，拆分承诺无法摊薄。"""

        if resource_type == "funding":
            query = (
                "SELECT commitment_id FROM jv_commitments WHERE platform_id=? "
                "AND organization_id=? AND resource_type=? AND status IN ('active','blocked')")
            parameters: list[Any] = [platform_id, organization_id, resource_type]
        else:
            query = (
                "SELECT commitment_id FROM jv_commitments WHERE platform_id=? "
                "AND organization_id=? AND resource_type=? AND period_key=? "
                "AND status IN ('active','blocked')")
            parameters = [platform_id, organization_id, resource_type, period_key]
        used = 0
        for r in conn.execute(query, parameters):
            if r["commitment_id"] in exclude:
                continue
            used += self._net_granted(conn, r["commitment_id"], as_of)
        return used

    def _insert_allocation(self, conn, *, commitment_id: str, kind: str, qty: int,
                           effective_on: date, detail: dict[str, Any]) -> None:
        if qty <= 0:
            return
        conn.execute(
            "INSERT INTO jv_allocations(allocation_id,commitment_id,kind,qty,effective_on,"
            "detail_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, commitment_id, kind, qty, effective_on.isoformat(),
             canonical_json(detail), "system", self._now()),
        )

    # ------------------------------------------------------------------ 履约

    def record_performance(self, *, request_id: str, actor_id: str, commitment_id: str, qty: int,
                           occurred_on: str, ref_type: str | None = None,
                           ref_key: str | None = None, detail: dict[str, Any] | None = None):
        """登记部分或全部实际履约；累计履约不能超过当日净占用额。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "qty": qty,
                   "occurred_on": occurred_on, "ref_type": ref_type, "ref_key": ref_key}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_WRITE_ROLES)
            row = self._commitment(conn, commitment_id)
            if row["status"] not in ("active", "completed"):
                raise ConflictError("只有在保承诺可以登记履约")
            on = parse_date(occurred_on, "occurred_on")
            qty = int(qty)
            if qty <= 0:
                raise ValidationError("qty 必须为正整数")
            net = self._net_granted(conn, commitment_id, on)
            already = self._performed(conn, commitment_id, on)
            if already + qty > net:
                raise ValidationError(
                    f"累计履约 {already + qty} 超过当日净占用额 {net}，不能虚增已完成投入")

            def create():
                performance_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO jv_performances(performance_id,commitment_id,qty,occurred_on,"
                    "ref_type,ref_key,detail_json,recorded_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (performance_id, commitment_id, qty, on.isoformat(), ref_type, ref_key,
                     canonical_json(detail or {}), actor_id, self._now()),
                )
                performed_total = already + qty
                if row["status"] == "active" and performed_total >= self._net_granted(conn, commitment_id, on):
                    self._lifecycle(conn, commitment_id=commitment_id, from_status="active",
                                    to_status="completed", effective_on=on,
                                    reason="fully_performed")
                self._audit(conn, actor_id=actor_id, action="jv.performance_recorded",
                            resource_type="jv_performance", resource_id=performance_id,
                            detail={"commitment_id": commitment_id, "qty": qty})
                return "jv_performance", performance_id, {
                    "performance_id": performance_id, "performed_total": performed_total,
                    "net_granted": net}

            return self._idempotent(conn, request_id=request_id, action="jv.record_performance",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 异动：离任/停机/资金

    def record_person_departed(self, *, request_id: str, actor_id: str, person_id: str,
                               effective_on: str | None = None, note: str | None = None):
        """人员离任：释放当前及未来未履约义务，已完成投入不回写。"""

        payload = {"actor_id": actor_id, "person_id": person_id, "effective_on": effective_on}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_CONFIG_ROLES)
            person = conn.execute("SELECT * FROM jv_persons WHERE person_id=?", (person_id,)).fetchone()
            if person is None:
                raise NotFoundError("人才不存在")
            if person["status"] == "departed":
                raise ConflictError("该人才已离任，异动不能重复登记")
            on = parse_date(effective_on, "effective_on") if effective_on else self._today()
            if on < self._today():
                raise ValidationError("离任生效日不能早于今天（历史义务不得回写）")

            def create():
                event_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO jv_resource_events(event_id,resource_type,resource_id,event_type,"
                    "effective_start,note,occurred_on,recorded_by,created_at) "
                    "VALUES(?,?,?, 'departed', ?,?,?,?,?)",
                    (event_id, "person_hours", person_id, on.isoformat(), note,
                     on.isoformat(), actor_id, self._now()),
                )
                conn.execute("UPDATE jv_persons SET status='departed', departed_on=? WHERE person_id=?",
                             (on.isoformat(), person_id))
                affected = self._terminalize(conn, person_id, "person_hours", on, actor,
                                             reason="person_departed")
                resolution = self._adjudicate(conn, person["platform_id"], "person_hours", on,
                                              trigger="person_departed")
                self._audit(conn, actor_id=actor_id, action="jv.person_departed",
                            resource_type="jv_person", resource_id=person_id,
                            detail={"effective_on": on.isoformat(), "affected": affected,
                                    "resolution": resolution})
                return "jv_resource_event", event_id, {
                    "event_id": event_id, "affected_commitments": affected,
                    "resolution": resolution}

            return self._idempotent(conn, request_id=request_id, action="jv.person_departed",
                                    payload=payload, create=create)

    def record_equipment_downtime(self, *, request_id: str, actor_id: str, equipment_id: str,
                                  effective_start: str, effective_end: str | None = None,
                                  note: str | None = None):
        """设备停机：只调减受影响的当前/未来月份容量并按优先规则重算。"""

        payload = {"actor_id": actor_id, "equipment_id": equipment_id,
                   "effective_start": effective_start, "effective_end": effective_end}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_CONFIG_ROLES)
            equipment = conn.execute("SELECT * FROM jv_equipment WHERE equipment_id=?",
                                     (equipment_id,)).fetchone()
            if equipment is None:
                raise NotFoundError("设备不存在")
            start = parse_date(effective_start, "effective_start")
            end = parse_date(effective_end, "effective_end") if effective_end else None
            if start < self._today():
                raise ValidationError("停机起始不能早于今天（历史义务不得回写）")
            if end and end < start:
                raise ValidationError("停机结束不能早于开始")
            open_event = conn.execute(
                "SELECT 1 FROM jv_resource_events WHERE resource_type='equipment' AND resource_id=? "
                "AND event_type='downtime' AND effective_end IS NULL", (equipment_id,)).fetchone()
            if open_event:
                raise ConflictError("该设备已有未结案的停机登记，请先登记恢复")
            conn.execute("UPDATE jv_equipment SET status='down' WHERE equipment_id=?", (equipment_id,))

            def create():
                event_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO jv_resource_events(event_id,resource_type,resource_id,event_type,"
                    "effective_start,effective_end,note,occurred_on,recorded_by,created_at) "
                    "VALUES(?,?,?, 'downtime', ?,?,?,?,?,?)",
                    (event_id, "equipment", equipment_id, start.isoformat(),
                     end.isoformat() if end else None, note, self._today().isoformat(),
                     actor_id, self._now()),
                )
                resolution = self._adjudicate(conn, equipment["platform_id"], "equipment", start,
                                              trigger="equipment_downtime")
                self._audit(conn, actor_id=actor_id, action="jv.equipment_downtime",
                            resource_type="jv_equipment", resource_id=equipment_id,
                            detail={"effective_start": start.isoformat(),
                                    "effective_end": end.isoformat() if end else None,
                                    "resolution": resolution})
                return "jv_resource_event", event_id, {"event_id": event_id, "resolution": resolution}

            return self._idempotent(conn, request_id=request_id, action="jv.equipment_downtime",
                                    payload=payload, create=create)

    def record_equipment_recovered(self, *, request_id: str, actor_id: str, equipment_id: str,
                                   effective_on: str, note: str | None = None):
        """设备恢复：结案停机事件，未来月份容量恢复，等待中的承诺按优先规则回填。"""

        payload = {"actor_id": actor_id, "equipment_id": equipment_id, "effective_on": effective_on}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_CONFIG_ROLES)
            equipment = conn.execute("SELECT * FROM jv_equipment WHERE equipment_id=?",
                                     (equipment_id,)).fetchone()
            if equipment is None:
                raise NotFoundError("设备不存在")
            on = parse_date(effective_on, "effective_on")
            if on < self._today():
                raise ValidationError("恢复日期不能早于今天")
            open_event = conn.execute(
                "SELECT * FROM jv_resource_events WHERE resource_type='equipment' AND resource_id=? "
                "AND event_type='downtime' AND effective_end IS NULL", (equipment_id,)).fetchone()
            if open_event is None:
                raise ConflictError("该设备没有未结案的停机登记")
            conn.execute("UPDATE jv_resource_events SET effective_end=? WHERE event_id=?",
                         (on.isoformat(), open_event["event_id"]))
            conn.execute("UPDATE jv_equipment SET status='active' WHERE equipment_id=?", (equipment_id,))

            def create():
                resolution = self._adjudicate(conn, equipment["platform_id"], "equipment", on,
                                              trigger="equipment_recovered")
                self._audit(conn, actor_id=actor_id, action="jv.equipment_recovered",
                            resource_type="jv_equipment", resource_id=equipment_id,
                            detail={"effective_on": on.isoformat(), "resolution": resolution})
                return "jv_equipment", equipment_id, {"equipment_id": equipment_id,
                                                      "resolution": resolution}

            return self._idempotent(conn, request_id=request_id, action="jv.equipment_recovered",
                                    payload=payload, create=create)

    def delay_fund_tranch(self, *, request_id: str, actor_id: str, commitment_id: str,
                          tranch_no: int, new_due_date: str, note: str | None = None):
        """资金分期延期：只改未来到期计划，已到账与已完成投入不变。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id,
                   "tranch_no": tranch_no, "new_due_date": new_due_date}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_WRITE_ROLES)
            row = self._commitment(conn, commitment_id)
            if row["resource_type"] != "funding":
                raise ValidationError("只有资金承诺可以登记分期延期")
            if row["status"] not in ("active", "blocked", "awaiting_signoff"):
                raise ConflictError("承诺已终局，不能再调整分期")
            no = int(tranch_no)
            new_due = parse_date(new_due_date, "new_due_date")
            latest = conn.execute(
                "SELECT * FROM jv_tranche_events WHERE commitment_id=? AND tranch_no=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1", (commitment_id, no)).fetchone()
            if latest is None:
                raise NotFoundError("分期不存在")
            if latest["event_type"] == "received":
                raise ConflictError("分期已到账，不能回写")
            old_due = parse_date(latest["event_date"], "event_date")
            if new_due <= old_due:
                raise ValidationError("延期后的到期日必须晚于原到期日")

            def create():
                tranch_event_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO jv_tranche_events(tranche_event_id,commitment_id,tranch_no,amount,"
                    "event_type,event_date,recorded_by,created_at) VALUES(?,?,?,?,'delayed',?,?,?)",
                    (tranch_event_id, commitment_id, no, latest["amount"], new_due.isoformat(),
                     actor_id, self._now()),
                )
                resolution = []
                platform_id = row["platform_id"]
                if row["status"] in ("active", "blocked"):
                    resolution = self._adjudicate(conn, platform_id, "funding", self._today(),
                                                  trigger="fund_tranch_delayed",
                                                  trigger_commitment=commitment_id)
                self._audit(conn, actor_id=actor_id, action="jv.fund_tranch_delayed",
                            resource_type="jv_commitment", resource_id=commitment_id,
                            detail={"tranch_no": no, "old_due_date": old_due.isoformat(),
                                    "new_due_date": new_due.isoformat(), "resolution": resolution})
                return "jv_tranche_event", tranch_event_id, {
                    "tranch_event_id": tranch_event_id, "tranch_no": no,
                    "new_due_date": new_due.isoformat(), "resolution": resolution}

            return self._idempotent(conn, request_id=request_id, action="jv.delay_fund_tranch",
                                    payload=payload, create=create)

    def receive_fund_tranch(self, *, request_id: str, actor_id: str, commitment_id: str,
                            tranch_no: int, received_on: str):
        """登记资金分期实际到账。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id,
                   "tranch_no": tranch_no, "received_on": received_on}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_WRITE_ROLES)
            self._commitment(conn, commitment_id)
            on = parse_date(received_on, "received_on")
            latest = conn.execute(
                "SELECT * FROM jv_tranche_events WHERE commitment_id=? AND tranch_no=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1", (commitment_id, int(tranch_no))).fetchone()
            if latest is None:
                raise NotFoundError("分期不存在")
            if latest["event_type"] == "received":
                raise ConflictError("分期已到账，不能重复登记")
            source_row = self._commitment(conn, commitment_id)

            def create():
                tranch_event_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO jv_tranche_events(tranche_event_id,commitment_id,tranch_no,amount,"
                    "event_type,event_date,recorded_by,created_at) VALUES(?,?,?,?,'received',?,?,?)",
                    (tranch_event_id, commitment_id, int(tranch_no), latest["amount"], on.isoformat(),
                     actor_id, self._now()),
                )
                resolution = self._adjudicate(conn, source_row["platform_id"], "funding", self._today(),
                                              trigger="fund_tranch_received",
                                              trigger_commitment=commitment_id)
                self._audit(conn, actor_id=actor_id, action="jv.fund_tranch_received",
                            resource_type="jv_commitment", resource_id=commitment_id,
                            detail={"tranch_no": int(tranch_no), "received_on": on.isoformat(),
                                    "resolution": resolution})
                return "jv_tranche_event", tranch_event_id, {
                    "tranch_event_id": tranch_event_id, "received_on": on.isoformat(),
                    "resolution": resolution}

            return self._idempotent(conn, request_id=request_id, action="jv.receive_fund_tranch",
                                    payload=payload, create=create)

    def _terminalize(self, conn, resource_id: str | None, resource_type: str, on: date, actor, *,
                     reason: str, commitment_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """释放当前及未来在保承诺中未履约的部分；已履约部分原样保留。

        可按资源（释放该资源上的全部在保承诺，含跨机构重复主张）或按显式
        承诺清单（用于成员退出其以他人资源作出的承诺）调用。
        """

        affected = []
        if commitment_ids is not None:
            rows = [self._commitment(conn, cid) for cid in commitment_ids]
        else:
            rows = conn.execute(
                "SELECT * FROM jv_commitments WHERE resource_type=? AND resource_id=? "
                "AND status IN ('active','blocked') AND period_key>=? ORDER BY commitment_id",
                (resource_type, resource_id, period_of(on))).fetchall()
        for row in rows:
            if row["status"] not in ("active", "blocked"):
                continue
            net = self._net_granted(conn, row["commitment_id"], on)
            performed = min(net, self._performed(conn, row["commitment_id"], on))
            future = net - performed
            if future > 0:
                self._insert_allocation(conn, commitment_id=row["commitment_id"], kind="release",
                                        qty=future, effective_on=on, detail={"reason": reason})
            to_status = "completed" if future == 0 and performed > 0 else "released"
            self._lifecycle(conn, commitment_id=row["commitment_id"], from_status=row["status"],
                            to_status=to_status, effective_on=on, reason=reason,
                            detail={"performed_kept": performed, "released": future})
            affected.append({"commitment_id": row["commitment_id"], "performed_kept": performed,
                             "released": future, "status": to_status})
        return affected

    # ------------------------------------------------------------------ 成员退出

    def record_member_exit(self, *, request_id: str, actor_id: str, platform_id: str,
                           organization_id: str, effective_on: str | None = None,
                           note: str | None = None):
        """成员退出：未来义务按资源逐笔释放，成果权益只影响尚未固化的里程碑。"""

        payload = {"actor_id": actor_id, "platform_id": platform_id,
                   "organization_id": organization_id, "effective_on": effective_on}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_CONFIG_ROLES)
            self._platform(conn, platform_id)
            membership = self._active_membership(conn, platform_id, organization_id)
            on = parse_date(effective_on, "effective_on") if effective_on else self._today()
            if on < self._today():
                raise ValidationError("退出生效日不能早于今天")

            def create():
                conn.execute(
                    "UPDATE jv_memberships SET status='exited', exited_on=? WHERE platform_id=? "
                    "AND organization_id=?",
                    (on.isoformat(), platform_id, organization_id))
                all_affected: list[dict[str, Any]] = []
                touched_types = set()
                # 退出机构拥有的资源：释放其上所有在保承诺（含其他机构的重复主张）。
                for resource_type, table, id_col in (
                        ("person_hours", "jv_persons", "person_id"),
                        ("equipment", "jv_equipment", "equipment_id"),
                        ("funding", "jv_fund_sources", "source_id")):
                    resources = conn.execute(
                        f"SELECT {id_col} AS rid FROM {table} WHERE platform_id=? AND organization_id=?",
                        (platform_id, organization_id)).fetchall()
                    for resource in resources:
                        affected = self._terminalize(
                            conn, resource["rid"], resource_type, on, actor, reason="member_exit")
                        if affected:
                            touched_types.add(resource_type)
                        all_affected.extend(affected)
                # 退出机构以他人资源作出的承诺同样终止（跨区重复主张失效）。
                foreign = conn.execute(
                    "SELECT commitment_id FROM jv_commitments WHERE platform_id=? AND organization_id=? "
                    "AND status IN ('active','blocked')",
                    (platform_id, organization_id)).fetchall()
                for item in foreign:
                    crow = self._commitment(conn, item["commitment_id"])
                    affected = self._terminalize(
                        conn, None, crow["resource_type"], on, actor, reason="member_exit",
                        commitment_ids=(item["commitment_id"],))
                    for entry in affected:
                        touched_types.add(crow["resource_type"])
                    all_affected.extend(affected)
                summary = []
                for resource_type in ("person_hours", "equipment", "funding"):
                    resolution = self._adjudicate(conn, platform_id, resource_type, on,
                                                  trigger="member_exit")
                    if resource_type in touched_types or any(
                            x["commitment_id"] in {a.get("commitment_id") for a in all_affected}
                            for x in resolution):
                        summary.append({"resource_type": resource_type, "resolution": resolution})
                self._audit(conn, actor_id=actor_id, action="jv.member_exited",
                            resource_type="jv_membership",
                            resource_id=f"{platform_id}:{organization_id}",
                            detail={"effective_on": on.isoformat(), "affected": all_affected,
                                    "resolutions": summary})
                return "jv_membership", f"{platform_id}:{organization_id}", {
                    "platform_id": platform_id, "organization_id": organization_id,
                    "exited_on": on.isoformat(), "affected": all_affected, "resolutions": summary}

            return self._idempotent(conn, request_id=request_id, action="jv.member_exit",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 成果权益

    def set_outcome_policy(self, *, request_id: str, actor_id: str, plan_id: str,
                           shares: dict[str, int], effective_from: str | None = None,
                           change_note: str | None = None):
        """登记计划的成果权益分配方案（按机构百分比，整数合计 100），版本化追加。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id, "shares": shares,
                   "effective_from": effective_from}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_CONFIG_ROLES)
            plan = conn.execute("SELECT * FROM jv_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan is None:
                raise NotFoundError("计划不存在")
            if not isinstance(shares, dict) or not shares:
                raise ValidationError("shares 必须是非空对象")
            clean = {str(k): int(v) for k, v in shares.items()}
            if any(v < 0 for v in clean.values()) or sum(clean.values()) != 100:
                raise ValidationError("权益比例必须是非负整数且合计 100")
            for org in clean:
                self._active_membership(conn, plan["platform_id"], org)
            effective = parse_date(effective_from, "effective_from") if effective_from else self._today()
            previous = conn.execute(
                "SELECT * FROM jv_outcome_policies WHERE plan_id=? ORDER BY version_no DESC LIMIT 1",
                (plan_id,)).fetchone()
            if previous and effective <= parse_date(previous["effective_from"], "effective_from"):
                raise ValidationError("新方案生效日期必须晚于既往方案")

            def create():
                policy_id = uuid.uuid4().hex
                version_no = previous["version_no"] + 1 if previous else 1
                if previous:
                    conn.execute("UPDATE jv_outcome_policies SET effective_to=? WHERE policy_id=?",
                                 (effective.isoformat(), previous["policy_id"]))
                conn.execute(
                    "INSERT INTO jv_outcome_policies(policy_id,plan_id,version_no,shares_json,"
                    "effective_from,effective_to,change_note,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (policy_id, plan_id, version_no, canonical_json(clean), effective.isoformat(),
                     None, change_note, actor_id, self._now()),
                )
                self._audit(conn, actor_id=actor_id, action="jv.outcome_policy_set",
                            resource_type="jv_outcome_policy", resource_id=policy_id,
                            detail={"plan_id": plan_id, "version_no": version_no})
                return "jv_outcome_policy", policy_id, {"policy_id": policy_id, "version_no": version_no}

            return self._idempotent(conn, request_id=request_id, action="jv.set_outcome_policy",
                                    payload=payload, create=create)

    def finalize_outcome(self, *, request_id: str, actor_id: str, milestone_id: str,
                         finalized_on: str | None = None):
        """固化里程碑成果权益；一旦固化不可回写，退出/章程变更只影响后续里程碑。"""

        payload = {"actor_id": actor_id, "milestone_id": milestone_id,
                   "finalized_on": finalized_on}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require_roles(actor, *_CONFIG_ROLES)
            milestone = conn.execute("SELECT * FROM jv_milestones WHERE milestone_id=?",
                                     (milestone_id,)).fetchone()
            if milestone is None:
                raise NotFoundError("里程碑不存在")
            if milestone["status"] != "completed":
                raise ValidationError("里程碑完成后才能固化成果权益")
            existing = conn.execute("SELECT 1 FROM jv_outcome_awards WHERE milestone_id=?",
                                    (milestone_id,)).fetchone()
            if existing:
                raise ConflictError("成果权益已固化，不能重复分配或回写")
            plan = conn.execute("SELECT * FROM jv_plans WHERE plan_id=?",
                                (milestone["plan_id"],)).fetchone()
            on = parse_date(finalized_on, "finalized_on") if finalized_on else self._today()
            charter = self._charter_on(conn, plan["platform_id"], on)
            policy = conn.execute(
                "SELECT * FROM jv_outcome_policies WHERE plan_id=? AND effective_from<=? "
                "ORDER BY version_no DESC LIMIT 1",
                (plan["plan_id"], on.isoformat())).fetchone()
            if policy is None:
                raise ValidationError("该日期没有生效的成果权益方案")
            shares = self._redistribute(conn, plan["platform_id"], self._json(policy["shares_json"]),
                                        on, plan, charter["outcome_redistribution"])
            basis = self._award_basis(conn, milestone_id, on)
            landing_conditions = self._landing_conditions(conn, plan["platform_id"], shares, on)

            def create():
                award_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO jv_outcome_awards(award_id,milestone_id,plan_id,shares_json,"
                    "landing_conditions_json,basis_json,finalized_on,finalized_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (award_id, milestone_id, plan["plan_id"], canonical_json(shares),
                     canonical_json(landing_conditions), canonical_json(basis), on.isoformat(),
                     actor_id, self._now()),
                )
                self._audit(conn, actor_id=actor_id, action="jv.outcome_finalized",
                            resource_type="jv_outcome_award", resource_id=award_id,
                            detail={"milestone_id": milestone_id, "shares": shares})
                return "jv_outcome_award", award_id, {"award_id": award_id, "shares": shares,
                                                      "landing_conditions": landing_conditions}

            return self._idempotent(conn, request_id=request_id, action="jv.finalize_outcome",
                                    payload=payload, create=create)

    def _redistribute(self, conn, platform_id: str, shares: dict[str, int], on: date,
                      plan, rule: str, simulate_exit_org: str | None = None) -> dict[str, Any]:
        active = set()
        org_rows = conn.execute(
            "SELECT organization_id, joined_on, status, exited_on FROM jv_memberships "
            "WHERE platform_id=?", (platform_id,)).fetchall()
        for membership in org_rows:
            org = membership["organization_id"]
            if (parse_date(membership["joined_on"], "joined_on") <= on and
                    not (membership["status"] == "exited" and
                         parse_date(membership["exited_on"], "exited_on") <= on) and
                    org != simulate_exit_org):
                active.add(org)
        remaining = {org: pct for org, pct in shares.items() if org in active and pct > 0}
        lapsed = sum(pct for org, pct in shares.items() if org not in active)
        result: dict[str, Any] = {org: pct for org, pct in remaining.items()}
        if lapsed == 0:
            return result
        if rule == "lapse":
            result["_lapsed"] = lapsed
            return result
        if rule == "lead":
            target = plan["lead_organization_id"]
            result[target] = result.get(target, 0) + lapsed
            return result
        base = sum(remaining.values())
        if base == 0:
            result[plan["lead_organization_id"]] = result.get(plan["lead_organization_id"], 0) + lapsed
            return result
        # 最大余数法保证整数比例仍合计 100。
        allocated = {org: pct * lapsed // base for org, pct in remaining.items()}
        leftover = lapsed - sum(allocated.values())
        for org, _ in sorted(remaining.items(),
                             key=lambda kv: (-(kv[1] * lapsed % base), kv[0]))[:leftover]:
            allocated[org] += 1
        for org, extra in allocated.items():
            result[org] = result.get(org, 0) + extra
        return result

    def _award_basis(self, conn, milestone_id: str, on: date) -> dict[str, Any]:
        basis: dict[str, Any] = {}
        rows = conn.execute(
            "SELECT * FROM jv_commitments WHERE milestone_id=?", (milestone_id,)).fetchall()
        for row in rows:
            net = self._net_granted(conn, row["commitment_id"], on)
            performed = self._performed(conn, row["commitment_id"], on)
            basis.setdefault(row["organization_id"], []).append({
                "commitment_id": row["commitment_id"], "resource_type": row["resource_type"],
                "qty": row["qty"], "net_granted": net, "performed": performed,
                "status_as_of": self._status_as_of(conn, row["commitment_id"], on)})
        return basis

    def _landing_conditions(self, conn, platform_id: str, shares: dict[str, int],
                            on: date) -> list[dict[str, Any]]:
        conditions = []
        for org in shares:
            if org.startswith("_"):
                continue
            membership = conn.execute(
                "SELECT * FROM jv_memberships WHERE platform_id=? AND organization_id=?",
                (platform_id, org)).fetchone()
            local = self._json(membership["local_conditions_json"]) if membership else {}
            if local:
                conditions.append({"organization_id": org, "kind": "local_condition",
                                   "conditions": local})
            for source in conn.execute(
                    "SELECT * FROM jv_fund_sources WHERE platform_id=? AND organization_id=?",
                    (platform_id, org)):
                restrictions = self._json(source["restrictions_json"])
                if restrictions:
                    conditions.append({"organization_id": org, "kind": "fund_restriction",
                                       "source_code": source["source_code"],
                                       "restrictions": restrictions})
        return conditions

    # ------------------------------------------------------------------ 查询与复原

    def get_charter_at(self, platform_id: str, as_of: str | None = None) -> dict[str, Any]:
        """按历史日期复原当时有效的章程版本。"""

        on = parse_date(as_of, "as_of") if as_of else self._today()
        conn = self.database.connection
        self._platform(conn, platform_id)
        row = conn.execute(
            "SELECT * FROM jv_charter_versions WHERE platform_id=? AND effective_from<=? "
            "ORDER BY version_no DESC LIMIT 1",
            (platform_id, on.isoformat())).fetchone()
        if row is None:
            return {"platform_id": platform_id, "as_of": on.isoformat(), "charter": None}
        return {"platform_id": platform_id, "as_of": on.isoformat(), "charter": {
            "charter_id": row["charter_id"], "version_no": row["version_no"],
            "priority_rules": self._json(row["priority_rules_json"]),
            "caps": self._json(row["caps_json"]),
            "outcome_redistribution": row["outcome_redistribution"],
            "content": self._json(row["content_json"]),
            "effective_from": row["effective_from"], "effective_to": row["effective_to"],
            "status": row["status"]}}

    def _status_as_of(self, conn, commitment_id: str, on: date) -> str:
        row = conn.execute(
            "SELECT to_status FROM jv_lifecycle_events WHERE commitment_id=? AND effective_on<=? "
            "ORDER BY effective_on DESC, rowid DESC LIMIT 1",
            (commitment_id, on.isoformat())).fetchone()
        return row["to_status"] if row else "draft"

    def get_commitment(self, commitment_id: str, as_of: str | None = None) -> dict[str, Any]:
        on = parse_date(as_of, "as_of") if as_of else self._today()
        conn = self.database.connection
        row = conn.execute("SELECT * FROM jv_commitments WHERE commitment_id=?",
                           (commitment_id,)).fetchone()
        if row is None:
            raise NotFoundError("承诺不存在")
        if row["created_at"][:10] > on.isoformat():
            raise NotFoundError("该日期此承诺尚未登记")
        allocations = [
            {"kind": r["kind"], "qty": r["qty"], "effective_on": r["effective_on"],
             "detail": self._json(r["detail_json"])}
            for r in conn.execute(
                "SELECT * FROM jv_allocations WHERE commitment_id=? AND effective_on<=? "
                "ORDER BY effective_on, rowid", (commitment_id, on.isoformat()))]
        performances = [
            {"qty": r["qty"], "occurred_on": r["occurred_on"]}
            for r in conn.execute(
                "SELECT * FROM jv_performances WHERE commitment_id=? AND occurred_on<=? "
                "ORDER BY occurred_on, rowid", (commitment_id, on.isoformat()))]
        signoffs = [
            {"organization_id": r["organization_id"], "signed_at": r["signed_at"]}
            for r in conn.execute(
                "SELECT * FROM jv_signoffs WHERE commitment_id=? AND signed_at<=? ORDER BY signed_at",
                (commitment_id, on.isoformat()))]
        return {
            "commitment_id": commitment_id, "platform_id": row["platform_id"],
            "plan_id": row["plan_id"], "milestone_id": row["milestone_id"],
            "organization_id": row["organization_id"], "resource_type": row["resource_type"],
            "resource_id": row["resource_id"], "qty": row["qty"],
            "period_key": row["period_key"], "window_start": row["window_start"],
            "window_end": row["window_end"],
            "status_as_of": self._status_as_of(conn, commitment_id, on),
            "charter_id": row["charter_id"], "signed_at": row["signed_at"],
            "required_signoffs": self._json(row["required_signoffs_json"]),
            "signoffs": signoffs, "allocations": allocations, "performances": performances,
            "net_granted": self._net_granted(conn, commitment_id, on),
            "performed": self._performed(conn, commitment_id, on)}

    def get_platform_snapshot(self, platform_id: str, as_of: str | None = None) -> dict[str, Any]:
        """按历史日期复原平台当时有效的章程、成员资格与全部承诺状态。"""

        on = parse_date(as_of, "as_of") if as_of else self._today()
        conn = self.database.connection
        self._platform(conn, platform_id)
        charter = self.get_charter_at(platform_id, on.isoformat())["charter"]
        memberships = []
        for row in conn.execute(
                "SELECT * FROM jv_memberships WHERE platform_id=? AND joined_on<=? ORDER BY member_order",
                (platform_id, on.isoformat())):
            exited = row["status"] == "exited" and row["exited_on"] and row["exited_on"] <= on.isoformat()
            memberships.append({"organization_id": row["organization_id"],
                                "member_role": row["member_role"], "member_order": row["member_order"],
                                "local_conditions": self._json(row["local_conditions_json"]),
                                "joined_on": row["joined_on"],
                                "status_as_of": "exited" if exited else "active"})
        commitments = []
        for row in conn.execute(
                "SELECT commitment_id FROM jv_commitments WHERE platform_id=? AND created_at<=? "
                "ORDER BY rowid", (platform_id, on.isoformat() + "T23:59:59")):
            commitments.append(self.get_commitment(row["commitment_id"], on.isoformat()))
        awards = []
        for row in conn.execute(
                "SELECT a.* FROM jv_outcome_awards a JOIN jv_plans p ON a.plan_id=p.plan_id "
                "WHERE p.platform_id=? AND a.finalized_on<=? ORDER BY finalized_on",
                (platform_id, on.isoformat())):
            awards.append({"award_id": row["award_id"], "milestone_id": row["milestone_id"],
                           "shares": self._json(row["shares_json"]),
                           "finalized_on": row["finalized_on"]})
        return {"platform_id": platform_id, "as_of": on.isoformat(), "charter": charter,
                "memberships": memberships, "commitments": commitments, "outcome_awards": awards}

    def get_milestone_readiness(self, milestone_id: str, as_of: str | None = None) -> dict[str, Any]:
        """理事会视图：每个里程碑是否具备真实资源、缺口由谁补足、退出影响。"""

        on = parse_date(as_of, "as_of") if as_of else self._today()
        conn = self.database.connection
        milestone = conn.execute("SELECT * FROM jv_milestones WHERE milestone_id=?",
                                 (milestone_id,)).fetchone()
        if milestone is None:
            raise NotFoundError("里程碑不存在")
        plan = conn.execute("SELECT * FROM jv_plans WHERE plan_id=?",
                            (milestone["plan_id"],)).fetchone()
        platform_id = plan["platform_id"]
        charter = self._charter_on(conn, platform_id, on)
        caps = self._json(charter["caps_json"])
        commitments = conn.execute(
            "SELECT * FROM jv_commitments WHERE milestone_id=? AND created_at<=? ORDER BY rowid",
            (milestone_id, on.isoformat() + "T23:59:59")).fetchall()

        coverage: dict[str, Any] = {}
        for requirement in self._json(milestone["requirements_json"]):
            key = f"{requirement['resource_type']}:{requirement.get('resource_id') or '*'}:{requirement.get('period_key') or '*'}"
            coverage[key] = {"requirement": requirement, "allocated": 0, "performed": 0,
                             "received_funding": 0, "scheduled_funding": 0, "sources": []}
        for row in commitments:
            net = self._net_granted(conn, row["commitment_id"], on)
            performed = self._performed(conn, row["commitment_id"], on)
            req = self._matching_requirement(self._json(milestone["requirements_json"]), row)
            if req is None:
                continue
            key = f"{req['resource_type']}:{req.get('resource_id') or '*'}:{req.get('period_key') or '*'}"
            entry = coverage[key]
            entry["allocated"] += net
            entry["performed"] += performed
            entry["sources"].append({
                "commitment_id": row["commitment_id"], "organization_id": row["organization_id"],
                "status_as_of": self._status_as_of(conn, row["commitment_id"], on),
                "net_granted": net, "performed": performed})
            if row["resource_type"] == "funding":
                received, scheduled = self._tranche_totals(conn, row["commitment_id"], on)
                entry["received_funding"] += received
                entry["scheduled_funding"] += scheduled

        gaps = []
        ready = True
        for key, entry in coverage.items():
            requirement = entry["requirement"]
            if entry["allocated"] < requirement["qty"]:
                ready = False
                gaps.append(self._gap_detail(conn, platform_id, requirement, entry, caps, on))
        return {
            "milestone_id": milestone_id, "plan_id": plan["plan_id"],
            "platform_id": platform_id, "as_of": on.isoformat(),
            "due_date": milestone["due_date"], "milestone_status": milestone["status"],
            "charter_id": charter["charter_id"], "charter_version_no": charter["version_no"],
            "ready": ready, "coverage": list(coverage.values()), "gaps": gaps,
            "exit_impacts": self._exit_impacts(conn, platform_id, milestone_id, on, plan, charter)}

    def _matching_requirement(self, requirements: list[dict[str, Any]], row) -> dict[str, Any] | None:
        candidates = [r for r in requirements if r["resource_type"] == row["resource_type"]]
        exact = [r for r in candidates if r.get("resource_id") == row["resource_id"]]
        if exact:
            period_match = [r for r in exact if not r.get("period_key") or r["period_key"] == row["period_key"]]
            return (period_match or exact)[0]
        loose = [r for r in candidates if not r.get("resource_id")]
        period_match = [r for r in loose if not r.get("period_key") or r["period_key"] == row["period_key"]]
        return (period_match or loose or [None])[0]

    def _tranche_totals(self, conn, commitment_id: str, on: date) -> tuple[int, int]:
        received = 0
        scheduled = 0
        rows = conn.execute(
            "SELECT tranch_no, event_type, event_date, amount FROM jv_tranche_events "
            "WHERE commitment_id=? ORDER BY tranch_no, rowid", (commitment_id,)).fetchall()
        latest: dict[int, object] = {}
        for row in rows:
            latest[row["tranch_no"]] = row
        for row in latest.values():
            if row["event_type"] == "received" and parse_date(row["event_date"], "event_date") <= on:
                received += row["amount"]
            elif row["event_type"] in ("scheduled", "delayed") and parse_date(row["event_date"], "event_date") <= on:
                scheduled += row["amount"]
        return received, scheduled

    def _gap_detail(self, conn, platform_id: str, requirement: dict[str, Any],
                    entry: dict[str, Any], caps: dict[str, int], on: date,
                    exclude_org: str | None = None) -> dict[str, Any]:
        rtype = requirement["resource_type"]
        gap = requirement["qty"] - entry["allocated"]
        period = requirement.get("period_key") or period_of(on)
        cap_key = CAP_KEYS[rtype]
        candidates = []
        memberships = conn.execute(
            "SELECT * FROM jv_memberships WHERE platform_id=? AND joined_on<=? "
            "AND (status='active' OR exited_on>?) ORDER BY member_order",
            (platform_id, on.isoformat(), on.isoformat())).fetchall()
        for membership in memberships:
            org = membership["organization_id"]
            if org == exclude_org:
                continue
            if rtype == "person_hours":
                pool = conn.execute(
                    "SELECT person_id, organization_id, capacity_hours_monthly FROM jv_persons "
                    "WHERE platform_id=? AND organization_id=? AND status='active'",
                    (platform_id, org)).fetchall()
            elif rtype == "equipment":
                pool = conn.execute(
                    "SELECT equipment_id AS person_id, organization_id, "
                    "monthly_capacity_hours AS capacity_hours_monthly FROM jv_equipment "
                    "WHERE platform_id=? AND organization_id=? AND status='active'",
                    (platform_id, org)).fetchall()
            else:
                pool = conn.execute(
                    "SELECT source_id AS person_id, organization_id, total_amount AS capacity_hours_monthly "
                    "FROM jv_fund_sources WHERE platform_id=? AND organization_id=?",
                    (platform_id, org)).fetchall()
            org_used = self._org_cap_used(conn, platform_id, org, rtype,
                                          period if rtype != "funding" else "*",
                                          exclude=set(), as_of=on)
            org_headroom = None
            if caps.get(cap_key) is not None:
                org_headroom = max(0, caps[cap_key] - org_used)
            for item in pool:
                rid = item["person_id"]
                if requirement.get("resource_id") and requirement["resource_id"] != rid:
                    continue
                if rtype == "funding":
                    committed = self._resource_committed(conn, rid, on)
                    resource_headroom = item["capacity_hours_monthly"] - committed
                else:
                    committed = self._resource_committed(conn, rid, on, period)
                    capacity = item["capacity_hours_monthly"]
                    if rtype == "equipment":
                        capacity -= self._downtime_reduction(conn, rid, period, on)
                    resource_headroom = max(0, capacity - committed)
                headroom = resource_headroom
                if org_headroom is not None:
                    headroom = min(headroom, org_headroom)
                if headroom > 0:
                    candidates.append({"organization_id": org, "resource_id": rid,
                                       "headroom": headroom,
                                       "member_role": membership["member_role"]})
        candidates.sort(key=lambda c: (0 if c["organization_id"] != exclude_org else 1, -c["headroom"]))
        lead_note = "牵头单位负责协调补足" if gap > 0 else None
        return {"resource_type": rtype, "resource_id": requirement.get("resource_id"),
                "period_key": period, "required": requirement["qty"],
                "allocated": entry["allocated"], "gap": gap,
                "candidates": candidates, "note": lead_note}

    def _resource_committed(self, conn, resource_id: str, on: date, period_key: str | None = None) -> int:
        if period_key is None:
            rows = conn.execute(
                "SELECT commitment_id FROM jv_commitments WHERE resource_id=? "
                "AND status IN ('active','blocked') AND period_key>=?",
                (resource_id, period_of(on))).fetchall()
            return sum(self._net_granted(conn, r["commitment_id"], on) for r in rows)
        rows = conn.execute(
            "SELECT commitment_id FROM jv_commitments WHERE resource_id=? AND period_key=? "
            "AND status IN ('active','blocked')", (resource_id, period_key)).fetchall()
        return sum(self._net_granted(conn, r["commitment_id"], on) for r in rows)

    def _exit_impacts(self, conn, platform_id: str, milestone_id: str, on: date, plan, charter) -> list[dict[str, Any]]:
        """模拟任一在保成员退出时对该里程碑后续权益与资源的影响（不写库）。"""

        impacts = []
        for membership in conn.execute(
                "SELECT * FROM jv_memberships WHERE platform_id=? AND status='active' ORDER BY member_order",
                (platform_id,)):
            org = membership["organization_id"]
            policy = conn.execute(
                "SELECT * FROM jv_outcome_policies WHERE plan_id=? AND effective_from<=? "
                "ORDER BY version_no DESC LIMIT 1", (plan["plan_id"], on.isoformat())).fetchone()
            future_shares = self._json(policy["shares_json"]) if policy else {}
            simulated = self._redistribute(conn, platform_id, future_shares, on, plan,
                                           charter["outcome_redistribution"],
                                           simulate_exit_org=org) if future_shares else {}
            released_by_type: dict[str, int] = {}
            performed_by_type: dict[str, int] = {}
            for row in conn.execute(
                    "SELECT * FROM jv_commitments WHERE milestone_id=? AND organization_id=? "
                    "AND status IN ('active','blocked')", (milestone_id, org)):
                net = self._net_granted(conn, row["commitment_id"], on)
                performed = min(net, self._performed(conn, row["commitment_id"], on))
                released_by_type[row["resource_type"]] = \
                    released_by_type.get(row["resource_type"], 0) + net - performed
                performed_by_type[row["resource_type"]] = \
                    performed_by_type.get(row["resource_type"], 0) + performed
            if future_shares.get(org) or any(released_by_type.values()):
                impacts.append({"organization_id": org,
                                "current_share": future_shares.get(org, 0),
                                "shares_after_exit": simulated,
                                "future_obligation_released": released_by_type,
                                "performed_kept": performed_by_type})
        return impacts
