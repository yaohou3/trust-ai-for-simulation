"""
coverage_validator.py — Deterministic validation of the DSL + compliance_spec pair.

v5.2 — SEMANTIC EXTENSIONS
==========================
Adds required-field checks for 9 new semantic sections:
    eligibility_rules, coverage_rules, routing_rules, assignment_continuity,
    policy_thresholds, semantic_scenarios, coupling_rules, control_policies,
    pool_composition_rules (B41)

Advisory-only sections (scenario_guidance, semantic_scenarios,
coupling_rules, control_policies, composition_bands) are treated as
warn-only: a missing-field in these sections produces WARN, not
BLOCKED, and observability gaps in these sections are demoted to WARN
because they are verified via Phase 4.5 counterfactuals rather than
single-trace checks.

Runs five checks before code generation is allowed:

CHECK 1 — DSL-to-Compliance Coverage
    For every checkable DSL element, at least one compliance assertion
    with that source_id must exist.  Coverage must be 100 % (or every
    uncovered element must carry a valid not_checkable_reason).

CHECK 2 — Compliance-to-DSL Grounding
    Every compliance assertion must cite at least one source_id that
    exists in the DSL.  Orphan assertions (invented by the LLM without
    a DSL basis) are a BLOCKED error.

CHECK 3 — Trace Observability
    Every non-empty compliance section must reference only trace fields
    and events that exist in the canonical Trace Contract.  Sections
    requiring fields that the declared trace contract does not emit are
    flagged as observability gaps (WARN for advisory-only sections).

CHECK 4 — Vague Clause Detection
    DSL descriptions containing non-operationalisable language
    ("quickly", "as needed", "when appropriate", "usually", "often", …)
    without an explicit not_checkable_reason are flagged as warnings.
    v5.2 also flags assumed_default=True elements without a reason.

CHECK 5 — Schema Validation
    Validates that the compliance_spec follows the expected structure
    (all required keys present, list types, non-null required fields).

Verdict:
    PASS          — all checks pass
    NEEDS_REVISION — warnings exist but no blocking errors
    BLOCKED        — blocking errors must be resolved before code generation
"""

from __future__ import annotations

import re
from typing import Any

from dsl_schema import (
    DslElement, CoverageReport, ValidationResult,
    OBSERVABILITY_REQUIREMENTS,
    ADVISORY_ONLY_SECTIONS,
)

# ─────────────────────────────────────────────────────────────────────────────
# Canonical Trace Contract: fields declared as present in run_simulation output
# ─────────────────────────────────────────────────────────────────────────────

# Baseline fields guaranteed by the Trace Contract
TRACE_CONTRACT_REQUIRED_FIELDS: set[str] = {"time", "event", "entity_id"}

# Optional but commonly emitted fields
TRACE_CONTRACT_OPTIONAL_FIELDS: set[str] = {
    "entity_type", "resource", "queue", "process", "state", "loss_type",
    "priority", "seq", "segment_id", "cycle_id", "next_state",
    # v5.2 — recommended for assignment_continuity and control_policy checks
    "server_id", "shift_id", "signal_source", "signal_time",
    "threshold_breach", "preempted_process",
}

# All canonical event names
CANONICAL_EVENTS: set[str] = {
    "system_arrival", "queue_enter", "service_start", "service_end",
    "preempt", "resume", "state_change", "system_departure", "loss",
}

# ─────────────────────────────────────────────────────────────────────────────
# Vague language patterns that cannot be operationalised
# ─────────────────────────────────────────────────────────────────────────────

VAGUE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bquickly\b", re.I),
    re.compile(r"\bsoon\b", re.I),
    re.compile(r"\bas needed\b", re.I),
    re.compile(r"\bwhen appropriate\b", re.I),
    re.compile(r"\busually\b", re.I),
    re.compile(r"\boften\b", re.I),
    re.compile(r"\bsometimes\b", re.I),
    re.compile(r"\boccasionally\b", re.I),
    re.compile(r"\btypically\b", re.I),
    re.compile(r"\bgenerally\b", re.I),
    re.compile(r"\bfrequently\b", re.I),
    re.compile(r"\bregularly\b", re.I),   # "regularly" alone is vague
    re.compile(r"\bperiodically\b", re.I),
    re.compile(r"\bif needed\b", re.I),
    re.compile(r"\bif necessary\b", re.I),
]

