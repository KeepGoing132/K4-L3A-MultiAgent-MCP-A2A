from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

logger = logging.getLogger(__name__)


def _parse_datetime(val: Any) -> datetime | None:
    if not val or not isinstance(val, str):
        return None
    cleaned = val.strip().replace(" ", "T")
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1]
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


@dataclass
class CaseContext:
    case_id: str
    case_raw: dict[str, Any]
    discovered_tools: list[str] = field(default_factory=list)
    order_ids: set[str] = field(default_factory=set)
    item_ids: set[str] = field(default_factory=set)
    seller_ids: set[str] = field(default_factory=set)
    payment_references: set[str] = field(default_factory=set)
    shipment_ids: set[str] = field(default_factory=set)
    customer_ids: set[str] = field(default_factory=set)

    evidence_refs: list[str] = field(default_factory=list)
    evidence_by_domain: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    data_conflicts: list[dict[str, Any]] = field(default_factory=list)
    claims: list[dict[str, Any]] = field(default_factory=list)

    # Findings
    order_data: dict[str, Any] | None = None
    items_data: list[dict[str, Any]] = field(default_factory=list)
    payment_data: list[dict[str, Any]] = field(default_factory=list)
    shipment_data: dict[str, Any] | None = None
    policy_data: dict[str, Any] | None = None

    # Assessment
    primary_issue: str = "insufficient_evidence"
    case_status: str = "needs_investigation"
    confidence: float = 0.5
    ranked_causes: list[dict[str, Any]] = field(default_factory=list)
    responsible_parties: list[dict[str, Any]] = field(default_factory=list)
    recommended_refund_brl: float = 0.0
    refund_lines: list[dict[str, Any]] = field(default_factory=list)
    resolution_actions: list[str] = field(default_factory=list)
    claim_assessments: list[dict[str, Any]] = field(default_factory=list)


class ToolInvoker:
    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter, case_id: str) -> None:
        self.gateway = gateway
        self.trace = trace
        self.case_id = case_id
        self._tools_cache: dict[str, Any] | None = None

    async def get_tool_names(self) -> list[str]:
        return await self.gateway.list_tools()

    async def call_tool_safe(
        self,
        tool_name: str,
        actor: str,
        context: CaseContext,
        **arguments: Any,
    ) -> dict[str, Any] | None:
        str_args: dict[str, str] = {}
        for k, v in arguments.items():
            if v is not None:
                str_args[k] = str(v)

        try:
            evidence = await self.gateway.call(tool_name, case_id=self.case_id, **str_args)
        except Exception as exc:
            logger.debug(f"Tool {tool_name} call failed: {exc}")
            return None

        evidence_ref = evidence.get("evidence_ref")
        if evidence_ref and evidence_ref not in context.evidence_refs:
            context.evidence_refs.append(evidence_ref)

        domain = evidence.get("domain", "order")
        context.evidence_by_domain.setdefault(domain, []).append(evidence)

        # Emit observable tool_result_consumed trace
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[evidence_ref] if evidence_ref else None,
            attributes={"domain": domain},
        )
        return evidence


class OrderSpecialist:
    """Specialist agent responsible for order and item data investigation."""

    def __init__(self, invoker: ToolInvoker, trace: TraceWriter) -> None:
        self.invoker = invoker
        self.trace = trace

    async def run(self, context: CaseContext) -> None:
        actor = "order_agent"
        tools = context.discovered_tools

        order_tools = [t for t in tools if "order" in t.lower() and "item" not in t.lower() and "payment" not in t.lower() and "shipment" not in t.lower()]
        item_tools = [t for t in tools if "item" in t.lower()]

        target_order_ids = list(context.order_ids)
        if not target_order_ids:
            # If no explicit order_id in context, look into general tools or fallback
            return

        for order_id in target_order_ids:
            # Call order tool
            for tool_name in (order_tools or ["get_order"]):
                if tool_name in tools:
                    evidence = await self.invoker.call_tool_safe(
                        tool_name, actor, context, order_id=order_id
                    )
                    if evidence and "data" in evidence:
                        context.order_data = evidence["data"]
                        self._extract_entities_from_order(evidence["data"], context)
                        break

            # Call item tool if present
            for tool_name in (item_tools or ["get_order_items", "get_items", "get_item"]):
                if tool_name in tools:
                    evidence = await self.invoker.call_tool_safe(
                        tool_name, actor, context, order_id=order_id
                    )
                    if evidence and "data" in evidence:
                        data = evidence["data"]
                        if isinstance(data, list):
                            context.items_data.extend(data)
                        elif isinstance(data, dict):
                            context.items_data.append(data)
                        self._extract_entities_from_items(context.items_data, context)
                        break

    def _extract_entities_from_order(self, data: dict[str, Any], context: CaseContext) -> None:
        if not isinstance(data, dict):
            return
        if "order_id" in data:
            context.order_ids.add(str(data["order_id"]))
        if "customer_id" in data:
            context.customer_ids.add(str(data["customer_id"]))
        if "items" in data and isinstance(data["items"], list):
            self._extract_entities_from_items(data["items"], context)

    def _extract_entities_from_items(self, items: list[dict[str, Any]], context: CaseContext) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            if "item_id" in item:
                context.item_ids.add(str(item["item_id"]))
            elif "order_item_id" in item:
                context.item_ids.add(str(item["order_item_id"]))
            elif "product_id" in item:
                context.item_ids.add(str(item["product_id"]))
            if "seller_id" in item:
                context.seller_ids.add(str(item["seller_id"]))


