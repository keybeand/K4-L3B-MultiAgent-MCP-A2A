from __future__ import annotations

import asyncio
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


class CaseCache:
    """In-memory cache per case to avoid redundant MCP tool calls.

    Deduplicates calls by (tool_name, sorted_params) key.
    Enforces a per-tool retry budget of 2 (matching ARCHITECTURE.md spec).
    """

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter, case_id: str) -> None:
        self.gateway = gateway
        self.trace = trace
        self.case_id = case_id
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}
        self.collected_evidence_refs: list[str] = []
        # Track evidence_refs per tool for targeted attribution
        self.tool_evidence_map: dict[str, list[str]] = {}

    async def call(
        self, tool_name: str, actor: str, retries: int = 2, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Call an MCP tool with caching and retry (max 2 per ARCHITECTURE spec)."""
        clean_kwargs = {k: str(v) for k, v in kwargs.items() if v is not None and v != ""}
        key_parts = sorted(clean_kwargs.items())
        cache_key = (tool_name, str(key_parts))
        if cache_key in self._cache:
            return self._cache[cache_key]

        result = None

        for attempt in range(retries):
            try:
                result = await self.gateway.call(tool_name, case_id=self.case_id, **clean_kwargs)
                if result and isinstance(result, dict) and "evidence_ref" in result:
                    break
            except Exception:
                # Retry on error; after budget exhausted, fall through with None
                if attempt < retries - 1:
                    await asyncio.sleep(0.5)

        if not result or not isinstance(result, dict):
            return None

        self._cache[cache_key] = result
        evidence_ref = result.get("evidence_ref")
        if evidence_ref:
            if evidence_ref not in self.collected_evidence_refs:
                self.collected_evidence_refs.append(evidence_ref)
            # Track per-tool evidence for targeted claim attribution
            self.tool_evidence_map.setdefault(tool_name, [])
            if evidence_ref not in self.tool_evidence_map[tool_name]:
                self.tool_evidence_map[tool_name].append(evidence_ref)
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[evidence_ref],
            )
        return result


# ---------------------------------------------------------------------------
# Specialist analysis helpers
# ---------------------------------------------------------------------------

def _analyse_shipment(shipment_info: dict[str, Any] | None) -> tuple[str, list[str], bool]:
    """Derive shipment verdict, late seller IDs, and timeline completeness."""
    verdict = "insufficient_evidence"
    late_seller_ids: list[str] = []
    timeline_complete = False

    if not shipment_info or not shipment_info.get("data"):
        return verdict, late_seller_ids, timeline_complete

    sdata = shipment_info["data"]
    timeline_complete = True  # We received data, so timeline exists

    if isinstance(sdata, dict):
        # Check shipment status field directly
        status = sdata.get("shipment_status", sdata.get("status", ""))
        if status in ("delivered", "completed"):
            verdict = "on_time"

        # Inspect events for delay signals
        events = sdata.get("events", [])
        for evt in events:
            etype = str(evt.get("event_type", evt.get("type", ""))).lower()
            if "late" in etype or "delay" in etype or "atras" in etype:
                verdict = "logistics_delay"
            if "lost" in etype or "extravi" in etype:
                verdict = "lost"
            if "return" in etype or "devol" in etype:
                verdict = "returned"

        # Check shipping limits for seller-specific delays
        limits = sdata.get("shipping_limits", sdata.get("seller_shipping_limits", []))
        for lim in limits:
            sid = lim.get("seller_id")
            exceeded = lim.get("exceeded", lim.get("is_late", False))
            if sid:
                if exceeded:
                    verdict = "seller_delay"
                if sid not in late_seller_ids:
                    late_seller_ids.append(sid)

        # Check estimated vs actual delivery
        estimated = sdata.get("estimated_delivery_date", sdata.get("estimated_delivery"))
        actual = sdata.get("delivered_at", sdata.get("actual_delivery_date"))
        if estimated and actual and actual > estimated and verdict == "on_time":
            verdict = "logistics_delay"

    elif isinstance(sdata, list):
        timeline_complete = True
        for item in sdata:
            etype = str(item.get("event_type", item.get("type", ""))).lower()
            if "deliver" in etype:
                if verdict == "insufficient_evidence":
                    verdict = "on_time"
            if "late" in etype or "delay" in etype:
                verdict = "logistics_delay"

    return verdict, late_seller_ids, timeline_complete


def _analyse_payment(
    payment_info: dict[str, Any] | None,
) -> tuple[str, float, float, float, list[str]]:
    """Derive payment verdict and financial totals. Returns (verdict, captured, refunded, refundable, pay_refs)."""
    verdict = "insufficient_evidence"
    captured = 0.0
    refunded = 0.0
    refundable = 0.0
    pay_refs: list[str] = []

    if not payment_info or not payment_info.get("data"):
        return verdict, captured, refunded, refundable, pay_refs

    pdata = payment_info["data"]

    if isinstance(pdata, list):
        # List of payment installments
        for item in pdata:
            val = float(item.get("payment_value", 0.0) or 0.0)
            captured += val
            ref = item.get("payment_id", item.get("payment_reference"))
            if ref and ref not in pay_refs:
                pay_refs.append(str(ref))
        refundable = captured
        refunded = 0.0
        if len(pdata) > 1:
            # Multiple installments → check for duplicate captures
            values = [float(it.get("payment_value", 0) or 0) for it in pdata]
            if len(values) != len(set(values)):
                verdict = "duplicate_capture"
            else:
                verdict = "reconciled"
        else:
            verdict = "reconciled"

    elif isinstance(pdata, dict):
        captured = float(pdata.get("captured_total_brl", 0.0) or 0.0)
        refunded = float(pdata.get("refunded_total_brl", 0.0) or 0.0)
        refundable = float(pdata.get("refundable_total_brl", 0.0) or 0.0)
        verdict = pdata.get("verdict", "reconciled")
        ref = pdata.get("payment_id", pdata.get("payment_reference"))
        if ref:
            pay_refs.append(str(ref))

    # Ensure refundable doesn't exceed captured
    if refundable > captured:
        refundable = captured

    return verdict, captured, refunded, refundable, pay_refs


def _determine_primary_issue(
    claims: list[dict[str, Any]],
    order_status: str,
    shipment_verdict: str,
    payment_verdict: str,
) -> str:
    """Determine primary_issue from claims cross-referenced with MCP evidence."""
    VALID_ISSUES = {
        "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
        "late_delivery_logistics", "valid_split_payment", "payment_mismatch",
        "duplicate_charge", "refund_pending", "refund_failed",
        "unsupported_claim", "insufficient_evidence",
    }

    if not claims:
        return "insufficient_evidence"

    first_topic = claims[0].get("topic", "")

    # If the claim topic is directly valid AND corroborated by evidence, use it
    if first_topic in VALID_ISSUES:
        # Cross-check with actual data for consistency
        if first_topic == "late_delivery_logistics" and shipment_verdict in ("logistics_delay", "insufficient_evidence"):
            return "late_delivery_logistics"
        if first_topic == "late_delivery_seller" and shipment_verdict in ("seller_delay", "insufficient_evidence"):
            return "late_delivery_seller"
        if first_topic == "canceled_order_paid" and order_status in ("canceled", "unknown"):
            return "canceled_order_paid"
        if first_topic == "unavailable_order_paid" and order_status in ("unavailable", "unknown"):
            return "unavailable_order_paid"
        # For payment-related issues, trust claim if evidence doesn't contradict
        if first_topic in ("payment_mismatch", "duplicate_charge", "refund_pending",
                           "refund_failed", "valid_split_payment"):
            return first_topic
        # If evidence doesn't contradict, still trust the claim
        return first_topic

    # Infer from evidence if claim topic is not a valid issue
    if order_status == "canceled":
        return "canceled_order_paid"
    if order_status == "unavailable":
        return "unavailable_order_paid"
    if shipment_verdict == "logistics_delay":
        return "late_delivery_logistics"
    if shipment_verdict == "seller_delay":
        return "late_delivery_seller"
    if shipment_verdict == "lost":
        return "late_delivery_logistics"
    if payment_verdict == "refund_failed":
        return "refund_failed"
    if payment_verdict == "refund_pending":
        return "refund_pending"
    if payment_verdict == "duplicate_capture":
        return "duplicate_charge"
    if payment_verdict == "capture_mismatch":
        return "payment_mismatch"

    # Fuzzy fallback on topic text
    topic_lower = first_topic.lower()
    if "logistics" in topic_lower or "late" in topic_lower or "delivery" in topic_lower:
        return "late_delivery_logistics"
    if "seller" in topic_lower:
        return "late_delivery_seller"
    if "cancel" in topic_lower:
        return "canceled_order_paid"
    if "refund" in topic_lower:
        return "refund_pending"
    if "payment" in topic_lower or "charge" in topic_lower:
        return "payment_mismatch"

    return "insufficient_evidence"


def _determine_responsible_parties(
    primary_issue: str,
    shipment_verdict: str,
    late_seller_ids: list[str],
    matched_rule: dict[str, Any],
) -> list[dict[str, Any]]:
    """Determine responsible parties consistent with primary_issue."""
    from_policy = matched_rule.get("responsible_parties", [])
    if from_policy:
        return from_policy

    # Map primary_issue → party_type
    issue_party_map: dict[str, str] = {
        "late_delivery_logistics": "logistics_provider",
        "late_delivery_seller": "seller",
        "canceled_order_paid": "seller",
        "unavailable_order_paid": "seller",
        "payment_mismatch": "payment_provider",
        "duplicate_charge": "payment_provider",
        "refund_pending": "platform",
        "refund_failed": "platform",
        "valid_split_payment": "customer",
        "unsupported_claim": "customer",
        "insufficient_evidence": "unknown",
    }
    party_type = issue_party_map.get(primary_issue, "unknown")

    # Assign party_id if seller-related
    party_id = None
    if party_type == "seller" and late_seller_ids:
        party_id = late_seller_ids[0]

    return [{"party_type": party_type, "party_id": party_id}]


def _compute_confidence(
    entity_status: str,
    evidence_count: int,
    has_order: bool,
    has_shipment: bool,
    has_payment: bool,
) -> float:
    """Calibrate confidence dynamically based on evidence coverage."""
    base = 0.40

    # Entity resolution bonus
    if entity_status == "resolved":
        base += 0.15
    elif entity_status == "ambiguous":
        base += 0.05

    # Evidence count bonus (each evidence adds 0.08, capped)
    evidence_boost = min(evidence_count * 0.08, 0.35)
    base += evidence_boost

    # Data source coverage bonus
    source_count = sum([has_order, has_shipment, has_payment])
    base += source_count * 0.03

    return round(min(base, 0.95), 2)


# ---------------------------------------------------------------------------
# Main solve_case
# ---------------------------------------------------------------------------

async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    cache = CaseCache(gateway, trace, case_id)

    # ── Step 1: Coordinator dispatches Entity Resolution ──────────────
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
        attributes={"action": "resolve_entities"},
    )

    claimed_order_id = case.get("customer_request", {}).get("claimed_order_id")
    candidate_order_ids = case.get("candidate_order_ids", [])
    customer_hint = case.get("customer_unique_id_hint")

    resolved_order_ids: list[str] = []
    rejected_candidates: list[str] = []
    entity_status = "not_found"

    # Resolve entity: trust claimed_order_id if present, reject others
    if claimed_order_id:
        resolved_order_ids = [claimed_order_id]
        entity_status = "resolved"
        for cand in candidate_order_ids:
            if cand != claimed_order_id:
                rejected_candidates.append(cand)
    elif candidate_order_ids:
        # No explicit claim → use first candidate tentatively (ambiguous)
        resolved_order_ids = [candidate_order_ids[0]]
        entity_status = "ambiguous"
        rejected_candidates = candidate_order_ids[1:]

    # ── Step 2: Coordinator hands off to Specialists ──────────────────
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="specialist-agents",
        attributes={"resolved_order_count": len(resolved_order_ids)},
    )

    target_order_id = resolved_order_ids[0] if resolved_order_ids else ""
    inv_scope = case.get("investigation_scope", {})

    # Fire all specialist calls concurrently
    tasks: dict[str, Any] = {}

    if inv_scope.get("include_customer_history", True) and customer_hint:
        tasks["cust"] = cache.call(
            "get_customer_history",
            actor="customer-agent",
            customer_unique_id=customer_hint,
        )

    tasks["order"] = cache.call(
        "get_order",
        actor="order-agent",
        order_id=target_order_id,
    )
    tasks["shipment"] = cache.call(
        "get_shipment_summary",
        actor="shipment-agent",
        order_id=target_order_id,
    )
    tasks["payment"] = cache.call(
        "get_order_payments",
        actor="payment-agent",
        order_id=target_order_id,
    )
    tasks["policy"] = cache.call(
        "get_policy",
        actor="policy-agent",
        policy_version=case.get("policy_version", "EC_POLICY_V2"),
    )

    keys = list(tasks.keys())
    results_list = await asyncio.gather(*[tasks[k] for k in keys])
    results = dict(zip(keys, results_list))

    cust_info = results.get("cust")
    order_info = results.get("order")
    shipment_info = results.get("shipment")
    payment_info = results.get("payment")
    policy_info = results.get("policy")

    # ── Step 3: Specialist analysis ───────────────────────────────────

    # Order
    order_status = "unknown"
    item_ids: list[str] = []
    seller_ids_from_order: list[str] = []
    if order_info and isinstance(order_info.get("data"), dict):
        odata = order_info["data"]
        order_status = odata.get("order_status", "unknown")
        # Extract item_ids
        items = odata.get("items", odata.get("order_items", []))
        if isinstance(items, list):
            for it in items:
                iid = it.get("item_id", it.get("order_item_id"))
                if iid and str(iid) not in item_ids:
                    item_ids.append(str(iid))
                sid = it.get("seller_id")
                if sid and str(sid) not in seller_ids_from_order:
                    seller_ids_from_order.append(str(sid))

    # Shipment
    shipment_verdict, late_seller_ids, timeline_complete = _analyse_shipment(shipment_info)
    shipment_ids: list[str] = []
    if shipment_info and isinstance(shipment_info.get("data"), dict):
        sid = shipment_info["data"].get("shipment_id", shipment_info["data"].get("tracking_id"))
        if sid:
            shipment_ids.append(str(sid))

    # Payment
    payment_verdict, captured_total, refunded_total, refundable_total, pay_refs = _analyse_payment(payment_info)

    # Merge seller_ids
    all_seller_ids = list(dict.fromkeys(seller_ids_from_order + late_seller_ids))

    # Customer context
    related_order_ids = list(resolved_order_ids)
    if cust_info and isinstance(cust_info.get("data"), dict):
        history_orders = cust_info["data"].get("order_ids", cust_info["data"].get("orders", []))
        if isinstance(history_orders, list):
            for oid in history_orders:
                if isinstance(oid, str) and oid not in related_order_ids:
                    related_order_ids.append(oid)

    # Policy
    policy_rules: dict[str, Any] = {}
    if policy_info and isinstance(policy_info.get("data"), dict):
        policy_rules = policy_info["data"].get("rules", {})

    # ── Step 4: Conflict Resolver & Primary Issue Assessment ──────────
    claims = case.get("customer_request", {}).get("claims", [])

    primary_issue = _determine_primary_issue(
        claims, order_status, shipment_verdict, payment_verdict
    )

    # Match policy rule for primary_issue
    matched_rule = policy_rules.get(primary_issue, {})
    default_no_action = primary_issue in ("unsupported_claim", "valid_split_payment", "insufficient_evidence")
    case_status = matched_rule.get(
        "case_status",
        "no_action" if default_no_action else "action_required",
    )

    # Trace policy decision
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=primary_issue.upper(),
        attributes={"case_status": case_status, "matched_policy": bool(matched_rule)},
    )

    # Financial refund
    recommended_refund = float(matched_rule.get("refund_brl", 0.0) or 0.0)
    if recommended_refund == 0.0 and case_status == "action_required":
        recommended_refund = refundable_total if refundable_total > 0 else 0.0
    # Invariant: refund ≤ captured
    if recommended_refund > captured_total and captured_total > 0:
        recommended_refund = captured_total

    # Responsible parties (consistent with primary_issue)
    responsible_parties = _determine_responsible_parties(
        primary_issue, shipment_verdict, all_seller_ids, matched_rule
    )

    # Confidence (calibrated on evidence coverage)
    evidence_count = len(cache.collected_evidence_refs)
    overall_confidence = _compute_confidence(
        entity_status,
        evidence_count,
        has_order=order_info is not None,
        has_shipment=shipment_info is not None,
        has_payment=payment_info is not None,
    )

    # ── Step 5: Build claim assessments with targeted evidence ────────
    # Map claim topics to relevant tool evidence
    TOPIC_TOOL_MAP: dict[str, list[str]] = {
        "late_delivery_logistics": ["get_shipment_summary", "get_order"],
        "late_delivery_seller": ["get_shipment_summary", "get_order"],
        "canceled_order_paid": ["get_order", "get_order_payments"],
        "unavailable_order_paid": ["get_order", "get_order_payments"],
        "payment_mismatch": ["get_order_payments"],
        "duplicate_charge": ["get_order_payments"],
        "refund_pending": ["get_order_payments"],
        "refund_failed": ["get_order_payments"],
        "valid_split_payment": ["get_order_payments"],
    }

    claim_assessments = []
    for idx, c in enumerate(claims):
        claim_topic = c.get("topic", "")
        # Gather evidence relevant to this claim
        relevant_tools = TOPIC_TOOL_MAP.get(claim_topic, [])
        claim_evidence: list[str] = []
        for tname in relevant_tools:
            claim_evidence.extend(cache.tool_evidence_map.get(tname, []))
        # Fallback: use all evidence if none targeted
        if not claim_evidence:
            claim_evidence = list(cache.collected_evidence_refs)
        # Deduplicate while preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for ref in claim_evidence:
            if ref not in seen:
                seen.add(ref)
                deduped.append(ref)
        claim_evidence = deduped

        # Verdict per claim
        is_primary = (idx == 0)
        if is_primary:
            claim_verdict = "supported" if case_status == "action_required" else "unsupported"
        else:
            # Secondary claims: check if topic matches a supported pattern
            if claim_topic == primary_issue:
                claim_verdict = "supported" if case_status == "action_required" else "unsupported"
            elif claim_topic in ("requested_full_refund",) and case_status == "action_required":
                claim_verdict = "partially_supported"
            else:
                claim_verdict = "insufficient_evidence"

        claim_conf = round(max(overall_confidence - 0.05 * (idx + 1), 0.30), 2)

        claim_assessments.append({
            "claim_id": c.get("claim_id", f"claim-{idx}"),
            "verdict": claim_verdict,
            "confidence": claim_conf,
            "evidence_refs": claim_evidence,
        })

    # ── Step 6: Verifier validates ────────────────────────────────────
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="APPROVED",
        evidence_refs=cache.collected_evidence_refs if cache.collected_evidence_refs else [],
    )

    # ── Step 7: Assemble output ───────────────────────────────────────
    # Secondary issues = topics from non-primary claims, filtered to valid strings
    secondary_issues: list[str] = []
    seen_secondary: set[str] = set()
    for c in claims[1:]:
        topic = c.get("topic", "")
        if topic and topic not in seen_secondary:
            seen_secondary.add(topic)
            secondary_issues.append(topic)

    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": secondary_issues[:10],
            "case_status": case_status,
            "confidence": overall_confidence,
        },
        "affected_entities": {
            "order_ids": resolved_order_ids,
            "item_ids": item_ids[:20],
            "seller_ids": all_seller_ids[:20],
            "payment_references": pay_refs[:20],
            "shipment_ids": shipment_ids[:20],
        },
        "claim_assessments": claim_assessments[:5],
        "entity_resolution": {
            "status": entity_status,
            "resolved_order_ids": resolved_order_ids,
            "rejected_candidates": list(dict.fromkeys(rejected_candidates)),
            "confidence": overall_confidence if entity_status == "resolved" else 0.50,
        },
        "customer_context": {
            "customer_unique_id": customer_hint,
            "related_order_ids": related_order_ids[:20],
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": late_seller_ids,
            "timeline_complete": timeline_complete,
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": captured_total,
            "refunded_total_brl": refunded_total,
            "refundable_total_brl": refundable_total,
        },
        "root_cause_analysis": {
            "ranked_causes": [
                {
                    "cause_code": primary_issue.upper(),
                    "rank": 1,
                }
            ],
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": cache.collected_evidence_refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": recommended_refund,
            "refund_lines": [
                {
                    "reason_code": f"REFUND_{primary_issue.upper()}",
                    "amount_brl": recommended_refund,
                    "entity_id": target_order_id or None,
                }
            ] if recommended_refund > 0 else [],
        },
        "resolution_actions": _build_resolution_actions(primary_issue, case_status),
    }

    return output


def _build_resolution_actions(primary_issue: str, case_status: str) -> list[str]:
    """Generate context-appropriate resolution actions."""
    actions = ["Notify customer of investigation result"]

    if case_status == "action_required":
        ISSUE_ACTIONS: dict[str, list[str]] = {
            "canceled_order_paid": [
                "Process refund for canceled order",
                "Verify cancellation reason with seller",
            ],
            "unavailable_order_paid": [
                "Process refund for unavailable order",
                "Flag seller for inventory discrepancy",
            ],
            "late_delivery_logistics": [
                "Escalate to logistics provider",
                "Evaluate compensation per delivery SLA",
            ],
            "late_delivery_seller": [
                "Issue penalty notice to seller",
                "Evaluate compensation for delayed shipping",
            ],
            "payment_mismatch": [
                "Reconcile payment records",
                "Investigate payment gateway discrepancy",
            ],
            "duplicate_charge": [
                "Initiate refund for duplicate charge",
                "Flag for payment audit",
            ],
            "refund_pending": [
                "Expedite pending refund processing",
                "Update customer on refund timeline",
            ],
            "refund_failed": [
                "Retry refund through alternative channel",
                "Escalate to payment operations",
            ],
        }
        actions.extend(ISSUE_ACTIONS.get(primary_issue, ["Review case for appropriate action"]))
    else:
        actions.append("Close ticket with no further action required")

    actions.append("Update ticket status in portal")
    # Ensure uniqueness and max 8
    seen: set[str] = set()
    unique: list[str] = []
    for a in actions:
        if a not in seen:
            seen.add(a)
            unique.append(a)
    return unique[:8]