# ─────────────────────────────────────────────────────────────────────────────
# Compliance sections that require non-null critical fields
# ─────────────────────────────────────────────────────────────────────────────

REQUIRED_FIELDS_PER_SECTION: dict[str, list[str]] = {
    # Original sections
    "arrivals":              ["expected_rate_per_day", "tolerance"],
    "recurring_obligations": ["due_every_hours", "tolerance"],
    "service_rates":         ["resource", "tolerance"],
    "periodic_processes":    ["expected_per_day", "tolerance"],
    "scheduled_windows":     ["windows_hours"],
    "handoff_sequences":     ["cycle_hours", "rules"],
    "coordination_patterns": ["type", "resources"],
    "sequencing":            ["before", "after"],
    "temporal":              ["event"],
    "preemption_rules":      [],
    "state_transition_rules":["state"],
    "entity_type_constraints":["process"],
    "terminal_outcomes":     ["terminal_events"],
    "losses":                ["loss_type", "must_exist"],
    "flow_accounting":       ["identity"],
    "aggregate_targets":     ["metric", "expected"],
    "scenario_guidance":     ["name"],
    "process_contracts":     ["process_name", "trigger"],

    # NEW v5.2 sections
    "eligibility_rules":     ["process", "predicate"],
    "coverage_rules":        ["coverage_target", "interval_hours"],
    "routing_rules":         ["routing_from", "routing_to"],
    "assignment_continuity": ["process", "continuity_scope"],
    "policy_thresholds":     ["breach_condition", "escalation_outcome"],
    # semantic_scenarios is advisory-only (verified counterfactually in Phase
    # 4.5, not at pre-code time). The v5.2 mapper carries direction/magnitude in
    # the `expectation` block, not the deprecated `expected_directional_effect`
    # scalar (which it emits as None), so require only the structural fields —
    # requiring the deprecated field forced a permanent schema WARN.
    "semantic_scenarios":    ["name", "scenario_category"],
    "coupling_rules":        ["source_process", "target_process",
                              "coupling_type"],
    "control_policies":      ["policy_signals"],
    "pool_composition_rules":["process", "roles", "pool_policy", "windows"],
}


# ─────────────────────────────────────────────────────────────────────────────
# Check implementations
# ─────────────────────────────────────────────────────────────────────────────

def _check_coverage(
    coverage: CoverageReport,
    errors: list[str],
    warnings: list[str],
) -> None:
    """Check 1: every checkable DSL element has at least one compliance assertion."""
    if coverage.unmapped_items:
        errors.append(
            f"DSL-to-compliance coverage gap: {len(coverage.unmapped_items)} "
            f"checkable element(s) have no compliance assertion: "
            f"{coverage.unmapped_items}"
        )
    for item in coverage.items:
        if not item.not_checkable and not item.mapped_to:
            errors.append(
                f"  Unmapped: dsl_id={item.dsl_id} ({item.element_type}) "
                "has no linked compliance assertion and no not_checkable_reason."
            )


def _check_grounding(
    compliance_spec: dict[str, list],
    dsl_ids: set[str],
    errors: list[str],
    warnings: list[str],
) -> None:
    """Check 2: every compliance assertion cites at least one valid DSL source_id."""
    for section, entries in compliance_spec.items():
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                errors.append(f"  {section}[{i}] is not a dict.")
                continue
            source_ids = entry.get("source_ids", [])
            if not source_ids:
                warnings.append(
                    f"  {section}[{i}] has no source_ids — may be an ungrounded "
                    "LLM-invented assertion."
                )
                continue
            for sid in source_ids:
                if sid not in dsl_ids:
                    errors.append(
                        f"  {section}[{i}] source_id='{sid}' does not exist in DSL. "
                        "Orphan compliance assertion."
                    )


