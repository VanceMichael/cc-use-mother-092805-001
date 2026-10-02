"""交接班摘要与 HTTP 接口冒烟。"""

import http.client
import json
import threading
import unittest

from src.backend import shift_handover
from src.backend.api import make_server

from tests.helpers import make_service, make_site


class ShiftHandoverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = make_service()
        self.community, self.shelter = make_site(self.svc, capacity=1)
        self.person = self.svc.register_person(
            "rec", "reception", display_name="A", age_band="adult", shelter_id=self.shelter
        )

    def test_handover_collects_pending_work(self) -> None:
        other = self.svc.register_shelter(
            "coord", "coordinator", community_id=self.community, name="二号安置点", capacity=100
        )
        self.svc.plan_transfer("rec", "reception", person_id=self.person,
                               to_shelter_id=other, reason="医疗转运", reason_category="medical")
        self.svc.submit_receipt(
            "log", "logistics", kind="supply_delivery", source_team="甲队", external_id="R-1",
            payload={"kind": "food", "item": "饮用水", "quantity": 10, "unit": "瓶",
                     "shelter_id": self.shelter},
        )
        self.svc.submit_receipt(
            "log", "logistics", kind="supply_delivery", source_team="甲队", external_id="R-1",
            payload={"kind": "food", "item": "饮用水", "quantity": 12, "unit": "瓶",
                     "shelter_id": self.shelter},
        )
        report = shift_handover(self.svc, "sup", "supervisor")
        # 容量预警（1/1 住满）
        self.assertEqual(len(report["capacity_alerts"]), 1)
        self.assertEqual(report["capacity_alerts"][0]["shelter"], "一号安置点")
        # 待确认转运
        self.assertEqual(len(report["transfer_confirmations"]), 1)
        self.assertEqual(report["planned_transfers"][0]["to_shelter"], "二号安置点")
        # 回执冲突待主管核对
        self.assertEqual(len(report["open_conflicts"]), 1)


class ApiSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.svc, _ = make_service()
        cls.server = make_server(cls.svc, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, command: str, body: dict, role: str = "coordinator") -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(
            "POST", f"/commands/{command}", body=json.dumps(body),
            headers={"Content-Type": "application/json",
                     "X-Actor-Id": "api-tester", "X-Actor-Role": role},
        )
        response = conn.getresponse()
        return response.status, json.loads(response.read())

    def _get(self, query: str, params: str = "", role: str = "supervisor") -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(
            "GET", f"/queries/{query}?{params}",
            headers={"X-Actor-Id": "api-tester", "X-Actor-Role": role},
        )
        response = conn.getresponse()
        return response.status, json.loads(response.read())

    def test_command_then_query_roundtrip(self) -> None:
        status, body = self._post("register_community", {"name": "湄林社区", "province": "清迈"})
        self.assertEqual(status, 200)
        community_id = body["result"]
        status, body = self._post(
            "register_shelter",
            {"community_id": community_id, "name": "接口安置点", "capacity": 10},
        )
        self.assertEqual(status, 200)
        status, body = self._get("shift_handover")
        self.assertEqual(status, 200)
        self.assertIn("pending_tasks", body["result"])

    def test_access_denied_maps_to_403(self) -> None:
        status, body = self._post("register_community", {"name": "X", "province": "Y"},
                                  role="logistics")
        self.assertEqual(status, 403)
        self.assertEqual(body["type"], "AccessDeniedError")

    def test_missing_identity_headers_rejected(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("POST", "/commands/register_community", body="{}")
        response = conn.getresponse()
        self.assertEqual(response.status, 400)

    def test_unknown_route_is_404(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/queries/nonexistent",
                     headers={"X-Actor-Id": "t", "X-Actor-Role": "supervisor"})
        response = conn.getresponse()
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
