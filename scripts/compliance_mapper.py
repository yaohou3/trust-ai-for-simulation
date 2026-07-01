"""
compliance_mapper.py — Deterministic DSL → compliance_spec mapping.

v5.2 — SEMANTIC EXTENSIONS
==========================
Extended with 8 new element types and semantic field pass-through:

  NEW element types (v5.2):
    eligibility_rule        → eligibility_rules
    coverage_rule           → coverage_rules
    routing_rule            → routing_rules
    assignment_continuity   → assignment_continuity
    policy_threshold        → policy_thresholds
    semantic_scenario       → semantic_scenarios        (Phase 4.5)
    coupling_rule           → coupling_rules            (Phase 4.5)
    control_policy          → control_policies          (Phase 4.5)

  Existing element types gain semantic field pass-through so downstream
  validators (B24–B41) can read duration_scope, consumption_mode,
  discipline, calendar, composition_band, etc., from the compliance_spec
  without re-reading the DSL.

Given a list of DslElement objects, this module mechanically produces:
  1. A compliance_spec dict compatible with phase0_vvuq.py / vvuq_utils.py
  2. A CoverageReport recording which DSL ID maps to which compliance section(s)

The mapping is purely rule-based — no LLM call required.
"""

from __future__ import annotations
from typing import Any

from dsl_schema import (
    DslElement, CoverageItem, CoverageReport,
    DSL_ELEMENT_TO_COMPLIANCE, NON_CHECKABLE_ELEMENT_TYPES,
)


# ─────────────────────────────────────────────────────────────────────────────
# Default tolerances (can be overridden per element via DslElement.tolerance)
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_TOLERANCES: dict[str, float] = {
    # Original sections
    "arrivals":               0.10,
    "recurring_obligations":  0.15,
    "service_rates":          0.20,
    "periodic_processes":     0.10,
    "scheduled_windows":      0.10,
    "handoff_sequences":      0.0,   # exact ordering
    "coordination_patterns":  0.0,   # structural, not rate-based
    "sequencing":             0.0,   # exact ordering
    "temporal":               0.0,   # structural minimum
    "preemption_rules":       0.05,
    "state_transition_rules": 0.0,
    "entity_type_constraints":0.0,
    "terminal_outcomes":      0.0,
    "losses":                 0.0,
    "flow_accounting":        0.0,
    "aggregate_targets":      0.20,
    "scenario_guidance":      0.0,
    "process_contracts":      0.10,

    # NEW in v5.2
    "eligibility_rules":      0.0,   # categorical — any violation is an error
    "coverage_rules":         0.10,  # interval tolerance
    "routing_rules":          0.05,  # probability tolerance
    "assignment_continuity":  0.05,  # fraction of episodes allowed to break
    "policy_thresholds":      0.0,   # strict: breach MUST trigger outcome
    "semantic_scenarios":     0.15,  # magnitude tolerance for expected effect
    "coupling_rules":         0.15,
    "control_policies":       0.10,
    "escalations":            0.15,  # Phase 4.5 — min_effect_size default
    "workload_responses":     0.10,  # Phase 4.5 — min_effect_size default
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers for carrying semantic fields through to compliance entries
# ─────────────────────────────────────────────────────────────────────────────

def _opt(d: dict, key: str, value: Any) -> None:
    """Attach value to d[key] only if it's not None / not empty list."""
    if value is None:
        return
    if isinstance(value, (list, tuple)) and len(value) == 0:
        return
    d[key] = value


def _tol(declared, default):
    """Tolerance with a default, preserving an explicit 0.0. `x or default`
    silently discards a deliberate tolerance of 0.0 (exact-match intent),
    because 0.0 is falsy; this only substitutes the default when the author
    declared nothing (None)."""
    return default if declared is None else declared


def _semantic_passthrough(el: DslElement, entry: dict) -> None:
    """Attach semantic fields from the DSL element to a compliance entry
    so B24–B41 validators can read them directly."""
    _opt(entry, "granularity", el.granularity)
    _opt(entry, "assumed_default", el.assumed_default)
    _opt(entry, "assumed_default_reason", el.assumed_default_reason)
    # Dynamics-mechanism choice (v5.3): propagate so §1.6 trace-temporal-
    # distribution check can read it from the compliance_spec.
    _opt(entry, "dynamics", el.dynamics)


# ─────────────────────────────────────────────────────────────────────────────
# Individual mapper functions (one per compliance section)
# ─────────────────────────────────────────────────────────────────────────────

def _map_arrival(el: DslElement) -> dict:
    """arrival → arrivals entry."""
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "entity_type": el.entity_type,
        "expected_rate_per_day": el.rate_per_day,
        "tolerance": _tol(el.tolerance, DEFAULT_TOLERANCES["arrivals"]),
    }
    _opt(entry, "distribution", el.distribution)
    _opt(entry, "calendar", el.calendar)
    _semantic_passthrough(el, entry)
    return entry


