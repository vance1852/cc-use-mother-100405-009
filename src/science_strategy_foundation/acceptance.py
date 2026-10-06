"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .errors import ConflictError
from .joint import JointService
from .service import DomainService
from .storage import Database


def _run_joint(service: DomainService) -> dict[str, object]:
    """执行三区域共建平台的联合投入链并返回核对结果。"""

    joint = JointService(service)
    joint.register_charter_version(request_id="req-charter", actor_id="admin-001",
                                   charter_id="charter-001", version=1,
                                   title="交叉学科平台合作章程",
                                   terms={"exit_share_policy": "lapse", "purpose": "共建交叉学科平台"},
                                   effective_from="2026-01-01")
    service.register_organization(request_id="req-org-2", actor_id="admin-001",
                                  organization_id="org-002", name="区域创新中心二")
    service.register_organization(request_id="req-org-3", actor_id="admin-001",
                                  organization_id="org-003", name="区域创新中心三")
    service.register_actor(request_id="req-op-2", actor_id="admin-001", new_actor_id="operator-002",
                           display_name="中心二联络员", role="operator", organization_id="org-002")
    service.register_actor(request_id="req-op-3", actor_id="admin-001", new_actor_id="operator-003",
                           display_name="中心三联络员", role="operator", organization_id="org-003")
    for member_id, role in (("org-001", "lead"), ("org-002", "participant"), ("org-003", "participant")):
        joint.admit_member(request_id=f"req-member-{member_id}", actor_id="admin-001",
                           member_id=member_id, charter_id="charter-001", role=role,
                           fund_cap=1000, hour_cap=800, slot_cap=500, joined_at="2026-01-15")
    joint.register_resource(request_id="req-res-expert", actor_id="operator-001",
                            resource_id="res-expert-001", kind="talent_hour",
                            unique_key="expert-cert-0001", owner_member_id="org-001",
                            label="首席专家工时", capacity=800)
    joint.register_resource(request_id="req-res-equip", actor_id="operator-002",
                            resource_id="res-equip-001", kind="equipment_slot",
                            unique_key="equip-serial-0001", owner_member_id="org-002",
                            label="精密设备机时", capacity=500)
    joint.register_resource(request_id="req-res-fund", actor_id="operator-003",
                            resource_id="res-fund-001", kind="fund",
                            unique_key="fund-acct-0001", owner_member_id="org-003",
                            label="企业联合资金", capacity=1000,
                            conditions={"allowed_purposes": ["设备购置", "材料费"],
                                        "outcome_conditions": "成果需在出资方所在地落地"})
    duplicate_blocked = False
    try:
        joint.register_resource(request_id="req-res-dup", actor_id="operator-001",
                                resource_id="res-equip-copy", kind="equipment_slot",
                                unique_key="equip-serial-0001", owner_member_id="org-001",
                                label="重复登记设备", capacity=500)
    except ConflictError:
        duplicate_blocked = True
    joint.define_milestone(request_id="req-ms-1", actor_id="admin-001", milestone_id="ms-001",
                           charter_id="charter-001", title="平台一期建成", due_date="2026-12-31",
                           requirements=[{"resource_id": "res-expert-001", "quantity": 400},
                                         {"resource_id": "res-equip-001", "quantity": 200},
                                         {"kind": "fund", "quantity": 600}])
    joint.freeze_priority_rules(request_id="req-rules", actor_id="admin-001", ruleset_id="rules-001",
                                version=1, rules={"order": [
                                    {"key": "milestone_due_date", "direction": "asc"},
                                    {"key": "commitment_created_at", "direction": "asc"}]})
    joint.draft_commitment(request_id="req-c1", actor_id="operator-001", commitment_id="c-001",
                           member_id="org-001", milestone_id="ms-001",
                           lines=[{"resource_id": "res-expert-001", "quantity": 400}])
    joint.draft_commitment(request_id="req-c2", actor_id="operator-002", commitment_id="c-002",
                           member_id="org-002", milestone_id="ms-001",
                           lines=[{"resource_id": "res-equip-001", "quantity": 200}])
    joint.draft_commitment(request_id="req-c3", actor_id="operator-003", commitment_id="c-003",
                           member_id="org-003", milestone_id="ms-001",
                           lines=[{"resource_id": "res-fund-001", "quantity": 600, "purpose": "设备购置"}])
    signers = (("org-001", "operator-001"), ("org-002", "operator-002"), ("org-003", "operator-003"))
    for commitment_id in ("c-001", "c-002", "c-003"):
        for member_id, operator in signers:
            joint.countersign_commitment(request_id=f"req-sign-{commitment_id}-{member_id}",
                                         actor_id=operator, commitment_id=commitment_id,
                                         signer_member_id=member_id)
    joint.draft_commitment(request_id="req-c4", actor_id="operator-002", commitment_id="c-004",
                           member_id="org-002", milestone_id="ms-001",
                           lines=[{"resource_id": "res-expert-001", "quantity": 500}])
    overflow_blocked = False
    try:
        for member_id, operator in signers:
            joint.countersign_commitment(request_id=f"req-sign-c-004-{member_id}",
                                         actor_id=operator, commitment_id="c-004",
                                         signer_member_id=member_id)
    except ConflictError:
        overflow_blocked = True
    joint.record_fulfillment(request_id="req-f1", actor_id="operator-001", fulfillment_id="f-001",
                             commitment_id="c-001", resource_id="res-expert-001",
                             quantity=150, occurred_on="2026-09-20")
    joint.record_disruption(request_id="req-d1", actor_id="operator-002", disruption_id="d-001",
                            kind="equipment_downtime", resource_id="res-equip-001",
                            capacity_delta=-100, effective_from="2026-10-01",
                            detail={"reason": "设备计划检修"})
    joint.distribute_outcome(request_id="req-dist", actor_id="admin-001", distribution_id="dist-001",
                             milestone_id="ms-001",
                             allocations=[{"member_id": "org-001", "share": 0.5},
                                          {"member_id": "org-002", "share": 0.2},
                                          {"member_id": "org-003", "share": 0.3}],
                             decided_on="2026-09-25")
    joint.record_disruption(request_id="req-exit", actor_id="admin-001", disruption_id="d-002",
                            kind="member_exit", member_id="org-002",
                            effective_from="2026-11-01", detail={"reason": "成员退出"})
    readiness_now = joint.milestone_readiness("ms-001")
    readiness_future = joint.milestone_readiness("ms-001", as_of="2026-11-15")
    exit_info = joint.exit_impact("org-002")
    charter = joint.charter_at("charter-001", "2026-06-01")
    commitments_now = joint.commitments_at("2026-09-25")
    expert_capacity = joint.resource_capacity("res-expert-001")
    return {"status": "ok", "duplicate_blocked": duplicate_blocked,
            "overflow_blocked": overflow_blocked,
            "ready_now": readiness_now["ready"],
            "ready_after_exit": readiness_future["ready"],
            "exit_released": len(exit_info["released_future_obligations"]),
            "active_commitments": len(commitments_now),
            "charter_version": charter["version"],
            "expert_available": expert_capacity["available"]}


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
        joint = _run_joint(service)
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed, "joint": joint}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    joint = result["joint"]
    ok = (result["status"] == "ok" and result["audit_valid"]
          and joint["duplicate_blocked"] and joint["overflow_blocked"]
          and joint["ready_now"] and not joint["ready_after_exit"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
