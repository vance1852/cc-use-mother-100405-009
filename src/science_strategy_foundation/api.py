"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .jv_service import JointVentureService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    jv_service = getattr(service, "jv", None)
    if jv_service is None:
        jv_service = JointVentureService(service.database, service.clock)
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        status, payload = _route_jv(jv_service, method, parsed, body, actor_id)
        if status is not None:
            return status, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _query(parsed, key: str, default: str | None = None) -> str | None:
    return parse_qs(parsed.query).get(key, [default])[0]


def _route_jv(jv: JointVentureService, method: str, parsed, body: dict[str, Any],
              actor_id: str) -> tuple[int | None, dict[str, Any]]:
    """分派区域创新联合投入相关路由。"""

    path = parsed.path
    parts = [p for p in path.split("/") if p]
    if not parts or parts[0] != "jv":
        return None, {}
    rest = parts[1:]

    def created(receipt) -> tuple[int, dict[str, Any]]:
        return 200 if receipt.replayed else 201, receipt.__dict__

    if method == "POST":
        if rest == ["platforms"]:
            return created(jv.create_platform(actor_id=actor_id, **body))
        if rest == ["memberships"]:
            return created(jv.register_membership(actor_id=actor_id, **body))
        if rest == ["charters"]:
            return created(jv.publish_charter(actor_id=actor_id, **body))
        if rest == ["persons"]:
            return created(jv.register_person(actor_id=actor_id, **body))
        if rest == ["equipment"]:
            return created(jv.register_equipment(actor_id=actor_id, **body))
        if rest == ["fund-sources"]:
            return created(jv.register_fund_source(actor_id=actor_id, **body))
        if rest == ["plans"]:
            return created(jv.create_plan(actor_id=actor_id, **body))
        if rest == ["milestones"]:
            return created(jv.create_milestone(actor_id=actor_id, **body))
        if len(rest) == 3 and rest[0] == "milestones" and rest[2] == "complete":
            return created(jv.complete_milestone(actor_id=actor_id, milestone_id=rest[1],
                                                 **{k: v for k, v in body.items() if k != "milestone_id"}))
        if rest == ["commitments"]:
            return created(jv.register_commitment(actor_id=actor_id, **body))
        if len(rest) == 3 and rest[0] == "commitments" and rest[2] == "submit":
            return 200, jv.submit_commitment(actor_id=actor_id, commitment_id=rest[1])
        if len(rest) == 3 and rest[0] == "commitments" and rest[2] == "withdraw":
            return created(jv.withdraw_commitment(actor_id=actor_id, commitment_id=rest[1], **body))
        if len(rest) == 3 and rest[0] == "commitments" and rest[2] == "sign":
            return 200, jv.sign_commitment(actor_id=actor_id, commitment_id=rest[1],
                                           note=body.get("note"))
        if rest == ["performances"]:
            return created(jv.record_performance(actor_id=actor_id, **body))
        if rest == ["events", "person-departed"]:
            return created(jv.record_person_departed(actor_id=actor_id, **body))
        if rest == ["events", "equipment-downtime"]:
            return created(jv.record_equipment_downtime(actor_id=actor_id, **body))
        if rest == ["events", "equipment-recovered"]:
            return created(jv.record_equipment_recovered(actor_id=actor_id, **body))
        if rest == ["fund-tranches", "delay"]:
            return created(jv.delay_fund_tranch(actor_id=actor_id, **body))
        if rest == ["fund-tranches", "receive"]:
            return created(jv.receive_fund_tranch(actor_id=actor_id, **body))
        if rest == ["member-exits"]:
            return created(jv.record_member_exit(actor_id=actor_id, **body))
        if rest == ["outcome-policies"]:
            return created(jv.set_outcome_policy(actor_id=actor_id, **body))
        if rest == ["outcomes", "finalize"]:
            return created(jv.finalize_outcome(actor_id=actor_id, **body))
    if method == "GET":
        if len(rest) == 3 and rest[0] == "platforms" and rest[2] == "snapshot":
            return 200, jv.get_platform_snapshot(rest[1], _query(parsed, "as_of"))
        if rest == ["charters"]:
            platform_id = _query(parsed, "platform_id")
            if not platform_id:
                raise ValidationError("platform_id 不能为空")
            return 200, jv.get_charter_at(platform_id, _query(parsed, "as_of"))
        if len(rest) == 2 and rest[0] == "commitments":
            return 200, jv.get_commitment(rest[1], _query(parsed, "as_of"))
        if len(rest) == 3 and rest[0] == "milestones" and rest[2] == "readiness":
            return 200, jv.get_milestone_readiness(rest[1], _query(parsed, "as_of"))
    return 404, {"error": "route_not_found", "message": "接口不存在"}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动科技战略协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