class PaymentSpecialist:
    """Specialist agent responsible for payment reconciliation and refund inspection."""

    def __init__(self, invoker: ToolInvoker, trace: TraceWriter) -> None:
        self.invoker = invoker
        self.trace = trace

    async def run(self, context: CaseContext) -> None:
        actor = "payment_agent"
        tools = context.discovered_tools
        payment_tools = [t for t in tools if "payment" in t.lower()]

        for order_id in list(context.order_ids):
            for tool_name in (payment_tools or ["get_payment", "get_order_payments"]):
                if tool_name in tools:
                    evidence = await self.invoker.call_tool_safe(
                        tool_name, actor, context, order_id=order_id
                    )
                    if evidence and "data" in evidence:
                        data = evidence["data"]
                        if isinstance(data, list):
                            context.payment_data.extend(data)
                        elif isinstance(data, dict):
                            if "payments" in data and isinstance(data["payments"], list):
                                context.payment_data.extend(data["payments"])
                            else:
                                context.payment_data.append(data)
                        self._extract_payment_entities(context.payment_data, context)
                        break

    def _extract_payment_entities(self, payments: list[dict[str, Any]], context: CaseContext) -> None:
        for p in payments:
            if not isinstance(p, dict):
                continue
            for ref_key in ("payment_reference", "payment_id", "transaction_id", "payment_sequential"):
                if ref_key in p:
                    context.payment_references.add(str(p[ref_key]))


class ShipmentSpecialist:
    """Specialist agent responsible for shipment logistics and tracking inspection."""

    def __init__(self, invoker: ToolInvoker, trace: TraceWriter) -> None:
        self.invoker = invoker
        self.trace = trace

    async def run(self, context: CaseContext) -> None:
        actor = "shipment_agent"
        tools = context.discovered_tools
        shipment_tools = [t for t in tools if "shipment" in t.lower() or "tracking" in t.lower() or "delivery" in t.lower()]

        for order_id in list(context.order_ids):
            for tool_name in (shipment_tools or ["get_shipment", "get_order_shipment"]):
                if tool_name in tools:
                    evidence = await self.invoker.call_tool_safe(
                        tool_name, actor, context, order_id=order_id
                    )
                    if evidence and "data" in evidence:
                        data = evidence["data"]
                        context.shipment_data = data
                        if isinstance(data, dict):
                            for s_key in ("shipment_id", "tracking_number", "tracking_id"):
                                if s_key in data:
                                    context.shipment_ids.add(str(data[s_key]))
                        break


