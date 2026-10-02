"""岗位访问控制：儿童、老人和医疗资料只对相应岗位开放。"""

import unittest

from src.backend import AccessDeniedError

from tests.helpers import make_service, make_site


class SensitiveNeedAccessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = make_service()
        _, self.shelter = make_site(self.svc)
        self.person = self.svc.register_person(
            "rec", "reception", display_name="Som", age_band="adult", shelter_id=self.shelter
        )

    def test_medical_need_only_visible_to_medical(self) -> None:
        self.svc.record_need(
            "med", "medical", person_id=self.person, category="health", detail="每日胰岛素"
        )
        self.assertEqual(self.svc.list_needs("log", "logistics", self.person), [])
        self.assertEqual(self.svc.list_needs("rec", "reception", self.person), [])
        medical_view = self.svc.list_needs("med", "medical", self.person)
        self.assertEqual(len(medical_view), 1)
        supervisor_view = self.svc.list_needs("sup", "supervisor", self.person)
        self.assertEqual(len(supervisor_view), 1)

    def test_child_and_elder_needs_restricted_to_own_posts(self) -> None:
        self.svc.record_need(
            "cp", "child_protection", person_id=self.person,
            category="child_welfare", detail="需要监护人陪同",
        )
        self.svc.record_need(
            "ec", "elder_care", person_id=self.person,
            category="elder_care", detail="夜间需巡护",
        )
        self.assertEqual(len(self.svc.list_needs("cp", "child_protection", self.person)), 1)
        self.assertEqual(len(self.svc.list_needs("ec", "elder_care", self.person)), 1)
        self.assertEqual(self.svc.list_needs("log", "logistics", self.person), [])
        self.assertEqual(len(self.svc.list_needs("sup", "supervisor", self.person)), 2)

    def test_recording_need_requires_matching_post(self) -> None:
        with self.assertRaises(AccessDeniedError):
            self.svc.record_need(
                "rec", "reception", person_id=self.person, category="health", detail="开药"
            )
        with self.assertRaises(AccessDeniedError):
            self.svc.record_need(
                "med", "medical", person_id=self.person,
                category="child_welfare", detail="儿童心理安抚",
            )

    def test_general_need_visible_to_all_staff(self) -> None:
        self.svc.record_need(
            "rec", "reception", person_id=self.person, category="other", detail="需要毛毯"
        )
        self.assertEqual(len(self.svc.list_needs("log", "logistics", self.person)), 1)


class SensitiveProfileAccessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = make_service()
        _, self.shelter = make_site(self.svc)

    def test_child_profile_details_only_for_child_protection(self) -> None:
        child = self.svc.register_person(
            "rec", "reception", display_name="Nong", age_band="child", shelter_id=self.shelter
        )
        basic = self.svc.get_person("rec", "reception", child)
        self.assertEqual(basic["sensitivity"], "child")
        self.assertNotIn("id_document", basic)
        with self.assertRaises(AccessDeniedError):
            self.svc.get_person("rec", "reception", child, include_sensitive=True)
        with self.assertRaises(AccessDeniedError):
            self.svc.get_person("med", "medical", child, include_sensitive=True)
        detail = self.svc.get_person("cp", "child_protection", child, include_sensitive=True)
        self.assertIn("registered_by", detail)

    def test_elderly_profile_details_only_for_elder_care(self) -> None:
        elder = self.svc.register_person(
            "rec", "reception", display_name="Pa", age_band="elderly", shelter_id=self.shelter
        )
        with self.assertRaises(AccessDeniedError):
            self.svc.get_person("cp", "child_protection", elder, include_sensitive=True)
        detail = self.svc.get_person("ec", "elder_care", elder, include_sensitive=True)
        self.assertIn("registered_by", detail)

    def test_medical_batch_not_accessible_to_reception(self) -> None:
        batch = self.svc.register_supply_batch(
            "log", "logistics", kind="medicine", item="胰岛素", quantity=10,
            unit="支", shelter_id=self.shelter,
        )
        with self.assertRaises(AccessDeniedError):
            self.svc.batch_ledger("rec", "reception", batch)
        with self.assertRaises(AccessDeniedError):
            self.svc.distribute("rec", "reception", batch_id=batch, person_id="x", quantity=1)
        ledger = self.svc.batch_ledger("log", "logistics", batch)
        self.assertEqual(ledger["remaining"], 10)


if __name__ == "__main__":
    unittest.main()