def _map_recurring_obligation(el: DslElement) -> dict:
    """recurring_activity → recurring_obligations entry."""
    rate = 1.0 / el.recurrence_hours if el.recurrence_hours else None
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "entity_type": el.entity_type,
        "process": el.name or None,
        "responsible_role": el.resources[0] if el.resources else None,
        "due_every_hours": el.recurrence_hours,
        "expected_rate_per_entity_hour": rate,
        "tolerance": _tol(el.tolerance, DEFAULT_TOLERANCES["recurring_obligations"]),
    }
    _opt(entry, "calendar", el.calendar)
    _opt(entry, "interrupt_policy", el.interrupt_policy)
    _opt(entry, "rearm_policy", el.rearm_policy)
    _semantic_passthrough(el, entry)
    return entry


def _map_service_rate(el: DslElement) -> list[dict]:
    """service_process → one service_rates entry per resource."""
    entries = []
    for res in (el.resources or [None]):
        entry: dict[str, Any] = {
            "source_ids": [el.id],
            "resource": res,
            "entity_type": el.entity_type,
            "process": el.name or None,
            "tolerance": _tol(el.tolerance, DEFAULT_TOLERANCES["service_rates"]),
        }
        # Derive per_entity_hour from service_time distribution if provided
        if el.service_time and "mean" in el.service_time:
            mean_sec = float(el.service_time["mean"])
            if mean_sec > 0:
                entry["per_entity_hour"] = 3_600.0 / mean_sec
                entry["mean_service_seconds"] = mean_sec
        _opt(entry, "service_time", el.service_time)
        # Semantic duration fields
        _opt(entry, "duration_scope", el.duration_scope)
        _opt(entry, "interrupt_policy", el.interrupt_policy)
        _opt(entry, "setup_seconds", el.setup_seconds)
        _opt(entry, "teardown_seconds", el.teardown_seconds)
        # Capacity-consumption
        _opt(entry, "consumption_mode", el.consumption_mode)
        _opt(entry, "inventory_init", el.inventory_init)
        _opt(entry, "replenishment", el.replenishment)
        _opt(entry, "max_concurrent", el.max_concurrent)
        _opt(entry, "overlap_allowed_with", el.overlap_allowed_with)
        # Queue discipline
        _opt(entry, "discipline", el.discipline)
        _opt(entry, "tie_break", el.tie_break)
        _opt(entry, "aging", el.aging)
        _opt(entry, "reentry_position", el.reentry_position)
        _opt(entry, "balk_renege_policy", el.balk_renege_policy)
        # Calendar
        _opt(entry, "calendar", el.calendar)
        _semantic_passthrough(el, entry)
        entries.append(entry)
    return entries


def _map_periodic_process(el: DslElement) -> list[dict]:
    """scheduled_process → periodic_processes + optional scheduled_windows."""
    entries: list[dict] = []
    primary: dict[str, Any] = {
        "source_ids": [el.id],
        "process": el.name or None,
        "entity_type": el.entity_type,
        "event": "system_arrival",
        "expected_per_day": (el.rate_per_day
                             or (24.0 / el.recurrence_hours if el.recurrence_hours else None)),
        "tolerance": _tol(el.tolerance, DEFAULT_TOLERANCES["periodic_processes"]),
    }
    _opt(primary, "calendar", el.calendar)
    _opt(primary, "boundary_policy", el.boundary_policy)
    _semantic_passthrough(el, primary)
    entries.append(primary)

    # If a schedule with windows_hours is specified, also add scheduled_windows
    if el.schedule and "windows_hours" in el.schedule:
        window_entry: dict[str, Any] = {
            "_section": "scheduled_windows",
            "source_ids": [el.id],
            "process": el.name or None,
            "entity_type": el.entity_type,
            "event": "system_arrival",
            "windows_hours": el.schedule["windows_hours"],
            "expected_per_day": primary["expected_per_day"],
            "tolerance": _tol(el.tolerance, DEFAULT_TOLERANCES["scheduled_windows"]),
        }
        _opt(window_entry, "calendar", el.calendar)
        _opt(window_entry, "boundary_policy", el.boundary_policy)
        _semantic_passthrough(el, window_entry)
        entries.append(window_entry)
    return entries


