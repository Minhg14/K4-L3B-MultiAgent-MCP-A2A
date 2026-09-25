from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import sys

from student_agent.contracts import Contracts
from student_agent.openrouter import OpenRouterAdvisor
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

sys.path.insert(0, str(Path(__file__).parent))
from test_workflow import FakeGateway, sample_case, sample_records


def test_openrouter_advisor_init() -> None:
    advisor = OpenRouterAdvisor("sk-or-test-key", model="liquid/lfm-2.5-2.6b:free")
    assert advisor.model == "liquid/lfm-2.5-2.6b:free"
    assert advisor.max_calls == 100
    assert advisor.actor_name == "openrouter-advisor"
    assert not advisor.disabled


def test_openrouter_extract_choice_direct_json() -> None:
    advisor = OpenRouterAdvisor("sk-or-test-key")
    choices = ["late_delivery_seller", "late_delivery_logistics"]
    assert advisor._extract_choice('{"primary_issue": "late_delivery_seller"}', choices) == "late_delivery_seller"


def test_openrouter_extract_choice_markdown_fence() -> None:
    advisor = OpenRouterAdvisor("sk-or-test-key")
    choices = ["late_delivery_seller", "late_delivery_logistics"]
    raw = 'Here is the decision:\n```json\n{"primary_issue": "late_delivery_logistics"}\n```'
    assert advisor._extract_choice(raw, choices) == "late_delivery_logistics"


def test_openrouter_extract_choice_fallback_single_match() -> None:
    advisor = OpenRouterAdvisor("sk-or-test-key")
    choices = ["late_delivery_seller", "payment_mismatch"]
    raw = "Based on the evidence, the primary issue is late_delivery_seller."
    assert advisor._extract_choice(raw, choices) == "late_delivery_seller"


def test_openrouter_extract_choice_rejects_unknown() -> None:
    advisor = OpenRouterAdvisor("sk-or-test-key")
    choices = ["late_delivery_seller", "payment_mismatch"]
    raw = '{"primary_issue": "unrelated_issue"}'
    assert advisor._extract_choice(raw, choices) is None


def test_workflow_with_mock_openrouter_advisor(tmp_path: Path) -> None:
    class MockOpenRouterAdvisor:
        def __init__(self) -> None:
            self.model = "liquid/lfm-2.5-2.6b:free"
            self.calls = 0
            self.actor_name = "openrouter-advisor"

        async def choose_issue(self, candidates: list[str], facts: dict[str, Any]) -> str | None:
            self.calls += 1
            return candidates[0] if candidates else None

    advisor = MockOpenRouterAdvisor()
    records = sample_records()
    records[("get_payment_timeline", "ORDER_1")][1]["events"].append(
        {
            "order_id": "ORDER_1",
            "event_at": "2018-02-20T10:00:00-03:00",
            "event_type": "reconciliation_mismatch",
            "status": "open",
        }
    )
    gateway = FakeGateway(records)
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    case = sample_case()
    case["customer_request"]["claims"][0]["topic"] = "late_delivery_seller"

    output = asyncio.run(solve_case(case, gateway, TraceWriter(trace_path, contracts), advisor=advisor))
    contracts.validate_output(output, "mock openrouter case")
    events = trace_path.read_text().splitlines()
    assert any("openrouter-advisor" in ev for ev in events)
