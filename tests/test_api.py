import unittest

from science_strategy_foundation.api import route
from science_strategy_foundation.service import DomainService
from science_strategy_foundation.storage import Database


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)

    def tearDown(self):
        self.database.close()

    def test_health_is_available_without_actor(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_returns_404(self):
        status, payload = route(self.service, "GET", "/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_invalid_json_shape_returns_400(self):
        status, payload = route(self.service, "POST", "/organizations", {"request_id": "x"},
                                {"X-Actor-Id": "bootstrap"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])


class JointVentureApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)

    def tearDown(self):
        self.database.close()

    def _call(self, method, path, body=None, actor="adm"):
        return route(self.service, method, path, body or {}, {"X-Actor-Id": actor})

    def _bootstrap(self):
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o-a", "name": "甲区"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "adm", "new_actor_id": "adm", "display_name": "管理员",
               "role": "admin", "organization_id": "o-a"}, {"X-Actor-Id": "bootstrap"})

    def test_jv_charter_and_readiness_endpoints(self):
        self._bootstrap()
        status, payload = self._call("POST", "/jv/platforms",
                                     {"request_id": "p", "platform_id": "p", "name": "平台"})
        self.assertEqual(201, status)
        status, payload = self._call("POST", "/jv/memberships", {
            "request_id": "m", "platform_id": "p", "organization_id": "o-a",
            "member_role": "lead", "member_order": 0})
        self.assertEqual(201, status)
        status, payload = self._call("POST", "/jv/charters", {
            "request_id": "c", "platform_id": "p", "priority_rules": {"type": "plan_rank"},
            "caps": {"person_hours_monthly": 160}, "outcome_redistribution": "pro_rata",
            "effective_from": "2026-10-01"})
        self.assertEqual(201, status)
        status, payload = self._call("GET", "/jv/charters?platform_id=p&as_of=2026-10-02")
        self.assertEqual(200, status)
        self.assertEqual(1, payload["charter"]["version_no"])

    def test_jv_unknown_route_is_404(self):
        self._bootstrap()
        status, payload = self._call("GET", "/jv/unknown")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