def _map_sequencing(el: DslElement) -> dict:
    """precedence → sequencing entry."""
    before_pattern: dict[str, str] = {}
    after_pattern:  dict[str, str] = {}
    if el.precedence_before:
        before_pattern["resource"] = el.precedence_before
        before_pattern["event"]    = "service_start"
    if el.precedence_after:
        after_pattern["resource"] = el.precedence_after
        after_pattern["event"]    = "service_start"
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "entity_type": el.entity_type,
        "before": before_pattern or None,
        "after":  after_pattern or None,
        "description": el.description,
    }
    _semantic_passthrough(el, entry)
    return entry


def _map_temporal(el: DslElement) -> dict:
    """time_threshold → temporal entry."""
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "entity_type": el.entity_type,
        "event": "system_departure",
        "min_seconds": el.min_seconds,
        "max_seconds": el.max_seconds,
        "description": el.description,
    }
    _semantic_passthrough(el, entry)
    return entry


def _map_coordination(el: DslElement) -> list[dict]:
    """hard/soft/role_anchored coordination → coordination_patterns (+ optional
    pool_composition_rules for soft_pool with declared pool_policy).

    Returns a list of dicts. Entries targeting the secondary
    pool_composition_rules section carry "_section" = "pool_composition_rules".
    """
    ctype = el.coordination_type or (
        "hard_pool"     if el.element_type == "hard_pool_coordination"     else
        "soft_pool"     if el.element_type == "soft_pool_coordination"     else
        "role_anchored"
    )
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "process": el.name or None,
        "type": ctype,
        "resources": el.resources,
        "min_occurrences": 1,
        "min_participants": 1,
        "dynamic_join": el.element_type == "soft_pool_coordination",
    }
    # Participation semantics
    _opt(entry, "substitutable_roles", el.substitutable_roles)
    _opt(entry, "quorum_min", el.quorum_min)
    _opt(entry, "staggered_allowed", el.staggered_allowed)
    _opt(entry, "supervisory", el.supervisory)
    _opt(entry, "sync_type", el.sync_type)
    _opt(entry, "batched_start", el.batched_start)
    _opt(entry, "batched_complete", el.batched_complete)
    _opt(entry, "handoff_pair", el.handoff_pair)
    _semantic_passthrough(el, entry)

    entries: list[dict] = [entry]

    # Soft-pool with declared pool_policy → also emit pool_composition_rules (B41)
    if (el.element_type == "soft_pool_coordination"
            and getattr(el, "pool_policy", None)):
        pool_entry: dict[str, Any] = {
            "_section": "pool_composition_rules",
            "source_id": el.id,
            "source_ids": [el.id],
            "process": el.name or None,
            "roles": list(el.resources or []),
            "resources_by_role": getattr(el, "schedule", None) and
                                 (el.schedule.get("resources_by_role") if el.schedule else None)
                                 or {r: [r] for r in (el.resources or [])},
            "pool_policy": el.pool_policy,
            "pool_size": getattr(el, "pool_size", None),
            "admits_late_joiners": getattr(el, "admits_late_joiners", None),
            "release_on_exit": getattr(el, "release_on_exit", None),
            "windows": (el.schedule or {}).get("windows_hours") or [],
            "onset_tolerance_seconds": 60.0,
        }
        _opt(pool_entry, "pool_granularity", getattr(el, "pool_granularity", None))
        _opt(pool_entry, "server_id_required", getattr(el, "server_id_required", None))
        _semantic_passthrough(el, pool_entry)
        entries.append(pool_entry)

    return entries


def _map_preemption_rule(el: DslElement) -> dict:
    """preemption_rule → preemption_rules entry."""
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "applies_to_processes": [el.name] if el.name else [],
        "require_resume_or_terminal_exit": True,
        "expected_preempt_count": None if el.interruptible else 0,
        "tolerance": _tol(el.tolerance, DEFAULT_TOLERANCES["preemption_rules"]),
    }
    _opt(entry, "post_preempt_policy", el.post_preempt_policy)
    _opt(entry, "can_initiate", el.can_initiate)
    _opt(entry, "preemptible_by", el.preemptible_by)
    _opt(entry, "equal_priority", el.equal_priority)
    _opt(entry, "min_service_chunk_s", el.min_service_chunk_s)
    _opt(entry, "preemption_policy", el.preemption_policy)
    _semantic_passthrough(el, entry)
    return entry