def _check_observability(
    compliance_spec: dict[str, list],
    trace_fields: set[str],
    trace_events: set[str],
    observability_gaps: list[str],
    warnings: list[str],
) -> None:
    """Check 3: compliance assertions only reference observable trace fields/events.

    Advisory-only sections (scenario_guidance, semantic_scenarios, coupling_rules,
    control_policies) are not subject to hard observability blocking — their
    gaps become warnings instead, since they are verified via Phase 4.5
    counterfactuals.
    """
    all_fields = trace_fields | TRACE_CONTRACT_OPTIONAL_FIELDS

    for section, entries in compliance_spec.items():
        if not entries:
            continue
        req = OBSERVABILITY_REQUIREMENTS.get(section, {})
        req_events: list[str] = req.get("required_events", [])
        req_fields: list[str] = req.get("required_fields", [])
        is_advisory = section in ADVISORY_ONLY_SECTIONS

        for ev in req_events:
            if ev not in trace_events:
                msg = (
                    f"  Section '{section}' requires event '{ev}' "
                    "which is not in the declared trace contract. "
                    "Either enrich the trace contract or remove this section."
                )
                (warnings if is_advisory else observability_gaps).append(msg)
        for fld in req_fields:
            if fld not in all_fields:
                msg = (
                    f"  Section '{section}' requires trace field '{fld}' "
                    "which is not in the declared trace contract."
                )
                (warnings if is_advisory else observability_gaps).append(msg)


def _check_schema(
    compliance_spec: dict[str, list],
    errors: list[str],
    warnings: list[str],
) -> None:
    """Check 5: compliance_spec has expected structure and required non-null fields.

    Advisory-only sections are exempt from BLOCKED on missing fields — they
    produce WARN instead because they're verified downstream in Phase 4.5.
    """
    for section, required_fields in REQUIRED_FIELDS_PER_SECTION.items():
        if section not in compliance_spec:
            errors.append(f"  compliance_spec missing section '{section}'.")
            continue
        if not isinstance(compliance_spec[section], list):
            errors.append(f"  compliance_spec['{section}'] must be a list.")
            continue
        for i, entry in enumerate(compliance_spec[section]):
            if not isinstance(entry, dict):
                errors.append(f"  compliance_spec['{section}'][{i}] is not a dict.")
                continue
            for f in required_fields:
                if f not in entry or entry[f] is None:
                    warnings.append(
                        f"  compliance_spec['{section}'][{i}] "
                        f"missing required field '{f}'."
                    )


def _check_vague_clauses(
    elements: list[DslElement],
    warnings: list[str],
) -> None:
    """Check 4: flag vague language and assumed defaults without a reason."""
    for el in elements:
        # Assumed-default discipline (v5.2)
        if getattr(el, "assumed_default", False) and not getattr(el, "assumed_default_reason", None):
            warnings.append(
                f"  DSL element {el.id} ({el.element_type}) is flagged "
                "assumed_default=True but has no assumed_default_reason. "
                "Supply a short rationale describing the assumption."
            )

        if el.not_checkable_reason:
            continue  # explicitly acknowledged
        for pat in VAGUE_PATTERNS:
            if pat.search(el.description or ""):
                warnings.append(
                    f"  DSL element {el.id} ({el.element_type}) contains vague language "
                    f"matching '{pat.pattern}' in description: \"{(el.description or '')[:80]}…\". "
                    "Either operationalise it or add a not_checkable_reason."
                )
                break  # one warning per element is enough