class PolicySpecialist:
    """Specialist agent responsible for applying policy rules and resolving issues."""

    def __init__(self, invoker: ToolInvoker, trace: TraceWriter) -> None:
        self.invoker = invoker
        self.trace = trace

    async def run(self, context: CaseContext) -> None:
        actor = "policy_agent"
        tools = context.discovered_tools
        policy_tools = [t for t in tools if "policy" in t.lower()]

        for tool_name in policy_tools:
            if tool_name in tools:
                evidence = await self.invoker.call_tool_safe(
                    tool_name, actor, context, topic="complaints"
                )
                if evidence and "data" in evidence:
                    context.policy_data = evidence["data"]
                break

        # Analyze synthesized data
        self._evaluate(context)

        # Emit observable policy_decided trace event
        self.trace.emit(
            case_id=context.case_id,
            event_type="policy_decided",
            actor=actor,
            decision_code=context.primary_issue.upper(),
            evidence_refs=context.evidence_refs[:20] if context.evidence_refs else None,
            attributes={
                "case_status": context.case_status,
                "recommended_refund_brl": float(context.recommended_refund_brl),
            },
        )

    def _evaluate(self, context: CaseContext) -> None:
        order = context.order_data or {}
        payments = context.payment_data or []
        items = context.items_data or []
        shipment = context.shipment_data or {}

        # Fallback values from order object directly if available
        order_status = str(order.get("order_status", "")).lower()
        delivered_customer_str = order.get("order_delivered_customer_date") or shipment.get("delivered_customer_date")
        estimated_delivery_str = order.get("order_estimated_delivery_date") or shipment.get("estimated_delivery_date")
        delivered_carrier_str = order.get("order_delivered_carrier_date") or shipment.get("delivered_carrier_date")

        shipping_limit_str = None
        for itm in items:
            if itm.get("shipping_limit_date"):
                shipping_limit_str = itm.get("shipping_limit_date")
                break

        total_paid = sum(float(p.get("payment_value", 0.0)) for p in payments if isinstance(p, dict))
        if total_paid == 0.0 and "payment_value" in order:
            total_paid = float(order.get("payment_value", 0.0))

        # Check order canceled
        if order_status == "canceled":
            context.primary_issue = "canceled_order_paid"
            context.case_status = "action_required"
            context.confidence = 0.95
            context.recommended_refund_brl = total_paid
            seller_id = next(iter(context.seller_ids), None)
            context.ranked_causes = [
                {"cause_code": "ORDER_CANCELED_BEFORE_SHIPMENT", "rank": 1}
            ]
            context.responsible_parties = [
                {"party_type": "seller", "party_id": seller_id}
            ]
            context.refund_lines = [
                {
                    "reason_code": "FULL_REFUND_CANCELED_ORDER",
                    "amount_brl": total_paid,
                    "entity_id": next(iter(context.order_ids), None),
                }
            ]
            context.resolution_actions = ["PROCESS_REFUND", "NOTIFY_CUSTOMER"]
            return

        # Check unavailable order
        if order_status == "unavailable":
            context.primary_issue = "unavailable_order_paid"
            context.case_status = "action_required"
            context.confidence = 0.95
            context.recommended_refund_brl = total_paid
            seller_id = next(iter(context.seller_ids), None)
            context.ranked_causes = [
                {"cause_code": "ITEM_UNAVAILABLE_AT_SELLER", "rank": 1}
            ]
            context.responsible_parties = [
                {"party_type": "seller", "party_id": seller_id}
            ]
            context.refund_lines = [
                {
                    "reason_code": "FULL_REFUND_UNAVAILABLE_ITEM",
                    "amount_brl": total_paid,
                    "entity_id": next(iter(context.order_ids), None),
                }
            ]
            context.resolution_actions = ["PROCESS_REFUND", "NOTIFY_CUSTOMER"]
            return

        # Check delivery dates
        dt_delivered = _parse_datetime(delivered_customer_str)
        dt_estimated = _parse_datetime(estimated_delivery_str)
        dt_carrier = _parse_datetime(delivered_carrier_str)
        dt_shipping_limit = _parse_datetime(shipping_limit_str)

        if dt_delivered and dt_estimated and dt_delivered > dt_estimated:
            # Late delivery! Check responsible party
            if dt_carrier and dt_shipping_limit and dt_carrier > dt_shipping_limit:
                context.primary_issue = "late_delivery_seller"
                context.ranked_causes = [
                    {"cause_code": "SELLER_DISPATCH_PAST_LIMIT", "rank": 1}
                ]
                context.responsible_parties = [
                    {"party_type": "seller", "party_id": next(iter(context.seller_ids), None)}
                ]
            else:
                context.primary_issue = "late_delivery_logistics"
                context.ranked_causes = [
                    {"cause_code": "CARRIER_TRANSIT_DELAY", "rank": 1}
                ]
                context.responsible_parties = [
                    {"party_type": "logistics_provider", "party_id": next(iter(context.shipment_ids), None)}
                ]
            context.case_status = "action_required"
            context.confidence = 0.92
            context.recommended_refund_brl = 0.0
            context.resolution_actions = ["NOTIFY_CUSTOMER", "UPDATE_SELLER_SCORE"]
            return

        # Check duplicate charge or payment mismatch
        if len(payments) > 1:
            payment_types = [p.get("payment_type") for p in payments if isinstance(p, dict)]
            payment_values = [p.get("payment_value") for p in payments if isinstance(p, dict)]
            if len(payment_values) == 2 and payment_values[0] == payment_values[1] and payment_types[0] == payment_types[1]:
                context.primary_issue = "duplicate_charge"
                context.case_status = "action_required"
                context.confidence = 0.90
                duplicate_val = float(payment_values[0])
                context.recommended_refund_brl = duplicate_val
                context.ranked_causes = [
                    {"cause_code": "PAYMENT_GATEWAY_DUPLICATE_CAPTURE", "rank": 1}
                ]
                context.responsible_parties = [
                    {"party_type": "payment_provider", "party_id": None}
                ]
                context.refund_lines = [
                    {
                        "reason_code": "REFUND_DUPLICATE_CHARGE",
                        "amount_brl": duplicate_val,
                        "entity_id": next(iter(context.payment_references), None),
                    }
                ]
                context.resolution_actions = ["PROCESS_REFUND", "NOTIFY_PAYMENT_PROVIDER"]
                return
            elif "voucher" in payment_types and len(set(payment_types)) > 1:
                context.primary_issue = "valid_split_payment"
                context.case_status = "no_action"
                context.confidence = 0.92
                context.recommended_refund_brl = 0.0
                context.ranked_causes = [
                    {"cause_code": "CUSTOMER_SPLIT_PAYMENT_AUTHORIZED", "rank": 1}
                ]
                context.responsible_parties = [
                    {"party_type": "customer", "party_id": None}
                ]
                context.resolution_actions = ["CLOSE_CLAIM_EXPLAINED"]
                return

        # Check if customer claim is unsupported (e.g. order delivered on time, valid payment)
        if order_status == "delivered" and dt_delivered and dt_estimated and dt_delivered <= dt_estimated:
            context.primary_issue = "unsupported_claim"
            context.case_status = "no_action"
            context.confidence = 0.94
            context.recommended_refund_brl = 0.0
            context.ranked_causes = [
                {"cause_code": "ORDER_FULFILLED_PER_POLICY", "rank": 1}
            ]
            context.responsible_parties = [
                {"party_type": "customer", "party_id": None}
            ]
            context.resolution_actions = ["CLOSE_CLAIM_REJECTED"]
            return

        # Default fallback if insufficient data
        if not context.evidence_refs:
            context.primary_issue = "insufficient_evidence"
            context.case_status = "needs_investigation"
            context.confidence = 0.50
            context.recommended_refund_brl = 0.0
            context.ranked_causes = [
                {"cause_code": "EVIDENCE_RETRIEVAL_INCOMPLETE", "rank": 1}
            ]
            context.responsible_parties = [
                {"party_type": "unknown", "party_id": None}
            ]
            context.resolution_actions = ["ESCALATE_TO_MANUAL_REVIEW"]
        else:
            context.primary_issue = "unsupported_claim"
            context.case_status = "no_action"
            context.confidence = 0.85
            context.recommended_refund_brl = 0.0
            context.ranked_causes = [
                {"cause_code": "CLAIM_NOT_SUBSTANTIATED", "rank": 1}
            ]
            context.responsible_parties = [
                {"party_type": "customer", "party_id": None}
            ]
            context.resolution_actions = ["CLOSE_CLAIM_NO_ACTION"]