def _map_state_restriction(el: DslElement) -> dict:
    """state_restriction → state_transition_rules entry."""
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "state_field": "state",
        "state": el.state,
        "forbidden_events":    el.forbidden_events or [],
        "forbidden_processes": [],
    }
    _opt(entry, "dwell_distribution", el.dwell_distribution)
    _opt(entry, "hazard_rate", el.hazard_rate)
    _opt(entry, "competing_risks", el.competing_risks)
    _semantic_passthrough(el, entry)
    return entry


def _map_entity_type_constraint(el: DslElement) -> dict:
    """entity_type_constraint → entity_type_constraints entry."""
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "process": el.name or None,
        "entity_type": el.entity_type,
        "not_entity_type": None,
    }
    _semantic_passthrough(el, entry)
    return entry


def _map_terminal_outcome(el: DslElement) -> dict:
    """terminal_outcome → terminal_outcomes entry."""
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "entity_type": el.entity_type,
        "terminal_events": el.terminal_events or ["system_departure", "loss"],
        "exactly_one_terminal_event": True,
    }
    _semantic_passthrough(el, entry)
    return entry


def _map_loss(el: DslElement) -> dict:
    """loss_type → losses entry."""
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "loss_type": el.loss_type,
        "must_exist": el.must_exist if el.must_exist is not None else True,
    }
    _opt(entry, "condition_expr", el.condition_expr)
    _opt(entry, "threshold_seconds", el.threshold_seconds)
    _opt(entry, "cause_attribution", el.cause_attribution)
    _semantic_passthrough(el, entry)
    return entry


def _map_flow_conservation(el: DslElement) -> dict:
    """flow_conservation → flow_accounting entry."""
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "name": "system_balance",
        "identity": "arrivals = departures + losses + in_system_end",
    }
    _opt(entry, "accounting_basis", el.accounting_basis)
    _semantic_passthrough(el, entry)
    return entry


def _map_aggregate_target(el: DslElement) -> dict:
    """aggregate_target → aggregate_targets entry."""
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "metric": el.metric,
        "expected": el.expected_value,
        "tolerance": _tol(el.tolerance, DEFAULT_TOLERANCES["aggregate_targets"]),
    }
    _opt(entry, "numerator", el.numerator)
    _opt(entry, "denominator", el.denominator)
    _opt(entry, "basis", el.basis)
    _opt(entry, "warmup_handling", el.warmup_handling)
    _opt(entry, "composition_band", el.composition_band)
    _semantic_passthrough(el, entry)
    return entry


def _map_scenario_hint(el: DslElement) -> dict:
    """scenario_hint → scenario_guidance entry."""
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "name": el.name or f"scenario_{el.id}",
        "type": "scenario_check",
        "description": el.description,
    }
    _semantic_passthrough(el, entry)
    return entry


def _map_process_contract(el: DslElement) -> dict:
    """process_contract → process_contracts entry (B18–B23)."""
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "process_name": el.process_name or el.name or None,
        "trigger": el.trigger,
        "trigger_condition": el.trigger_condition,
    }
    _opt(entry, "every_hours", el.every_hours)
    _opt(entry, "times_per_day", el.times_per_day)
    _opt(entry, "schedule_times_hour", el.schedule_times_hour)
    _opt(entry, "required_roles", el.required_roles)
    _opt(entry, "optional_roles", el.optional_roles)
    _opt(entry, "shift_opening_roles", el.shift_opening_roles)
    _opt(entry, "shift_length_hours", el.shift_length_hours)
    _opt(entry, "interruptible", el.interruptible)
    _opt(entry, "preemption_policy", el.preemption_policy)
    _opt(entry, "count_unit", el.count_unit)
    _opt(entry, "episode_definition", el.episode_definition)
    _opt(entry, "expected_jitter_cv", el.expected_jitter_cv)
    _opt(entry, "rearm_policy", el.rearm_policy)
    _opt(entry, "one_shot", el.one_shot)
    _opt(entry, "boundary_policy", el.boundary_policy)
    _opt(entry, "calendar", el.calendar)
    _opt(entry, "tolerance", _tol(el.tolerance, DEFAULT_TOLERANCES["process_contracts"]))
    _semantic_passthrough(el, entry)
    return entry


# ─────────────────────────────────────────────────────────────────────────────
# NEW mappers for v5.2 semantic element types
# ─────────────────────────────────────────────────────────────────────────────

