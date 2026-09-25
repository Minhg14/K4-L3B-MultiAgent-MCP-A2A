from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx2
from jsonschema import Draft202012Validator

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

CASE_ID = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,63}$")
RUN_ID = secrets.token_urlsafe(8)
MAX_ATTEMPTS = 18

# Names and argument shapes observed through MCP discovery. A role is enabled only
# when the current server advertises the same name and compatible input schema.
TOOL_ROLES = {
    "order": ("get_order", "order_id", "order"),
    "history": ("get_customer_history", "customer_unique_id", "customer"),
    "items": ("get_order_items", "order_id", "item"),
    "products": ("get_product_context", "order_id", "product"),
    "sellers": ("get_sellers", "order_id", "seller"),
    "shipment": ("get_shipment_summary", "order_id", "shipment"),
    "payments": ("get_order_payments", "order_id", "payment"),
    "payment_timeline": ("get_payment_timeline", "order_id", "payment"),
    "refunds": ("get_refund_timeline", "order_id", "refund"),
    "policy": ("get_policy", "policy_version", "policy"),
}


@dataclass(frozen=True)
class Evidence:
    ref: str
    tool: str
    domain: str
    data: Any
    case_id: str
    request_id: str
    arguments_digest: str
    envelope: dict[str, Any]
    received_at: datetime


@dataclass
class CaseContext:
    case: dict[str, Any]
    gateway: EvidenceGateway
    trace: TraceWriter
    tools: dict[str, dict[str, Any]]
    cache: dict[tuple[str, str], Evidence | None] = field(default_factory=dict)
    ledger: dict[str, Evidence] = field(default_factory=dict)
    used: dict[str, Evidence] = field(default_factory=dict)
    consumers: dict[str, set[str]] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 0
    sequence: int = 0

    @property
    def case_id(self) -> str:
        return self.case["case_id"]

    def emit(self, event_type: str, actor: str, **kwargs: Any) -> None:
        self.sequence += 1
        attributes = {"run_id": RUN_ID, "sequence": self.sequence}
        attributes.update(kwargs.pop("attributes", {}))
        self.trace.emit(
            case_id=self.case_id,
            event_type=event_type,
            actor=actor,
            attributes=attributes,
            **kwargs,
        )

    def assign(self, actor: str) -> None:
        self.emit(
            "task_assigned",
            "coordinator",
            target=actor,
            attributes={"stage": actor, "correlation_id": f"{self.case_id}:{actor}"},
        )

    def handoff(self, actor: str, status: str) -> None:
        self.emit(
            "handoff",
            actor,
            target="coordinator",
            decision_code=status,
            attributes={"stage": actor, "correlation_id": f"{self.case_id}:{actor}"},
        )

    def consume(self, evidence: Evidence | None, actor: str) -> None:
        if evidence is None:
            return
        self.consumers.setdefault(evidence.ref, set()).add(actor)
        if evidence.ref in self.used:
            return
        self.used[evidence.ref] = evidence
        self.emit(
            "tool_result_consumed", actor, tool_name=evidence.tool, evidence_refs=[evidence.ref]
        )

    def conflict(
        self, field_name: str, sources: list[str], selected: str | None, code: str
    ) -> None:
        if len(self.conflicts) >= 5:
            self.gaps.append("conflict_limit_exceeded")
            return
        if len(set(sources)) < 2:
            return
        if any(item["field"] == field_name for item in self.conflicts):
            return
        self.conflicts.append(
            {
                "field": field_name,
                "sources": list(dict.fromkeys(sources))[:5],
                "selected_source": selected,
                "resolution_code": code,
            }
        )

    async def fetch(self, role: str, value: str) -> Evidence | None:
        name, argument, domain = TOOL_ROLES[role]
        key = (role, value)
        if key in self.cache:
            return self.cache[key]
        spec = self.tools.get(role)
        if spec is None:
            self.gaps.append(f"tool_unavailable:{role}")
            self.cache[key] = None
            return None
        payload = {"case_id": self.case_id, argument: value}
        if not Draft202012Validator(spec["input_schema"]).is_valid(payload):
            self.gaps.append(f"tool_arguments_invalid:{role}")
            self.cache[key] = None
            return None
        result: Evidence | None = None
        for attempt in range(2):
            if self.attempts >= MAX_ATTEMPTS:
                self.gaps.append("call_budget_exhausted")
                break
            self.attempts += 1
            try:
                envelope = await self.gateway.call(name, case_id=self.case_id, **{argument: value})
                self.trace.contracts.validate_evidence(envelope, f"{name} evidence")
                if envelope["domain"] != domain or not _valid_data(role, envelope["data"], value):
                    self.gaps.append(f"invalid_data:{role}")
                    break
                ref = envelope["evidence_ref"]
                digest = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                if ref in self.ledger and (
                    self.ledger[ref].tool != name or self.ledger[ref].arguments_digest != digest
                ):
                    self.gaps.append(f"ref_collision:{role}")
                    break
                result = Evidence(
                    ref=ref,
                    tool=name,
                    domain=domain,
                    data=envelope["data"],
                    case_id=self.case_id,
                    request_id=f"{self.case_id}:{self.attempts}",
                    arguments_digest=digest,
                    envelope=envelope,
                    received_at=datetime.now(UTC),
                )
                self.ledger[ref] = result
                break
            except (TimeoutError, httpx2.TransportError):
                if attempt == 0:
                    continue
                self.gaps.append(f"timeout:{role}")
            except (RuntimeError, ValueError) as exc:
                # The gateway does not document whether an error means not found.
                self.gaps.append(f"tool_error:{role}:{type(exc).__name__}")
                break
        self.cache[key] = result
        return result


