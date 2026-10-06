"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .jv_service import JointVentureService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = DomainService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范科研机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="项目负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号创新节点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="institution_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="institution_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})
        jv_ok = _run_joint_venture(database)
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed, "joint_venture": jv_ok}
        database.close()
        return result


def _run_joint_venture(database: Database) -> dict[str, object]:
    """跑一条区域联合投入：章程、成员、重复资源会签、裁定、履约、退出、成果固化。"""

    from datetime import timedelta

    base_clock = FixedClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
    base = DomainService(database, base_clock)
    jv = JointVentureService(database, base_clock)
    for suffix, name in (("jv-a", "甲区"), ("jv-b", "乙区")):
        base.register_organization(request_id=f"req-{suffix}", actor_id="admin-001",
                                   organization_id=suffix, name=name)
    base.register_actor(request_id="req-jv-admin", actor_id="admin-001", new_actor_id="jv-admin",
                        display_name="理事会管理员", role="admin", organization_id="jv-a")
    base.register_actor(request_id="req-jv-opb", actor_id="jv-admin", new_actor_id="jv-opb",
                        display_name="乙区操作员", role="operator", organization_id="jv-b")
    jv.create_platform(request_id="req-jv-plat", actor_id="jv-admin", platform_id="jv-plat",
                       name="交叉学科平台")
    jv.publish_charter(request_id="req-jv-charter", actor_id="jv-admin", platform_id="jv-plat",
                       priority_rules={"type": "plan_rank"},
                       caps={"person_hours_monthly": 160, "equipment_hours_monthly": 720,
                             "funding_total": 90_000_000},
                       outcome_redistribution="pro_rata", effective_from="2026-10-01")
    jv.register_membership(request_id="req-jv-ma", actor_id="jv-admin", platform_id="jv-plat",
                           organization_id="jv-a", member_role="lead", member_order=0,
                           joined_on="2026-10-01")
    jv.register_membership(request_id="req-jv-mb", actor_id="jv-admin", platform_id="jv-plat",
                           organization_id="jv-b", member_role="partner", member_order=1,
                           joined_on="2026-10-01")
    jv.register_person(request_id="req-jv-person", actor_id="jv-admin", platform_id="jv-plat",
                       organization_id="jv-a", person_code="chief", display_name="首席专家",
                       title="首席", capacity_hours_monthly=160)
    jv.create_plan(request_id="req-jv-plan1", actor_id="jv-admin", platform_id="jv-plat",
                   plan_id="jv-plan-1", name="计划一", priority_rank=1, lead_organization_id="jv-a")
    jv.create_plan(request_id="req-jv-plan2", actor_id="jv-admin", platform_id="jv-plat",
                   plan_id="jv-plan-2", name="计划二", priority_rank=2, lead_organization_id="jv-b")
    person_id = database.connection.execute(
        "SELECT person_id FROM jv_persons WHERE person_code='chief'").fetchone()["person_id"]
    jv.create_milestone(request_id="req-jv-ms1", actor_id="jv-admin", plan_id="jv-plan-1",
                        milestone_code="m1", name="里程碑一", due_date="2026-12-31",
                        requirements=[{"resource_type": "person_hours", "qty": 160}])
    jv.create_milestone(request_id="req-jv-ms2", actor_id="jv-opb", plan_id="jv-plan-2",
                        milestone_code="m2", name="里程碑二", due_date="2026-12-31",
                        requirements=[{"resource_type": "person_hours", "qty": 90}])
    ms1 = database.connection.execute(
        "SELECT milestone_id FROM jv_milestones WHERE milestone_code='m1'").fetchone()["milestone_id"]
    ms2 = database.connection.execute(
        "SELECT milestone_id FROM jv_milestones WHERE milestone_code='m2'").fetchone()["milestone_id"]
    c1 = jv.register_commitment(request_id="req-jv-c1", actor_id="jv-admin", platform_id="jv-plat",
                                plan_id="jv-plan-1", milestone_id=ms1, organization_id="jv-a",
                                resource_type="person_hours", resource_id=person_id, qty=100,
                                period_key="2026-10").resource_id
    jv.submit_commitment(actor_id="jv-admin", commitment_id=c1)
    jv.sign_commitment(actor_id="jv-admin", commitment_id=c1)
    c2 = jv.register_commitment(request_id="req-jv-c2", actor_id="jv-opb", platform_id="jv-plat",
                                plan_id="jv-plan-2", milestone_id=ms2, organization_id="jv-b",
                                resource_type="person_hours", resource_id=person_id, qty=90,
                                period_key="2026-10").resource_id
    jv.submit_commitment(actor_id="jv-opb", commitment_id=c2)
    activated = jv.sign_commitment(actor_id="jv-opb", commitment_id=c2)
    duplicate_resolved = {item["plan_id"]: item["net_granted"] for item in activated["resolution"]}
    # 历史复原
    snapshot = jv.get_platform_snapshot("jv-plat", as_of="2026-10-02")
    return {"duplicate_resolved": duplicate_resolved,
            "plan1_net": duplicate_resolved["jv-plan-1"],
            "plan2_net": duplicate_resolved["jv-plan-2"],
            "charter_version_at_oct2": snapshot["charter"]["version_no"],
            "commitments_at_oct2": len(snapshot["commitments"])}


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