def _map_eligibility(el: DslElement) -> dict:
    """eligibility_rule → eligibility_rules entry (B24)."""
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "process": el.name or None,
        "predicate": el.eligibility_predicate,
        "eligible_states": el.eligible_states or [],
        "eligible_entity_types": el.eligible_entity_types or [],
        "requires_prerequisites": el.requires_prerequisites or [],
        "tolerance": _tol(el.tolerance, DEFAULT_TOLERANCES["eligibility_rules"]),
    }
    _semantic_passthrough(el, entry)
    return entry


def _map_coverage(el: DslElement) -> dict:
    """coverage_rule → coverage_rules entry (B25)."""
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "name": el.name or None,
        "entity_type": el.entity_type,
        "coverage_target": el.coverage_target,
        "interval_hours": el.interval_hours,
        "missed_coverage_policy": el.missed_coverage_policy or "track",
        "duplicate_policy": el.duplicate_policy or "allowed",
        "tolerance": _tol(el.tolerance, DEFAULT_TOLERANCES["coverage_rules"]),
    }
    _opt(entry, "calendar", el.calendar)
    _semantic_passthrough(el, entry)
    return entry


def _map_routing(el: DslElement) -> dict:
    """routing_rule → routing_rules entry (B26)."""
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "name": el.name or None,
        "routing_from": el.routing_from,
        "routing_to": el.routing_to or [],
        "fallback_target": el.fallback_target,
        "tolerance": _tol(el.tolerance, DEFAULT_TOLERANCES["routing_rules"]),
    }
    _semantic_passthrough(el, entry)
    return entry


def _map_assignment_continuity(el: DslElement) -> dict:
    """assignment_continuity → assignment_continuity entry (B27)."""
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "process": el.name or None,
        "resources": el.resources,
        "continuity_scope": el.continuity_scope or "episode",
        "persistent": el.persistent if el.persistent is not None else True,
        "load_balance_policy": el.load_balance_policy,
        "tolerance": _tol(el.tolerance, DEFAULT_TOLERANCES["assignment_continuity"]),
    }
    _semantic_passthrough(el, entry)
    return entry


def _map_policy_threshold(el: DslElement) -> dict:
    """policy_threshold → policy_thresholds entry (B28 + Phase 4.5 driver).

    Carries the Phase 4.5 GPT-shape fields (metrics / expectation / evaluation)
    when the DSL supplied them so the counterfactual harness can test whether
    enabling the declared escalation_outcome actually fires the response.
    """
    expectation = _derive_expectation(el)
    evaluation = _default_evaluation(el)
    metrics = _derive_metrics(el, expectation)
    if metrics and "direction" not in expectation:
        expectation["direction"] = "up"
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "name": el.name or None,
        "entity_type": el.entity_type,
        "breach_condition": el.breach_condition or el.condition_expr,
        "breach_threshold_seconds": el.breach_threshold_seconds or el.threshold_seconds,
        "escalation_outcome": el.escalation_outcome,
        "monitoring_interval_seconds": el.monitoring_interval_seconds,
        "tolerance": _tol(el.tolerance, DEFAULT_TOLERANCES["policy_thresholds"]),
    }
    if metrics:
        entry["metrics"] = metrics
        entry["expectation"] = expectation
        entry["evaluation"] = evaluation
    _semantic_passthrough(el, entry)
    return entry


def _normalize_cell(cell: dict | None) -> dict:
    """Accept either {'parameters': {...}, ...} or a flat overrides dict and
    always emit the GPT shape with a 'parameters' key so semantic_scenarios.py
    drivers can consume it uniformly.
    """
    if not cell:
        return {"parameters": {}}
    if isinstance(cell, dict) and "parameters" in cell:
        out = dict(cell)
        out["parameters"] = dict(cell.get("parameters") or {})
        return out
    # flat shape — wrap it
    return {"parameters": dict(cell)}


def _derive_expectation(el: DslElement) -> dict:
    """Prefer el.expectation; else synthesize from legacy expected_directional_effect."""
    if el.expectation:
        return dict(el.expectation)
    legacy = el.expected_directional_effect or {}
    out: dict[str, Any] = {}
    if legacy.get("direction"):
        out["direction"] = legacy["direction"]
    if "min_effect_size" in legacy:
        out["min_effect_size"] = legacy["min_effect_size"]
    elif "min_magnitude" in legacy:
        out["min_effect_size"] = legacy["min_magnitude"]
    if legacy.get("metric") and "metric" not in out:
        out["metric"] = legacy["metric"]
    return out