class VerifierAgent:
    """Agent responsible for verification invariants, consistency checks, and contract auditing."""

    def __init__(self, trace: TraceWriter) -> None:
        self.trace = trace

    def verify_and_build(self, context: CaseContext) -> dict[str, Any]:
        # Consistency invariant 1: Ensure refund lines sum equals recommended_refund_brl
        refund_sum = sum(line["amount_brl"] for line in context.refund_lines)
        if abs(refund_sum - context.recommended_refund_brl) > 0.01:
            context.recommended_refund_brl = round(refund_sum, 2)

        # Consistency invariant 2: Status consistency
        if context.primary_issue == "unsupported_claim":
            context.case_status = "no_action"
            context.recommended_refund_brl = 0.0
            context.refund_lines = []
        elif context.primary_issue == "insufficient_evidence":
            context.case_status = "needs_investigation"

        if context.case_status == "no_action" and not context.resolution_actions:
            context.resolution_actions = ["CLOSE_CLAIM_NO_ACTION"]
        elif context.case_status == "action_required" and not context.resolution_actions:
            context.resolution_actions = ["PROCESS_REFUND"]

        # Consistency invariant 3: Deduplicate entity lists
        order_ids = sorted(context.order_ids)[:20]
        item_ids = sorted(context.item_ids)[:20]
        seller_ids = sorted(context.seller_ids)[:20]
        payment_refs = sorted(context.payment_references)[:20]
        shipment_ids = sorted(context.shipment_ids)[:20]

        # Invariant 4: Confidence range [0, 1]
        confidence = max(0.0, min(1.0, float(context.confidence)))

        # Build claim assessments if any claims existed in case
        claim_assessments = []
        for claim in context.claims:
            claim_id = str(claim.get("claim_id", "claim_1"))
            verdict = "supported" if context.case_status == "action_required" else "unsupported"
            claim_assessments.append({
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": context.evidence_refs[:30],
            })

        output: dict[str, Any] = {
            "schema_version": "day09-l3a-output-v2",
            "case_id": context.case_id,
            "assessment": {
                "primary_issue": context.primary_issue,
                "case_status": context.case_status,
                "confidence": confidence,
            },
            "affected_entities": {
                "order_ids": order_ids,
                "item_ids": item_ids,
                "seller_ids": seller_ids,
                "payment_references": payment_refs,
                "shipment_ids": shipment_ids,
            },
            "root_cause_analysis": {
                "ranked_causes": context.ranked_causes[:5],
                "responsible_parties": context.responsible_parties[:5],
            },
            "evidence_refs": context.evidence_refs[:30],
            "data_conflicts": context.data_conflicts[:5],
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": round(float(context.recommended_refund_brl), 2),
                "refund_lines": context.refund_lines[:10],
            },
            "resolution_actions": list(dict.fromkeys(context.resolution_actions))[:8],
        }

        if claim_assessments:
            output["claim_assessments"] = claim_assessments[:5]

        # Emit observable verification_completed trace event
        self.trace.emit(
            case_id=context.case_id,
            event_type="verification_completed",
            actor="verifier",
            decision_code="VERIFICATION_PASSED",
            attributes={
                "primary_issue": context.primary_issue,
                "case_status": context.case_status,
                "evidence_count": len(context.evidence_refs),
            },
        )
        return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the multi-agent coordinator, specialists, and verifier workflow."""
    case_id = str(case["case_id"])
    context = CaseContext(case_id=case_id, case_raw=case)

    # Initial entity extraction from case payload
    if "order_id" in case:
        context.order_ids.add(str(case["order_id"]))
    if "order_ids" in case and isinstance(case["order_ids"], list):
        context.order_ids.update(str(x) for x in case["order_ids"])
    if "customer_id" in case:
        context.customer_ids.add(str(case["customer_id"]))
    if "claims" in case and isinstance(case["claims"], list):
        context.claims = list(case["claims"])
    elif "claim_id" in case:
        context.claims = [{"claim_id": case["claim_id"]}]

    invoker = ToolInvoker(gateway, trace, case_id)
    try:
        context.discovered_tools = await invoker.get_tool_names()
    except Exception as exc:
        logger.warning(f"Tool discovery failed for case {case_id}: {exc}")
        context.discovered_tools = []

    # 1. Coordinator assigns task to Order Specialist
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order_agent",
        decision_code="INVESTIGATE_ORDER",
    )
    order_specialist = OrderSpecialist(invoker, trace)
    await order_specialist.run(context)

    # Order Specialist hands off to Payment Specialist
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="order_agent",
        target="payment_agent",
        decision_code="ORDER_INVESTIGATED",
    )

    # 2. Coordinator assigns task to Payment Specialist
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="payment_agent",
        decision_code="INVESTIGATE_PAYMENT",
    )
    payment_specialist = PaymentSpecialist(invoker, trace)
    await payment_specialist.run(context)

    # Payment Specialist hands off to Shipment Specialist
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="payment_agent",
        target="shipment_agent",
        decision_code="PAYMENT_INVESTIGATED",
    )

    # 3. Coordinator assigns task to Shipment Specialist
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="shipment_agent",
        decision_code="INVESTIGATE_SHIPMENT",
    )
    shipment_specialist = ShipmentSpecialist(invoker, trace)
    await shipment_specialist.run(context)

    # Shipment Specialist hands off to Policy Specialist
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="shipment_agent",
        target="policy_agent",
        decision_code="SHIPMENT_INVESTIGATED",
    )

    # 4. Coordinator assigns task to Policy Specialist
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="policy_agent",
        decision_code="EVALUATE_POLICY",
    )
    policy_specialist = PolicySpecialist(invoker, trace)
    await policy_specialist.run(context)

    # Policy Specialist hands off to Verifier
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy_agent",
        target="verifier",
        decision_code="POLICY_FORMULATED",
    )

    # 5. Coordinator assigns task to Verifier
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="verifier",
        decision_code="VERIFY_CASE_OUTPUT",
    )
    verifier = VerifierAgent(trace)
    output = verifier.verify_and_build(context)

    return output
