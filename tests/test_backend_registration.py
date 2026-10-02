"""接收登记、身份核验、临时档案合并。"""

import unittest

from src.backend import AccessDeniedError, ConflictError, ValidationError

from tests.helpers import make_service, make_site


class RegistrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock = make_service()
        self.community, self.shelter = make_site(self.svc)

    def test_undocumented_person_gets_temporary_profile(self) -> None:
        pid = self.svc.register_person(
            "rec-1", "reception", display_name="无名氏", age_band="adult", shelter_id=self.shelter
        )
        person = self.svc.get_person("rec-1", "reception", pid)
        self.assertTrue(person["is_temp"])
        self.assertEqual(person["identity_status"], "temp_unverified")

    def test_identity_verification_upgrades_temp_profile(self) -> None:
        pid = self.svc.register_person(
            "rec-1", "reception", display_name="Mali", age_band="adult", shelter_id=self.shelter
        )
        self.svc.record_identity_step(
            "rec-1", "reception", person_id=pid, step="interview", outcome="inconclusive"
        )
        self.assertEqual(
            self.svc.get_person("rec-1", "reception", pid)["identity_status"], "temp_unverified"
        )
        self.svc.record_identity_step(
            "rec-1", "reception", person_id=pid, step="community_witness", outcome="passed"
        )
        person = self.svc.get_person("rec-1", "reception", pid)
        self.assertEqual(person["identity_status"], "verified")
        self.assertFalse(person["is_temp"])

    def test_document_number_cannot_be_registered_twice(self) -> None:
        self.svc.register_person(
            "rec-1", "reception", display_name="A", age_band="adult",
            shelter_id=self.shelter, id_document="DOC-1",
        )
        with self.assertRaises(ConflictError):
            self.svc.register_person(
                "rec-1", "reception", display_name="B", age_band="adult",
                shelter_id=self.shelter, id_document="DOC-1",
            )

    def test_reception_requires_reception_role(self) -> None:
        with self.assertRaises(AccessDeniedError):
            self.svc.register_person(
                "log-1", "logistics", display_name="A", age_band="adult", shelter_id=self.shelter
            )


class MergeTest(unittest.TestCase):
    """通信恢复后重复登记：先各自建临时档案，线索确认后再合并。"""

    def setUp(self) -> None:
        self.svc, self.clock = make_service()
        self.community, self.shelter_a = make_site(self.svc)
        self.shelter_b = self.svc.register_shelter(
            "coord", "coordinator", community_id=self.community, name="二号安置点", capacity=100
        )
        self.first = self.svc.register_person(
            "rec-1", "reception", display_name="Mali Sook", age_band="adult",
            shelter_id=self.shelter_a,
        )
        self.clock.tick(30)
        self.second = self.svc.register_person(
            "rec-2", "reception", display_name="Mali  Sook", age_band="adult",
            shelter_id=self.shelter_b,
        )

    def _duplicate_clue_id(self) -> str:
        clues = self.svc.store.all(
            "SELECT id FROM tracing_clues WHERE clue_type = 'possible_duplicate'"
        )
        self.assertEqual(len(clues), 1)
        return clues[0]["id"]

    def test_duplicate_registration_raises_clue_automatically(self) -> None:
        clue_id = self._duplicate_clue_id()
        candidates = self.svc.store.all(
            "SELECT person_id FROM match_candidates WHERE clue_id = ?", (clue_id,)
        )
        self.assertEqual([c["person_id"] for c in candidates], [self.first])

    def test_merge_requires_confirmed_clue_and_supervisor(self) -> None:
        clue_id = self._duplicate_clue_id()
        with self.assertRaises(ConflictError):
            self.svc.merge_persons(
                "sup", "supervisor", survivor_id=self.first, merged_id=self.second,
                clue_id=clue_id, reason="同一人",
            )
        self.svc.confirm_clue("sup", "supervisor", clue_id=clue_id)
        with self.assertRaises(AccessDeniedError):
            self.svc.merge_persons(
                "rec-1", "reception", survivor_id=self.first, merged_id=self.second,
                clue_id=clue_id, reason="同一人",
            )

    def test_merge_preserves_original_reception_facts(self) -> None:
        clue_id = self._duplicate_clue_id()
        self.svc.confirm_clue("sup", "supervisor", clue_id=clue_id)
        self.svc.merge_persons(
            "sup", "supervisor", survivor_id=self.first, merged_id=self.second,
            clue_id=clue_id, reason="通信恢复后重复登记",
        )
        # 被并档案的原接收地点、时间、经办人继续可查
        merged_record = self.svc.reception_record("rec-1", "reception", self.second)
        self.assertEqual(merged_record["registered_shelter_id"], self.shelter_b)
        self.assertEqual(merged_record["registered_by"], "rec-2")
        self.assertEqual(merged_record["identity_status"], "merged")
        self.assertEqual(merged_record["merged_into"], self.first)
        survivor_record = self.svc.reception_record("rec-1", "reception", self.first)
        self.assertEqual(survivor_record["registered_shelter_id"], self.shelter_a)
        self.assertEqual(survivor_record["registered_by"], "rec-1")
        # 被并者腾出床位，占用回到 1
        occupancy = self.svc.store.one(
            "SELECT occupancy FROM shelters WHERE id = ?", (self.shelter_b,)
        )
        self.assertEqual(occupancy["occupancy"], 0)
        # 合并事件同时记录两份接收信息
        events = self.svc.store.events_for("person", self.first)
        merge_events = [e for e in events if e["type"] == "persons_merged"]
        self.assertEqual(len(merge_events), 1)
        payload = merge_events[0]["payload"]
        self.assertEqual(payload["merged_reception"]["by"], "rec-2")
        self.assertEqual(payload["survivor_reception"]["by"], "rec-1")

    def test_merged_person_cannot_be_merged_again(self) -> None:
        clue_id = self._duplicate_clue_id()
        self.svc.confirm_clue("sup", "supervisor", clue_id=clue_id)
        self.svc.merge_persons(
            "sup", "supervisor", survivor_id=self.first, merged_id=self.second,
            clue_id=clue_id, reason="同一人",
        )
        with self.assertRaises(ConflictError):
            self.svc.merge_persons(
                "sup", "supervisor", survivor_id=self.first, merged_id=self.second,
                clue_id=clue_id, reason="重复合并",
            )

    def test_merge_with_unrelated_clue_is_rejected(self) -> None:
        other_clue = self.svc.record_clue(
            "rec-1", "reception", clue_type="location_report", detail="无关线索"
        )
        self.svc.confirm_clue("sup", "supervisor", clue_id=other_clue)
        with self.assertRaises(ValidationError):
            self.svc.merge_persons(
                "sup", "supervisor", survivor_id=self.first, merged_id=self.second,
                clue_id=other_clue, reason="张冠李戴",
            )


if __name__ == "__main__":
    unittest.main()
