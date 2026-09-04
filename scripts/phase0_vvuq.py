"""
VVUQ Phase 0: Generic Compliance & Structural Validation
verification_suite_queueing_systems_v5.2  (semantic extensions)

Architecture (three layers + extensions)
=========================================
Layer A — Universal structural gate (A1–A8)
    Halts the pipeline on failure; trace is unreliable.

Layer B — Declarative contract gate (B01–B17)
    Driven entirely by `compliance_spec`; no domain knowledge hard-coded.

Layer B Extended — Process contracts (B18–B23)     → process_contracts.py
Layer B Extended — Semantic contracts (B24–B41)    → semantic_checks.py   [v5.2]

Layer C — Scenario smoke tests (advisory)
    Phase 0 emits INFO advisories only.
    Phase 4.5 (semantic_scenarios.py) handles counterfactual verification.

Domain-agnostic design
======================
This file contains NO hard-coded simulation behavior. Every check reads
from `ctx.config["compliance_spec"]` and `ctx.trace`. The pre-code mapper
(compliance_mapper.py) is what populates `compliance_spec` from the DSL;
phase0_vvuq.py just consumes it. The same checks fire on any simulation
that emits the canonical trace contract — ICU, manufacturing, ED, or
anything else.

Entry point
===========
    results = run_phase0(result)   # result = run_simulation(...) output
    status  = print_phase0_summary(results, "PHASE 0")

For a worked example with a ready-made compliance_spec, see
`examples/manufacturing_spec.py`. The `__main__` block at the bottom of
this file is also a thin demo; it imports an external sim only when
invoked directly, never at library-import time.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from typing import Any

# ── resolve simulation module path (for users who want to point the demo at a
# case-study sim via $VVUQ_SIM_PATH; library callers using run_phase0(...)
# don't need this).
_sim_path = os.environ.get("VVUQ_SIM_PATH", "")
if _sim_path and _sim_path not in sys.path:
    sys.path.insert(0, _sim_path)

# Library-API users provide the sim externally (run_vvuq.py imports it
# dynamically); phase0_vvuq has no static sim dependency. The `__main__`
# demo at the bottom of this file is the only place that loads a sim,
# and it does so via importlib so the failure mode is a clear message
# rather than an ImportError at module load.
run_simulation = None
DEFAULT_CONFIG: dict = {}
from vvuq_utils import (
    CheckResult, Phase0Context, Severity,
    post_warmup_events, filter_events, group_by_entity,
    match_event_pattern, compare_ratio,
    print_phase0_summary,
)
from process_contracts import validate_process_contracts
from semantic_checks import validate_semantic_contracts


# ─────────────────────────────────────────────────────────────────────────────
# Example compliance_spec — manufacturing case study
# ─────────────────────────────────────────────────────────────────────────────
# This is illustrative only. Phase 0's check functions never read it; they
# read whatever `compliance_spec` the user injects into the config. It is
# kept here as a worked example for the `__main__` demo and for users who
# want a starting template for a new simulation. The simulation's own
# compliance_spec — produced by compliance_mapper.py from the DSL — is the
# canonical input. Move this block to examples/manufacturing_spec.py at
# your convenience; nothing else in the framework imports it.
_EXAMPLE_MANUFACTURING_SPEC: dict[str, Any] = {

    # ── B01: arrival rate ────────────────────────────────────────────────────
    "arrivals": [
        {
            "entity_type": None,
            "expected_rate_per_day": 1_440.0,
            "tolerance": 0.05,
        }
    ],

    # ── B02: recurring obligations (N/A for manufacturing) ───────────────────
    "recurring_obligations": [],

    # ── B03: service rates ───────────────────────────────────────────────────
    "service_rates": [
        {"resource": "machining",  "entity_type": None, "per_entity_hour": 40.0,  "tolerance": 0.40},
        {"resource": "polishing",  "entity_type": None, "per_entity_hour": 55.0,  "tolerance": 0.40},
        {"resource": "packaging",  "entity_type": None, "per_entity_hour": 60.0,  "tolerance": 0.40},
    ],

    # ── B04-B07: N/A for this model ──────────────────────────────────────────
    "periodic_processes":    [],
    "scheduled_windows":     [],
    "handoff_sequences":     [],
    "coordination_patterns": [],

    # ── B08: sequencing ──────────────────────────────────────────────────────
    "sequencing": [
        {
            "entity_type": None,
            "before": {"event": "service_start", "resource": "machining"},
            "after":  {"event": "service_start", "resource": "polishing"},
            "description": "Machining before polishing",
        },
        {
            "entity_type": None,
            "before": {"event": "service_start", "resource": "polishing"},
            "after":  {"event": "service_start", "resource": "packaging"},
            "description": "Polishing before packaging (good parts only)",
        },
    ],

    # ── B09: temporal ────────────────────────────────────────────────────────
    "temporal": [
        {
            "entity_type": None,
            "event": "system_departure",
            "min_seconds": 40.0,
            "description": "Structural minimum cycle time (travel only)",
        }
    ],

    # ── B10: preemption rules ─────────────────────────────────────────────────
    "preemption_rules": [
        {
            "applies_to_processes": [],
            "require_resume_or_terminal_exit": True,
            "expected_preempt_count": 0,
            "tolerance": 0.0,
        }
    ],

    # ── B11-B12: N/A ─────────────────────────────────────────────────────────
    "state_transition_rules":  [],
    "entity_type_constraints": [],

    # ── B13: terminal outcomes ────────────────────────────────────────────────
    "terminal_outcomes": [
        {
            "entity_type": None,
            "terminal_events": ["system_departure", "loss"],
            "exactly_one_terminal_event": True,
        }
    ],

    # ── B14: losses ───────────────────────────────────────────────────────────
    "losses": [
        {"loss_type": "scrap",   "must_exist": True},
        {"loss_type": "balking", "must_exist": False},
    ],

    # ── B15: flow accounting ──────────────────────────────────────────────────
    "flow_accounting": [
        {"name": "system_balance",
         "identity": "arrivals = departures + losses + in_system_end"}
    ],

    # ── B16: aggregate targets ────────────────────────────────────────────────
    "aggregate_targets": [
        {"metric": "scrap_rate",              "expected": 0.18,        "tolerance": 0.20},
        {"metric": "utilization_polishing",   "expected": 55.0 / 60.0, "tolerance": 0.05},
        {"metric": "avg_machining_passes",    "expected": 1.0 / 0.9,   "tolerance": 0.03},
    ],

    # ── B17: scenario guidance ────────────────────────────────────────────────
    "scenario_guidance": [
        {
            "name": "ample_capacity_schedule_test",
            "type": "scenario_check",
            "description": "Verify arrival and service rates at very low load (no congestion)",
        },
        {
            "name": "congestion_stress_test",
            "type": "scenario_check",
            "description": "Verify rework cascade behavior near instability boundary",
        },
        {
            "name": "shutdown_robustness",
            "type": "scenario_check",
            "description": "Verify clean termination with active entities in system",
        },
    ],

    # ── v5.2 semantic sections — empty placeholders for the manufacturing demo ─
    "eligibility_rules":      [],
    "coverage_rules":         [],
    "routing_rules":          [],
    "assignment_continuity":  [],
    "policy_thresholds":      [],
    "semantic_scenarios":     [],
    "coupling_rules":         [],
    "control_policies":       [],
    "process_contracts":      [],
}


# ─────────────────────────────────────────────────────────────────────────────
# LAYER A — Universal structural gate
# ─────────────────────────────────────────────────────────────────────────────

# Universal keys every sim must declare — absence is a structural BLOCK.
_REQUIRED_CONFIG_KEYS = ["run_length", "warmup_time", "seed"]

# Conventional config keys most discrete-event simulations declare.
# Absence is an INFO advisory, not a block — a sim may legitimately omit
# any of these (e.g. a deterministic-arrival workshop has no
# `arrival_distribution`). These are domain-agnostic conventions, not
# case-study specifics.
_RECOMMENDED_CONFIG_KEYS = [
    "arrival_distribution", "service_distributions", "resources",
]

_CANONICAL_EVENTS = {
    "system_arrival", "queue_enter", "service_start", "service_end",
    "preempt", "resume", "state_change", "system_departure", "loss",
}


def _check_a1_config_schema(ctx: Phase0Context) -> list[CheckResult]:
    """A1: Required config sections are present.

    Universal keys (_REQUIRED_CONFIG_KEYS) missing → BLOCK.
    Recommended keys (_RECOMMENDED_CONFIG_KEYS) missing → INFO advisory only
    (they're specific to the manufacturing case study shape).
    """
    out: list[CheckResult] = []
    missing_required = [k for k in _REQUIRED_CONFIG_KEYS if k not in ctx.config]
    if missing_required:
        out.append(CheckResult(
            "BLOCK", "A1_config_schema",
            f"Missing required config keys: {missing_required}",
            {"missing": missing_required}))
        return out
    missing_recommended = [k for k in _RECOMMENDED_CONFIG_KEYS if k not in ctx.config]
    out.append(CheckResult(
        "PASS", "A1_config_schema",
        f"All {len(_REQUIRED_CONFIG_KEYS)} required config keys present."))
    if missing_recommended:
        out.append(CheckResult(
            "INFO", "A1_recommended_keys",
            f"Recommended config keys absent: {missing_recommended}. "
            "These are conventional keys, not structural requirements."))
    return out


def _check_a2_result_structure(result: dict) -> list[CheckResult]:
    """A2: run_simulation() returns trace, metrics, config."""
    missing = [k for k in ("trace", "metrics", "config") if k not in result]
    if missing:
        return [CheckResult("BLOCK", "A2_result_structure",
                            f"Result dict missing keys: {missing}")]
    if not isinstance(result["trace"], list):
        return [CheckResult("BLOCK", "A2_result_structure",
                            "result['trace'] is not a list.")]
    if not isinstance(result["metrics"], dict):
        return [CheckResult("BLOCK", "A2_result_structure",
                            "result['metrics'] is not a dict.")]
    return [CheckResult("PASS", "A2_result_structure",
                        f"Result has trace ({len(result['trace']):,} events), "
                        f"metrics ({len(result['metrics'])} keys), config.")]


def _check_a3_trace_completeness(ctx: Phase0Context) -> list[CheckResult]:
    """A3: Every event has time, event, entity_id; warn on missing optional fields."""
    required = {"time", "event", "entity_id"}
    missing_fields: dict[str, int] = defaultdict(int)
    for ev in ctx.trace:
        for f in required:
            if f not in ev:
                missing_fields[f] += 1

    if missing_fields:
        return [CheckResult("BLOCK", "A3_trace_completeness",
                            f"Required fields missing from events: {dict(missing_fields)}",
                            {"missing_fields": dict(missing_fields)})]

    optional = {"entity_type", "resource"}
    absent_optional = [f for f in optional if not any(f in ev for ev in ctx.trace)]
    out = [CheckResult(
        "PASS", "A3_trace_completeness",
        f"All {len(ctx.trace):,} events have required fields."
        + (f" Optional fields absent: {absent_optional}." if absent_optional else ""),
    )]
    if absent_optional:
        out.append(CheckResult("INFO", "A3_optional_fields",
                               f"Recommended optional fields entirely absent: {absent_optional}. "
                               "Some Layer B checks may be skipped."))
    return out


def _check_a4_chronological_order(ctx: Phase0Context) -> list[CheckResult]:
    """A4: time non-decreasing; seq (if present) strictly increasing."""
    bad_time = 0
    bad_seq = 0
    prev_t = -1.0
    prev_seq: int | None = None

    for ev in ctx.trace:
        t = ev["time"]
        if t < prev_t - 1e-9:
            bad_time += 1
        prev_t = t
        s = ev.get("seq")
        if s is not None:
            if prev_seq is not None and s <= prev_seq:
                bad_seq += 1
            prev_seq = s

    issues = []
    if bad_time:  issues.append(f"{bad_time} time reversals")
    if bad_seq:   issues.append(f"{bad_seq} seq non-increases")
    if issues:
        return [CheckResult("BLOCK", "A4_chronological_order",
                            "Ordering violations: " + "; ".join(issues))]
    return [CheckResult("PASS", "A4_chronological_order",
                        f"All {len(ctx.trace):,} events in non-decreasing time order.")]


def _check_a5_canonical_events(ctx: Phase0Context) -> list[CheckResult]:
    """A5: No unknown event names."""
    seen = {ev["event"] for ev in ctx.trace}
    unknown = sorted(seen - _CANONICAL_EVENTS)
    known   = sorted(seen & _CANONICAL_EVENTS)
    if unknown:
        return [CheckResult("WARN", "A5_canonical_events",
                            f"Unknown event types: {unknown}. Known: {known}.",
                            {"unknown": unknown})]
    return [CheckResult("PASS", "A5_canonical_events",
                        f"All event types canonical: {known}.")]


def _check_a6_entity_lifecycle(ctx: Phase0Context) -> list[CheckResult]:
    """A6: Exactly one system_arrival per entity_id."""
    counts: dict[str, int] = defaultdict(int)
    for ev in ctx.trace:
        if ev["event"] == "system_arrival":
            counts[ev["entity_id"]] += 1
    multi = {eid: c for eid, c in counts.items() if c > 1}
    if multi:
        sample = dict(list(multi.items())[:5])
        return [CheckResult("FAIL", "A6_entity_lifecycle",
                            f"{len(multi)} entities with >1 arrival (sample: {sample}).",
                            {"multi_arrival_count": len(multi)})]
    return [CheckResult("PASS", "A6_entity_lifecycle",
                        f"{len(counts):,} entities, each with exactly one system_arrival.")]


def _check_a7_determinism(ctx: Phase0Context) -> list[CheckResult]:
    """A7: Same seed → same trace prefix (reruns and compares first 50 events).

    When this module is imported without manufacturing_sim being available
    (module-level run_simulation is None), A7 cannot re-execute the sim from
    Phase 0 and self-skips with INFO. In that mode the Phase 0 caller
    (e.g. run_vvuq.py) should drive determinism externally or re-inject
    the sim via VVUQ_RUN_SIMULATION.
    """
    sim = run_simulation or globals().get("VVUQ_RUN_SIMULATION")
    if sim is None:
        return [CheckResult(
            "INFO", "A7_determinism",
            "Skipped: no run_simulation available to re-run. "
            "Set phase0_vvuq.VVUQ_RUN_SIMULATION to a callable to enable A7.")]
    try:
        r2 = sim(ctx.config)
    except Exception as exc:
        return [CheckResult("BLOCK", "A7_determinism",
                            f"Second run raised {type(exc).__name__}: {exc}")]
    check_len = min(50, len(ctx.trace), len(r2["trace"]))
    mismatches = sum(
        1 for i in range(check_len)
        if ctx.trace[i].get("event") != r2["trace"][i].get("event")
        or abs(ctx.trace[i]["time"] - r2["trace"][i]["time"]) > 1e-9
    )
    if mismatches:
        return [CheckResult("FAIL", "A7_determinism",
                            f"{mismatches}/{check_len} mismatches with same seed.")]
    return [CheckResult("PASS", "A7_determinism",
                        f"First {check_len} events identical across two runs with same seed.")]


def _check_a8_clean_shutdown(ctx: Phase0Context) -> list[CheckResult]:
    """A8: No service_end without a preceding service_start for the same
    (resource, entity).

    Pairing semantics: we count opens and closes per (resource, entity_id)
    rather than using a set. The set-based implementation collapses
    multiple sequential or overlapping services on the same (resource,
    entity_id) pair into a single tracked-open, which then under-counts
    opens when closes catch up — producing phantom orphans for any sim
    where an entity is serviced multiple times on the same resource
    (re-entry, preemption-resume on overlapping segments, repeated checks
    such as hourly nurse_check on a patient). The counter-based pairing
    flags a true orphan only when the running close count exceeds the
    running open count for a given (resource, entity_id) pair.

    Segment-id pairing when available: if events carry a non-null
    segment_id, the pairing key includes it. This allows the check to
    distinguish overlapping segments that share (resource, entity_id) —
    the more precise pairing the framework already uses in B29 and the
    §4.2.x preemption checks.
    """
    opens: dict[tuple, int] = defaultdict(int)
    closes: dict[tuple, int] = defaultdict(int)
    # Also accumulate a no-segment-id fallback view per (resource, entity_id)
    # so we can detect cases where the trace mixes segment-id-bearing and
    # segment-id-less events for the same pair.
    for ev in ctx.trace:
        rn = ev.get("resource")
        if not rn:
            continue
        eid = ev["entity_id"]
        seg = ev.get("segment_id")
        # Prefer (resource, entity, segment) keying when segment is present;
        # fall back to (resource, entity) otherwise. Each event uses its own
        # key — a sim that emits segment_id on starts but not ends, or vice
        # versa, will surface as orphans here, which is the correct outcome
        # because the pairing contract is broken.
        key = (rn, eid, seg) if seg is not None else (rn, eid)
        if ev["event"] == "service_start":
            opens[key] += 1
        elif ev["event"] == "service_end":
            closes[key] += 1
    unmatched: dict[str, int] = defaultdict(int)
    for key, n_close in closes.items():
        n_open = opens.get(key, 0)
        if n_close > n_open:
            rn = key[0]
            unmatched[rn] += n_close - n_open
    if unmatched:
        return [CheckResult("FAIL", "A8_clean_shutdown",
                            f"Orphan service_end events (close > open per "
                            f"(resource, entity[, segment])): {dict(unmatched)}",
                            {"unmatched": dict(unmatched)})]
    in_service = sum(
        max(0, opens.get(k, 0) - closes.get(k, 0))
        for k in set(opens) | set(closes)
    )
    return [CheckResult("PASS", "A8_clean_shutdown",
                        f"No orphan service_end events. "
                        f"{in_service} (resource, entity, segment) pairs "
                        f"still in service at end (expected).")]


# ─────────────────────────────────────────────────────────────────────────────
# LAYER B — Declarative contract checkers
# ─────────────────────────────────────────────────────────────────────────────

def verify_arrivals(ctx: Phase0Context) -> list[CheckResult]:
    """B01: Arrival rate per day vs declared expected_rate_per_day.

    Regime-aware windowing: for terminating/burst simulations, arrivals
    occur only over a declared window that is typically much shorter
    than the run_length (which must extend past the arrival window so
    the system can drain). Measuring rate as arrivals ÷ full_run_length
    produces an artifactually low observed rate that depends on the
    arbitrary drain-buffer choice rather than on the modelled arrival
    process. When ``simulation_regime.type ∈ {terminating, burst}`` and
    ``arrival_window_seconds`` is declared, this check counts only
    arrivals that fell inside the declared window and divides by the
    window length (in days) rather than by effective_days. For
    steady-state simulations the behaviour is unchanged.
    """
    pwu = post_warmup_events(ctx)
    out: list[CheckResult] = []
    regime = ctx.simulation_regime
    terminating = ctx.is_terminating
    window_end = None
    if terminating and regime.get("arrival_window_seconds") is not None:
        try:
            window_end = ctx.warmup + float(regime["arrival_window_seconds"])
        except (TypeError, ValueError):
            window_end = None
    for i, spec in enumerate(ctx.spec.get("arrivals", [])):
        etype = spec.get("entity_type")
        arrivals = filter_events(pwu, event="system_arrival", entity_type=etype)
        if window_end is not None:
            arrivals_in_window = [ev for ev in arrivals
                                  if float(ev.get("time", 0.0)) <= window_end]
            n = len(arrivals_in_window)
            window_days = ctx.rate_measurement_window_days
            observed = n / window_days if window_days > 0 else 0.0
            base_result = compare_ratio(
                observed, spec["expected_rate_per_day"],
                spec.get("tolerance", 0.15),
                f"B01_arrivals[{i}]", "/day")
            # Annotate the diagnostic with the windowing note so the
            # measurement basis is visible in the report. compare_ratio
            # returns a CheckResult with (severity, check_name, message,
            # details). We rebuild the message to include the window
            # rationale.
            annotated = CheckResult(
                severity=base_result.severity,
                check_name=base_result.check_name,
                message=(
                    base_result.message
                    + f" [regime={regime['type']}: measured over declared "
                    + f"arrival window of {regime['arrival_window_seconds']}s "
                    + f"({window_days:.4f} days), not full run_length]"
                ),
                details=getattr(base_result, "details", None),
            )
            out.append(annotated)
        else:
            n = len(arrivals)
            observed = n / ctx.effective_days if ctx.effective_days > 0 else 0.0
            out.append(compare_ratio(observed, spec["expected_rate_per_day"],
                                     spec.get("tolerance", 0.15),
                                     f"B01_arrivals[{i}]", "/day"))
    if not out:
        out.append(CheckResult("INFO", "B01_arrivals", "No arrival specs; skipped."))
    return out


def verify_recurring_obligations(ctx: Phase0Context) -> list[CheckResult]:
    """B02: Time-driven recurring obligations per entity-hour."""
    pwu = post_warmup_events(ctx)
    specs = ctx.spec.get("recurring_obligations", [])
    if not specs:
        return [CheckResult("INFO", "B02_recurring_obligations",
                            "No recurring obligation specs; skipped.")]
    exposure = ctx.avg_census * ctx.effective_hours
    if exposure <= 0:
        return [CheckResult("INFO", "B02_recurring_obligations",
                            "Exposure = 0; skipped.")]
    out: list[CheckResult] = []
    for i, spec in enumerate(specs):
        # AUDIT FIX (H4): the mapper emits due_every_hours directly from the
        # DSL element (which permits null recurrence_hours), so this value
        # can legitimately be None. `None > 0` raises TypeError, and with
        # B01–B17 previously unwrapped that crashed the whole gate. Skip
        # with INFO — a recurring obligation with no declared cadence has
        # no rate assertion to verify (same convention as compare_ratio's
        # None-expected guard).
        due_every = spec.get("due_every_hours")
        if due_every is None or (isinstance(due_every, (int, float))
                                  and due_every <= 0):
            out.append(CheckResult(
                "INFO", f"B02_recurring_obligations[{i}]",
                f"no positive due_every_hours declared "
                f"(got {due_every!r}); cadence check skipped. Declare a "
                f"recurrence to enable verification."))
            continue
        expected = 1.0 / float(due_every)
        cnt = len(filter_events(pwu, event="service_start",
                                entity_type=spec.get("entity_type"),
                                process=spec.get("process"),
                                resource=spec.get("responsible_role")))
        out.append(compare_ratio(cnt / exposure, expected,
                                 spec.get("tolerance", 0.15),
                                 f"B02_recurring_obligations[{i}]", "/entity-hour"))
    return out


def verify_service_rates(ctx: Phase0Context) -> list[CheckResult]:
    """B03: observed mean service time at each declared resource matches the
    declared mean.

    Measures the per-resource mean service duration directly from paired
    service_start/service_end events and compares it to the declared mean
    service time. This is correct for any number of stages.

    Rationale for this formula (VALIDATOR fix): the earlier B03 compared a
    declared *service rate* (per_entity_hour = 3600/mean_sec, produced by
    compliance_mapper) against `service_starts / (avg_census × effective_hours)`
    — a system-wide, census-normalized frequency. Those are different
    quantities: for a single-stage queue the latter loosely tracks the rate,
    but for a multi-stage line `avg_census` spans every stage, so one station's
    service_starts divided by the whole-system census is unrelated to that
    station's service time, and B03 would FAIL every stage regardless of the
    simulation. Measuring the observed service duration per resource removes
    the dependency on system-wide census and is stage-count-agnostic.
    """
    pwu = post_warmup_events(ctx)
    specs = ctx.spec.get("service_rates", [])
    if not specs:
        return [CheckResult("INFO", "B03_service_rates", "No service rate specs; skipped.")]

    out: list[CheckResult] = []
    for i, spec in enumerate(specs):
        resource = spec.get("resource")
        entity_type = spec.get("entity_type")
        process = spec.get("process")

        # Declared mean service time (seconds). Prefer mean_service_seconds;
        # fall back to deriving it from per_entity_hour for older specs.
        expected_mean = spec.get("mean_service_seconds")
        if expected_mean is None and spec.get("per_entity_hour"):
            try:
                expected_mean = 3_600.0 / float(spec["per_entity_hour"])
            except (TypeError, ZeroDivisionError):
                expected_mean = None
        if expected_mean is None:
            out.append(CheckResult(
                "INFO", f"B03_service_rates[{i}]",
                f"resource='{resource}' has no declared mean service time "
                "(mean_service_seconds / per_entity_hour); semantic-only entry skipped."))
            continue

        # Pair service_start/resume → service_end/preempt per (entity, segment)
        # at this resource and collect observed durations.
        open_starts: dict[tuple, float] = {}
        durations: list[float] = []
        for ev in sorted(pwu, key=lambda e: (e.get("time", 0.0), e.get("seq", 0))):
            if ev.get("resource") != resource:
                continue
            if entity_type is not None and ev.get("entity_type") != entity_type:
                continue
            if process is not None and ev.get("process") != process:
                continue
            k = (ev.get("entity_id"), ev.get("segment_id"))
            evt = ev.get("event")
            if evt in ("service_start", "resume"):
                open_starts[k] = ev.get("time", 0.0)
            elif evt in ("service_end", "preempt"):
                t0 = open_starts.pop(k, None)
                if t0 is not None:
                    durations.append(ev.get("time", 0.0) - t0)

        if not durations:
            out.append(CheckResult(
                "INFO", f"B03_service_rates[{i}]",
                f"resource='{resource}' has no completed service intervals to measure."))
            continue

        observed_mean = sum(durations) / len(durations)
        out.append(compare_ratio(observed_mean, expected_mean,
                                 spec.get("tolerance", 0.15),
                                 f"B03_service_rates[{i}]", "s mean service time"))
    return out


def verify_periodic_processes(ctx: Phase0Context) -> list[CheckResult]:
    """B04: Scheduled process count per effective day."""
    pwu = post_warmup_events(ctx)
    out: list[CheckResult] = []
    for i, spec in enumerate(ctx.spec.get("periodic_processes", [])):
        cnt = len(filter_events(pwu, event=spec.get("event"),
                                entity_type=spec.get("entity_type"),
                                process=spec.get("process")))
        observed = cnt / ctx.effective_days if ctx.effective_days > 0 else 0.0
        out.append(compare_ratio(observed, spec["expected_per_day"],
                                 spec.get("tolerance", 0.10),
                                 f"B04_periodic_processes[{i}]", "/day"))
    if not out:
        out.append(CheckResult("INFO", "B04_periodic_processes",
                               "No periodic process specs; skipped."))
    return out


def verify_scheduled_windows(ctx: Phase0Context) -> list[CheckResult]:
    """B05: Declared events occur inside specified time-of-day windows."""
    pwu = post_warmup_events(ctx)
    out: list[CheckResult] = []
    for i, spec in enumerate(ctx.spec.get("scheduled_windows", [])):
        relevant = filter_events(pwu, event=spec.get("event"),
                                 entity_type=spec.get("entity_type"),
                                 process=spec.get("process"))
        if not relevant:
            out.append(CheckResult("FAIL", f"B05_scheduled_windows[{i}]",
                                   "No matching events in post-warmup trace."))
            continue
        raw_windows = spec.get("windows_hours") or []
        # Accept either a list of [lo,hi] pairs or a single flat [lo,hi];
        # iterating `for lo, hi in windows` on a flat [8, 20] would raise
        # "too many values to unpack" and crash the check.
        if (len(raw_windows) == 2
                and all(isinstance(x, (int, float)) for x in raw_windows)):
            windows = [(float(raw_windows[0]), float(raw_windows[1]))]
        else:
            windows = [(float(w[0]), float(w[1])) for w in raw_windows
                       if isinstance(w, (list, tuple)) and len(w) == 2]
        if not windows:
            out.append(CheckResult("INFO", f"B05_scheduled_windows[{i}]",
                                   "No valid windows_hours declared; skipped."))
            continue
        def in_window(t: float, _w=windows) -> bool:
            h = (t % 86_400.0) / 3_600.0
            return any(lo <= h <= hi for lo, hi in _w)
        frac = sum(1 for e in relevant if in_window(e["time"])) / len(relevant)
        out.append(compare_ratio(frac, 1.0, spec.get("tolerance", 0.10),
                                 f"B05_scheduled_windows[{i}]", " in-window fraction"))
    if not out:
        out.append(CheckResult("INFO", "B05_scheduled_windows",
                               "No scheduled window specs; skipped."))
    return out


def verify_handoff_sequences(ctx: Phase0Context) -> list[CheckResult]:
    """B06: Role ordering and occurrence limits within declared cycles."""
    pwu = post_warmup_events(ctx)
    out: list[CheckResult] = []
    for i, spec in enumerate(ctx.spec.get("handoff_sequences", [])):
        cycle_sec = spec["cycle_hours"] * 3_600.0
        process = spec.get("process")
        etype = spec.get("entity_type")
        groups = group_by_entity(pwu)
        violations = checked = 0
        for _, evs in groups.items():
            relevant = [e for e in evs
                        if e.get("event") == "service_start"
                        and (etype is None or e.get("entity_type") == etype)
                        and (process is None or e.get("process") == process)]
            if not relevant:
                continue
            by_cycle: dict[int, list] = defaultdict(list)
            for e in relevant:
                by_cycle[int(e["time"] // cycle_sec)].append(e)
            for _, cyc in by_cycle.items():
                cyc.sort(key=lambda x: (x["time"], x.get("seq", 0)))
                checked += 1
                first: dict[str, dict] = {}
                for e in cyc:
                    first.setdefault(e.get("resource"), e)
                ok = True
                for rule in spec.get("rules", []):
                    role = rule["role"]
                    if rule.get("ordinal") == "first":
                        if not cyc or cyc[0].get("resource") != role:
                            ok = False
                    for dep_key in ("after_role", "after_roles"):
                        deps = rule.get(dep_key, [])
                        if isinstance(deps, str):
                            deps = [deps]
                        for dep in deps:
                            if (role not in first or dep not in first
                                    or first[role]["time"] < first[dep]["time"]):
                                ok = False
                    if "max_per_cycle" in rule:
                        if sum(1 for e in cyc if e.get("resource") == role) > rule["max_per_cycle"]:
                            ok = False
                if not ok:
                    violations += 1
        sev: Severity = "PASS" if violations == 0 else "FAIL"
        out.append(CheckResult(sev, f"B06_handoff_sequences[{i}]",
                               f"{violations}/{checked} cycles violate handoff rules.",
                               {"violations": violations, "checked": checked}))
    if not out:
        out.append(CheckResult("INFO", "B06_handoff_sequences",
                               "No handoff sequence specs; skipped."))
    return out


def verify_coordination_patterns(ctx: Phase0Context) -> list[CheckResult]:
    """B07: hard_pool / soft_pool / role_anchored coordination semantics."""
    pwu = post_warmup_events(ctx)
    out: list[CheckResult] = []
    for i, spec in enumerate(ctx.spec.get("coordination_patterns", [])):
        proc = spec.get("process")
        typ = spec["type"]
        resources = set(spec["resources"])
        if typ == "hard_pool":
            buckets: dict[tuple, set] = defaultdict(set)
            for e in pwu:
                if (e.get("event") == "queue_enter"
                        and (proc is None or e.get("process") == proc)
                        and e.get("resource") in resources):
                    buckets[(e["entity_id"], round(e["time"], 3))].add(e["resource"])
            matches = sum(1 for rs in buckets.values() if rs >= resources)
            min_occ = spec.get("min_occurrences", 1)
            sev = "PASS" if matches >= min_occ else "FAIL"
            out.append(CheckResult(sev, f"B07_coordination[{i}]",
                                   f"Hard-pool: {matches} matches, required≥{min_occ}."))
        elif typ == "role_anchored":
            proc_evs = [e for e in pwu
                        if (proc is None or e.get("process") == proc)
                        and e.get("event") == "service_start"]
            seen = {e.get("resource") for e in proc_evs}
            missing = sorted(resources - seen)
            sev = "PASS" if not missing else "FAIL"
            out.append(CheckResult(sev, f"B07_coordination[{i}]",
                                   f"Role-anchored missing: {missing}" if missing
                                   else "All required roles observed."))
        elif typ == "soft_pool":
            proc_evs = [e for e in pwu
                        if (proc is None or e.get("process") == proc)
                        and e.get("event") == "service_start"]
            seen = {e.get("resource") for e in proc_evs}
            min_p = spec.get("min_participants", 1)
            sev = "PASS" if len(seen) >= min_p else "FAIL"
            out.append(CheckResult(sev, f"B07_coordination[{i}]",
                                   f"Soft-pool: {len(seen)} participants, required≥{min_p}."))
        else:
            out.append(CheckResult("INFO", f"B07_coordination[{i}]",
                                   f"Unknown type '{typ}'; skipped."))
    if not out:
        out.append(CheckResult("INFO", "B07_coordination_patterns",
                               "No coordination pattern specs; skipped."))
    return out


def verify_sequencing(ctx: Phase0Context) -> list[CheckResult]:
    """B08: General before/after precedence rules per entity."""
    pwu = post_warmup_events(ctx)
    groups = group_by_entity(pwu)
    out: list[CheckResult] = []
    for i, spec in enumerate(ctx.spec.get("sequencing", [])):
        # AUDIT FIX (H5): the mapper emits "before": None (explicitly) for a
        # one-sided precedence declaration, and match_event_pattern calls
        # .items() on the pattern — None crashed the whole gate. A one-sided
        # precedence has no before/after ordering to verify; skip with INFO.
        before_pat = spec.get("before")
        after_pat = spec.get("after")
        if not isinstance(before_pat, dict) or not isinstance(after_pat, dict):
            out.append(CheckResult(
                "INFO", f"B08_sequencing[{i}]",
                f"one-sided or malformed precedence "
                f"(before={'set' if isinstance(before_pat, dict) else before_pat!r}, "
                f"after={'set' if isinstance(after_pat, dict) else after_pat!r}); "
                f"an ordering check needs both sides — skipped."))
            continue
        etype = spec.get("entity_type")
        violations = checked = 0
        for _, evs in groups.items():
            rel = [e for e in evs if etype is None or e.get("entity_type") == etype]
            if not rel:
                continue
            checked += 1
            idx_b = next((k for k, e in enumerate(rel)
                          if match_event_pattern(e, before_pat)), None)
            idx_a = next((k for k, e in enumerate(rel)
                          if match_event_pattern(e, after_pat)),  None)
            if idx_b is None or idx_a is None:
                continue
            if idx_a < idx_b:
                violations += 1
        desc = spec.get("description", f"spec[{i}]")
        sev = "FAIL" if violations else "PASS"
        out.append(CheckResult(sev, f"B08_sequencing[{i}]",
                               f"{desc}: {violations}/{checked} entities violated.",
                               {"violations": violations, "checked": checked}))
    if not out:
        out.append(CheckResult("INFO", "B08_sequencing",
                               "No sequencing specs; skipped."))
    return out


def verify_temporal(ctx: Phase0Context) -> list[CheckResult]:
    """B09: Min/max sojourn time before a declared terminal event.

    Bound semantics: a temporal declaration may legitimately specify only
    one side of the bound (e.g. "LOS must be ≥ 0 hours" with no declared
    upper limit, or "must complete within 7 days" with no declared lower
    limit). Missing or explicit-null bounds are treated as unconstrained
    on that side, not as a defect. The earlier implementation used
    ``dict.get(key, default)`` which returns ``None`` for explicit-null
    declarations rather than the default, causing arithmetic on ``None``
    and an unhandled crash. This is a recognised B09 robustness fix.
    """
    pwu = post_warmup_events(ctx)
    groups = group_by_entity(pwu)
    out: list[CheckResult] = []
    for i, spec in enumerate(ctx.spec.get("temporal", [])):
        etype = spec.get("entity_type")
        tgt_event = spec["event"]
        # Resolve min/max bounds with explicit-null handling. Order of
        # precedence: <field>_seconds wins if non-null; <field>_hours
        # next if non-null; otherwise the side is unconstrained.
        min_s = spec.get("min_seconds")
        if min_s is None:
            min_h = spec.get("min_hours")
            min_s = (min_h * 3_600.0) if min_h is not None else 0.0
        max_s = spec.get("max_seconds")
        if max_s is None:
            max_h = spec.get("max_hours")
            max_s = (max_h * 3_600.0) if max_h is not None else float("inf")
        violations = checked = 0
        for _, evs in groups.items():
            rel = [e for e in evs if etype is None or e.get("entity_type") == etype]
            arr = next((e for e in rel if e["event"] == "system_arrival"), None)
            tgt = next((e for e in rel if e["event"] == tgt_event), None)
            if arr is None or tgt is None:
                continue
            checked += 1
            soj = tgt["time"] - arr["time"]
            if soj < min_s - 1e-6 or soj > max_s + 1e-6:
                violations += 1
        desc = spec.get("description", f"spec[{i}]")
        sev = "FAIL" if violations else "PASS"
        out.append(CheckResult(sev, f"B09_temporal[{i}]",
                               f"{desc}: {violations}/{checked} entities violated.",
                               {"violations": violations, "checked": checked}))
    if not out:
        out.append(CheckResult("INFO", "B09_temporal",
                               "No temporal specs; skipped."))
    return out


def verify_preemption_rules(ctx: Phase0Context) -> list[CheckResult]:
    """B10: Preempt events paired with resume or terminal exit."""
    pwu = post_warmup_events(ctx)
    out: list[CheckResult] = []
    for i, spec in enumerate(ctx.spec.get("preemption_rules", [])):
        applies = set(spec.get("applies_to_processes", []))
        def relevant(e: dict) -> bool:
            return not applies or e.get("process") in applies
        preempts  = [e for e in pwu if e.get("event") == "preempt"  and relevant(e)]
        resumes   = {(e["entity_id"], e.get("segment_id")) for e in pwu
                     if e.get("event") == "resume"  and relevant(e)}
        terminals = {e["entity_id"] for e in pwu
                     if e.get("event") in ("system_departure", "loss")}
        exp_count = spec.get("expected_preempt_count")
        if exp_count is not None:
            # AUDIT FIX (H1): expected == 0 is the STRONGEST form of the
            # contract ("preemption must not occur", emitted by the mapper
            # for interruptible=False). compare_ratio INFO-skips any
            # expected <= 0, which silently disarmed exactly that case.
            # Handle zero as an exact assertion, locally — do NOT change
            # compare_ratio globally (B16/B02 legitimately rely on its
            # skip semantics for zero/absent expectations).
            if float(exp_count) == 0.0:
                sev = "PASS" if len(preempts) == 0 else "FAIL"
                out.append(CheckResult(
                    sev, f"B10_preemption_count[{i}]",
                    (f"declared non-preemptible (expected_preempt_count=0): "
                     f"{len(preempts)} preempt event(s) observed"
                     + ("" if sev == "PASS" else
                        " — preemption occurred on a process declared "
                        "non-interruptible")),
                    {"expected": 0, "observed": len(preempts)}))
            else:
                out.append(compare_ratio(float(len(preempts)), float(exp_count),
                                         spec.get("tolerance", 0.05),
                                         f"B10_preemption_count[{i}]"))
        if spec.get("require_resume_or_terminal_exit", True):
            unpaired = sum(1 for e in preempts
                           if (e["entity_id"], e.get("segment_id")) not in resumes
                           and e["entity_id"] not in terminals)
            sev = "PASS" if unpaired == 0 else "FAIL"
            out.append(CheckResult(sev, f"B10_preemption_pairs[{i}]",
                                   f"{unpaired}/{len(preempts)} preempts missing "
                                   "resume or terminal exit.",
                                   {"unpaired": unpaired, "total": len(preempts)}))
    if not out:
        out.append(CheckResult("INFO", "B10_preemption_rules",
                               "No preemption rule specs; skipped."))
    return out


def verify_state_transition_rules(ctx: Phase0Context) -> list[CheckResult]:
    """B11: Forbidden events/processes in declared states."""
    pwu = post_warmup_events(ctx)
    out: list[CheckResult] = []
    for i, spec in enumerate(ctx.spec.get("state_transition_rules", [])):
        sf = spec.get("state_field", "state")
        target = spec["state"]
        violations = sum(
            1 for e in pwu
            if e.get(sf) == target
            and (("forbidden_events" in spec and e.get("event") in spec["forbidden_events"])
                 or ("forbidden_processes" in spec and e.get("process") in spec["forbidden_processes"]))
        )
        sev = "PASS" if violations == 0 else "FAIL"
        out.append(CheckResult(sev, f"B11_state_transitions[{i}]",
                               f"State '{target}': {violations} forbidden transitions."))
    if not out:
        out.append(CheckResult("INFO", "B11_state_transition_rules",
                               "No state transition specs; skipped."))
    return out


def verify_entity_type_constraints(ctx: Phase0Context) -> list[CheckResult]:
    """B12: Process/entity_type typing constraints.

    AUDIT FIX (H2): the previous implementation checked only the
    NEGATIVE form (`not_entity_type`), which the mapper always emits as
    None and which no DSL field can even express — so the check compared
    event entity_types against None: vacuous when every event carries a
    type, spuriously failing when any event lacks one. The POSITIVE form
    ("process X serves only entity_type A"), which the mapper actually
    emits under `entity_type`, was never enforced. Both forms are now
    checked, each only when its field is non-null, and events lacking an
    entity_type field are counted separately (data-quality note, not a
    violation).
    """
    pwu = post_warmup_events(ctx)
    out: list[CheckResult] = []
    specs = ctx.spec.get("entity_type_constraints", [])
    # Multiple constraints may declare the same process with DIFFERENT
    # entity_types (e.g. the ER pattern: four acuity classes each pinned to
    # the shared 'treatment' process). The correct positive semantics is
    # the per-process UNION of declared types — enforcing each constraint's
    # single type independently would spuriously fail every multi-type
    # process. Forbidden types are likewise unioned per process.
    from collections import defaultdict as _dd
    allowed_by_proc: dict[str, set] = _dd(set)
    forbidden_by_proc: dict[str, set] = _dd(set)
    source_idx: dict[str, list[int]] = _dd(list)
    for i, spec in enumerate(specs):
        proc = spec.get("process")
        if not proc:
            out.append(CheckResult(
                "INFO", f"B12_entity_type_constraint[{i}]",
                "no process declared; nothing to enforce."))
            continue
        source_idx[proc].append(i)
        if spec.get("entity_type") is not None:
            allowed_by_proc[proc].add(spec["entity_type"])
        if spec.get("not_entity_type") is not None:
            forbidden_by_proc[proc].add(spec["not_entity_type"])
    for proc in sorted(source_idx):
        allowed = allowed_by_proc.get(proc) or set()
        forbidden = forbidden_by_proc.get(proc) or set()
        idxs = source_idx[proc]
        if not allowed and not forbidden:
            out.append(CheckResult(
                "INFO", f"B12_entity_type_constraint[{idxs[0]}]",
                f"process '{proc}': neither entity_type nor not_entity_type "
                f"declared; nothing to enforce."))
            continue
        proc_evs = [e for e in pwu if e.get("process") == proc]
        typed_evs = [e for e in proc_evs if e.get("entity_type") is not None]
        untyped = len(proc_evs) - len(typed_evs)
        bad: list[dict] = []
        if allowed:
            bad.extend(e for e in typed_evs
                       if e.get("entity_type") not in allowed)
        if forbidden:
            bad.extend(e for e in typed_evs
                       if e.get("entity_type") in forbidden)
        sev = "PASS" if not bad else "FAIL"
        offending = sorted({str(e.get("entity_type")) for e in bad})[:5]
        msg = (f"{len(bad)} violations for process '{proc}'"
               + (f" (allowed={sorted(allowed)})" if allowed else "")
               + (f" (forbidden={sorted(forbidden)})" if forbidden else "")
               + (f"; offending types: {offending}" if bad else "")
               + (f"; {untyped} event(s) lack an entity_type field "
                  f"(not counted as violations)" if untyped else ""))
        out.append(CheckResult(
            sev, f"B12_entity_type_constraint[{idxs[0]}]", msg,
            {"process": proc, "violations": len(bad), "untyped": untyped,
             "allowed": sorted(allowed), "forbidden": sorted(forbidden),
             "merged_spec_indices": idxs}))
    if not out:
        out.append(CheckResult("INFO", "B12_entity_type_constraints",
                               "No entity type constraint specs; skipped."))
    return out


def verify_terminal_outcomes(ctx: Phase0Context) -> list[CheckResult]:
    """B13: each entity ends in exactly one terminal event from the UNION of
    declared outcomes for its entity_type — with in-system entities tolerated.

    "Exactly one terminal" is a SYSTEM-LEVEL property, not a per-outcome one.
    Branching outcomes (good parts → system_departure, scrap → loss) are
    naturally declared as separate terminal_outcome elements; the old per-spec
    check demanded every entity satisfy each spec individually, which is
    impossible whenever outcomes branch (good parts can't have a 'loss', scrap
    can't have a 'system_departure'). We instead union the declared terminal
    events per entity_type and require ≤1 terminal per entity:
      * exactly 1 terminal  → completed (the normal case)
      * 0 terminals         → still in system at the horizon (tolerated, the
                              same boundary residual A8 / flow-accounting allow)
      * ≥2 terminals        → FAIL (an entity both departed and was lost, or
                              terminated twice — a genuine double-count bug)
    Per-spec `must_exist` existence is reported separately.
    """
    pwu = post_warmup_events(ctx)
    groups = group_by_entity(pwu)
    specs = ctx.spec.get("terminal_outcomes", [])
    if not specs:
        return [CheckResult("INFO", "B13_terminal_outcomes",
                            "No terminal outcome specs; skipped.")]

    # Group specs by entity_type so the "exactly one terminal" property is
    # evaluated against the union of that type's declared terminal events.
    by_etype: dict = defaultdict(list)
    for i, spec in enumerate(specs):
        by_etype[spec.get("entity_type")].append((i, spec))

    out: list[CheckResult] = []
    for etype, group in by_etype.items():
        union_events: set = set()
        require_one = False
        idxs = []
        for i, spec in group:
            union_events |= set(spec.get("terminal_events", []))
            require_one = require_one or spec.get("exactly_one_terminal_event", False)
            idxs.append(i)
        label = "[" + "+".join(str(i) for i in idxs) + "]"

        # Per-spec existence (must_exist): the declared outcome occurs somewhere.
        for i, spec in group:
            if spec.get("must_exist"):
                ev = set(spec.get("terminal_events", []))
                seen = any(e["event"] in ev for evs in groups.values() for e in evs
                           if (etype is None or e.get("entity_type") == etype))
                out.append(CheckResult(
                    "PASS" if seen else "FAIL", f"B13_terminal_outcomes[{i}]_exists",
                    f"declared outcome {sorted(ev)} "
                    + ("observed." if seen else "never observed.")))

        if not require_one:
            continue

        multi = checked = in_system = 0
        for _, evs in groups.items():
            rel = [e for e in evs if etype is None or e.get("entity_type") == etype]
            if not rel:
                continue
            checked += 1
            n_term = sum(1 for e in rel if e["event"] in union_events)
            if n_term >= 2:
                multi += 1
            elif n_term == 0:
                in_system += 1
        sev = "PASS" if multi == 0 else "FAIL"
        out.append(CheckResult(
            sev, f"B13_terminal_outcomes{label}",
            f"{multi}/{checked} entities with >1 terminal event from "
            f"{sorted(union_events)} (double-count); {in_system} still in system "
            "at horizon (tolerated).",
            {"multi_terminal": multi, "checked": checked, "in_system": in_system,
             "union_events": sorted(union_events)}))
    return out


def verify_losses(ctx: Phase0Context) -> list[CheckResult]:
    """B14: Required loss types present; forbidden loss types absent."""
    pwu = post_warmup_events(ctx)
    out: list[CheckResult] = []
    for i, spec in enumerate(ctx.spec.get("losses", [])):
        lt = spec["loss_type"]
        cnt = len(filter_events(pwu, event="loss", loss_type=lt))
        must = spec["must_exist"]
        if must and cnt == 0:
            sev, msg = "FAIL", f"loss_type='{lt}' required but absent."
        elif (not must) and cnt > 0:
            sev, msg = "FAIL", f"loss_type='{lt}' forbidden but found {cnt}."
        else:
            sev, msg = "PASS", f"loss_type='{lt}': count={cnt}, must_exist={must} ✓"
        out.append(CheckResult(sev, f"B14_losses[{i}]", msg,
                               {"loss_type": lt, "count": cnt}))
    if not out:
        out.append(CheckResult("INFO", "B14_losses",
                               "No loss specs; skipped."))
    return out


def verify_flow_accounting(ctx: Phase0Context) -> list[CheckResult]:
    """B15: arrivals = departures + losses + in_system_at_end.

    Counts are derived FROM THE TRACE (the canonical contract), never from the
    simulation's self-reported `metrics` dict. A verifier must not trust the
    artifact under test's own aggregate counters: if the sim mis-reports
    total_departures/total_losses (e.g. double-counting), trusting metrics
    produces a spurious FAIL while the trace balances fine. (This is why B21
    rooted_flow_conservation, which counts from the trace, disagreed with the
    old metrics-trusting B15.) If the sim DOES report these counters and they
    disagree with the trace, that disagreement is surfaced as its own WARN.
    """
    out: list[CheckResult] = []
    # Trace-derived counts (post-warmup, by unique entity).
    arrived: set = set(); departed: set = set(); lost: set = set()
    for e in ctx.trace:
        if e.get("time", 0) < ctx.warmup:
            continue
        eid, ev = e.get("entity_id"), e.get("event")
        if ev == "system_arrival":     arrived.add(eid)
        elif ev == "system_departure": departed.add(eid)
        elif ev == "loss":             lost.add(eid)
    # AUDIT FIX (C6): the previous formulation computed dep / los / ins as an
    # exact partition of `arrived` (dep = departed∩arrived, los = the lost
    # NOT already counted as departed, ins = the remainder), so the balance
    # identity held BY CONSTRUCTION and the check could never fail — a
    # tautology. In particular, an entity that both departed AND was lost
    # (double-termination, a genuine accounting bug) was silently absorbed
    # into `dep`. We now count terminals NON-exclusively and fail explicitly
    # on the overlap.
    dep = len(departed & arrived)
    los = len(lost & arrived)                    # non-exclusive
    dbl = departed & lost & arrived              # double-terminated entities
    arr = len(arrived)
    ins = len(arrived - departed - lost)

    for i, spec in enumerate(ctx.spec.get("flow_accounting", [])):
        name = spec.get("name", f"spec[{i}]")
        # (1) Exclusivity: no entity may have BOTH a departure and a loss.
        if dbl:
            sample = sorted(str(x) for x in dbl)[:5]
            out.append(CheckResult(
                "FAIL", f"B15_{name}",
                f"{len(dbl)} entit{'y' if len(dbl)==1 else 'ies'} terminated "
                f"BOTH by departure and by loss (double-termination) — e.g. "
                f"{sample}. Each entity must end in exactly one terminal "
                f"outcome; fix the sim's terminal accounting.",
                {"arrivals": arr, "departures": dep, "losses": los,
                 "double_terminated": len(dbl),
                 "double_terminated_sample": sample}))
            continue
        # (2) Balance with non-exclusive counts: with the overlap empty this
        # is a real constraint (dep + los + ins == arr can now genuinely
        # fail if the trace has orphan exits or accounting drift).
        balanced = abs(arr - (dep + los + ins)) <= 1
        sev = "PASS" if balanced else "FAIL"
        out.append(CheckResult(sev, f"B15_{name}",
                               f"arrivals={arr} = departures={dep} + losses={los} "
                               f"+ in_system={ins} "
                               f"(error={abs(arr - dep - los - ins)}); counted from "
                               f"trace with non-exclusive terminal counts; "
                               f"double-termination checked separately.",
                               {"arrivals": arr, "departures": dep,
                                "losses": los, "in_system": ins,
                                "double_terminated": 0}))
        # Cross-check the sim's self-reported metrics, if present, and flag
        # any disagreement (it points to a sim metrics-accounting bug).
        m_dep = ctx.metrics.get("total_departures")
        m_los = ctx.metrics.get("total_losses")
        mism = []
        if isinstance(m_dep, (int, float)) and abs(m_dep - dep) > 1:
            mism.append(f"metrics.total_departures={m_dep} vs trace={dep}")
        if isinstance(m_los, (int, float)) and abs(m_los - los) > 1:
            mism.append(f"metrics.total_losses={m_los} vs trace={los}")
        if mism:
            out.append(CheckResult(
                "WARN", f"B15_{name}_metrics_consistency",
                "sim's self-reported metrics disagree with its trace: "
                + "; ".join(mism) + " — the trace is authoritative; fix the "
                "sim's metric accounting.",
                {"mismatch": mism}))
    if not out:
        out.append(CheckResult("INFO", "B15_flow_accounting",
                               "No flow accounting specs; skipped."))
    return out


def verify_aggregate_targets(ctx: Phase0Context) -> list[CheckResult]:
    """B16: Key metrics within declared expected ranges."""
    out: list[CheckResult] = []
    for i, spec in enumerate(ctx.spec.get("aggregate_targets", [])):
        m = spec["metric"]
        if m not in ctx.metrics:
            out.append(CheckResult("INFO", f"B16_aggregate_target[{i}]",
                                   f"Metric '{m}' absent; skipped."))
            continue
        out.append(compare_ratio(float(ctx.metrics[m]), spec["expected"],
                                 spec.get("tolerance", 0.20),
                                 f"B16_{m}"))
    if not out:
        out.append(CheckResult("INFO", "B16_aggregate_targets",
                               "No aggregate target specs; skipped."))
    return out


def verify_scenario_guidance(ctx: Phase0Context) -> list[CheckResult]:
    """B17: Emit INFO advisories for declared scenario checks."""
    out: list[CheckResult] = []
    for i, spec in enumerate(ctx.spec.get("scenario_guidance", [])):
        out.append(CheckResult("INFO", f"B17_scenario[{i}]_{spec['name']}",
                               f"{spec.get('description', '')} "
                               "(run via scenario harness, not executed here)."))
    if not out:
        out.append(CheckResult("INFO", "B17_scenario_guidance",
                               "No scenario guidance specs declared."))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# LAYER C — Scenario harness (Phase 0 smoke pass)
# ─────────────────────────────────────────────────────────────────────────────

def run_scenario_harness(
    base_config: dict,
    scenarios: list[dict],
    sim_callable=None,
) -> dict[str, list[CheckResult]]:
    """Run each scenario override through a full Phase 0 pass.

    Each scenario dict: {"name": str, "overrides": dict, "description": str}
    Returns: {scenario_name: [CheckResult, ...]}

    `sim_callable` is the run_simulation function. If None, this falls back
    to the module-level run_simulation (set by the __main__ demo) or to
    VVUQ_RUN_SIMULATION injected by an external caller. Keeping the sim
    callable as a parameter is what makes this harness domain-agnostic.
    """
    import copy
    sim = sim_callable or run_simulation or globals().get("VVUQ_RUN_SIMULATION")
    if sim is None:
        return {s["name"]: [CheckResult("BLOCK", f"scenario_{s['name']}",
            "run_scenario_harness needs a sim_callable; none provided "
            "and module-level run_simulation is not bound.")] for s in scenarios}
    outputs: dict[str, list[CheckResult]] = {}
    for scenario in scenarios:
        cfg = copy.deepcopy(base_config)
        cfg.update(scenario.get("overrides", {}))
        try:
            result = sim(cfg)
        except Exception as exc:
            outputs[scenario["name"]] = [CheckResult(
                "BLOCK", f"scenario_{scenario['name']}",
                f"run_simulation raised {type(exc).__name__}: {exc}",
            )]
            continue
        outputs[scenario["name"]] = run_phase0(result)
    return outputs


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_phase0(result: dict) -> list[CheckResult]:
    """Run all Phase 0 checks on a run_simulation() result.

    Layers B and C are skipped if any Layer A check returns BLOCK.
    Returns the full list of CheckResult objects.
    """
    all_results: list[CheckResult] = []

    # ── Layer A ──────────────────────────────────────────────────────────────
    all_results += _check_a2_result_structure(result)
    if any(r.severity == "BLOCK" for r in all_results):
        return all_results

    ctx = Phase0Context.from_result(result)
    all_results += _check_a1_config_schema(ctx)
    all_results += _check_a3_trace_completeness(ctx)
    all_results += _check_a4_chronological_order(ctx)
    all_results += _check_a5_canonical_events(ctx)
    all_results += _check_a6_entity_lifecycle(ctx)
    all_results += _check_a7_determinism(ctx)
    all_results += _check_a8_clean_shutdown(ctx)

    if any(r.severity == "BLOCK" for r in all_results):
        return all_results  # gate: broken trace → skip contracts

    # ── Layer B (B01–B17) ────────────────────────────────────────────────────
    # AUDIT FIX (H3): each checker runs inside a try/except that converts a
    # crash into a BLOCK CheckResult, matching the B24–B41 wrapper. Layer B
    # previously ran unwrapped, so one malformed spec entry (e.g. a null
    # cadence or one-sided precedence before those got their own guards)
    # raised out of run_phase0 entirely — no gate verdict, no per-check
    # diagnostics, and the self-heal loop had nothing to classify. A crashed
    # checker is absence of verification, not a pass, hence BLOCK.
    _layer_b_checks = [
        verify_arrivals, verify_recurring_obligations, verify_service_rates,
        verify_periodic_processes, verify_scheduled_windows,
        verify_handoff_sequences, verify_coordination_patterns,
        verify_sequencing, verify_temporal, verify_preemption_rules,
        verify_state_transition_rules, verify_entity_type_constraints,
        verify_terminal_outcomes, verify_losses, verify_flow_accounting,
        verify_aggregate_targets,
    ]
    for _chk in _layer_b_checks:
        try:
            all_results += _chk(ctx)
        except Exception as _exc:
            import traceback as _tb
            all_results.append(CheckResult(
                "BLOCK", f"B_layer_crash[{_chk.__name__}]",
                f"{_chk.__name__} raised {_exc!r} — a crashed checker is "
                f"absence of verification, not a pass. Classify as "
                f"VALIDATOR_FIX (checker bug) or DSL_SPEC (malformed spec "
                f"entry) and resolve before trusting this phase.",
                {"error": str(_exc), "traceback": _tb.format_exc(limit=5)}))

    # ── Layer B Extended — Process Contract Validators (B18–B23) ─────────────
    all_results += validate_process_contracts(ctx)

    # ── Layer B Extended — Semantic Contract Validators (B24–B41)  [v5.2] ────
    all_results += validate_semantic_contracts(ctx)

    # ── Layer C (advisory) ──────────────────────────────────────────────────
    all_results += verify_scenario_guidance(ctx)

    return all_results


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Demo entry point. Reads the sim path from $VVUQ_SIM_MODULE (default:
    # manufacturing_sim) and runs Phase 0 against it with the bundled
    # example spec. Library callers don't go through this path —
    # run_vvuq.py / self_heal_orchestrator.py call run_phase0() directly
    # against any sim that exposes run_simulation(config).
    import argparse
    import importlib

    p = argparse.ArgumentParser(description="VVUQ Phase 0 demo runner.")
    p.add_argument("--sim", default=os.environ.get("VVUQ_SIM_MODULE", "manufacturing_sim"),
                   help="module name to import (must expose run_simulation, DEFAULT_CONFIG)")
    p.add_argument("--spec", default="example",
                   help="'example' uses the bundled illustrative spec; otherwise expects "
                        "a JSON file path to load as compliance_spec")
    args = p.parse_args()

    print("=" * 70)
    print(f"VVUQ Phase 0 v5.2 — demo runner ({args.sim})")
    print("=" * 70)

    try:
        sim_mod = importlib.import_module(args.sim)
    except ImportError as exc:
        print(f"could not import --sim '{args.sim}': {exc}", file=sys.stderr)
        sys.exit(2)
    run_sim = getattr(sim_mod, "run_simulation", None)
    base_cfg = getattr(sim_mod, "DEFAULT_CONFIG", {})
    if run_sim is None:
        print(f"'{args.sim}' does not expose run_simulation(config)", file=sys.stderr)
        sys.exit(2)

    if args.spec == "example":
        spec = _EXAMPLE_MANUFACTURING_SPEC
    else:
        import json
        with open(args.spec) as f:
            spec = json.load(f)

    cfg = {
        **base_cfg,
        "compliance_spec": spec,
        "seed": base_cfg.get("seed", 42),
        "rep_id": 0,
    }
    cfg.setdefault("run_length", 180_000)
    cfg.setdefault("warmup_time", 18_000)

    print("Running simulation…")
    result = run_sim(cfg)
    print(f"Trace: {len(result['trace']):,} events  |  "
          f"Metrics: {len(result['metrics'])} keys\n")

    results = run_phase0(result)
    status = print_phase0_summary(results, "PHASE 0")
    sys.exit(0 if status in ("PASSED", "PASSED_WITH_WARNINGS") else 1)