def _valid_data(role: str, data: Any, value: str) -> bool:
    if role in {"items", "products", "sellers", "payments"}:
        if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
            return False
        if role in {"items", "payments"}:
            return all(row.get("order_id") == value for row in data)
        return True
    if not isinstance(data, dict):
        return False
    if role == "history":
        orders = data.get("orders")
        return (
            data.get("customer_unique_id") == value
            and isinstance(orders, list)
            and all(isinstance(row, dict) for row in orders)
        )
    if role == "policy":
        return data.get("policy_version") == value and isinstance(data.get("rules"), dict)
    if data.get("order_id") != value:
        return False
    if role == "shipment":
        return isinstance(data.get("events", []), list) and isinstance(
            data.get("shipping_limits", []), list
        )
    if role in {"payment_timeline", "refunds"}:
        return isinstance(data.get("events", []), list)
    return True


def _money(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
        cents = amount * 100
        if not amount.is_finite() or amount < 0 or cents != cents.to_integral_value():
            return None
        return int(cents)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _brl(cents: int | None) -> float | None:
    return None if cents is None else cents / 100


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _normalize(case: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(case, dict) or not isinstance(case.get("case_id"), str):
        raise ValueError("case must have a string case_id")
    if not CASE_ID.fullmatch(case["case_id"]) or _time(case.get("opened_at")) is None:
        raise ValueError("invalid case_id or opened_at")
    request = case.get("customer_request")
    if not isinstance(request, dict) or not isinstance(request.get("claims"), list):
        raise ValueError("missing customer claims")
    claims = request["claims"]
    if not 1 <= len(claims) <= 5:
        raise ValueError("expected one to five claims")
    ids = []
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            raise ValueError("invalid claim")
        claim_id = claim["claim_id"]
        if not 1 <= len(claim_id) <= 64 or not isinstance(claim.get("topic"), str):
            raise ValueError("invalid claim ID or topic")
        ids.append(claim_id)
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate claim_id")
    candidates = case.get("candidate_order_ids")
    if (
        not isinstance(candidates, list)
        or not 1 <= len(candidates) <= 20
        or any(not isinstance(item, str) or not 1 <= len(item) <= 128 for item in candidates)
        or len(set(candidates)) != len(candidates)
    ):
        raise ValueError("invalid candidate_order_ids")
    for key in ("claimed_order_id",):
        value = request.get(key)
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 128):
            raise ValueError(f"invalid {key}")
    hint = case.get("customer_unique_id_hint")
    if hint is not None and (not isinstance(hint, str) or not 1 <= len(hint) <= 128):
        raise ValueError("invalid customer_unique_id_hint")
    policy = case.get("policy_version")
    if not isinstance(policy, str) or not 1 <= len(policy) <= 128:
        raise ValueError("invalid policy_version")
    scope = case.get("investigation_scope")
    if not isinstance(scope, dict) or any(
        not isinstance(scope.get(key), bool)
        for key in (
            "include_customer_history",
            "include_product_context",
            "require_independent_verification",
        )
    ):
        raise ValueError("invalid investigation_scope")
    return case


def _bind_tools(specs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    bound: dict[str, dict[str, Any]] = {}
    for role, (name, argument, _) in TOOL_ROLES.items():
        spec = specs.get(name)
        if not isinstance(spec, dict):
            continue
        schema = spec.get("input_schema")
        if not isinstance(schema, dict):
            continue
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if (
            isinstance(properties, dict)
            and isinstance(required, list)
            and {"case_id", argument}.issubset(properties)
            and {"case_id", argument}.issubset(required)
            and isinstance(properties["case_id"], dict)
            and isinstance(properties[argument], dict)
            and properties["case_id"].get("type") == "string"
            and properties[argument].get("type") == "string"
        ):
            bound[role] = spec
    return bound


async def _entity(ctx: CaseContext) -> dict[str, Any]:
    ctx.assign("entity-agent")
    orders: dict[str, Evidence] = {}
    for candidate in sorted(ctx.case["candidate_order_ids"]):
        found = await ctx.fetch("order", candidate)
        if found is not None and found.data.get("order_id") == candidate:
            orders[candidate] = found
    hint = ctx.case.get("customer_unique_id_hint")
    history = (
        await ctx.fetch("history", hint)
        if hint and ctx.case["investigation_scope"]["include_customer_history"]
        else None
    )
    history_rows = (
        [row for row in history.data["orders"] if isinstance(row, dict)] if history else []
    )
    matches: list[str] = []
    rejected: list[str] = []
    for candidate, evidence in orders.items():
        customer_id = evidence.data.get("customer_id")
        linked = [row for row in history_rows if row.get("order_id") == candidate]
        if linked and customer_id and any(row.get("customer_id") == customer_id for row in linked):
            matches.append(candidate)
        elif (
            linked and customer_id and all(row.get("customer_id") != customer_id for row in linked)
        ):
            rejected.append(candidate)
            ctx.consume(evidence, "entity-agent")
            ctx.consume(history, "entity-agent")
    resolved = matches[0] if len(matches) == 1 else None
    if resolved:
        ctx.consume(orders[resolved], "entity-agent")
        ctx.consume(history, "entity-agent")
        linked = [row for row in history_rows if row.get("order_id") == resolved]
        statuses = {row.get("order_status") for row in linked if row.get("order_status")}
        statuses.add(orders[resolved].data.get("order_status"))
        if len(statuses) > 1:
            ctx.conflict(
                "order_status",
                ["get_order", "get_customer_history"],
                None,
                "UNRESOLVED_STATUS_HISTORY",
            )
    status = "resolved" if resolved else "ambiguous"
    related = (
        sorted({row["order_id"] for row in history_rows if isinstance(row.get("order_id"), str)})[
            :20
        ]
        if resolved
        else []
    )
    ctx.handoff("entity-agent", status)
    return {
        "status": status,
        "order_id": resolved,
        "order": orders[resolved].data if resolved else None,
        "history": history,
        "rejected": sorted(rejected),
        "related": related,
        "customer_unique_id": hint if resolved and history else None,
        "confidence": 0.82 if resolved else 0.25,
    }


async def _fulfillment(ctx: CaseContext, entity: dict[str, Any]) -> dict[str, Any]:
    ctx.assign("fulfillment-agent")
    order_id = entity["order_id"]
    if not order_id:
        ctx.handoff("fulfillment-agent", "identity_gap")
        return {
            "verdict": "insufficient_evidence",
            "complete": False,
            "items": [],
            "sellers": [],
            "late_sellers": [],
            "expected": None,
            "freight": None,
        }
    items_ev = await ctx.fetch("items", order_id)
    products_ev = (
        await ctx.fetch("products", order_id)
        if ctx.case["investigation_scope"]["include_product_context"]
        else None
    )
    shipment_ev = await ctx.fetch("shipment", order_id)
    topic = ctx.case["customer_request"]["claims"][0]["topic"]
    sellers_ev = (
        await ctx.fetch("sellers", order_id)
        if topic in {"late_delivery_seller", "unavailable_order_paid"}
        else None
    )
    for ev in (items_ev, products_ev, shipment_ev, sellers_ev):
        ctx.consume(ev, "fulfillment-agent")
    rows = [row for row in items_ev.data if row.get("order_id") == order_id] if items_ev else []
    by_item: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item_id = row.get("order_item_id")
        if isinstance(item_id, str):
            by_item.setdefault(item_id, []).append(row)
    expected: int | None = 0 if by_item else None
    freight: int | None = 0 if by_item else None
    sellers: set[str] = set()
    for item_id, variants in by_item.items():
        amounts = {(_money(row.get("price")), _money(row.get("freight_value"))) for row in variants}
        seller_ids = {row.get("seller_id") for row in variants}
        if len(amounts) != 1 or None in next(iter(amounts)):
            expected = freight = None
            ctx.conflict(
                f"items.{item_id}.amount",
                ["get_order_items", "get_order_items:row"],
                None,
                "UNRESOLVED_ITEM_AMOUNT",
            )
        elif expected is not None and freight is not None:
            price, shipping = next(iter(amounts))
            expected += price + shipping
            freight += shipping
        if len(seller_ids) == 1 and isinstance(next(iter(seller_ids)), str):
            sellers.add(next(iter(seller_ids)))
        elif len(seller_ids) > 1:
            ctx.conflict(
                f"items.{item_id}.seller_id",
                ["get_order_items", "get_order_items:row"],
                None,
                "UNRESOLVED_SELLER",
            )
    verdict = "insufficient_evidence"
    complete = False
    late_sellers: list[str] = []
    shipment = shipment_ev.data if shipment_ev else None
    if shipment:
        events = [ev for ev in shipment.get("events", []) if isinstance(ev, dict)]
        late_events = [
            ev
            for ev in events
            if ev.get("event_type") == "delivered_late"
            and ev.get("status") == "confirmed"
            and _time(ev.get("event_at"))
        ]
        actors = {ev.get("actor") for ev in late_events}
        if len(actors) > 1:
            verdict = "conflicting"
            ctx.conflict(
                "shipment_analysis.verdict",
                ["shipment_event:seller", "shipment_event:logistics"],
                None,
                "UNRESOLVED_CAUSE",
            )
        elif actors == {"seller"}:
            verdict = "seller_delay"
            late_sellers = sorted(sellers)
            complete = bool(late_sellers)
        elif actors == {"logistics_provider"}:
            verdict = "logistics_delay"
            complete = True
        elif shipment.get("order_status") == "returned":
            verdict, complete = "returned", True
        elif shipment.get("order_status") == "lost":
            verdict, complete = "lost", True
        else:
            delivered = _time(shipment.get("delivered_customer_at"))
            estimated = _time(shipment.get("estimated_delivery_at"))
            carrier = _time(shipment.get("delivered_carrier_at"))
            limits = [
                _time(row.get("shipping_limit_at"))
                for row in shipment.get("shipping_limits", [])
                if isinstance(row, dict)
            ]
            if delivered and estimated:
                if delivered <= estimated:
                    verdict, complete = "on_time", True
                elif carrier and limits and all(limit is not None for limit in limits):
                    if carrier > max(limits):
                        verdict, complete = "seller_delay", bool(sellers)
                        late_sellers = sorted(sellers)
                    else:
                        verdict, complete = "logistics_delay", True
        order_status = entity["order"].get("order_status")
        if (
            order_status
            and shipment.get("order_status")
            and order_status != shipment["order_status"]
        ):
            ctx.conflict(
                "shipment.order_status",
                ["get_order", "get_shipment_summary"],
                None,
                "UNRESOLVED_STATUS",
            )
        if (
            late_events
            and shipment.get("delivered_customer_at")
            and shipment.get("estimated_delivery_at")
        ):
            delivered = _time(shipment["delivered_customer_at"])
            estimated = _time(shipment["estimated_delivery_at"])
            if delivered and estimated and delivered <= estimated:
                ctx.conflict(
                    "shipment.delivery_lateness",
                    ["get_shipment_summary", "shipment_event"],
                    "shipment_event",
                    "DIRECT_EVENT_PRECEDENCE",
                )
                complete = False
    ctx.handoff("fulfillment-agent", "completed" if shipment_ev and items_ev else "evidence_gap")
    return {
        "verdict": verdict,
        "complete": complete,
        "items": sorted(by_item),
        "sellers": sorted(sellers),
        "late_sellers": late_sellers,
        "expected": expected,
        "freight": freight,
    }


async def _finance(
    ctx: CaseContext, entity: dict[str, Any], fulfillment: dict[str, Any]
) -> dict[str, Any]:
    ctx.assign("finance-agent")
    order_id = entity["order_id"]
    if not order_id:
        ctx.handoff("finance-agent", "identity_gap")
        return {
            "verdict": "insufficient_evidence",
            "captured": None,
            "refunded": None,
            "payment_types": set(),
            "references": [],
            "issues": [],
            "capture_verdict": "insufficient_evidence",
        }
    payments_ev = await ctx.fetch("payments", order_id)
    timeline_ev = await ctx.fetch("payment_timeline", order_id)
    refunds_ev = await ctx.fetch("refunds", order_id)
    for ev in (payments_ev, timeline_ev, refunds_ev):
        ctx.consume(ev, "finance-agent")
    rows = (
        [row for row in payments_ev.data if row.get("order_id") == order_id] if payments_ev else []
    )
    if not rows and timeline_ev:
        rows = [
            row
            for row in timeline_ev.data.get("payments", [])
            if isinstance(row, dict) and row.get("order_id") == order_id
        ]
    types = {row.get("payment_type") for row in rows if isinstance(row.get("payment_type"), str)}
    events = timeline_ev.data.get("events", []) if timeline_ev else []
    events = [row for row in events if isinstance(row, dict) and row.get("order_id") == order_id]
    captures = [
        row
        for row in events
        if row.get("event_type") == "captured"
        and row.get("status") == "confirmed"
        and _money(row.get("amount_brl")) is not None
        and _time(row.get("event_at")) is not None
    ]
    capture_keys = {
        (
            row["transaction_id"]
            if isinstance(row.get("transaction_id"), str)
            else row["event_at"],
            row["amount_brl"],
            row["event_type"],
        )
        for row in captures
    }
    captured = sum(_money(amount) for _, amount, _ in capture_keys) if captures else None
    capture_values: dict[str, set[int]] = {}
    for row in captures:
        if isinstance(row.get("transaction_id"), str):
            capture_values.setdefault(row["transaction_id"], set()).add(_money(row["amount_brl"]))
    if any(len(amounts) > 1 for amounts in capture_values.values()):
        captured = None
        ctx.conflict(
            "payment.capture_amount",
            ["payment_event", "payment_event:duplicate_id"],
            None,
            "UNRESOLVED_TRANSACTION_AMOUNT",
        )
    refund_events = refunds_ev.data.get("events", []) if refunds_ev else []
    refund_events = [
        row for row in refund_events if isinstance(row, dict) and row.get("order_id") == order_id
    ]
    refund_groups: dict[str, list[dict[str, Any]]] = {}
    for row in refund_events:
        if _time(row.get("event_at")) is not None:
            key = row["refund_id"] if isinstance(row.get("refund_id"), str) else "unidentified"
            refund_groups.setdefault(key, []).append(row)
    latest_refund_states: set[str] = set()
    for rows_for_refund in refund_groups.values():
        latest_time = max(_time(row["event_at"]) for row in rows_for_refund)
        states_at_latest = {
            row.get("status")
            for row in rows_for_refund
            if _time(row["event_at"]) == latest_time and isinstance(row.get("status"), str)
        }
        if len(states_at_latest) > 1:
            ctx.conflict(
                "refund.current_status",
                ["refund_event", "refund_event:other"],
                None,
                "UNRESOLVED_REFUND_STATE",
            )
        latest_refund_states.update(states_at_latest)
    settled = [
        row
        for row in refund_events
        if row.get("status") in {"settled", "completed", "refunded"}
        and _money(row.get("amount_brl")) is not None
        and _time(row.get("event_at")) is not None
    ]
    settled_keys = {
        (
            row["refund_id"] if isinstance(row.get("refund_id"), str) else row["event_at"],
            row["amount_brl"],
            row["event_type"],
        )
        for row in settled
    }
    refunded = sum(_money(amount) for _, amount, _ in settled_keys) if refunds_ev else None
    refund_values: dict[str, set[int]] = {}
    for row in settled:
        if isinstance(row.get("refund_id"), str):
            refund_values.setdefault(row["refund_id"], set()).add(_money(row["amount_brl"]))
    if any(len(amounts) > 1 for amounts in refund_values.values()):
        refunded = None
        ctx.conflict(
            "refund.settled_amount",
            ["refund_event", "refund_event:duplicate_id"],
            None,
            "UNRESOLVED_REFUND_AMOUNT",
        )
    issues: list[str] = []
    if any(
        row.get("event_type") == "reconciliation_mismatch" and row.get("status") == "open"
        for row in events
    ):
        issues.append("payment_mismatch")
    if captured is not None and fulfillment["expected"] is not None:
        if captured > fulfillment["expected"] and len(capture_keys) > 1:
            issues.append("duplicate_charge")
        elif captured != fulfillment["expected"]:
            issues.append("payment_mismatch")
    if "failed" in latest_refund_states:
        issues.append("refund_failed")
    elif "pending" in latest_refund_states:
        issues.append("refund_pending")
    elif latest_refund_states & {"settled", "completed", "refunded"}:
        issues.append("refunded")
    capture_verdict = "insufficient_evidence"
    if "payment_mismatch" in issues:
        capture_verdict = "capture_mismatch"
    elif "duplicate_charge" in issues:
        capture_verdict = "duplicate_capture"
    elif captured is not None and fulfillment["expected"] == captured:
        capture_verdict = "reconciled"
    verdict = "insufficient_evidence"
    for issue, mapped in (
        ("refund_failed", "refund_failed"),
        ("refund_pending", "refund_pending"),
        ("refunded", "refunded"),
        ("payment_mismatch", "capture_mismatch"),
        ("duplicate_charge", "duplicate_capture"),
    ):
        if issue in issues:
            verdict = mapped
            break
    if (
        verdict == "insufficient_evidence"
        and captured is not None
        and fulfillment["expected"] == captured
    ):
        verdict = "reconciled"
    refs = sorted(
        {row["payment_reference"] for row in rows if isinstance(row.get("payment_reference"), str)}
    )[:20]
    ctx.handoff("finance-agent", "completed" if timeline_ev and refunds_ev else "evidence_gap")
    return {
        "verdict": verdict,
        "captured": captured,
        "refunded": refunded,
        "payment_types": types,
        "references": refs,
        "issues": list(dict.fromkeys(issues)),
        "capture_verdict": capture_verdict,
    }


def _first_claim(
    topic: str,
    entity: dict[str, Any],
    fulfillment: dict[str, Any],
    finance: dict[str, Any],
    ctx: CaseContext,
) -> str:
    shipment = fulfillment["verdict"]
    payment = finance["capture_verdict"]
    status = entity["order"].get("order_status") if entity["order"] else None
    status_conflict = any(c["field"] == "order_status" for c in ctx.conflicts)
    if topic in {"refund_pending", "refund_failed"} and any(
        c["field"] == "refund.current_status" for c in ctx.conflicts
    ):
        return "insufficient_evidence"
    if topic in {"valid_split_payment", "payment_mismatch", "duplicate_charge"} and any(
        c["field"] == "payment.capture_amount" for c in ctx.conflicts
    ):
        return "insufficient_evidence"
    if topic == "late_delivery_seller":
        return (
            "supported"
            if shipment == "seller_delay"
            else "unsupported"
            if shipment in {"on_time", "logistics_delay"}
            else "insufficient_evidence"
        )
    if topic == "late_delivery_logistics":
        return (
            "supported"
            if shipment == "logistics_delay"
            else "unsupported"
            if shipment in {"on_time", "seller_delay"}
            else "insufficient_evidence"
        )
    if topic == "valid_split_payment":
        return (
            "supported"
            if payment == "reconciled" and len(finance["payment_types"]) > 1
            else "unsupported"
            if payment in {"capture_mismatch", "duplicate_capture"}
            else "insufficient_evidence"
        )
    if topic == "payment_mismatch":
        return (
            "supported"
            if "payment_mismatch" in finance["issues"]
            else "unsupported"
            if payment == "reconciled"
            else "insufficient_evidence"
        )
    if topic == "duplicate_charge":
        return (
            "supported"
            if "duplicate_charge" in finance["issues"]
            else "unsupported"
            if payment == "reconciled"
            else "insufficient_evidence"
        )
    if topic == "refund_pending":
        return (
            "supported"
            if "refund_pending" in finance["issues"]
            else "unsupported"
            if finance["verdict"] == "refunded"
            else "insufficient_evidence"
        )
    if topic == "refund_failed":
        return (
            "supported"
            if "refund_failed" in finance["issues"]
            else "unsupported"
            if finance["verdict"] == "refunded"
            else "insufficient_evidence"
        )
    if topic in {"canceled_order_paid", "unavailable_order_paid"}:
        expected_status = "canceled" if topic == "canceled_order_paid" else "unavailable"
        if status_conflict or status is None or finance["captured"] is None:
            return "insufficient_evidence"
        if status == expected_status and finance["captured"] > 0:
            return "supported"
        return "unsupported" if status != expected_status else "insufficient_evidence"
    return "insufficient_evidence"


async def _policy(
    ctx: CaseContext,
    entity: dict[str, Any],
    fulfillment: dict[str, Any],
    finance: dict[str, Any],
    advisor: Any = None,
) -> dict[str, Any]:
    ctx.assign("policy-agent")
    topic = ctx.case["customer_request"]["claims"][0]["topic"]
    first_verdict = _first_claim(topic, entity, fulfillment, finance, ctx)
    issue = topic if first_verdict == "supported" else "insufficient_evidence"
    if topic == "valid_split_payment" and first_verdict == "supported":
        for actionable in (
            "refund_failed",
            "refund_pending",
            "duplicate_charge",
            "payment_mismatch",
        ):
            if actionable in finance["issues"]:
                issue = actionable
                break
    alternatives: list[str] = []
    if first_verdict == "unsupported":
        shipment_issue = {
            "seller_delay": "late_delivery_seller",
            "logistics_delay": "late_delivery_logistics",
        }.get(fulfillment["verdict"])
        alternatives = [
            candidate
            for candidate in [shipment_issue, *finance["issues"]]
            if candidate
            in {
                "late_delivery_seller",
                "late_delivery_logistics",
                "payment_mismatch",
                "duplicate_charge",
                "refund_pending",
                "refund_failed",
            }
        ]
        issue = alternatives[0] if alternatives else "unsupported_claim"
    else:
        alternatives = [issue, *finance["issues"]]
    candidates = list(dict.fromkeys(alternatives))
    if advisor is not None and len(candidates) > 1:
        prior_calls = advisor.calls
        selected = await advisor.choose_issue(
            candidates,
            {
                "claim_topic": topic,
                "claim_verdict": first_verdict,
                "shipment_verdict": fulfillment["verdict"],
                "verified_payment_issues": sorted(set(finance["issues"])),
                "has_material_conflicts": bool(ctx.conflicts),
            },
        )
        if advisor.calls > prior_calls:
            ctx.emit(
                "handoff",
                "nvidia-advisor",
                decision_code="selected" if selected in candidates else "fallback",
                attributes={"model": advisor.model},
            )
        if selected in candidates:
            issue = selected
    policy_ev = await ctx.fetch("policy", ctx.case["policy_version"])
    rule = None
    if policy_ev and policy_ev.data.get("currency") == "BRL":
        ctx.consume(policy_ev, "policy-agent")
        maybe = policy_ev.data["rules"].get(issue)
        if (
            isinstance(maybe, dict)
            and maybe.get("case_status") in {"action_required", "no_action", "needs_investigation"}
            and isinstance(maybe.get("recommended_action"), str)
            and _money(maybe.get("refund_brl")) is not None
        ):
            rule = maybe
            ctx.emit("policy_decided", "policy-agent", decision_code=issue)
    if not rule:
        ctx.gaps.append("policy_rule_unavailable")
    captured, refunded = finance["captured"], finance["refunded"]
    remaining = (
        max(0, captured - refunded) if captured is not None and refunded is not None else None
    )
    policy_amount = _money(rule["refund_brl"]) if rule else None
    refundable = (
        min(policy_amount, remaining)
        if policy_amount is not None and remaining is not None
        else None
    )
    recommendation = 0
    if (
        rule
        and rule.get("case_status") == "action_required"
        and refundable is not None
        and policy_amount is not None
        and policy_amount > 0
        and refundable > 0
    ):
        recommendation = refundable
    full_verdict = "insufficient_evidence"
    if rule and (policy_amount == 0 or remaining == 0):
        full_verdict = "unsupported"
    elif rule and remaining is not None and policy_amount is not None:
        if policy_amount >= remaining and remaining > 0:
            full_verdict = "supported"
        elif policy_amount > 0:
            full_verdict = "partially_supported"
    ctx.handoff("policy-agent", "completed" if rule else "policy_gap")
    return {
        "issue": issue,
        "first_verdict": first_verdict,
        "full_verdict": full_verdict,
        "rule": rule,
        "refundable": refundable,
        "recommendation": recommendation,
    }


def _draft(
    ctx: CaseContext,
    entity: dict[str, Any],
    fulfillment: dict[str, Any],
    finance: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    issue = policy["issue"]
    rule = policy["rule"]
    amount = policy["recommendation"]
    status = "needs_investigation"
    if rule and issue != "insufficient_evidence":
        if rule.get("case_status") == "no_action":
            status = "no_action"
        elif rule.get("case_status") == "action_required" and (
            amount > 0 or rule.get("recommended_action") == "reconcile_payment"
        ):
            status = "action_required"
    if "conflict_limit_exceeded" in ctx.gaps:
        status = "needs_investigation"
        amount = 0
    action = rule.get("recommended_action") if rule and status != "needs_investigation" else None
    if not isinstance(action, str) or not 1 <= len(action) <= 80:
        if status == "needs_investigation":
            if any("refunds" in gap for gap in ctx.gaps):
                action = "verify_refund_timeline_and_settled_amounts"
            elif any("policy" in gap for gap in ctx.gaps):
                action = "verify_applicable_refund_policy"
            elif not entity["order_id"]:
                action = "verify_order_customer_linkage"
            else:
                action = "verify_missing_payment_or_shipment_evidence"
        else:
            action = None
    actions = [action] if action else []
    if status == "needs_investigation" and not actions:
        actions = ["verify_missing_evidence"]
    refs = sorted(ctx.used)
    claim_ids = [claim["claim_id"] for claim in ctx.case["customer_request"]["claims"]]
    topic = ctx.case["customer_request"]["claims"][0]["topic"]
    first_domains = {"order", "customer", "item"}
    if topic.startswith("late_delivery"):
        first_domains |= {"shipment", "seller"}
    elif topic in {"refund_pending", "refund_failed"}:
        first_domains |= {"refund", "payment"}
    elif topic in {"canceled_order_paid", "unavailable_order_paid"}:
        first_domains |= {"payment", "refund"}
    else:
        first_domains.add("payment")
    first_refs = [ref for ref in refs if ctx.used[ref].domain in first_domains]
    full_refs = [
        ref
        for ref in refs
        if ctx.used[ref].domain in {"order", "item", "payment", "refund", "policy"}
    ]
    claims = [
        {
            "claim_id": claim_ids[0],
            "verdict": policy["first_verdict"],
            "confidence": (0.6 if ctx.conflicts else 0.75)
            if policy["first_verdict"] != "insufficient_evidence"
            else 0.25,
            "evidence_refs": first_refs,
        }
    ]
    for claim_id in claim_ids[1:]:
        claims.append(
            {
                "claim_id": claim_id,
                "verdict": policy["full_verdict"],
                "confidence": (0.55 if ctx.conflicts else 0.7)
                if policy["full_verdict"] != "insufficient_evidence"
                else 0.25,
                "evidence_refs": full_refs,
            }
        )
    confidence = 0.72 if issue != "insufficient_evidence" else 0.25
    if ctx.conflicts:
        confidence = min(confidence, 0.6)
    secondary = [
        found
        for found in finance["issues"]
        if found != issue
        and found in {"payment_mismatch", "duplicate_charge", "refund_pending", "refund_failed"}
    ]
    if policy["first_verdict"] == "supported" and topic != issue:
        secondary.append(topic)
    shipment_issue = {
        "seller_delay": "late_delivery_seller",
        "logistics_delay": "late_delivery_logistics",
    }.get(fulfillment["verdict"])
    if shipment_issue and shipment_issue != issue and shipment_issue not in secondary:
        secondary.append(shipment_issue)
    parties = []
    if rule:
        raw_parties = rule.get("responsible_parties")
        for party in raw_parties if isinstance(raw_parties, list) else []:
            if not isinstance(party, dict) or party.get("party_type") not in {
                "seller",
                "platform",
                "logistics_provider",
                "payment_provider",
                "customer",
                "unknown",
            }:
                continue
            party_id = party.get("party_id")
            if party["party_type"] == "seller" and party_id not in fulfillment["sellers"]:
                continue
            parties.append({"party_type": party["party_type"], "party_id": party_id})
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": secondary[:10],
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [entity["order_id"]] if entity["order_id"] else [],
            "item_ids": fulfillment["items"],
            "seller_ids": fulfillment["sellers"],
            "payment_references": finance["references"],
            "shipment_ids": [],
        },
        "claim_assessments": claims,
        "entity_resolution": {
            "status": entity["status"],
            "resolved_order_ids": [entity["order_id"]] if entity["order_id"] else [],
            "rejected_candidates": entity["rejected"],
            "confidence": entity["confidence"],
        },
        "customer_context": {
            "customer_unique_id": entity["customer_unique_id"],
            "related_order_ids": entity["related"],
        },
        "shipment_analysis": {
            "verdict": fulfillment["verdict"],
            "late_seller_ids": fulfillment["late_sellers"],
            "timeline_complete": fulfillment["complete"],
        },
        "payment_analysis": {
            "verdict": finance["verdict"],
            "captured_total_brl": _brl(finance["captured"]),
            "refunded_total_brl": _brl(finance["refunded"]),
            "refundable_total_brl": _brl(policy["refundable"]),
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}]
            if issue != "insufficient_evidence"
            else [],
            "responsible_parties": parties[:5],
        },
        "evidence_refs": refs[:30],
        "data_conflicts": ctx.conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": _brl(amount),
            "refund_lines": [
                {"reason_code": issue, "amount_brl": _brl(amount), "entity_id": entity["order_id"]}
            ]
            if amount
            else [],
        },
        "resolution_actions": actions,
    }