def _check_routing_probabilities(
    compliance_spec: dict[str, list],
    warnings: list[str],
    errors: list[str],
) -> None:
    """Additional v5.2 check: routing_rules probabilities should sum to ~1.0.

    One routing_rules entry is ONE decision point (one routing_from). Its
    branches are mutually-exclusive alternatives whose probabilities sum to
    1.0 ACROSS ALL branches — `condition` is each branch's predicate/label
    (e.g. 'good_machining' vs 'needs_rework'), not a regime selector, so the
    branches must be summed together, NOT grouped by their distinct condition
    labels. (Grouping by condition made every distinctly-labelled branch a
    1-element group that could never sum to 1.0 unless its single probability
    were 1.0 — a spurious error on any normal probabilistic split.)

    Genuine conditional routing TABLES (different branch sets per regime) are
    declared as separate routing_rules entries, one per regime, so per-entry
    summation still holds.
    """
    for i, entry in enumerate(compliance_spec.get("routing_rules", [])):
        routes = entry.get("routing_to") or []
        if not routes:
            continue
        total = 0.0
        missing_prob = False
        for r in routes:
            if not isinstance(r, dict):
                continue
            p = r.get("probability")
            if p is None:
                missing_prob = True
                continue
            total += float(p)
        if missing_prob and not entry.get("fallback_target"):
            warnings.append(
                f"  routing_rules[{i}] has routes with no probability and no "
                "fallback_target — coverage may be incomplete."
            )
        # Only enforce the sum when every branch carried a probability (a
        # fallback_target legitimately absorbs the remainder otherwise).
        if not missing_prob and abs(total - 1.0) > 0.02:
            errors.append(
                f"  routing_rules[{i}] branch probabilities sum to {total:.3f}, "
                "not 1.0 (summed across all branches of the decision point)."
            )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def validate(
    elements: list[DslElement],
    compliance_spec: dict[str, list],
    coverage_report: CoverageReport,
    *,
    trace_fields: set[str] | None = None,
    trace_events: set[str] | None = None,
) -> ValidationResult:
    """Run all validation checks and return a ValidationResult.

    Args:
        elements:         List of DslElement objects from the DSL.
        compliance_spec:  Generated compliance_spec dict.
        coverage_report:  Coverage report from compliance_mapper.
        trace_fields:     Set of field names the model's trace emits.
                          Defaults to TRACE_CONTRACT_REQUIRED_FIELDS ∪ optional.
        trace_events:     Set of event names the model emits.
                          Defaults to CANONICAL_EVENTS.
    """
    if trace_fields is None:
        trace_fields = TRACE_CONTRACT_REQUIRED_FIELDS
    if trace_events is None:
        trace_events = CANONICAL_EVENTS

    errors: list[str] = []
    warnings: list[str] = []
    observability_gaps: list[str] = []
    grounding_errors: list[str] = []

    dsl_ids = {el.id for el in elements}

    # Run all checks
    _check_coverage(coverage_report, errors, warnings)
    _check_grounding(compliance_spec, dsl_ids, grounding_errors, warnings)
    _check_observability(compliance_spec, trace_fields, trace_events,
                         observability_gaps, warnings)
    _check_schema(compliance_spec, errors, warnings)
    _check_vague_clauses(elements, warnings)
    _check_routing_probabilities(compliance_spec, warnings, errors)

    # Combine grounding errors into main errors list for verdict
    all_errors = errors + grounding_errors

    if all_errors or observability_gaps:
        status = "BLOCKED"
    elif warnings:
        status = "NEEDS_REVISION"
    else:
        status = "PASS"

    return ValidationResult(
        status=status,
        errors=errors,
        warnings=warnings,
        observability_gaps=observability_gaps,
        grounding_errors=grounding_errors,
        coverage_fraction=coverage_report.coverage_fraction,
    )


def print_validation_report(result: ValidationResult, label: str = "VALIDATION") -> None:
    """Print a formatted validation report."""
    STATUS_ICONS = {"PASS": "✓", "NEEDS_REVISION": "⚠", "BLOCKED": "⊘"}
    icon = STATUS_ICONS.get(result.status, "?")

    print(f"\n{'=' * 70}")
    print(f"DSL PIPELINE — {label}")
    print("=" * 70)
    print(f"\n  {icon} Status:   {result.status}")
    print(f"  Coverage: {result.coverage_fraction:.0%}")

    if result.errors:
        print(f"\n  BLOCKING ERRORS ({len(result.errors)}):")
        for e in result.errors:
            print(f"    ✗ {e}")
    if result.grounding_errors:
        print(f"\n  GROUNDING ERRORS ({len(result.grounding_errors)}):")
        for e in result.grounding_errors:
            print(f"    ✗ {e}")
    if result.observability_gaps:
        print(f"\n  OBSERVABILITY GAPS ({len(result.observability_gaps)}):")
        for o in result.observability_gaps:
            print(f"    ⊘ {o}")
    if result.warnings:
        print(f"\n  WARNINGS ({len(result.warnings)}):")
        for w in result.warnings:
            print(f"    ⚠ {w}")
    if result.status == "PASS":
        print("\n  All checks passed. Artifact approved for code generation.")
    elif result.status == "NEEDS_REVISION":
        print("\n  Warnings require review. Code generation allowed with caution.")
    else:
        print("\n  BLOCKED: Resolve errors before code generation proceeds.")
    print()