def _default_evaluation(el: DslElement) -> dict:
    if el.evaluation:
        return {
            "method":       el.evaluation.get("method", "bootstrap_mean_diff"),
            "confidence":   float(el.evaluation.get("confidence", 0.95)),
            "replications": int(el.evaluation.get("replications", 2000)),
        }
    return {"method": "bootstrap_mean_diff", "confidence": 0.95, "replications": 2000}


def _derive_metrics(el: DslElement, fallback: dict) -> list:
    """Normalize metrics list — accept strings or dicts.  Fallback to
    expectation.metric when metrics[] is empty."""
    ms = el.metrics or []
    out: list = []
    for m in ms:
        if isinstance(m, str):
            out.append({"name": m})
        elif isinstance(m, dict) and m.get("name"):
            out.append(dict(m))
    if not out and fallback.get("metric"):
        out.append({"name": fallback["metric"]})
    return out


def _map_semantic_scenario(el: DslElement) -> dict:
    """semantic_scenario → semantic_scenarios entry (Phase 4.5, GPT shape)."""
    expectation = _derive_expectation(el)
    evaluation = _default_evaluation(el)
    metrics = _derive_metrics(el, expectation)
    # Default min_effect_size from the section tolerance if not supplied
    if "min_effect_size" not in expectation:
        expectation["min_effect_size"] = _tol(el.tolerance, DEFAULT_TOLERANCES["semantic_scenarios"])

    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "name": el.name or f"scenario_{el.id}",
        "description": el.description,
        "scenario_category": el.scenario_category or "emergent_pattern",
        "control":    _normalize_cell(el.control),
        "treatment":  _normalize_cell(el.treatment),
        "metrics":    metrics,
        "expectation": expectation,
        "evaluation":  evaluation,
        "tolerance":   _tol(el.tolerance, DEFAULT_TOLERANCES["semantic_scenarios"]),
        # Keep legacy field populated for any downstream reader still on it
        "expected_directional_effect": el.expected_directional_effect or None,
    }
    _semantic_passthrough(el, entry)
    return entry


def _map_coupling(el: DslElement) -> dict:
    """coupling_rule → coupling_rules entry (Phase 4.5)."""
    expectation = _derive_expectation(el)
    evaluation = _default_evaluation(el)
    metrics = _derive_metrics(el, expectation)
    # Infer default direction from coupling_type if not explicit
    if "direction" not in expectation:
        ct = (el.coupling_type or "").lower()
        expectation["direction"] = "down" if ct in ("suppresses", "delays") else (
            "unchanged" if ct == "unchanged" else "up")
    # Default magnitude: bracket around expected_effect_size or fall through
    if "min_effect_size" not in expectation and el.expected_effect_size is not None:
        expectation["min_effect_size"] = abs(el.expected_effect_size) * 0.5
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "name": el.name or None,
        "source_process": el.source_process,
        "target_process": el.target_process,
        "coupling_type": el.coupling_type,
        "expected_effect_size": el.expected_effect_size,
        "tolerance": _tol(el.tolerance, DEFAULT_TOLERANCES["coupling_rules"]),
    }
    if metrics:
        entry["metrics"] = metrics
        entry["expectation"] = expectation
        entry["evaluation"] = evaluation
    _semantic_passthrough(el, entry)
    return entry


def _map_control_policy(el: DslElement) -> dict:
    """control_policy → control_policies entry (Phase 4.5)."""
    expectation = _derive_expectation(el)
    evaluation = _default_evaluation(el)
    metrics = _derive_metrics(el, expectation)
    if metrics and "direction" not in expectation:
        # Observation-delay counterfactual: treatment has delay, expect loss up.
        expectation["direction"] = "up"
    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "name": el.name or None,
        "policy_signals": el.policy_signals or [],
        "update_cadence_s": el.update_cadence_s,
        "observation_delay_s": el.observation_delay_s,
        "information_source": el.information_source,
        "fallback_policy": el.fallback_policy,
        "override_conditions": el.override_conditions or [],
        "tolerance": _tol(el.tolerance, DEFAULT_TOLERANCES["control_policies"]),
    }
    if metrics:
        entry["metrics"] = metrics
        entry["expectation"] = expectation
        entry["evaluation"] = evaluation
    _semantic_passthrough(el, entry)
    return entry