def _verify(ctx: CaseContext, output: dict[str, Any]) -> None:
    ctx.trace.contracts.validate_output(output, f"case {ctx.case_id}")
    if output["case_id"] != ctx.case_id:
        raise ValueError("case_id mismatch")
    expected_claims = [claim["claim_id"] for claim in ctx.case["customer_request"]["claims"]]
    if [claim["claim_id"] for claim in output["claim_assessments"]] != expected_claims:
        raise ValueError("claim ID mismatch")
    refs = set(output["evidence_refs"])
    if not refs.issubset(ctx.ledger) or not refs.issubset(ctx.used):
        raise ValueError("unconsumed or unknown evidence ref")
    if any(
        ctx.ledger[ref].case_id != ctx.case_id or not ctx.ledger[ref].arguments_digest
        for ref in refs
    ):
        raise ValueError("evidence request scope mismatch")
    if any(not set(claim["evidence_refs"]).issubset(refs) for claim in output["claim_assessments"]):
        raise ValueError("claim ref outside case output")
    if set(output["entity_resolution"]["rejected_candidates"]) & set(
        output["entity_resolution"]["resolved_order_ids"]
    ):
        raise ValueError("resolved candidate also rejected")
    if not set(output["entity_resolution"]["rejected_candidates"]).issubset(
        ctx.case["candidate_order_ids"]
    ):
        raise ValueError("rejected candidate outside input")
    if not set(output["entity_resolution"]["resolved_order_ids"]).issubset(
        output["affected_entities"]["order_ids"]
    ):
        raise ValueError("resolved order missing from affected entities")
    if not set(output["shipment_analysis"]["late_seller_ids"]).issubset(
        output["affected_entities"]["seller_ids"]
    ):
        raise ValueError("late seller lacks entity linkage")
    ranks = [cause["rank"] for cause in output["root_cause_analysis"]["ranked_causes"]]
    if len(ranks) != len(set(ranks)):
        raise ValueError("duplicate cause rank")
    refund = output["financial_resolution"]
    if _money(refund["recommended_refund_brl"]) != sum(
        _money(line["amount_brl"]) for line in refund["refund_lines"]
    ):
        raise ValueError("refund lines do not sum")
    if (
        refund["recommended_refund_brl"] > 0
        and output["assessment"]["case_status"] != "action_required"
    ):
        raise ValueError("refund without required action")
    if refund["recommended_refund_brl"] > 0:
        payment = output["payment_analysis"]
        captured = _money(payment["captured_total_brl"])
        refunded = _money(payment["refunded_total_brl"])
        amount = _money(refund["recommended_refund_brl"])
        if captured is None or refunded is None or amount > captured - refunded:
            raise ValueError("refund exceeds supported remaining capture")
        if not any(ctx.used[ref].domain == "policy" for ref in refs):
            raise ValueError("refund lacks policy evidence")
        if any(
            line["entity_id"] not in output["affected_entities"]["order_ids"]
            for line in refund["refund_lines"]
        ):
            raise ValueError("refund line lacks order linkage")


