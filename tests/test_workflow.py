from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import TOOL_ROLES, _normalize, solve_case


class FakeGateway:
    def __init__(self, records: dict[tuple[str, str], tuple[str, Any]]) -> None:
        self.records = records
        self.calls: list[tuple[str, str, str]] = []

    async def describe_tools(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "input_schema": {
                    "type": "object",
                    "properties": {"case_id": {"type": "string"}, argument: {"type": "string"}},
                    "required": ["case_id", argument],
                }
            }
            for name, argument, _ in TOOL_ROLES.values()
        }

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        assert len(arguments) == 1
        value = next(iter(arguments.values()))
        self.calls.append((case_id, tool_name, value))
        record = self.records.get((tool_name, value))
        if record is None:
            raise RuntimeError("no documented result")
        domain, data = record
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{len(self.calls):024d}",
            "result_hash": "sha256:" + "0" * 64,
            "domain": domain,
            "data": data,
        }


def sample_case() -> dict[str, Any]:
    return {
        "case_id": "L3B_CASE_901",
        "opened_at": "2018-02-20T09:00:00-03:00",
        "customer_request": {
            "claimed_order_id": "ORDER_1",
            "claims": [
                {"claim_id": "claim-a", "topic": "late_delivery_logistics"},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V2",
        "candidate_order_ids": ["ORDER_1", "WRONG_2"],
        "customer_unique_id_hint": "CUSTOMER_1",
        "investigation_scope": {
            "include_customer_history": True,
            "include_product_context": True,
            "require_independent_verification": True,
        },
    }


def sample_records() -> dict[tuple[str, str], tuple[str, Any]]:
    order = {"order_id": "ORDER_1", "customer_id": "ROW_1", "order_status": "delivered"}
    return {
        ("get_order", "ORDER_1"): ("order", order),
        ("get_customer_history", "CUSTOMER_1"): (
            "customer",
            {"customer_unique_id": "CUSTOMER_1", "orders": [order]},
        ),
        ("get_order_items", "ORDER_1"): (
            "item",
            [
                {
                    "order_id": "ORDER_1",
                    "order_item_id": "ITEM_1",
                    "seller_id": "SELLER_1",
                    "price": "80.00",
                    "freight_value": "10.00",
                }
            ],
        ),
        ("get_product_context", "ORDER_1"): ("product", [{"order_item_id": "ITEM_1"}]),
        ("get_shipment_summary", "ORDER_1"): (
            "shipment",
            {
                "order_id": "ORDER_1",
                "order_status": "delivered",
                "delivered_carrier_at": "2018-02-21T09:00:00-03:00",
                "delivered_customer_at": "2018-02-25T09:00:00-03:00",
                "estimated_delivery_at": "2018-02-24T09:00:00-03:00",
                "events": [
                    {
                        "order_id": "ORDER_1",
                        "event_at": "2018-02-25T09:00:00-03:00",
                        "event_type": "delivered_late",
                        "actor": "logistics_provider",
                        "status": "confirmed",
                    }
                ],
                "shipping_limits": [],
            },
        ),
        ("get_order_payments", "ORDER_1"): (
            "payment",
            [{"order_id": "ORDER_1", "payment_type": "credit_card", "payment_value": "90.00"}],
        ),
        ("get_payment_timeline", "ORDER_1"): (
            "payment",
            {
                "order_id": "ORDER_1",
                "events": [
                    {
                        "order_id": "ORDER_1",
                        "event_at": "2018-02-20T10:00:00-03:00",
                        "event_type": "captured",
                        "status": "confirmed",
                        "amount_brl": "90.00",
                    }
                ],
            },
        ),
        ("get_refund_timeline", "ORDER_1"): ("refund", {"order_id": "ORDER_1", "events": []}),
        ("get_policy", "EC_POLICY_V2"): (
            "policy",
            {
                "policy_version": "EC_POLICY_V2",
                "currency": "BRL",
                "rules": {
                    "late_delivery_logistics": {
                        "case_status": "action_required",
                        "recommended_action": "refund_freight",
                        "refund_brl": "10.00",
                        "responsible_parties": [
                            {"party_type": "logistics_provider", "party_id": None}
                        ],
                    }
                },
            },
        ),
    }


def run_case(
    tmp_path: Path, records: dict[tuple[str, str], tuple[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]], FakeGateway]:
    gateway = FakeGateway(records)
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    output = asyncio.run(solve_case(sample_case(), gateway, TraceWriter(trace_path, contracts)))
    contracts.validate_output(output, "synthetic case")
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    return output, events, gateway


def test_supported_delivery_and_partial_refund_use_scoped_evidence(tmp_path: Path) -> None:
    output, events, gateway = run_case(tmp_path, sample_records())
    assert output["entity_resolution"]["status"] == "resolved"
    assert output["entity_resolution"]["rejected_candidates"] == []
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["case_status"] == "action_required"
    assert [claim["verdict"] for claim in output["claim_assessments"]] == [
        "supported",
        "partially_supported",
    ]
    assert output["financial_resolution"]["recommended_refund_brl"] == 10
    assert output["payment_analysis"]["captured_total_brl"] == 90
    assert events[-2]["event_type"] == "verification_completed"
    consumed = {
        ref
        for event in events
        if event["event_type"] == "tool_result_consumed"
        for ref in event["evidence_refs"]
    }
    assert set(output["evidence_refs"]) == consumed
    assert {case_id for case_id, _, _ in gateway.calls} == {sample_case()["case_id"]}
    assert len(gateway.calls) <= 18


def test_missing_refund_timeline_holds_amount(tmp_path: Path) -> None:
    records = sample_records()
    del records[("get_refund_timeline", "ORDER_1")]
    output, _, _ = run_case(tmp_path, records)
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["payment_analysis"]["refunded_total_brl"] is None
    assert output["resolution_actions"] == ["verify_refund_timeline_and_settled_amounts"]


def test_unresolved_identity_stops_order_scoped_calls(tmp_path: Path) -> None:
    records = sample_records()
    del records[("get_customer_history", "CUSTOMER_1")]
    output, _, gateway = run_case(tmp_path, records)
    assert output["entity_resolution"]["status"] == "ambiguous"
    assert output["affected_entities"]["order_ids"] == []
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert [claim["verdict"] for claim in output["claim_assessments"]] == [
        "insufficient_evidence",
        "insufficient_evidence",
    ]
    assert not any(
        name in {"get_order_items", "get_payment_timeline", "get_shipment_summary"}
        for _, name, _ in gateway.calls
    )


def test_all_supplied_cases_normalize_and_duplicate_claims_fail() -> None:
    root = Path(__file__).resolve().parents[1]
    files = sorted((root / "inputs" / "l3b-inputs-v1" / "inputs").glob("*.json"))
    if not files:
        pytest.skip("released input set is not installed")
    assert len(files) == 100
    for path in files:
        assert _normalize(json.loads(path.read_text()))["case_id"] == path.stem
    malformed = sample_case()
    malformed["customer_request"]["claims"][1]["claim_id"] = "claim-a"
    try:
        _normalize(malformed)
    except ValueError as exc:
        assert "duplicate claim_id" in str(exc)
    else:
        raise AssertionError("duplicate claims were accepted")


def test_distinct_duplicate_capture_has_bounded_refund(tmp_path: Path) -> None:
    records = sample_records()
    _, timeline = records[("get_payment_timeline", "ORDER_1")]
    timeline["events"].append(
        {
            "order_id": "ORDER_1",
            "event_at": "2018-02-20T11:00:00-03:00",
            "event_type": "captured",
            "status": "confirmed",
            "amount_brl": "30.00",
        }
    )
    _, policy = records[("get_policy", "EC_POLICY_V2")]
    policy["rules"]["duplicate_charge"] = {
        "case_status": "action_required",
        "recommended_action": "refund_duplicate_charge",
        "refund_brl": "30.00",
        "responsible_parties": [],
    }
    case = sample_case()
    case["customer_request"]["claims"][0]["topic"] = "duplicate_charge"
    gateway = FakeGateway(records)
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    output = asyncio.run(
        solve_case(case, gateway, TraceWriter(tmp_path / "trace.jsonl", contracts))
    )
    assert output["assessment"]["primary_issue"] == "duplicate_charge"
    assert output["payment_analysis"]["verdict"] == "duplicate_capture"
    assert output["financial_resolution"]["recommended_refund_brl"] == 30
    assert sum(x["amount_brl"] for x in output["financial_resolution"]["refund_lines"]) == 30


def test_conflicting_shipment_events_block_delivery_claim(tmp_path: Path) -> None:
    records = sample_records()
    _, shipment = records[("get_shipment_summary", "ORDER_1")]
    shipment["events"].append(
        {
            "order_id": "ORDER_1",
            "event_at": "2018-02-25T10:00:00-03:00",
            "event_type": "delivered_late",
            "actor": "seller",
            "status": "confirmed",
        }
    )
    output, _, _ = run_case(tmp_path, records)
    assert output["shipment_analysis"]["verdict"] == "conflicting"
    assert output["claim_assessments"][0]["verdict"] == "insufficient_evidence"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["data_conflicts"]


def test_wrong_domain_response_does_not_enter_output(tmp_path: Path) -> None:
    records = sample_records()
    _, refund_data = records[("get_refund_timeline", "ORDER_1")]
    records[("get_refund_timeline", "ORDER_1")] = ("payment", refund_data)
    output, events, _ = run_case(tmp_path, records)
    assert output["payment_analysis"]["refunded_total_brl"] is None
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert len(output["evidence_refs"]) == sum(
        len(event.get("evidence_refs", []))
        for event in events
        if event["event_type"] == "tool_result_consumed"
    )


def test_positive_customer_mismatch_rejects_wrong_candidate(tmp_path: Path) -> None:
    records = sample_records()
    records[("get_order", "WRONG_2")] = (
        "order",
        {"order_id": "WRONG_2", "customer_id": "OTHER_ROW", "order_status": "delivered"},
    )
    _, history = records[("get_customer_history", "CUSTOMER_1")]
    history["orders"].append({"order_id": "WRONG_2", "customer_id": "DIFFERENT_ROW"})
    output, _, _ = run_case(tmp_path, records)
    assert output["entity_resolution"]["resolved_order_ids"] == ["ORDER_1"]
    assert output["entity_resolution"]["rejected_candidates"] == ["WRONG_2"]


def test_failed_refund_keeps_claim_separate_from_full_request(tmp_path: Path) -> None:
    records = sample_records()
    records[("get_refund_timeline", "ORDER_1")] = (
        "refund",
        {
            "order_id": "ORDER_1",
            "events": [
                {
                    "order_id": "ORDER_1",
                    "event_at": "2018-02-26T09:00:00-03:00",
                    "event_type": "refund_requested",
                    "status": "failed",
                    "amount_brl": "40.00",
                }
            ],
        },
    )
    _, policy = records[("get_policy", "EC_POLICY_V2")]
    policy["rules"]["refund_failed"] = {
        "case_status": "action_required",
        "recommended_action": "retry_refund",
        "refund_brl": "40.00",
        "responsible_parties": [],
    }
    case = sample_case()
    case["customer_request"]["claims"][0]["topic"] = "refund_failed"
    gateway = FakeGateway(records)
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    output = asyncio.run(
        solve_case(case, gateway, TraceWriter(tmp_path / "trace.jsonl", contracts))
    )
    assert output["payment_analysis"]["verdict"] == "refund_failed"
    assert [claim["verdict"] for claim in output["claim_assessments"]] == [
        "supported",
        "partially_supported",
    ]
    assert output["financial_resolution"]["recommended_refund_brl"] == 40


def test_refuted_claim_uses_verified_alternative_issue(tmp_path: Path) -> None:
    case = sample_case()
    case["customer_request"]["claims"][0]["topic"] = "late_delivery_seller"
    records = sample_records()
    gateway = FakeGateway(records)
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    output = asyncio.run(
        solve_case(case, gateway, TraceWriter(tmp_path / "trace.jsonl", contracts))
    )
    assert output["claim_assessments"][0]["verdict"] == "unsupported"
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["case_status"] == "action_required"


def test_split_payment_claim_is_independent_of_failed_refund(tmp_path: Path) -> None:
    case = sample_case()
    case["customer_request"]["claims"][0]["topic"] = "valid_split_payment"
    records = sample_records()
    records[("get_order_payments", "ORDER_1")] = (
        "payment",
        [
            {"order_id": "ORDER_1", "payment_type": "credit_card", "payment_value": "45.00"},
            {"order_id": "ORDER_1", "payment_type": "voucher", "payment_value": "45.00"},
        ],
    )
    records[("get_payment_timeline", "ORDER_1")] = (
        "payment",
        {
            "order_id": "ORDER_1",
            "events": [
                {
                    "order_id": "ORDER_1",
                    "event_at": "2018-02-20T10:00:00-03:00",
                    "event_type": "captured",
                    "status": "confirmed",
                    "amount_brl": "45.00",
                },
                {
                    "order_id": "ORDER_1",
                    "event_at": "2018-02-20T11:00:00-03:00",
                    "event_type": "captured",
                    "status": "confirmed",
                    "amount_brl": "45.00",
                },
            ],
        },
    )
    records[("get_refund_timeline", "ORDER_1")] = (
        "refund",
        {
            "order_id": "ORDER_1",
            "events": [
                {
                    "order_id": "ORDER_1",
                    "event_at": "2018-02-26T09:00:00-03:00",
                    "event_type": "refund_requested",
                    "status": "failed",
                    "amount_brl": "10.00",
                }
            ],
        },
    )
    _, policy = records[("get_policy", "EC_POLICY_V2")]
    policy["rules"]["valid_split_payment"] = {
        "case_status": "no_action",
        "recommended_action": "document_no_action",
        "refund_brl": "0.00",
        "responsible_parties": [],
    }
    policy["rules"]["refund_failed"] = {
        "case_status": "action_required",
        "recommended_action": "retry_refund",
        "refund_brl": "10.00",
        "responsible_parties": [],
    }
    gateway = FakeGateway(records)
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    output = asyncio.run(
        solve_case(case, gateway, TraceWriter(tmp_path / "trace.jsonl", contracts))
    )
    assert output["claim_assessments"][0]["verdict"] == "supported"
    assert output["payment_analysis"]["verdict"] == "refund_failed"
    assert output["assessment"]["primary_issue"] == "refund_failed"
    assert "valid_split_payment" in output["assessment"]["secondary_issues"]
    assert output["assessment"]["case_status"] == "action_required"


def test_reused_ref_for_different_requests_is_rejected(tmp_path: Path) -> None:
    class ReusingGateway(FakeGateway):
        async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
            envelope = await super().call(tool_name, case_id=case_id, **arguments)
            envelope["evidence_ref"] = "ev_" + "x" * 24
            return envelope

    gateway = ReusingGateway(sample_records())
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    output = asyncio.run(
        solve_case(sample_case(), gateway, TraceWriter(tmp_path / "trace.jsonl", contracts))
    )
    assert output["entity_resolution"]["status"] == "ambiguous"
    assert output["evidence_refs"] == []


def test_later_refund_settlement_supersedes_failure(tmp_path: Path) -> None:
    case = sample_case()
    case["customer_request"]["claims"][0]["topic"] = "refund_failed"
    records = sample_records()
    records[("get_refund_timeline", "ORDER_1")] = (
        "refund",
        {
            "order_id": "ORDER_1",
            "events": [
                {
                    "order_id": "ORDER_1",
                    "refund_id": "REFUND_1",
                    "event_at": "2018-02-26T09:00:00-03:00",
                    "event_type": "refund_requested",
                    "status": "failed",
                    "amount_brl": "40.00",
                },
                {
                    "order_id": "ORDER_1",
                    "refund_id": "REFUND_1",
                    "event_at": "2018-02-27T09:00:00-03:00",
                    "event_type": "refund_settled",
                    "status": "settled",
                    "amount_brl": "40.00",
                },
            ],
        },
    )
    _, policy = records[("get_policy", "EC_POLICY_V2")]
    policy["rules"]["unsupported_claim"] = {
        "case_status": "no_action",
        "recommended_action": "document_no_action",
        "refund_brl": "0.00",
        "responsible_parties": [],
    }
    gateway = FakeGateway(records)
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    output = asyncio.run(
        solve_case(case, gateway, TraceWriter(tmp_path / "trace.jsonl", contracts))
    )
    assert output["payment_analysis"]["verdict"] == "refunded"
    assert output["payment_analysis"]["refunded_total_brl"] == 40
    assert output["claim_assessments"][0]["verdict"] == "unsupported"