def _map_escalation(el: DslElement) -> dict:
    """escalation → escalations entry (Phase 4.5).

    Exercises the policy-layer invariant 'when SLA is breached, escalation
    fires' by toggling the declared outcome on/off across a paired run.
    """
    expectation = _derive_expectation(el)
    evaluation = _default_evaluation(el)
    metrics = _derive_metrics(el, expectation) or [
        {"name": "escalation_count", "direction": "up"}
    ]
    if "direction" not in expectation:
        expectation["direction"] = "up"
    if "min_effect_size" not in expectation:
        expectation["min_effect_size"] = _tol(el.tolerance, DEFAULT_TOLERANCES["escalations"])

    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "name": el.name or f"escalation_{el.id}",
        "description": el.description,
        "scenario_category": "escalation",
        "escalation_trigger": el.escalation_trigger or el.breach_condition,
        "escalation_target":  el.escalation_target or el.escalation_outcome,
        "escalation_sla_seconds": el.escalation_sla_seconds or el.breach_threshold_seconds,
        "metrics": metrics,
        "expectation": expectation,
        "evaluation": evaluation,
        "tolerance":  _tol(el.tolerance, DEFAULT_TOLERANCES["escalations"]),
    }
    _semantic_passthrough(el, entry)
    return entry


def _map_workload_response(el: DslElement) -> dict:
    """workload_response → workload_responses entry (Phase 4.5).

    Exercises a stress claim: at treatment's `scale_factor` the declared
    response metric should move in the declared direction by at least
    `expectation.min_effect_size`.
    """
    expectation = _derive_expectation(el)
    evaluation = _default_evaluation(el)
    metrics = _derive_metrics(el, expectation) or [{"name": "loss_rate", "direction": "up"}]
    if "direction" not in expectation:
        expectation["direction"] = "up"
    if "min_effect_size" not in expectation:
        expectation["min_effect_size"] = _tol(el.tolerance, DEFAULT_TOLERANCES["workload_responses"])

    entry: dict[str, Any] = {
        "source_ids": [el.id],
        "name": el.name or f"workload_{el.id}",
        "description": el.description,
        "scenario_category": "workload_response",
        "scale_factor":    el.scale_factor if el.scale_factor is not None else 1.3,
        "baseline_factor": el.baseline_factor if el.baseline_factor is not None else 1.0,
        "metrics": metrics,
        "expectation": expectation,
        "evaluation": evaluation,
        "tolerance":  _tol(el.tolerance, DEFAULT_TOLERANCES["workload_responses"]),
    }
    _semantic_passthrough(el, entry)
    return entry


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch table
# ─────────────────────────────────────────────────────────────────────────────

def _dispatch(el: DslElement) -> tuple[str, Any] | list[tuple[str, Any]]:
    """Return (section_name, entry_dict) or a list of them for multi-section elements."""
    t = el.element_type

    # Original 17
    if t == "arrival":
        return ("arrivals", _map_arrival(el))
    if t == "recurring_activity":
        return ("recurring_obligations", _map_recurring_obligation(el))
    if t == "service_process":
        return [("service_rates", e) for e in _map_service_rate(el)]
    if t == "scheduled_process":
        results = _map_periodic_process(el)
        out = []
        for r in results:
            section = r.pop("_section", "periodic_processes")
            out.append((section, r))
        return out
    if t == "precedence":
        return ("sequencing", _map_sequencing(el))
    if t == "time_threshold":
        return ("temporal", _map_temporal(el))
    if t in ("hard_pool_coordination", "soft_pool_coordination", "role_anchored_coordination"):
        results = _map_coordination(el)
        out = []
        for r in results:
            section = r.pop("_section", "coordination_patterns")
            out.append((section, r))
        return out
    if t == "preemption_rule":
        return ("preemption_rules", _map_preemption_rule(el))
    if t == "state_restriction":
        return ("state_transition_rules", _map_state_restriction(el))
    if t == "entity_type_constraint":
        return ("entity_type_constraints", _map_entity_type_constraint(el))
    if t == "terminal_outcome":
        return ("terminal_outcomes", _map_terminal_outcome(el))
    if t == "loss_type":
        return ("losses", _map_loss(el))
    if t == "flow_conservation":
        return ("flow_accounting", _map_flow_conservation(el))
    if t == "aggregate_target":
        return ("aggregate_targets", _map_aggregate_target(el))
    if t == "scenario_hint":
        return ("scenario_guidance", _map_scenario_hint(el))
    if t == "process_contract":
        return ("process_contracts", _map_process_contract(el))

    # NEW v5.2 semantic types
    if t == "eligibility_rule":
        return ("eligibility_rules", _map_eligibility(el))
    if t == "coverage_rule":
        return ("coverage_rules", _map_coverage(el))
    if t == "routing_rule":
        return ("routing_rules", _map_routing(el))
    if t == "assignment_continuity":
        return ("assignment_continuity", _map_assignment_continuity(el))
    if t == "policy_threshold":
        return ("policy_thresholds", _map_policy_threshold(el))
    if t == "semantic_scenario":
        return ("semantic_scenarios", _map_semantic_scenario(el))
    if t == "coupling_rule":
        return ("coupling_rules", _map_coupling(el))
    if t == "control_policy":
        return ("control_policies", _map_control_policy(el))
    if t == "escalation":
        return ("escalations", _map_escalation(el))
    if t == "workload_response":
        return ("workload_responses", _map_workload_response(el))

    return []   # non-checkable or unknown


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

