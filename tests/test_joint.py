import unittest
from datetime import datetime, timezone

from science_strategy_foundation.clock import FixedClock
from science_strategy_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from science_strategy_foundation.joint import JointService
from science_strategy_foundation.service import DomainService
from science_strategy_foundation.storage import Database


class JointServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.foundation = DomainService(
            self.database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        self.joint = JointService(self.foundation)
        self.foundation.register_organization(request_id="org1", actor_id="bootstrap",
                                              organization_id="o1", name="区域中心一")
        self.foundation.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                       display_name="秘书处管理员", role="admin", organization_id="o1")
        for org, name in (("o2", "区域中心二"), ("o3", "区域中心三")):
            self.foundation.register_organization(request_id=f"org-{org}", actor_id="a1",
                                                  organization_id=org, name=name)
        for actor, org in (("op1", "o1"), ("op2", "o2"), ("op3", "o3")):
            self.foundation.register_actor(request_id=f"actor-{actor}", actor_id="a1",
                                           new_actor_id=actor, display_name=f"联络员{actor}",
                                           role="operator", organization_id=org)
        self.joint.register_charter_version(request_id="charter-1", actor_id="a1", charter_id="ch1",
                                            version=1, title="联合投入章程",
                                            terms={"exit_share_policy": "lapse"},
                                            effective_from="2026-01-01")
        for member, role in (("o1", "lead"), ("o2", "participant"), ("o3", "participant")):
            self.joint.admit_member(request_id=f"member-{member}", actor_id="a1", member_id=member,
                                    charter_id="ch1", role=role, fund_cap=1000, hour_cap=500,
                                    slot_cap=300, joined_at="2026-01-15")
        self.joint.register_resource(request_id="res-expert", actor_id="op1", resource_id="expert1",
                                     kind="talent_hour", unique_key="cert-001", owner_member_id="o1",
                                     label="首席专家", capacity=500)
        self.joint.register_resource(request_id="res-equip", actor_id="op2", resource_id="equip1",
                                     kind="equipment_slot", unique_key="equip-001", owner_member_id="o2",
                                     label="精密设备", capacity=300)
        self.joint.register_resource(request_id="res-fund", actor_id="op3", resource_id="fund1",
                                     kind="fund", unique_key="acct-001", owner_member_id="o3",
                                     label="企业资金", capacity=1000,
                                     conditions={"allowed_purposes": ["设备购置"]})
        self.joint.define_milestone(request_id="ms1", actor_id="a1", milestone_id="m1",
                                    charter_id="ch1", title="一期里程碑", due_date="2026-12-31",
                                    requirements=[{"resource_id": "expert1", "quantity": 200},
                                                  {"resource_id": "equip1", "quantity": 100},
                                                  {"kind": "fund", "quantity": 400}])
        self.joint.freeze_priority_rules(request_id="rules1", actor_id="a1", ruleset_id="pr1",
                                         version=1, rules={"order": [
                                             {"key": "milestone_due_date", "direction": "asc"},
                                             {"key": "commitment_created_at", "direction": "asc"}]})

    def tearDown(self):
        self.database.close()

    def _commit(self, request_id, commitment_id, member, lines, milestone="m1"):
        actor = {"o1": "op1", "o2": "op2", "o3": "op3"}[member]
        self.joint.draft_commitment(request_id=f"{request_id}-draft", actor_id=actor,
                                    commitment_id=commitment_id, member_id=member,
                                    milestone_id=milestone, lines=lines)
        for signer, operator in (("o1", "op1"), ("o2", "op2"), ("o3", "op3")):
            self.joint.countersign_commitment(request_id=f"{request_id}-sign-{signer}",
                                              actor_id=operator, commitment_id=commitment_id,
                                              signer_member_id=signer)

    def test_duplicate_resource_registration_rejected(self):
        with self.assertRaises(ConflictError):
            self.joint.register_resource(request_id="dup", actor_id="op2",
                                         resource_id="expert-copy", kind="talent_hour",
                                         unique_key="cert-001", owner_member_id="o2",
                                         label="重复专家", capacity=500)

    def test_draft_commitment_does_not_occupy_quota(self):
        self.joint.draft_commitment(request_id="d1", actor_id="op1", commitment_id="c1",
                                    member_id="o1", milestone_id="m1",
                                    lines=[{"resource_id": "expert1", "quantity": 200}])
        capacity = self.joint.resource_capacity("expert1")
        self.assertEqual(0, capacity["allocated"])
        self.assertEqual(500, capacity["available"])
        self.assertEqual([], self.joint.commitments_at("2026-09-25"))

    def test_full_countersign_activates_and_occupies_quota(self):
        self.joint.draft_commitment(request_id="d1", actor_id="op1", commitment_id="c1",
                                    member_id="o1", milestone_id="m1",
                                    lines=[{"resource_id": "expert1", "quantity": 200}])
        self.joint.countersign_commitment(request_id="s1", actor_id="op1",
                                          commitment_id="c1", signer_member_id="o1")
        self.joint.countersign_commitment(request_id="s2", actor_id="op2",
                                          commitment_id="c1", signer_member_id="o2")
        self.assertEqual([], self.joint.commitments_at("2026-09-25"))
        self.assertEqual(0, self.joint.resource_capacity("expert1")["allocated"])
        last = self.joint.countersign_commitment(request_id="s3", actor_id="op3",
                                                 commitment_id="c1", signer_member_id="o3")
        self.assertFalse(last.replayed)
        self.assertEqual(200, self.joint.resource_capacity("expert1")["allocated"])
        self.assertEqual(1, len(self.joint.commitments_at("2026-09-25")))

    def test_member_cap_aggregates_split_commitments(self):
        self.joint.register_resource(request_id="res-expert2", actor_id="op1",
                                     resource_id="expert2", kind="talent_hour",
                                     unique_key="cert-002", owner_member_id="o1",
                                     label="专家二", capacity=1000)
        self._commit("c1", "c1", "o1", [{"resource_id": "expert2", "quantity": 300}])
        self.joint.draft_commitment(request_id="c2-draft", actor_id="op1", commitment_id="c2",
                                    member_id="o1", milestone_id="m1",
                                    lines=[{"resource_id": "expert2", "quantity": 300}])
        self.joint.countersign_commitment(request_id="c2-s1", actor_id="op1",
                                          commitment_id="c2", signer_member_id="o1")
        self.joint.countersign_commitment(request_id="c2-s2", actor_id="op2",
                                          commitment_id="c2", signer_member_id="o2")
        with self.assertRaises(ConflictError) as ctx:
            self.joint.countersign_commitment(request_id="c2-s3", actor_id="op3",
                                              commitment_id="c2", signer_member_id="o3")
        self.assertIn("上限", str(ctx.exception))
        self.assertEqual(300, self.joint.resource_capacity("expert2")["allocated"])

    def test_capacity_conflict_is_ruled_by_frozen_priority(self):
        self._commit("c1", "c1", "o1", [{"resource_id": "expert1", "quantity": 400}])
        self.joint.draft_commitment(request_id="c2-draft", actor_id="op2", commitment_id="c2",
                                    member_id="o2", milestone_id="m1",
                                    lines=[{"resource_id": "expert1", "quantity": 200}])
        self.joint.countersign_commitment(request_id="c2-s1", actor_id="op1",
                                          commitment_id="c2", signer_member_id="o1")
        self.joint.countersign_commitment(request_id="c2-s2", actor_id="op2",
                                          commitment_id="c2", signer_member_id="o2")
        with self.assertRaises(ConflictError) as ctx:
            self.joint.countersign_commitment(request_id="c2-s3", actor_id="op3",
                                              commitment_id="c2", signer_member_id="o3")
        self.assertIn("pr1", str(ctx.exception))
        self.assertEqual(400, self.joint.resource_capacity("expert1")["allocated"])

    def test_equipment_downtime_only_changes_future_capacity(self):
        self._commit("c1", "c1", "o2", [{"resource_id": "equip1", "quantity": 100}])
        self.joint.record_fulfillment(request_id="f1", actor_id="op2", fulfillment_id="f1",
                                      commitment_id="c1", resource_id="equip1",
                                      quantity=60, occurred_on="2026-09-20")
        self.joint.record_disruption(request_id="d1", actor_id="op2", disruption_id="d1",
                                     kind="equipment_downtime", resource_id="equip1",
                                     capacity_delta=-120, effective_from="2026-10-01",
                                     detail={"reason": "设备检修"})
        before = self.joint.resource_capacity("equip1", as_of="2026-09-30")
        after = self.joint.resource_capacity("equip1", as_of="2026-10-02")
        self.assertEqual(300, before["effective_capacity"])
        self.assertEqual(180, after["effective_capacity"])
        self.assertEqual(100, after["allocated"])
        position = self.joint.member_position("o2")
        self.assertEqual(60, position["fulfilled"]["equipment_slot"])

    def test_disruption_rejects_past_effective_date(self):
        with self.assertRaises(ValidationError):
            self.joint.record_disruption(request_id="d1", actor_id="op2", disruption_id="d1",
                                         kind="equipment_downtime", resource_id="equip1",
                                         capacity_delta=-50, effective_from="2026-09-01",
                                         detail={})

    def test_partial_fulfillment_close_releases_future_obligation(self):
        self._commit("c1", "c1", "o1", [{"resource_id": "expert1", "quantity": 200}])
        self.joint.record_fulfillment(request_id="f1", actor_id="op1", fulfillment_id="f1",
                                      commitment_id="c1", resource_id="expert1",
                                      quantity=80, occurred_on="2026-09-20")
        self.joint.close_commitment(request_id="close1", actor_id="op1", commitment_id="c1",
                                    reason="partial_fulfillment", effective_from="2026-09-26")
        self.assertEqual(1, len(self.joint.commitments_at("2026-09-25")))
        self.assertEqual([], self.joint.commitments_at("2026-09-26"))
        readiness = self.joint.milestone_readiness("m1", as_of="2026-09-26")
        item = next(entry for entry in readiness["items"] if entry.get("resource_id") == "expert1")
        self.assertEqual(0, item["committed"])
        self.assertEqual(80, item["fulfilled"])
        self.assertEqual(200, item["gap"])

    def test_member_exit_recalculates_future_only(self):
        self._commit("c1", "c1", "o2", [{"resource_id": "equip1", "quantity": 100}])
        self._commit("c2", "c2", "o3", [{"resource_id": "fund1", "quantity": 400,
                                         "purpose": "设备购置"}])
        self.joint.distribute_outcome(request_id="dist1", actor_id="a1", distribution_id="dist1",
                                      milestone_id="m1",
                                      allocations=[{"member_id": "o1", "share": 0.4},
                                                   {"member_id": "o2", "share": 0.3},
                                                   {"member_id": "o3", "share": 0.3}],
                                      decided_on="2026-09-24")
        self.joint.record_disruption(request_id="exit1", actor_id="a1", disruption_id="exit1",
                                     kind="member_exit", member_id="o2",
                                     effective_from="2026-10-01", detail={"reason": "成员退出"})
        self.assertEqual(2, len(self.joint.commitments_at("2026-09-30")))
        remaining = self.joint.commitments_at("2026-10-02")
        self.assertEqual(["c2"], [row["commitment_id"] for row in remaining])
        impact = self.joint.exit_impact("o2")
        self.assertTrue(impact["exited"])
        self.assertEqual([{"commitment_id": "c1", "milestone_id": "m1",
                           "resource_id": "equip1", "quantity": 100}],
                         impact["released_future_obligations"])
        self.assertEqual(1, len(impact["completed_distributions_unchanged"]))
        self.assertEqual(0.3, impact["completed_distributions_unchanged"][0]["share"])
        gaps = impact["affected_milestones"][0]["gaps"]
        self.assertEqual(100, gaps[0]["gap_increase"])
        self.assertEqual("exited", self.joint.member_position("o2")["status"])

    def test_exited_member_cannot_gain_new_shares(self):
        self.joint.record_disruption(request_id="exit1", actor_id="a1", disruption_id="exit1",
                                     kind="member_exit", member_id="o2",
                                     effective_from="2026-09-25", detail={})
        self.joint.define_milestone(request_id="ms2", actor_id="a1", milestone_id="m2",
                                    charter_id="ch1", title="二期里程碑", due_date="2027-06-30",
                                    requirements=[{"kind": "fund", "quantity": 100}])
        with self.assertRaises(ConflictError):
            self.joint.distribute_outcome(request_id="dist2", actor_id="a1",
                                          distribution_id="dist2", milestone_id="m2",
                                          allocations=[{"member_id": "o1", "share": 0.5},
                                                       {"member_id": "o2", "share": 0.5}],
                                          decided_on="2026-09-25")

    def test_charter_versions_recalled_by_date(self):
        self.joint.register_charter_version(request_id="charter-2", actor_id="a1",
                                            charter_id="ch1", version=2, title="修订章程",
                                            terms={"exit_share_policy": "redistribute"},
                                            effective_from="2026-07-01")
        self.assertEqual(1, self.joint.charter_at("ch1", "2026-03-01")["version"])
        self.assertEqual(2, self.joint.charter_at("ch1", "2026-09-25")["version"])
        self.assertEqual("redistribute",
                         self.joint.charter_at("ch1", "2026-09-25")["terms"]["exit_share_policy"])
        with self.assertRaises(NotFoundError):
            self.joint.charter_at("ch1", "2025-12-31")
        with self.assertRaises(ConflictError):
            self.joint.register_charter_version(request_id="charter-1b", actor_id="a1",
                                                charter_id="ch1", version=1, title="重复版本",
                                                terms={"x": 1}, effective_from="2026-08-01")

    def test_commitment_replay_is_idempotent(self):
        first = self.joint.draft_commitment(request_id="c1", actor_id="op1", commitment_id="c1",
                                            member_id="o1", milestone_id="m1",
                                            lines=[{"resource_id": "expert1", "quantity": 100}])
        second = self.joint.draft_commitment(request_id="c1", actor_id="op1", commitment_id="c1",
                                             member_id="o1", milestone_id="m1",
                                             lines=[{"resource_id": "expert1", "quantity": 100}])
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        count = self.database.connection.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        self.assertEqual(1, count)

    def test_operator_cannot_sign_for_other_member(self):
        self.joint.draft_commitment(request_id="c1", actor_id="op1", commitment_id="c1",
                                    member_id="o1", milestone_id="m1",
                                    lines=[{"resource_id": "expert1", "quantity": 100}])
        with self.assertRaises(PermissionDenied):
            self.joint.countersign_commitment(request_id="s1", actor_id="op1",
                                              commitment_id="c1", signer_member_id="o2")

    def test_readiness_reports_gap_and_headroom(self):
        readiness = self.joint.milestone_readiness("m1")
        self.assertFalse(readiness["ready"])
        gaps = {item["kind"]: item["gap"] for item in readiness["items"]}
        self.assertEqual(200, gaps["talent_hour"])
        self.assertEqual(100, gaps["equipment_slot"])
        self.assertEqual(400, gaps["fund"])
        suggest = {(entry["member_id"], entry["kind"]): entry["headroom"]
                   for entry in readiness["suggestions"]}
        self.assertEqual(500, suggest[("o1", "talent_hour")])
        self._commit("c1", "c1", "o1", [{"resource_id": "expert1", "quantity": 200}])
        self._commit("c2", "c2", "o2", [{"resource_id": "equip1", "quantity": 100}])
        self._commit("c3", "c3", "o3", [{"resource_id": "fund1", "quantity": 400,
                                         "purpose": "设备购置"}])
        self.assertTrue(self.joint.milestone_readiness("m1")["ready"])

    def test_exit_impact_simulation_lists_released_obligations(self):
        self._commit("c1", "c1", "o2", [{"resource_id": "equip1", "quantity": 100}])
        impact = self.joint.exit_impact("o2")
        self.assertFalse(impact["exited"])
        self.assertEqual(100, impact["released_future_obligations"][0]["quantity"])
        self.assertEqual("lapse", impact["future_share_policy"])
        self.assertEqual(100, impact["affected_milestones"][0]["gaps"][0]["gap_increase"])

    def test_completed_distribution_cannot_be_rewritten(self):
        self.joint.distribute_outcome(request_id="dist1", actor_id="a1", distribution_id="dist1",
                                      milestone_id="m1",
                                      allocations=[{"member_id": "o1", "share": 0.5},
                                                   {"member_id": "o2", "share": 0.3},
                                                   {"member_id": "o3", "share": 0.2}],
                                      decided_on="2026-09-24")
        with self.assertRaises(ConflictError):
            self.joint.distribute_outcome(request_id="dist2", actor_id="a1",
                                          distribution_id="dist2", milestone_id="m1",
                                          allocations=[{"member_id": "o1", "share": 1.0}],
                                          decided_on="2026-09-25")

    def test_fulfillment_cannot_exceed_commitment(self):
        self._commit("c1", "c1", "o1", [{"resource_id": "expert1", "quantity": 200}])
        self.joint.record_fulfillment(request_id="f1", actor_id="op1", fulfillment_id="f1",
                                      commitment_id="c1", resource_id="expert1",
                                      quantity=150, occurred_on="2026-09-20")
        with self.assertRaises(ConflictError):
            self.joint.record_fulfillment(request_id="f2", actor_id="op1", fulfillment_id="f2",
                                          commitment_id="c1", resource_id="expert1",
                                          quantity=60, occurred_on="2026-09-21")

    def test_fund_purpose_must_match_conditions(self):
        with self.assertRaises(ValidationError):
            self.joint.draft_commitment(request_id="c9", actor_id="op3", commitment_id="c9",
                                        member_id="o3", milestone_id="m1",
                                        lines=[{"resource_id": "fund1", "quantity": 100,
                                                "purpose": "人员费"}])

    def test_audit_chain_stays_valid(self):
        self._commit("c1", "c1", "o1", [{"resource_id": "expert1", "quantity": 200}])
        valid, count = self.foundation.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
