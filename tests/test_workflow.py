from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


@pytest.fixture
def contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


@pytest.fixture
def trace_writer(tmp_path: Path, contracts: Contracts) -> TraceWriter:
    trace_path = tmp_path / "traces" / "trace.jsonl"
    return TraceWriter(trace_path, contracts)


def test_solve_case_canceled_order(contracts: Contracts, trace_writer: TraceWriter) -> None:
    async def _test() -> None:
        case_id = "L3A_CASE_001"
        case = {
            "case_id": case_id,
            "order_id": "order_abc123",
            "customer_id": "cust_xyz",
            "claims": [{"claim_id": "c_1"}],
        }

        mock_gateway = AsyncMock()
        mock_gateway.list_tools.return_value = ["get_order", "get_payment", "get_shipment"]

        async def mock_call(tool_name: str, *, case_id: str, **kwargs: Any) -> dict[str, Any]:
            if tool_name == "get_order":
                return {
                    "schema_version": "day09-mcp-evidence-v1",
                    "evidence_ref": "ev_0123456789abcdef0123456789",
                    "result_hash": f"sha256:{'a' * 64}",
                    "domain": "order",
                    "data": {
                        "order_id": "order_abc123",
                        "order_status": "canceled",
                        "customer_id": "cust_xyz",
                        "items": [{"item_id": "item_1", "seller_id": "seller_1"}],
                    },
                }
            elif tool_name == "get_payment":
                return {
                    "schema_version": "day09-mcp-evidence-v1",
                    "evidence_ref": "ev_payment123456789abcdef0123",
                    "result_hash": f"sha256:{'b' * 64}",
                    "domain": "payment",
                    "data": [
                        {
                            "payment_reference": "pay_ref_1",
                            "payment_type": "credit_card",
                            "payment_value": 150.0,
                        }
                    ],
                }
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_shipment123456789abcdef012",
                "result_hash": f"sha256:{'c' * 64}",
                "domain": "shipment",
                "data": {},
            }

        mock_gateway.call.side_effect = mock_call

        output = await solve_case(case, mock_gateway, trace_writer)

        # 1. Output must pass l3a-output-v2 JSON schema
        contracts.validate_output(output, "test_output")

        # 2. Check business logic
        assert output["case_id"] == case_id
        assert output["assessment"]["primary_issue"] == "canceled_order_paid"
        assert output["assessment"]["case_status"] == "action_required"
        assert output["financial_resolution"]["recommended_refund_brl"] == 150.0
        assert "ev_0123456789abcdef0123456789" in output["evidence_refs"]
        assert "ev_payment123456789abcdef0123" in output["evidence_refs"]

        # 3. Check trace events
        events = [
            json.loads(line)
            for line in trace_writer.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        event_types = [e["event_type"] for e in events]
        assert "task_assigned" in event_types
        assert "tool_result_consumed" in event_types
        assert "handoff" in event_types
        assert "policy_decided" in event_types
        assert "verification_completed" in event_types

    asyncio.run(_test())


def test_solve_case_unsupported_claim(contracts: Contracts, trace_writer: TraceWriter) -> None:
    async def _test() -> None:
        case_id = "L3A_CASE_002"
        case = {
            "case_id": case_id,
            "order_id": "order_ontime",
        }

        mock_gateway = AsyncMock()
        mock_gateway.list_tools.return_value = ["get_order", "get_payment"]

        async def mock_call(tool_name: str, *, case_id: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_ontime123456789abcdef01234",
                "result_hash": f"sha256:{'d' * 64}",
                "domain": "order",
                "data": {
                    "order_id": "order_ontime",
                    "order_status": "delivered",
                    "order_delivered_customer_date": "2023-01-10 10:00:00",
                    "order_estimated_delivery_date": "2023-01-15 10:00:00",
                },
            }

        mock_gateway.call.side_effect = mock_call

        output = await solve_case(case, mock_gateway, trace_writer)
        contracts.validate_output(output, "test_output")

        assert output["assessment"]["primary_issue"] == "unsupported_claim"
        assert output["assessment"]["case_status"] == "no_action"
        assert output["financial_resolution"]["recommended_refund_brl"] == 0.0

    asyncio.run(_test())


def test_solve_case_no_evidence_fallback(contracts: Contracts, trace_writer: TraceWriter) -> None:
    async def _test() -> None:
        case_id = "L3A_CASE_003"
        case = {
            "case_id": case_id,
            "order_id": "order_nonexistent",
        }

        mock_gateway = AsyncMock()
        mock_gateway.list_tools.return_value = ["get_order"]
        mock_gateway.call.side_effect = RuntimeError("Order not found")

        output = await solve_case(case, mock_gateway, trace_writer)
        contracts.validate_output(output, "test_output")

        assert output["assessment"]["primary_issue"] == "insufficient_evidence"
        assert output["assessment"]["case_status"] == "needs_investigation"

    asyncio.run(_test())