# All compliance_spec sections (empty lists by default)
_EMPTY_SPEC: dict[str, list] = {
    # Original
    "arrivals": [], "recurring_obligations": [], "service_rates": [],
    "periodic_processes": [], "scheduled_windows": [], "handoff_sequences": [],
    "coordination_patterns": [], "sequencing": [], "temporal": [],
    "preemption_rules": [], "state_transition_rules": [], "entity_type_constraints": [],
    "terminal_outcomes": [], "losses": [], "flow_accounting": [],
    "aggregate_targets": [], "scenario_guidance": [],
    # process_contracts is a list of contract entries here, like every other
    # section. The one consumer that needs dict semantics
    # (process_contracts.py, which iterates with .items()) normalizes the list
    # into a dict keyed by process_name itself. Keeping the producer uniform
    # avoids the list/dict mismatch class of bug across the other consumers
    # (coverage_validator iterates it as a list).
    "process_contracts": [],

    # NEW v5.2
    "eligibility_rules": [],
    "coverage_rules": [],
    "routing_rules": [],
    "assignment_continuity": [],
    "policy_thresholds": [],
    "semantic_scenarios": [],
    "coupling_rules": [],
    "control_policies": [],
    "escalations": [],
    "workload_responses": [],

    # Pool composition (B41): soft_pool_coordination with declared pool_policy
    "pool_composition_rules": [],
}


def map_dsl_to_compliance(
    elements: list[DslElement],
) -> tuple[dict[str, list], CoverageReport]:
    """Deterministically map DSL elements to a compliance_spec.

    Returns:
        (compliance_spec, coverage_report)

    Each entry in compliance_spec carries a `source_ids` list so the
    validator can check that every compliance assertion is grounded.
    """
    spec: dict[str, list] = {k: [] for k in _EMPTY_SPEC}
    coverage_items: list[CoverageItem] = []

    for el in elements:
        # Non-checkable element types
        if el.element_type in NON_CHECKABLE_ELEMENT_TYPES:
            coverage_items.append(CoverageItem(
                dsl_id=el.id,
                element_type=el.element_type,
                mapped_to=[],
                not_checkable=True,
                reason=el.not_checkable_reason or f"element_type={el.element_type}",
            ))
            continue

        result = _dispatch(el)
        if not result:
            # Unmapped — will be flagged in coverage report
            coverage_items.append(CoverageItem(
                dsl_id=el.id,
                element_type=el.element_type,
                mapped_to=[],
                not_checkable=False,
            ))
            continue

        # Normalise to list of (section, entry) pairs
        if isinstance(result, tuple):
            pairs = [result]
        else:
            pairs = result

        mapped_sections = []
        for section, entry in pairs:
            if section in spec:
                spec[section].append(entry)
                mapped_sections.append(section)

        coverage_items.append(CoverageItem(
            dsl_id=el.id,
            element_type=el.element_type,
            mapped_to=mapped_sections,
        ))

    # Build summary
    checkable = [ci for ci in coverage_items if not ci.not_checkable]
    mapped    = [ci for ci in checkable if ci.mapped_to]
    unmapped  = [ci.dsl_id for ci in checkable if not ci.mapped_to]

    report = CoverageReport(
        dsl_items_total=len(elements),
        dsl_items_mapped=len(mapped),
        dsl_items_not_checkable=sum(1 for ci in coverage_items if ci.not_checkable),
        unmapped_items=unmapped,
        items=coverage_items,
    )
    return spec, report


def compliance_spec_summary(spec: dict[str, list]) -> str:
    """Human-readable summary of a compliance_spec."""
    lines = ["compliance_spec contents:"]
    for section, entries in spec.items():
        if entries:
            lines.append(f"  {section}: {len(entries)} assertion(s)")
    empty = [k for k, v in spec.items() if not v]
    if empty:
        lines.append(f"  (empty sections: {', '.join(empty)})")
    return "\n".join(lines)
