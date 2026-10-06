"""区域创新联合投入与履约服务的规则测试。"""

import unittest
from datetime import datetime, timezone

from science_strategy_foundation.clock import FixedClock
from science_strategy_foundation.errors import ConflictError, PermissionDenied, ValidationError
from science_strategy_foundation.jv_service import JointVentureService
from science_strategy_foundation.service import DomainService
from science_strategy_foundation.storage import Database


class JointVentureTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
        self.base = DomainService(self.database, self.clock)
        self.jv = JointVentureService(self.database, self.clock)
        self._bootstrap()
        self._registry()

    def tearDown(self):
        self.database.close()

    def _bootstrap(self):
        for i, (oid, name) in enumerate([("o-a", "甲区"), ("o-b", "乙区"), ("o-c", "丙区")], start=1):
            self.base.register_organization(request_id=f"org-{i}", actor_id="bootstrap",
                                            organization_id=oid, name=name)
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="adm",
                                 display_name="管理员", role="admin", organization_id="o-a")
        for i, oid in enumerate(["o-a", "o-b", "o-c"], start=1):
            self.base.register_actor(request_id=f"op-{i}", actor_id="adm", new_actor_id=f"op{i}",
                                     display_name=f"操作员{i}", role="operator", organization_id=oid)

    def _registry(self):
        self.jv.create_platform(request_id="plat", actor_id="adm", platform_id="p", name="平台")
        self.jv.publish_charter(request_id="ch1", actor_id="adm", platform_id="p",
                                priority_rules={"type": "plan_rank"},
                                caps={"person_hours_monthly": 160, "equipment_hours_monthly": 720,
                                      "funding_total": 90_000_000},
                                outcome_redistribution="pro_rata", effective_from="2026-10-01")
        for i, (oid, role) in enumerate([("o-a", "lead"), ("o-b", "partner"), ("o-c", "partner")]):
            self.jv.register_membership(request_id=f"mem-{i}", actor_id="adm", platform_id="p",
                                        organization_id=oid, member_role=role, member_order=i,
                                        joined_on="2026-10-01", local_conditions={"city": oid})
        self.jv.register_person(request_id="pa", actor_id="op1", platform_id="p", organization_id="o-a",
                                person_code="chief", display_name="首席", title="首席",
                                capacity_hours_monthly=160)
        self.jv.register_person(request_id="pb", actor_id="op2", platform_id="p", organization_id="o-b",
                                person_code="expert", display_name="专家", title="专家",
                                capacity_hours_monthly=160)
        self.jv.register_equipment(request_id="ea", actor_id="op1", platform_id="p", organization_id="o-a",
                                   equipment_code="tem", display_name="电镜", monthly_capacity_hours=720)
        self.jv.register_fund_source(request_id="fa", actor_id="op1", platform_id="p", organization_id="o-a",
                                     source_code="cash", display_name="企业资金", total_amount=50_000_000,
                                     currency="CNY", restrictions={"landing": "甲区"})
        self.jv.create_plan(request_id="p1", actor_id="adm", platform_id="p", plan_id="plan-1",
                            name="计划一", priority_rank=1, lead_organization_id="o-a")
        self.jv.create_plan(request_id="p2", actor_id="adm", platform_id="p", plan_id="plan-2",
                            name="计划二", priority_rank=2, lead_organization_id="o-b")
        self.jv.create_milestone(request_id="m1", actor_id="op1", plan_id="plan-1", milestone_code="m1",
                                 name="里程碑一", due_date="2026-12-31",
                                 requirements=[{"resource_type": "person_hours", "qty": 160}])
        self.jv.create_milestone(request_id="m2", actor_id="op2", plan_id="plan-2", milestone_code="m2",
                                 name="里程碑二", due_date="2026-12-31",
                                 requirements=[{"resource_type": "person_hours", "qty": 90}])
        self.person_a = self.database.connection.execute(
            "SELECT person_id FROM jv_persons WHERE person_code='chief'").fetchone()["person_id"]
        self.person_b = self.database.connection.execute(
            "SELECT person_id FROM jv_persons WHERE person_code='expert'").fetchone()["person_id"]
        self.equip_a = self.database.connection.execute(
            "SELECT equipment_id FROM jv_equipment WHERE equipment_code='tem'").fetchone()["equipment_id"]
        self.fund_a = self.database.connection.execute(
            "SELECT source_id FROM jv_fund_sources WHERE source_code='cash'").fetchone()["source_id"]
        self.ms1 = self.database.connection.execute(
            "SELECT milestone_id FROM jv_milestones WHERE milestone_code='m1'").fetchone()["milestone_id"]
        self.ms2 = self.database.connection.execute(
            "SELECT milestone_id FROM jv_milestones WHERE milestone_code='m2'").fetchone()["milestone_id"]

    def _commit(self, request_id, actor, plan, milestone, org, rtype, rid, qty, period="2026-10"):
        receipt = self.jv.register_commitment(
            request_id=request_id, actor_id=actor, platform_id="p", plan_id=plan,
            milestone_id=milestone, organization_id=org, resource_type=rtype,
            resource_id=rid, qty=qty, period_key=period)
        cid = receipt.resource_id
        self.jv.submit_commitment(actor_id=actor, commitment_id=cid)
        return cid

    # ---------------------------------------------------------------- 规则

    def test_draft_and_pending_commitments_occupy_nothing(self):
        cid = self._commit("c-draft", "op1", "plan-1", self.ms1, "o-a",
                           "person_hours", self.person_a, 100)
        # 提交会签但未签署：净占用仍为零
        detail = self.jv.get_commitment(cid)
        self.assertEqual("awaiting_signoff", detail["status_as_of"])
        self.assertEqual(0, detail["net_granted"])
        readiness = self.jv.get_milestone_readiness(self.ms1)
        self.assertEqual(0, readiness["coverage"][0]["allocated"])

    def test_duplicate_expert_counted_twice_adjudicated_by_frozen_priority(self):
        # 甲把首席 100h 计入 plan-1
        cid1 = self._commit("c1", "op1", "plan-1", self.ms1, "o-a",
                            "person_hours", self.person_a, 100)
        self.jv.sign_commitment(actor_id="op1", commitment_id=cid1)
        # 乙把同一位首席 90h 计入 plan-2（重复计入）
        cid2 = self._commit("c2", "op2", "plan-2", self.ms2, "o-b",
                            "person_hours", self.person_a, 90)
        result = self.jv.sign_commitment(actor_id="op2", commitment_id=cid2)
        nets = {x["plan_id"]: x["net_granted"] for x in result["resolution"]}
        self.assertEqual(100, nets["plan-1"])
        self.assertEqual(60, nets["plan-2"])
        # 两条承诺冻结到同一章程版本，优先规则不可被后续章程改写
        self.assertEqual(self.jv.get_commitment(cid1)["charter_id"],
                         self.jv.get_commitment(cid2)["charter_id"])

    def test_splitting_commitment_cannot_evade_org_cap(self):
        # 甲把首席容量拆成两条：160 + 30，合计 190 > 机构月上限 160 / 资源容量 160
        cid1 = self._commit("s1", "op1", "plan-1", self.ms1, "o-a",
                            "person_hours", self.person_a, 160)
        self.jv.sign_commitment(actor_id="op1", commitment_id=cid1)
        cid2 = self._commit("s2", "op1", "plan-2", self.ms2, "o-a",
                            "person_hours", self.person_a, 30)
        # 第二条承诺方与 plan-2 牵头方不同，需要双方会签
        self.jv.sign_commitment(actor_id="op1", commitment_id=cid2)
        result = self.jv.sign_commitment(actor_id="op2", commitment_id=cid2)
        nets = {x["commitment_id"]: x["net_granted"] for x in result["resolution"]}
        self.assertEqual(160, nets[cid1])
        self.assertEqual(0, nets[cid2])  # 拆出的第二条被上限完全阻塞

    def test_equipment_downtime_prorates_capacity(self):
        cid1 = self._commit("e1", "op1", "plan-1", self.ms1, "o-a",
                            "equipment", self.equip_a, 500)
        self.jv.sign_commitment(actor_id="op1", commitment_id=cid1)
        cid2 = self._commit("e2", "op2", "plan-2", self.ms2, "o-b",
                            "equipment", self.equip_a, 400)
        self.jv.sign_commitment(actor_id="op2", commitment_id=cid2)
        self.assertEqual(220, self.jv.get_commitment(cid2)["net_granted"])
        self.clock.advance(days=15)
        down = self.jv.record_equipment_downtime(
            request_id="d1", actor_id="adm", equipment_id=self.equip_a,
            effective_start="2026-10-23").response
        nets = {x["commitment_id"]: x["net_granted"] for x in down["resolution"]}
        # 停机 9 天折算后容量约 511：高优先级 plan-1 保住 500，plan-2 仅剩个位数
        self.assertEqual(500, nets[cid1])
        self.assertLess(nets[cid2], 220)
        # 设备当日即修复，停机区间结案为空 → 当月容量恢复并回填
        recovered = self.jv.record_equipment_recovered(
            request_id="d2", actor_id="adm", equipment_id=self.equip_a,
            effective_on="2026-10-23").response
        nets2 = {x["commitment_id"]: x["net_granted"] for x in recovered["resolution"]}
        self.assertEqual(220, nets2[cid2])

    def test_departure_keeps_performed_and_releases_only_future(self):
        cid = self._commit("dep1", "op1", "plan-1", self.ms1, "o-a",
                           "person_hours", self.person_a, 100)
        self.jv.sign_commitment(actor_id="op1", commitment_id=cid)
        self.clock.advance(days=4)
        self.jv.record_performance(request_id="pf", actor_id="op1", commitment_id=cid,
                                   qty=40, occurred_on="2026-10-10")
        self.clock.advance(days=10)
        self.jv.record_person_departed(request_id="depart", actor_id="adm",
                                       person_id=self.person_a)
        detail = self.jv.get_commitment(cid)
        self.assertEqual(40, detail["performed"])
        self.assertEqual(40, detail["net_granted"])
        self.assertEqual("released", detail["status_as_of"])
        # 历史日期复原：10-09 时承诺仍是在保 100
        past = self.jv.get_commitment(cid, as_of="2026-10-09")
        self.assertEqual(100, past["net_granted"])
        self.assertEqual("active", past["status_as_of"])

    def test_performance_cannot_exceed_net_granted(self):
        cid = self._commit("cap1", "op1", "plan-1", self.ms1, "o-a",
                           "person_hours", self.person_a, 100)
        self.jv.sign_commitment(actor_id="op1", commitment_id=cid)
        with self.assertRaises(ValidationError):
            self.jv.record_performance(request_id="over", actor_id="op1", commitment_id=cid,
                                       qty=101, occurred_on="2026-10-10")

    def test_fund_delay_only_recomputes_future_and_received_is_immutable(self):
        cid = self.jv.register_commitment(
            request_id="fc", actor_id="op1", platform_id="p", plan_id="plan-1",
            milestone_id=self.ms1, organization_id="o-a", resource_type="funding",
            resource_id=self.fund_a, qty=20_000_000, period_key="2026-10",
            tranches=[{"tranch_no": 1, "amount": 12_000_000, "due_date": "2026-10-15"},
                      {"tranch_no": 2, "amount": 8_000_000, "due_date": "2026-11-15"}]).resource_id
        self.jv.submit_commitment(actor_id="op1", commitment_id=cid)
        self.jv.sign_commitment(actor_id="op1", commitment_id=cid)
        # 10 月只有 1200 万可占用
        self.assertEqual(12_000_000, self.jv.get_commitment(cid)["net_granted"])
        self.clock.advance(days=9)
        self.jv.receive_fund_tranch(request_id="recv", actor_id="op1", commitment_id=cid,
                                    tranch_no=1, received_on="2026-10-15")
        self.clock.advance(days=1)
        # 已到账分期不能再改期
        with self.assertRaises(ConflictError):
            self.jv.delay_fund_tranch(request_id="delay1", actor_id="op1", commitment_id=cid,
                                      tranch_no=1, new_due_date="2026-12-01")

    def test_member_exit_only_touches_future_and_finalized_outcome_is_locked(self):
        cid = self._commit("x1", "op1", "plan-1", self.ms1, "o-a",
                           "person_hours", self.person_a, 100)
        self.jv.sign_commitment(actor_id="op1", commitment_id=cid)
        self.jv.set_outcome_policy(request_id="pol", actor_id="adm", plan_id="plan-1",
                                   shares={"o-a": 60, "o-b": 30, "o-c": 10},
                                   effective_from="2026-10-01")
        # 乙 10-20 退出
        self.clock.advance(days=14)
        self.jv.record_member_exit(request_id="exit", actor_id="adm", platform_id="p",
                                   organization_id="o-b", effective_on="2026-10-20")
        self.clock.advance(days=50)
        self.jv.complete_milestone(request_id="done", actor_id="adm", milestone_id=self.ms1)
        award = self.jv.finalize_outcome(request_id="aw", actor_id="adm",
                                         milestone_id=self.ms1).response
        # 乙 30% 按 60:10 比例用最大余数法分给甲、丙
        self.assertEqual({"o-a": 86, "o-c": 14}, award["shares"])
        # 甲已履约/固化结果不受乙退出影响
        self.assertEqual(100, self.jv.get_commitment(cid)["net_granted"])
        # 固化不可回写
        with self.assertRaises(ConflictError):
            self.jv.finalize_outcome(request_id="aw2", actor_id="adm", milestone_id=self.ms1)

    def test_historical_snapshot_restores_charter_and_membership(self):
        before = self.jv.get_platform_snapshot("p", as_of="2026-09-30")
        self.assertIsNone(before["charter"])
        at = self.jv.get_platform_snapshot("p", as_of="2026-10-02")
        self.assertEqual(1, at["charter"]["version_no"])
        self.assertEqual(3, len(at["memberships"]))
        # 发布新章程后，旧日期仍复原 v1
        self.clock.advance(days=25)
        self.jv.publish_charter(request_id="ch2", actor_id="adm", platform_id="p",
                                priority_rules={"type": "plan_rank"},
                                caps={"person_hours_monthly": 120},
                                outcome_redistribution="lead", effective_from="2026-11-01")
        old = self.jv.get_charter_at("p", as_of="2026-10-15")["charter"]
        self.assertEqual(1, old["version_no"])
        self.assertEqual(160, old["caps"]["person_hours_monthly"])

    def test_new_charter_cannot_cut_into_signed_priority_disputes(self):
        # 同一资源冲突会签后，新章程调小上限也不能把高优先级承诺压到已履约以下
        cid1 = self._commit("f1", "op1", "plan-1", self.ms1, "o-a",
                            "person_hours", self.person_a, 100)
        self.jv.sign_commitment(actor_id="op1", commitment_id=cid1)
        cid2 = self._commit("f2", "op2", "plan-2", self.ms2, "o-b",
                            "person_hours", self.person_a, 90)
        self.jv.sign_commitment(actor_id="op2", commitment_id=cid2)
        self.clock.advance(days=4)
        self.jv.record_performance(request_id="pfx", actor_id="op1", commitment_id=cid1,
                                   qty=100, occurred_on="2026-10-10")
        self.clock.advance(days=25)
        self.jv.publish_charter(request_id="chx", actor_id="adm", platform_id="p",
                                priority_rules={"type": "plan_rank"},
                                caps={"person_hours_monthly": 80},
                                outcome_redistribution="pro_rata", effective_from="2026-11-01")
        # 11 月重新裁定不影响 10 月已履约承诺
        self.assertEqual(100, self.jv.get_commitment(cid1, as_of="2026-11-02")["performed"])

    def test_auditor_cannot_register_platform(self):
        self.base.register_actor(request_id="au", actor_id="adm", new_actor_id="aud",
                                 display_name="审计员", role="auditor", organization_id="o-a")
        with self.assertRaises(PermissionDenied):
            self.jv.create_platform(request_id="x", actor_id="aud", platform_id="p2", name="X")


if __name__ == "__main__":
    unittest.main()
