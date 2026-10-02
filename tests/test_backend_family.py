"""家庭拆分与团聚：三支救援队分别送走同一家人。"""

import unittest

from src.backend import family_story

from tests.helpers import make_service, make_site


class FamilySeparationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock = make_service()
        self.community, _ = make_site(self.svc)
        self.shelters = {}
        for name in ("甲", "乙", "丙"):
            self.shelters[name] = self.svc.register_shelter(
                "coord", "coordinator", community_id=self.community,
                name=f"{name}安置点", capacity=100,
            )
        # 三支救援队各自送走一名家庭成员
        self.members = {}
        for team, shelter, person in (
            ("救援队一", "甲", ("Somchai Jaidee", "adult")),
            ("救援队二", "乙", ("Suda Jaidee", "adult")),
            ("救援队三", "丙", ("Nong Jaidee", "child")),
        ):
            batch = self.svc.create_evacuation_batch(
                "rec", "reception", team=team, source_community_id=self.community,
                dest_shelter_id=self.shelters[shelter],
            )
            self.members[person[0]] = self.svc.register_person(
                "rec", "reception", display_name=person[0], age_band=person[1],
                shelter_id=self.shelters[shelter], batch_id=batch,
            )
        self.family = self.svc.create_family("rec", "reception", name="Jaidee 一家")
        self.svc.add_family_member("rec", "reception", family_id=self.family,
                                   person_id=self.members["Somchai Jaidee"], relation="户主")
        self.svc.add_family_member("rec", "reception", family_id=self.family,
                                   person_id=self.members["Suda Jaidee"], relation="配偶")
        self.svc.add_family_member("rec", "reception", family_id=self.family,
                                   person_id=self.members["Nong Jaidee"], relation="子女")

    def test_family_marked_separated_with_locations(self) -> None:
        family = self.svc.store.one("SELECT * FROM families WHERE id = ?", (self.family,))
        self.assertEqual(family["status"], "separated")
        events = [e for e in self.svc.store.events_for("family", self.family)
                  if e["type"] == "family_split"]
        self.assertTrue(events)
        locations = events[-1]["payload"]["member_locations"]
        self.assertEqual(len(locations), 3)
        # 拆分原因与各成员当时所在安置点、撤离批次都可查
        shelter_of = {
            info["name"]: info["shelter_id"] for info in locations.values()
        }
        self.assertEqual(shelter_of["Somchai Jaidee"], self.shelters["甲"])
        self.assertEqual(shelter_of["Nong Jaidee"], self.shelters["丙"])
        self.assertTrue(all(info["batch_id"] for info in locations.values()))

    def test_reunification_records_supporting_clues(self) -> None:
        clue = self.svc.record_clue(
            "rec", "reception", clue_type="location_report",
            detail="志愿者在乙安置点见到 Suda", subject_person_id=self.members["Suda Jaidee"],
        )
        self.svc.confirm_clue("sup", "supervisor", clue_id=clue)
        t1 = self.svc.plan_transfer("rec", "reception", person_id=self.members["Somchai Jaidee"],
                                    to_shelter_id=self.shelters["乙"], reason="家庭团聚转运",
                                    reason_category="reunification")
        self.svc.confirm_transfer("rec", "reception", transfer_id=t1)
        # 孩子还在丙点，家庭仍是拆分状态
        self.assertEqual(
            self.svc.store.one("SELECT status FROM families WHERE id = ?", (self.family,))["status"],
            "separated",
        )
        t2 = self.svc.plan_transfer("rec", "reception", person_id=self.members["Nong Jaidee"],
                                    to_shelter_id=self.shelters["乙"], reason="家庭团聚转运",
                                    reason_category="reunification")
        self.svc.confirm_transfer("rec", "reception", transfer_id=t2)
        self.assertEqual(
            self.svc.store.one("SELECT status FROM families WHERE id = ?", (self.family,))["status"],
            "reunited",
        )
        story = family_story(self.svc, "rec", "reception", self.family)
        self.assertEqual(story["status"], "reunited")
        self.assertEqual(len(story["reunifications"]), 1)
        clue_details = [c["detail"] for c in story["reunifications"][0]["clues"]]
        self.assertIn("志愿者在乙安置点见到 Suda", clue_details)
        # 时间线能讲清来龙去脉
        types = [entry["type"] for entry in story["timeline"]]
        for expected in ("person_registered", "family_split", "transfer_confirmed", "family_reunited"):
            self.assertIn(expected, types)


if __name__ == "__main__":
    unittest.main()