def _safe_draft(ctx: CaseContext, entity: dict[str, Any]) -> dict[str, Any]:
    identity_refs = sorted(
        ref for ref, evidence in ctx.used.items() if evidence.domain in {"order", "customer"}
    )
    order_id = entity["order_id"]
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": 0.2,
        },
        "affected_entities": {
            "order_ids": [order_id] if order_id else [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": [
            {
                "claim_id": claim["claim_id"],
                "verdict": "insufficient_evidence",
                "confidence": 0.2,
                "evidence_refs": identity_refs,
            }
            for claim in ctx.case["customer_request"]["claims"]
        ],
        "entity_resolution": {
            "status": entity["status"],
            "resolved_order_ids": [order_id] if order_id else [],
            "rejected_candidates": entity["rejected"],
            "confidence": entity["confidence"],
        },
        "customer_context": {
            "customer_unique_id": entity["customer_unique_id"],
            "related_order_ids": entity["related"],
        },
        "shipment_analysis": {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        },
        "payment_analysis": {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": identity_refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": ["verify_decisive_case_evidence"],
    }


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter, advisor: Any = None
) -> dict[str, Any]:
    """Investigate one case with discovered, scoped MCP evidence and verified handoffs."""
    normalized = _normalize(case)
    tools = _bind_tools(await gateway.describe_tools())
    ctx = CaseContext(normalized, gateway, trace, tools)
    entity = await _entity(ctx)
    fulfillment = await _fulfillment(ctx, entity)
    finance = await _finance(ctx, entity, fulfillment)
    policy = await _policy(ctx, entity, fulfillment, finance, advisor)
    draft = _draft(ctx, entity, fulfillment, finance, policy)
    ctx.assign("verifier")
    try:
        _verify(ctx, draft)
        code = "passed"
    except ValueError:
        draft = _safe_draft(ctx, entity)
        _verify(ctx, draft)
        code = "degraded"
    ctx.emit("verification_completed", "verifier", decision_code=code)
    ctx.handoff("verifier", code)
    return draft
