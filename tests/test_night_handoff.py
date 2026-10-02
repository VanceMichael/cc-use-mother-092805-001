"""端到端：一个家庭被三支救援队分别送走后的夜班交接全流程。"""

import tempfile
import unittest
from pathlib import Path

from src.shelter import Backend, Scheduler, connect
from tests.support import SimClock


class NightHandoffEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mktemp(suffix=".db"))
        self.clock = SimClock()  # 2026-10-01 12:00 UTC
        self.conn = connect(self.tmp)
        self.b = Backend(self.conn, self.clock)
        self.s = Scheduler(self.b)
        self.op = self.b.register_operator("K1", "หัวหน้าเวร", "coordinator", team="夜班协调")
        self.med = self.b.register_operator("M1", "หมอ", "medical", team="医疗队")
        self.cp = self.b.register_operator("C1", "พี่เลี้ยง", "child_protection", team="儿童保护组")
        self.log = self.b.register_operator("L1", "พัสดุ", "logistics", team="物资组")

    def tearDown(self) -> None:
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.tmp) + suffix).unlink(missing_ok=True)

    def _evacuate(self, code, team, origin, person_ids, shelter_id) -> None:
        bid = self.b.create_evacuation_batch(
            code, origin_location=origin, team=team,
            destination_shelter_id=shelter_id,
        )
        for pid in person_ids:
            self.b.assign_to_batch(bid, pid)
        self.b.depart_batch(bid, departed_at="2026-10-01T06:00:00+00:00")
        self.b.arrive_batch(bid, shelter_id,
                            arrived_at="2026-10-01T09:00:00+00:00",
                            arrived_person_ids=person_ids)

    def test_full_night_handoff(self) -> None:
        b, s = self.b, self.s
        # ---------- 社区、家庭、四个安置点 ----------
        community = b.register_community("สุขสวัสดิ์ ชุมชน", district="ดอนเมือง")
        family = b.create_family(community_id=community)
        sh_a = b.register_shelter("甲校安置点", capacity_total=100)
        sh_b = b.register_shelter("乙寺安置点", capacity_total=100)
        sh_c = b.register_shelter("丙馆安置点", capacity_total=100)
        sh_d = b.register_shelter("丁院医疗点", capacity_total=100, capacity_accessible=10)

        # ---------- 同一家人，三支救援队分别送走 ----------
        mom = b.intake_person(
            received_location="寺庙集结点", received_by_team="救援队一队",
            received_by_operator="一队队员甲", family_id=family,
            full_name="มาลี", provisional=True, display_code="P-MOM",
        )
        grandma = b.intake_person(
            received_location="寺庙集结点", received_by_team="救援队一队",
            received_by_operator="一队队员甲", family_id=family,
            full_name="สมศรี", provisional=True, display_code="P-GMA",
            is_elder=True, age_group="elder",
        )
        dad = b.intake_person(
            received_location="自家屋顶", received_by_team="救援队二队",
            received_by_operator="二队队员乙", family_id=family,
            full_name="วิชัย", provisional=True, display_code="P-DAD",
        )
        child = b.intake_person(
            received_location="村口大树", received_by_team="救援队三队",
            received_by_operator="三队队员丙",
            display_code="TMP-CHILD", is_child=True, age_group="child",
        )
        b.add_relationship(child, mom, "mother", source="邻里指认",
                           created_by=self.cp)
        b.add_relationship(mom, child, "child", source="母亲陈述",
                           created_by=self.cp)

        self._evacuate("BAT-01", "救援队一队", "寺庙集结点", [mom, grandma], sh_a)
        self._evacuate("BAT-02", "救援队二队", "自家屋顶", [dad], sh_b)
        self._evacuate("BAT-03", "救援队三队", "村口大树", [child], sh_c)

        # ---------- 通信恢复后，孩子被重复登记 ----------
        dup_child = b.intake_person(
            received_location="丙馆前台", received_by_team="安置点登记组",
            received_by_operator="前台丁", shelter_id=sh_c,
            full_name="น้องต้น", provisional=True, display_code="P-DUPCH",
            is_child=True, age_group="child",
        )
        vid = b.add_verification(
            dup_child, "hotline_crosscheck", evidence_ref="热线 H-88 与腕带一致",
            confidence=96, created_by=self.cp,
        )
        b.decide_verification(vid, confirm=True, operator_id=self.cp)
        b.merge_persons(
            child, dup_child, verification_id=vid,
            reason="热线、腕带与三队接收记录互相印证，为同一儿童",
            operator_id=self.cp,
        )

        # ---------- 回执：同编号、数量不一致 → 主管队列 ----------
        arrival = {
            "kind": "supply_arrival", "batch_code": "INF-MILK-1",
            "item_name": "婴幼儿配方奶", "category": "infant",
            "unit": "罐", "quantity_in": 30,
            "shelter_code": "丙馆安置点", "source_team": "救援队三队",
        }
        b.ingest_receipt("RCP-77", "救援队三队", arrival, operator_id=self.log)
        from src.shelter import ReceiptConflict

        with self.assertRaises(ReceiptConflict):
            b.ingest_receipt(
                "RCP-77", "救援队二队", {**arrival, "quantity_in": 25},
                operator_id=None,
            )
        conflict_id = b.open_receipt_conflicts()[0]["receipt_id"]
        b.resolve_receipt_conflict(conflict_id, "keep_existing",
                                   operator_id=self.op)

        # ---------- 婴幼儿用品发给了“重复档案”，合并后仍可定位 ----------
        milk_batch = b.conn.execute(
            "SELECT id FROM supply_batches WHERE code='INF-MILK-1'"
        ).fetchone()["id"]
        issue = b.issue_supply(milk_batch, dup_child, 2,
                               operator_id=self.log, note="发给重复登记档案")
        # 药品发给母亲。
        meds = b.receive_supply_batch(
            "MED-INS", "胰岛素", "medicine", "支", 10,
            shelter_id=sh_a, created_by=self.med,
        )
        b.issue_supply(meds, mom, 3, operator_id=self.med)

        # 通过孩子当前（保留）档案即可查到其持有奶粉，台账行仍挂原始档案。
        holding = b.person_holding(child)
        self.assertEqual(holding[milk_batch], 2)
        issued_row = b.conn.execute(
            "SELECT person_id FROM stock_ledger WHERE id=?", (issue,)
        ).fetchone()
        self.assertEqual(issued_row["person_id"], dup_child)

        # ---------- 寻亲：母亲找孩子，线索打分、接受、主管确认团聚 ----------
        lead = b.create_tracing_lead(
            seeker_person_id=mom, source="hotline", source_team="夜班协调",
            sought_name="น้องต้น",
            sought_detail={"age_group": "child", "relative_mother": "มาลี"},
            created_by=self.cp,
        )
        s.ensure_default_recurring()
        s.run_due()  # 失联匹配任务先生成候选并预警
        candidates = b.match_lead(lead)
        self.assertTrue(candidates)
        top = candidates[0]
        self.assertEqual(b.canonical_person(top["person_id"]), child)
        b.accept_match(top["link_id"], operator_id=self.cp)
        decision = b.confirm_reunion(
            lead, family_id=family, operator_id=self.op,
            note="腕带、热线与三队原始接收记录一致，准予团聚",
        )
        self.assertTrue(decision)

        # ---------- 母亲跨区调剂到医疗点：未执行安排随之调整 ----------
        b.create_arrangement(
            mom, "medication_pickup", planned_at="2026-10-01T10:00:00+00:00",
            from_shelter_id=sh_a, payload={"medicine": "胰岛素"},
            created_by=self.med,
        )
        b.create_arrangement(
            mom, "return_home", planned_at="2026-10-05T08:00:00+00:00",
            from_shelter_id=sh_a, created_by=self.op,
        )
        b.record_movement(
            mom, "cross_district", to_shelter_id=sh_d,
            reason="祖母需无障碍医疗条件，随母跨区调剂",
            operator_id=self.op,
        )

        # ---------- 重启：下一班打开同一个库 ----------
        self.conn.commit()
        self.conn.close()
        self.conn = connect(self.tmp)
        self.b = Backend(self.conn, self.clock)
        self.s = Scheduler(self.b)
        b, s = self.b, self.s
        s.ensure_default_recurring()  # 不得产生重复周期任务
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM scheduled_jobs").fetchone()["n"],
            4,
        )
        s.run_due()

        # ================= 交接断言 =================
        handoff = b.family_handoff(family)

        # 1) 说清为何拆分：三支队伍、三个安置点。
        explanations = " ".join(handoff["separation"]["explanations"])
        self.assertIn("3 支救援队", explanations)
        self.assertIn("3 个不同安置点", explanations)
        teams = {leg["team"] for leg in handoff["separation"]["legs"]}
        self.assertEqual(teams, {"救援队一队", "救援队二队", "救援队三队"})

        # 2) 各成员当时在哪里（含跨区调剂后的最新位置）。
        where = {m["display_code"]: m["current_shelter_name"]
                 for m in handoff["members"]}
        self.assertEqual(where["P-DAD"], "乙寺安置点")
        self.assertEqual(where["P-GMA"], "甲校安置点")
        self.assertEqual(where["TMP-CHILD"], "丙馆安置点")
        self.assertEqual(where["P-MOM"], "丁院医疗点")

        # 3) 孩子两条原始接收记录都可查（三队村口 + 前台重复登记），且有合并节点。
        child_intakes = [
            e for e in handoff["timeline"]
            if e["kind"] == "intake" and e["person_id"] == child
        ]
        locations = {e["detail"]["location"] for e in child_intakes}
        self.assertEqual(locations, {"村口大树", "丙馆前台"})
        teams_on_intake = {e["detail"]["team"] for e in child_intakes}
        self.assertIn("救援队三队", teams_on_intake)
        self.assertIn("安置点登记组", teams_on_intake)
        self.assertTrue(any(e["kind"] == "merge" and e["person_id"] == child
                            for e in handoff["timeline"]))

        # 4) 团聚决定挂着线索依据。
        self.assertEqual(len(handoff["reunions"]), 1)
        self.assertTrue(handoff["reunions"][0]["leads"])

        # 5) 物资去向：母亲的胰岛素、孩子的配方奶都能对上人。
        self.assertEqual(b.person_holding(mom)[meds], 3)
        self.assertEqual(b.person_holding(child)[milk_batch], 2)

        # 6) 母亲跨区后：用药安排接续到医疗点、返家安排取消。
        follow = b.conn.execute(
            "SELECT * FROM arrangements WHERE type='medication_pickup' AND status='planned'"
        ).fetchone()
        self.assertEqual(follow["from_shelter_id"], sh_d)
        self.assertEqual(
            b.conn.execute(
                "SELECT status FROM arrangements WHERE type='return_home'"
            ).fetchone()["status"],
            "cancelled",
        )
        due = b.due_arrangements()
        self.assertTrue(any(a["type"] == "medication_pickup" for a in due))

        # 7) 主管处理过的回执冲突不再占用预警队列。
        self.assertFalse(any(
            a["type"] == "receipt_conflict" for a in b.open_alerts()
        ))


if __name__ == "__main__":
    unittest.main()
