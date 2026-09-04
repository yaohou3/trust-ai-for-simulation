"""
dsl_schema.py — Type definitions and JSON schemas for the DSL generation pipeline.

v5.2 — SEMANTIC EXTENSIONS
==========================
This version extends the original 17-element vocabulary with 8 new element
types and ~20 semantic fields on existing elements, addressing semantic-
validity concerns (does the code implement the intended *meaning* of the
DSL, not just the counts).

New element types (10):
  eligibility_rule        — who is permitted to enter a process
  coverage_rule           — entity-visit cadence with missed/duplicate policy
  routing_rule            — branch probabilities + conditional predicates
  assignment_continuity   — same-server-for-same-entity rules
  policy_threshold        — SLA breach condition + escalation outcome
  semantic_scenario       — Phase 4.5 counterfactual (treatment/control)
  coupling_rule           — process A affects process B's trigger/eligibility
  control_policy          — signals used, update cadence, fallback (advanced)
  escalation              — SLA-breach → response_event (Phase 4.5 driver)
  workload_response       — arrival-rate stress claim (Phase 4.5 driver)

New semantic fields (summary; see DSL_ELEMENT_SCHEMA for full list):
  • Duration:  duration_scope, interrupt_policy, setup_seconds, teardown_seconds
  • Capacity:  consumption_mode, inventory_init, replenishment, max_concurrent
  • Participation: substitutable_roles, quorum_min, staggered_allowed, supervisory,
                   sync_type, batched_start, batched_complete
  • Preemption:    post_preempt_policy, can_initiate, equal_priority, min_service_chunk_s
  • State:         dwell_distribution, hazard_rate, competing_risks
  • Calendar:      calendar (weekdays, holidays, breaks, maintenance)
  • Loss:          condition_expr, threshold_seconds, cause_attribution
  • KPI:           numerator, denominator, basis, warmup_handling, composition_band
  • Information:   observation_delay_s, information_source
  • Identity:      granularity
  • Provenance:    assumed_default (flag for fields not traceable to user description)

Three artifact types flow through the pipeline:

  DSL               — executable model structure extracted from natural language
  ComplianceSpec    — machine-checkable verification contract derived from DSL
  CoverageReport    — provenance table linking every DSL clause to assertions
  PipelineArtifact  — the paired output the LLM must emit
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal

# ─────────────────────────────────────────────────────────────────────────────
# DSL element types → required compliance sections (the mapping table)
# ─────────────────────────────────────────────────────────────────────────────

DSL_ELEMENT_TO_COMPLIANCE: dict[str, list[str]] = {
    # Original 17
    "arrival":                    ["arrivals"],
    "recurring_activity":         ["recurring_obligations"],
    "service_process":            ["service_rates"],
    "scheduled_process":          ["periodic_processes"],
    "precedence":                 ["sequencing"],
    "time_threshold":             ["temporal"],
    "hard_pool_coordination":     ["coordination_patterns"],
    "soft_pool_coordination":     ["coordination_patterns",
                                   "pool_composition_rules"],
    "role_anchored_coordination": ["coordination_patterns"],
    "preemption_rule":            ["preemption_rules"],
    "state_restriction":          ["state_transition_rules"],
    "entity_type_constraint":     ["entity_type_constraints"],
    "terminal_outcome":           ["terminal_outcomes"],
    "loss_type":                  ["losses"],
    "flow_conservation":          ["flow_accounting"],
    "aggregate_target":           ["aggregate_targets"],
    "scenario_hint":              ["scenario_guidance"],
    "process_contract":           ["process_contracts"],
    # NEW in v5.2 — semantic layer
    "eligibility_rule":           ["eligibility_rules"],
    "coverage_rule":              ["coverage_rules"],
    "routing_rule":               ["routing_rules"],
    "assignment_continuity":      ["assignment_continuity"],
    "policy_threshold":           ["policy_thresholds"],
    "semantic_scenario":          ["semantic_scenarios"],
    "coupling_rule":              ["coupling_rules"],
    "control_policy":             ["control_policies"],
    "escalation":                 ["escalations"],
    "workload_response":          ["workload_responses"],
}

# DSL element types that are NOT individually verifiable but are still valid
NON_CHECKABLE_ELEMENT_TYPES: set[str] = {
    "model_assumption",   # annotational, not behavioral
    "vague_clause",       # captured explicitly as non-checkable
}

# Sections whose verdicts are advisory only (WARN max) — plausibility/judgment
# rather than mechanical verifiability.
ADVISORY_ONLY_SECTIONS: set[str] = {
    "scenario_guidance",
    "semantic_scenarios",      # verified in Phase 4.5, not Phase 0
    "coupling_rules",          # verified in Phase 4.5
    "control_policies",        # verified in Phase 4.5
    "escalations",             # verified in Phase 4.5
    "workload_responses",      # verified in Phase 4.5
    "composition_bands",       # plausibility bands on aggregate_target
}

# ─────────────────────────────────────────────────────────────────────────────
# Trace observability requirements per compliance section
# ─────────────────────────────────────────────────────────────────────────────

OBSERVABILITY_REQUIREMENTS: dict[str, dict[str, Any]] = {
    # ── Original sections ────────────────────────────────────────────────────
    "arrivals": {
        "required_events": ["system_arrival"],
        "required_fields": ["time", "entity_id"],
        "optional_fields": ["entity_type"],
    },
    "recurring_obligations": {
        "required_events": ["service_start"],
        "required_fields": ["time", "entity_id", "resource"],
        "optional_fields": ["process", "entity_type"],
    },
    "service_rates": {
        "required_events": ["service_start"],
        "required_fields": ["time", "entity_id", "resource"],
        "optional_fields": ["entity_type"],
    },
    "periodic_processes": {
        "required_events": ["system_arrival"],
        "required_fields": ["time", "entity_id"],
        "optional_fields": ["entity_type", "process"],
    },
    "scheduled_windows": {
        "required_events": ["system_arrival"],
        "required_fields": ["time"],
        "optional_fields": ["entity_type", "process"],
    },
    "handoff_sequences": {
        "required_events": ["service_start"],
        "required_fields": ["time", "entity_id", "resource"],
        "optional_fields": ["process", "entity_type", "seq"],
    },
    "coordination_patterns": {
        "required_events": ["service_start", "queue_enter"],
        "required_fields": ["time", "entity_id", "resource"],
        "optional_fields": ["process", "segment_id"],
        "notes": "hard_pool requires queue_enter; soft_pool requires service_start; "
                 "role_anchored requires service_start",
    },
    "sequencing": {
        "required_events": ["service_start", "service_end"],
        "required_fields": ["time", "entity_id", "resource"],
        "optional_fields": ["process", "entity_type", "seq"],
    },
    "temporal": {
        "required_events": ["system_arrival", "system_departure"],
        "required_fields": ["time", "entity_id"],
        "optional_fields": ["entity_type"],
    },
    "preemption_rules": {
        "required_events": ["preempt", "resume"],
        "required_fields": ["time", "entity_id"],
        "optional_fields": ["process", "segment_id", "resource", "preempted_process"],
    },
    "state_transition_rules": {
        "required_events": ["state_change", "service_start"],
        "required_fields": ["time", "entity_id", "state"],
        "optional_fields": ["process", "next_state"],
    },
    "entity_type_constraints": {
        "required_events": ["service_start"],
        "required_fields": ["entity_type", "process"],
        "optional_fields": [],
    },
    "terminal_outcomes": {
        "required_events": ["system_departure", "loss"],
        "required_fields": ["time", "entity_id"],
        "optional_fields": ["entity_type"],
    },
    "losses": {
        "required_events": ["loss"],
        "required_fields": ["time", "entity_id", "loss_type"],
        "optional_fields": ["entity_type"],
    },
    "flow_accounting": {
        "required_events": ["system_arrival", "system_departure", "loss"],
        "required_fields": ["time", "entity_id"],
        "optional_fields": [],
    },
    "aggregate_targets": {
        "required_events": [],
        "required_fields": [],
        "optional_fields": [],
        "notes": "verified from metrics dict, not trace events directly",
    },
    "scenario_guidance": {
        "required_events": [],
        "required_fields": [],
        "optional_fields": [],
        "notes": "advisory only; verified via scenario harness runs",
    },
    "process_contracts": {
        "required_events": ["service_start"],
        "required_fields": ["time", "entity_id", "process"],
        "optional_fields": ["resource", "entity_type", "seq"],
        "notes": "B18–B23 validators",
    },

    # ── NEW sections (v5.2) ──────────────────────────────────────────────────
    "eligibility_rules": {
        "required_events": ["service_start"],
        "required_fields": ["entity_id", "process"],
        "optional_fields": ["entity_type", "state"],
        "notes": "B24: verify no service_start for ineligible entities.",
    },
    "coverage_rules": {
        "required_events": ["service_start", "system_arrival", "system_departure"],
        "required_fields": ["time", "entity_id"],
        "optional_fields": ["process", "entity_type"],
        "notes": "B25: verify coverage cadence per eligible entity.",
    },
    "routing_rules": {
        "required_events": ["service_start", "service_end"],
        "required_fields": ["time", "entity_id", "resource"],
        "optional_fields": ["state", "entity_type"],
        "notes": "B26: verify branch probabilities and conditional routing.",
    },
    "assignment_continuity": {
        "required_events": ["service_start"],
        "required_fields": ["time", "entity_id", "resource"],
        "optional_fields": ["server_id", "shift_id"],
        "notes": "B27: verify same-server persistence. server_id strongly recommended.",
    },
    "pool_composition_rules": {
        "required_events": ["service_start", "service_end"],
        "required_fields": ["time", "entity_id", "resource", "process"],
        "optional_fields": ["server_id", "window_id", "role"],
        "notes": "B41: verify soft_pool composition matches pool_policy. "
                 "Requires per-resource identity (server_id) to count distinct "
                 "staff members participating at window onset. If server_id is "
                 "missing, B41 downgrades to INFO_SKIP rather than BLOCK.",
    },
    "policy_thresholds": {
        "required_events": ["state_change", "loss", "service_start"],
        "required_fields": ["time", "entity_id"],
        "optional_fields": ["state", "loss_type", "threshold_breach"],
        "notes": "B28: verify threshold breach triggers declared escalation outcome.",
    },
    "semantic_scenarios": {
        "required_events": [],
        "required_fields": [],
        "optional_fields": [],
        "notes": "Phase 4.5 — counterfactual runs, not single-trace checks.",
    },
    "coupling_rules": {
        "required_events": [],
        "required_fields": [],
        "optional_fields": [],
        "notes": "Phase 4.5 — toggle source process and compare target process rate.",
    },
    "control_policies": {
        "required_events": ["state_change"],
        "required_fields": ["time"],
        "optional_fields": ["signal_source", "signal_time"],
        "notes": "Phase 4.5 — signal-perturbation counterfactual for observation delay.",
    },
    "escalations": {
        "required_events": [],
        "required_fields": [],
        "optional_fields": ["escalation_count", "sla_breach_rate"],
        "notes": "Phase 4.5 — toggle escalation outcome; compare response metric.",
    },
    "workload_responses": {
        "required_events": [],
        "required_fields": [],
        "optional_fields": ["arrival_rate_scale"],
        "notes": "Phase 4.5 — scale arrival rate; compare declared response metric.",
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# JSON Schema for the paired LLM output
# ─────────────────────────────────────────────────────────────────────────────

DSL_ELEMENT_SCHEMA: dict = {
    "type": "object",
    "required": ["id", "element_type", "description"],
    "properties": {
        "id":           {"type": "string", "pattern": "^[A-Z][A-Z0-9]*[0-9]+$",
                         "description": "Stable short ID, e.g. A1, P2, R3, EL1, CV2, RT3"},
        "element_type": {"type": "string",
                         "enum": list(DSL_ELEMENT_TO_COMPLIANCE) + list(NON_CHECKABLE_ELEMENT_TYPES)},
        "name":         {"type": "string"},
        "description":  {"type": "string"},

        # Provenance flag (v5.2): true when the LLM assumed a default not
        # explicitly in the user description. coverage_validator will warn
        # when assumed_default=True elements carry no reason.
        "assumed_default":      {"type": ["boolean", "null"]},
        "assumed_default_reason": {"type": ["string", "null"]},

        # ── Arrival-specific ─────────────────────────────────────────────────
        "entity_type":  {"type": ["string", "null"]},
        "rate_per_day": {"type": ["number", "null"]},
        "distribution": {"type": ["object", "null"]},
        "granularity":  {"type": ["string", "null"],
                         "enum": ["visit", "episode", "batch", "unit_cycle",
                                  "customer", "order", None],
                         "description": "Entity identity unit; B40 checks consistency."},

        # ── Process-specific ─────────────────────────────────────────────────
        "resources":          {"type": "array", "items": {"type": "string"}},
        "service_time":       {"type": ["object", "null"]},
        "schedule":           {"type": ["object", "null"]},
        "recurrence_hours":   {"type": ["number", "null"]},

        # Coordination/participation
        "coordination_type":  {"type": ["string", "null"],
                               "enum": ["hard_pool", "soft_pool", "role_anchored", None]},
        "substitutable_roles": {"type": "array", "items": {"type": "string"},
                                "description": "Roles that may substitute for a required role."},
        "quorum_min":         {"type": ["integer", "null"],
                               "description": "Minimum number of roles that must be present "
                                              "to start the process."},
        "staggered_allowed":  {"type": ["boolean", "null"],
                               "description": "Whether team members may join/leave across the episode."},
        "supervisory":        {"type": ["boolean", "null"],
                               "description": "True if this role supervises without exclusive seizure."},
        "sync_type":          {"type": ["string", "null"],
                               "enum": ["and", "or", "quorum", None],
                               "description": "Synchronization semantics for multi-resource starts."},
        "batched_start":      {"type": ["boolean", "null"]},
        "batched_complete":   {"type": ["boolean", "null"]},
        "handoff_pair":       {"type": ["object", "null"],
                               "description": "{from_resource, to_resource, max_gap_s}"},

        # ── Pool-composition semantics (v5.2, used by B41) ───────────────────
        # Disambiguates "available providers jointly perform the task" between
        # fixed one-of-each teams and all-idle-staff-participate pools.
        "pool_policy":          {"type": ["string", "null"],
                                 "enum": ["fixed_team",
                                          "all_idle_at_start",
                                          "all_available_through_window",
                                          None],
                                 "description":
                                   "fixed_team: exactly pool_size members of each role. "
                                   "all_idle_at_start: every idle staff of each role joins at window onset. "
                                   "all_available_through_window: all_idle_at_start plus late joiners as staff become idle."},
        "pool_size":            {"type": ["object", "integer", "string", "null"],
                                 "description":
                                   "Team size spec. int: exact size of each role. "
                                   "\"all\": every idle staff of each role. "
                                   "{\"min\": n, \"max\": m}: bounded team. "
                                   "Per-role overrides allowed via {role: spec}."},
        "admits_late_joiners":  {"type": ["boolean", "null"],
                                 "description":
                                   "If true, staff that become idle during the window also join "
                                   "the in-progress pool task. Required to be true when "
                                   "pool_policy=all_available_through_window."},
        "release_on_exit":      {"type": ["string", "null"],
                                 "enum": ["all_at_end", "as_task_completes",
                                          "on_patient_demand", None],
                                 "description":
                                   "When pool members release their resource seizure. "
                                   "all_at_end: window close or last patient covered. "
                                   "as_task_completes: per-patient release. "
                                   "on_patient_demand: preemption-driven."},
        "pool_granularity":     {"type": ["string", "null"],
                                 "enum": ["window", "per_patient", None],
                                 "description":
                                   "Grain at which the pooled task is traced. "
                                   "window: one service_start per engaged "
                                   "staff-unit per window. "
                                   "per_patient: nested per-patient service_starts "
                                   "inside the pooled team assembly. "
                                   "B41 uses this to interpret the trace correctly "
                                   "and to cross-check granularity via B40."},
        "server_id_required":   {"type": ["boolean", "null"],
                                 "description":
                                   "When true, B41 promotes missing server_id "
                                   "from INFO_SKIP to FAIL. Declare true for any "
                                   "pooled process whose composition must be "
                                   "mechanically verifiable."},

        # Duration semantics
        "duration_scope":     {"type": ["string", "null"],
                               "enum": ["per_episode", "per_attempt", None],
                               "description": "Whether service time is sampled once per episode "
                                              "or resampled on each attempt/retry."},
        "interrupt_policy":   {"type": ["string", "null"],
                               "enum": ["resume", "restart", "terminate",
                                        "complete_remaining", "expire", None],
                               "description": "What happens to work after preemption/interruption."},
        "setup_seconds":      {"type": ["number", "null"]},
        "teardown_seconds":   {"type": ["number", "null"]},

        # Capacity-consumption
        "consumption_mode":   {"type": ["string", "null"],
                               "enum": ["exclusive", "fractional", "consumable",
                                        "shared", None],
                               "description": "How the resource is consumed: "
                                              "exclusive (whole server), "
                                              "fractional (split attention), "
                                              "consumable (inventory depleted), "
                                              "shared (supervisory)."},
        "inventory_init":     {"type": ["number", "null"]},
        "replenishment":      {"type": ["object", "null"],
                               "description": "{rate_per_day, trigger: threshold|periodic, "
                                              "threshold_level}"},
        "max_concurrent":     {"type": ["integer", "null"],
                               "description": "Max simultaneous occupants (distinct from capacity "
                                              "when fractional/shared)."},
        "overlap_allowed_with": {"type": "array", "items": {"type": "string"},
                                  "description": "Process names that may run concurrently "
                                                 "on the same resource."},

        # Queue discipline
        "discipline":         {"type": ["string", "null"],
                               "enum": ["FIFO", "LIFO", "SIRO", "priority",
                                        "shortest_job", "custom", None]},
        "tie_break":          {"type": ["string", "null"]},
        "aging":              {"type": ["object", "null"],
                               "description": "{enabled, priority_bump_per_hour}"},
        "reentry_position":   {"type": ["string", "null"],
                               "enum": ["front", "back", "priority_preserve", None]},
        "balk_renege_policy": {"type": ["object", "null"],
                               "description": "{balk_if_queue_length: int, "
                                              "renege_after_seconds: float, "
                                              "jockey_allowed: bool}"},

        # Precedence / ordering / time
        "precedence_before":  {"type": ["string", "null"]},
        "precedence_after":   {"type": ["string", "null"]},
        "min_seconds":        {"type": ["number", "null"]},
        "max_seconds":        {"type": ["number", "null"]},

        # State
        "state":              {"type": ["string", "null"]},
        "forbidden_events":   {"type": "array", "items": {"type": "string"}},
        "dwell_distribution": {"type": ["object", "null"],
                               "description": "{type, mean_seconds, cv} for B36 state dwell."},
        "hazard_rate":        {"type": ["number", "null"]},
        "competing_risks":    {"type": "array", "items": {"type": "string"}},

        # Dynamics-mechanism choice (v5.3): every transition-bearing element
        # (state_transition_rule, terminal_outcome, recurring_activity that
        # implements a probabilistic transition) must commit to one of three
        # temporal-structure mechanisms. The choice determines what the
        # simulation code should emit (continuous-hazard sampling, per-check
        # Bernoulli, or event-triggered transition) and what §1.6 will check
        # the trace's inter-event distribution against.
        #
        #   continuous_hazard: at state entry, sample time-to-transition
        #     from an exponential (or other hazard) distribution. Requires
        #     `hazard_rate_per_hour` (or per-day). Trace shows exponentially
        #     distributed inter-event times.
        #
        #   discrete_check:    every fixed interval, draw a Bernoulli trial
        #     with the per-check probability. Requires `check_cadence_hours`
        #     and `probability_per_check`. Trace shows clustering at
        #     multiples of the cadence.
        #
        #   event_driven:      transition fires when a specific other event
        #     happens. Requires `trigger_event` (with optional eligibility
        #     condition). Trace shows transition co-occurring with trigger.
        #
        #   unspecified:       the user has not committed to a mechanism.
        #     Propagates as a placeholder requiring resolution at Stage A,
        #     and caps Decision Validity at WARN downstream — analogous to
        #     assumed_default on aggregate_target.
        "dynamics":           {"type": ["object", "null"],
                               "description":
                                 "Temporal-structure mechanism for this transition. "
                                 "{type: continuous_hazard|discrete_check|event_driven|unspecified, "
                                 "hazard_rate_per_hour?, check_cadence_hours?, "
                                 "probability_per_check?, trigger_event?, rationale?}. "
                                 "Verified by §1.6 trace-temporal-distribution check."},

        # Terminal / loss
        "terminal_events":    {"type": "array", "items": {"type": "string"}},
        "loss_type":          {"type": ["string", "null"]},
        "must_exist":         {"type": ["boolean", "null"]},
        "condition_expr":     {"type": ["string", "null"],
                               "description": "Predicate for when loss can fire "
                                              "(e.g. 'wait_time > 3h'). Human-readable; "
                                              "validator uses threshold_seconds for checks."},
        "threshold_seconds":  {"type": ["number", "null"]},
        "cause_attribution":  {"type": ["string", "null"]},

        # KPI (aggregate_target)
        "metric":             {"type": ["string", "null"]},
        "expected_value":     {"type": ["number", "null"]},
        "tolerance":          {"type": ["number", "null"]},
        "numerator":          {"type": ["string", "null"]},
        "denominator":        {"type": ["string", "null"]},
        "basis":              {"type": ["string", "null"],
                               "enum": ["episode", "segment", "time_average",
                                        "event_average", None]},
        "warmup_handling":    {"type": ["string", "null"],
                               "enum": ["exclude_warmup", "include_all", "partial", None]},
        "composition_band":   {"type": ["object", "null"],
                               "description": "{process_or_role: [min_frac, max_frac]} "
                                              "for B39 workload plausibility bands."},

        # Calendar / shift
        "calendar":           {"type": ["object", "null"],
                               "description": "{weekdays_only, holidays: [dates], "
                                              "breaks: [[start_h, end_h]], "
                                              "maintenance: [[start_h, end_h]]}"},

        # Observation / information semantics
        "observation_delay_s": {"type": ["number", "null"],
                                "description": "Delay between ground-truth state change "
                                               "and when decisions can observe it."},
        "information_source":  {"type": ["string", "null"],
                                "description": "Which state or signal this process reads "
                                               "when making decisions."},

        # Not-checkable
        "not_checkable_reason": {"type": ["string", "null"]},

        # ── process_contract-specific (unchanged from v5.1) ──────────────────
        "process_name":        {"type": ["string", "null"]},
        "trigger":             {"type": ["string", "null"],
                                "enum": ["periodic", "scheduled", "on_arrival",
                                         "on_discharge", "stochastic_hazard",
                                         "conditional_event", "threshold_crossing",
                                         "event_driven", None]},
        "trigger_condition":   {"type": ["string", "null"],
                                "description": "Predicate for event_driven / "
                                               "threshold_crossing triggers."},
        "every_hours":         {"type": ["number", "null"]},
        "times_per_day":       {"type": ["number", "null"]},
        "schedule_times_hour": {"type": "array", "items": {"type": "number"}},
        "required_roles":      {"type": "array", "items": {"type": "string"}},
        "optional_roles":      {"type": "array", "items": {"type": "string"}},
        "shift_opening_roles": {"type": "array", "items": {"type": "string"}},
        "shift_length_hours":  {"type": ["number", "null"]},
        "interruptible":       {"type": ["boolean", "null"]},
        "preemption_policy":   {"type": ["string", "null"],
                                "enum": ["can_preempt", "can_be_preempted", None]},
        "count_unit":          {"type": ["string", "null"],
                                "enum": ["episode", "role_start", "schedule_window", None]},
        "episode_definition":  {"type": ["object", "null"]},
        "expected_jitter_cv":  {"type": ["number", "null"]},
        "rearm_policy":        {"type": ["string", "null"],
                                "enum": ["after_completion", "after_exit",
                                         "on_reset", "never", None]},
        "one_shot":            {"type": ["boolean", "null"]},
        "boundary_policy":     {"type": ["string", "null"],
                                "enum": ["exclude", "include_partial", "pad", None]},

        # Preemption-rule-specific (v5.2)
        "post_preempt_policy": {"type": ["string", "null"],
                                "enum": ["resume", "restart", "terminate",
                                         "complete_remaining", None]},
        "can_initiate":        {"type": ["boolean", "null"],
                                "description":
                                  "True if this process can initiate preemption "
                                  "of other processes. Pool activities such as "
                                  "Rounding should set can_initiate=False."},
        "preemptible_by":      {"type": "array", "items": {"type": "string"},
                                "description":
                                  "List of process names whose arrival may "
                                  "preempt this process. Empty or null means "
                                  "non-preemptible. Use to declare 'Rounding "
                                  "is preemptible by Deterioration' — the "
                                  "direction distinct from can_initiate."},
        "equal_priority":      {"type": ["boolean", "null"]},
        "min_service_chunk_s": {"type": ["number", "null"]},

        # flow_conservation-specific
        "accounting_basis":    {"type": ["string", "null"],
                                "enum": ["rooted", "windowed", "both", None]},

        # ── NEW element-type-specific fields ─────────────────────────────────

        # eligibility_rule
        "eligibility_predicate": {"type": ["string", "null"],
                                   "description": "Human-readable predicate "
                                                  "(e.g. 'state == ADMITTED and acuity >= 3')"},
        "eligible_states":    {"type": "array", "items": {"type": "string"}},
        "eligible_entity_types": {"type": "array", "items": {"type": "string"}},
        "requires_prerequisites": {"type": "array", "items": {"type": "string"},
                                    "description": "Process names that must have completed."},

        # coverage_rule
        "coverage_target":    {"type": ["string", "null"],
                               "description": "Process or event that constitutes 'covered'."},
        "interval_hours":     {"type": ["number", "null"],
                               "description": "Max allowed gap between coverage events."},
        "missed_coverage_policy": {"type": ["string", "null"],
                                    "enum": ["track", "alert", "loss", "ignore", None]},
        "duplicate_policy":   {"type": ["string", "null"],
                               "enum": ["allowed", "deduplicate", "flag", None]},

        # routing_rule
        "routing_from":       {"type": ["string", "null"]},
        "routing_to":         {"type": "array", "items": {"type": "object"},
                               "description": "[{target, probability, condition}] entries. "
                                              "Probabilities must sum to 1 across entries "
                                              "with the same condition."},
        "fallback_target":    {"type": ["string", "null"]},

        # assignment_continuity
        "continuity_scope":   {"type": ["string", "null"],
                               "enum": ["shift", "episode", "entity_lifetime", "unit", None]},
        "persistent":         {"type": ["boolean", "null"]},
        "load_balance_policy": {"type": ["string", "null"],
                                 "enum": ["round_robin", "least_loaded",
                                          "random", "manual", None]},

        # policy_threshold
        "breach_condition":   {"type": ["string", "null"],
                               "description": "E.g. 'wait_time > 180min'."},
        "breach_threshold_seconds": {"type": ["number", "null"]},
        "escalation_outcome": {"type": ["string", "null"],
                               "description": "What MUST happen on breach "
                                              "(e.g. 'emit loss with type=divert')."},
        "monitoring_interval_seconds": {"type": ["number", "null"]},

        # semantic_scenario
        "treatment":          {"type": ["object", "null"],
                               "description": "Config overrides applied in treatment run."},
        "control":            {"type": ["object", "null"],
                               "description": "Config overrides applied in control run "
                                              "(often empty)."},
        "expected_directional_effect": {"type": ["object", "null"],
                                         "description": "{metric: 'up'|'down'|'unchanged', "
                                                        "min_magnitude, max_magnitude}"},
        "scenario_category":  {"type": ["string", "null"],
                               "enum": ["emergent_pattern", "coupling",
                                        "observation_delay", "control_policy",
                                        "stress", "boundary", None]},

        # coupling_rule
        "source_process":     {"type": ["string", "null"]},
        "target_process":     {"type": ["string", "null"]},
        "coupling_type":      {"type": ["string", "null"],
                               "enum": ["induces", "suppresses", "delays",
                                        "modifies_duration", "modifies_rate", None]},
        "expected_effect_size": {"type": ["number", "null"],
                                  "description": "Expected magnitude of rate/duration change "
                                                 "in target when source toggled."},

        # control_policy
        "policy_signals":     {"type": "array", "items": {"type": "string"}},
        "update_cadence_s":   {"type": ["number", "null"]},
        "fallback_policy":    {"type": ["string", "null"]},
        "override_conditions": {"type": "array", "items": {"type": "string"}},
    },
}

COVERAGE_ITEM_SCHEMA: dict = {
    "type": "object",
    "required": ["dsl_id", "element_type", "mapped_to"],
    "properties": {
        "dsl_id":       {"type": "string"},
        "element_type": {"type": "string"},
        "mapped_to":    {"type": "array", "items": {"type": "string"}},
        "not_checkable":{"type": "boolean"},
        "reason":       {"type": ["string", "null"]},
    },
}

PIPELINE_ARTIFACT_SCHEMA: dict = {
    "type": "object",
    "required": ["model_name", "explicit_user_requirements", "model_assumptions",
                 "dsl_elements", "compliance_spec", "coverage_report"],
    "properties": {
        "model_name": {"type": "string"},
        "explicit_user_requirements": {
            "type": "array", "items": {"type": "string"},
        },
        "model_assumptions": {
            "type": "array", "items": {"type": "string"},
        },
        # Simulation regime (v5.4): classical Sargent distinction between
        # terminating and non-terminating (steady-state) simulations.
        # Downstream checks that assume steady state (Little's Law utilization
        # identity §1.3, extreme-capacity divergence §2.3.1, warm-up detection
        # §3.1.3, arrival-rate window B01) read this field to select the
        # appropriate measurement basis or skip themselves when not applicable.
        #
        #   steady_state: default. Non-terminating queueing system with a
        #     stationary arrival process; steady-state metrics apply.
        #
        #   terminating:  finite population, defined end time, no meaningful
        #     steady state (e.g. single-shift analysis, one-off event).
        #     Little's Law utilization identity, MSER-5 warm-up, and
        #     M/M/c limiting-regime checks are not appropriate and are
        #     skipped with INFO. Terminating-aware variants apply where
        #     available.
        #
        #   burst:        subclass of terminating with a declared arrival
        #     window followed by a drain phase. Requires
        #     `arrival_window_seconds`. Rate-based checks measure over the
        #     window rather than the full run_length.
        "simulation_regime": {
            "type": ["object", "null"],
            "properties": {
                "type": {"type": "string",
                         "enum": ["steady_state", "terminating", "burst"]},
                "arrival_window_seconds": {"type": ["number", "null"]},
                "total_entities": {"type": ["integer", "null"]},
                "rationale": {"type": ["string", "null"]},
            },
            "description":
                "Classical Sargent terminating vs non-terminating regime. "
                "Steady-state checks (Little's Law utilization identity, "
                "MSER-5 warm-up, M/M/c limiting regimes, arrival-rate "
                "windowing) consult this field. Defaults to steady_state "
                "when absent.",
        },
        "dsl_elements": {
            "type": "array",
            "items": DSL_ELEMENT_SCHEMA,
        },
        "compliance_spec": {
            "type": "object",
        },
        "coverage_report": {
            "type": "object",
            "required": ["dsl_items_total", "dsl_items_mapped",
                         "dsl_items_not_checkable", "unmapped_items", "items"],
            "properties": {
                "dsl_items_total":        {"type": "integer"},
                "dsl_items_mapped":       {"type": "integer"},
                "dsl_items_not_checkable":{"type": "integer"},
                "unmapped_items":         {"type": "array", "items": {"type": "string"}},
                "items":                  {"type": "array", "items": COVERAGE_ITEM_SCHEMA},
            },
        },
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Python dataclasses mirroring the schema
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DslElement:
    id: str
    element_type: str
    description: str
    name: str = ""

    # provenance
    assumed_default: bool | None = None
    assumed_default_reason: str | None = None

    # arrival
    entity_type: str | None = None
    rate_per_day: float | None = None
    distribution: dict | None = None
    granularity: str | None = None

    # process
    resources: list[str] = field(default_factory=list)
    service_time: dict | None = None
    schedule: dict | None = None
    recurrence_hours: float | None = None

    # coordination / participation
    coordination_type: str | None = None
    substitutable_roles: list[str] = field(default_factory=list)
    quorum_min: int | None = None
    staggered_allowed: bool | None = None
    supervisory: bool | None = None
    sync_type: str | None = None
    batched_start: bool | None = None
    batched_complete: bool | None = None
    handoff_pair: dict | None = None

    # pool composition (v5.2, drives B41)
    pool_policy: str | None = None            # "fixed_team" | "all_idle_at_start" | "all_available_through_window"
    pool_size: object | None = None            # int, "all", or {"min":..,"max":..} or {role: spec}
    admits_late_joiners: bool | None = None
    release_on_exit: str | None = None         # "all_at_end" | "as_task_completes" | "on_patient_demand"
    pool_granularity: str | None = None        # "window" | "per_patient"
    server_id_required: bool | None = None     # when True, B41 promotes missing server_id INFO_SKIP → FAIL

    # duration
    duration_scope: str | None = None
    interrupt_policy: str | None = None
    setup_seconds: float | None = None
    teardown_seconds: float | None = None

    # capacity-consumption
    consumption_mode: str | None = None
    inventory_init: float | None = None
    replenishment: dict | None = None
    max_concurrent: int | None = None
    overlap_allowed_with: list[str] = field(default_factory=list)

    # queue discipline
    discipline: str | None = None
    tie_break: str | None = None
    aging: dict | None = None
    reentry_position: str | None = None
    balk_renege_policy: dict | None = None

    # precedence/time
    interruptible: bool | None = None
    precedence_before: str | None = None
    precedence_after: str | None = None
    min_seconds: float | None = None
    max_seconds: float | None = None

    # state
    state: str | None = None
    forbidden_events: list[str] = field(default_factory=list)
    dwell_distribution: dict | None = None
    hazard_rate: float | None = None
    competing_risks: list[str] = field(default_factory=list)
    # Dynamics-mechanism choice (v5.3) — see DSL_ELEMENT_SCHEMA for semantics.
    dynamics: dict | None = None

    # terminal / loss
    terminal_events: list[str] = field(default_factory=list)
    loss_type: str | None = None
    must_exist: bool | None = None
    condition_expr: str | None = None
    threshold_seconds: float | None = None
    cause_attribution: str | None = None

    # KPI (aggregate_target)
    metric: str | None = None
    expected_value: float | None = None
    tolerance: float | None = None
    numerator: str | None = None
    denominator: str | None = None
    basis: str | None = None
    warmup_handling: str | None = None
    composition_band: dict | None = None

    # calendar
    calendar: dict | None = None

    # observation
    observation_delay_s: float | None = None
    information_source: str | None = None

    not_checkable_reason: str | None = None

    # process_contract-specific
    process_name: str | None = None
    trigger: str | None = None
    trigger_condition: str | None = None
    every_hours: float | None = None
    times_per_day: float | None = None
    schedule_times_hour: list[float] = field(default_factory=list)
    required_roles: list[str] = field(default_factory=list)
    optional_roles: list[str] = field(default_factory=list)
    shift_opening_roles: list[str] = field(default_factory=list)
    shift_length_hours: float | None = None
    preemption_policy: str | None = None
    count_unit: str | None = None
    episode_definition: dict | None = None
    expected_jitter_cv: float | None = None
    rearm_policy: str | None = None
    one_shot: bool | None = None
    boundary_policy: str | None = None

    # preemption_rule-specific
    post_preempt_policy: str | None = None
    can_initiate: bool | None = None
    preemptible_by: list[str] = field(default_factory=list)
    equal_priority: bool | None = None
    min_service_chunk_s: float | None = None

    # flow_conservation-specific
    accounting_basis: str | None = None

    # eligibility_rule
    eligibility_predicate: str | None = None
    eligible_states: list[str] = field(default_factory=list)
    eligible_entity_types: list[str] = field(default_factory=list)
    requires_prerequisites: list[str] = field(default_factory=list)

    # coverage_rule
    coverage_target: str | None = None
    interval_hours: float | None = None
    missed_coverage_policy: str | None = None
    duplicate_policy: str | None = None

    # routing_rule
    routing_from: str | None = None
    routing_to: list[dict] = field(default_factory=list)
    fallback_target: str | None = None

    # assignment_continuity
    continuity_scope: str | None = None
    persistent: bool | None = None
    load_balance_policy: str | None = None

    # policy_threshold
    breach_condition: str | None = None
    breach_threshold_seconds: float | None = None
    escalation_outcome: str | None = None
    monitoring_interval_seconds: float | None = None

    # semantic_scenario — v5.2+ shape: control.parameters / treatment.parameters,
    # plus expectation + evaluation + metrics[] (typed per-metric direction/min_effect).
    # Legacy fields (expected_directional_effect) are still accepted by the mapper.
    treatment: dict | None = None                          # {"parameters": {...}, ...}
    control: dict | None = None                            # {"parameters": {...}, ...}
    expected_directional_effect: dict | None = None        # legacy; superseded by expectation
    expectation: dict | None = None                        # {"direction", "min_effect_size"}
    evaluation: dict | None = None                         # {"method","confidence","replications"}
    metrics: list[dict] = field(default_factory=list)      # [{"name","direction","min_effect_size"}]
    scenario_category: str | None = None

    # coupling_rule
    source_process: str | None = None
    target_process: str | None = None
    coupling_type: str | None = None
    expected_effect_size: float | None = None

    # control_policy
    policy_signals: list[str] = field(default_factory=list)
    update_cadence_s: float | None = None
    fallback_policy: str | None = None
    override_conditions: list[str] = field(default_factory=list)

    # escalation (Phase 4.5 driver)
    escalation_trigger: str | None = None                  # metric / state predicate
    escalation_target: str | None = None                   # named response_event / owner
    escalation_sla_seconds: float | None = None

    # workload_response (Phase 4.5 driver)
    scale_factor: float | None = None                      # treatment arrival multiplier
    baseline_factor: float | None = None                   # usually 1.0; control arrival mult


@dataclass
class CoverageItem:
    dsl_id: str
    element_type: str
    mapped_to: list[str]
    not_checkable: bool = False
    reason: str | None = None


@dataclass
class CoverageReport:
    dsl_items_total: int
    dsl_items_mapped: int
    dsl_items_not_checkable: int
    unmapped_items: list[str]
    items: list[CoverageItem]

    @property
    def coverage_fraction(self) -> float:
        checkable = self.dsl_items_total - self.dsl_items_not_checkable
        if checkable == 0:
            return 1.0
        return self.dsl_items_mapped / checkable

    @property
    def is_complete(self) -> bool:
        return len(self.unmapped_items) == 0


@dataclass
class ValidationResult:
    status: Literal["PASS", "NEEDS_REVISION", "BLOCKED"]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    observability_gaps: list[str] = field(default_factory=list)
    grounding_errors: list[str] = field(default_factory=list)
    coverage_fraction: float = 0.0

    def summary(self) -> str:
        lines = [f"Status: {self.status}",
                 f"Coverage: {self.coverage_fraction:.0%}"]
        if self.errors:
            lines += [f"  ERROR: {e}" for e in self.errors]
        if self.warnings:
            lines += [f"  WARN:  {w}" for w in self.warnings]
        if self.observability_gaps:
            lines += [f"  OBS:   {o}" for o in self.observability_gaps]
        if self.grounding_errors:
            lines += [f"  GND:   {g}" for g in self.grounding_errors]
        return "\n".join(lines)


@dataclass
class PipelineArtifact:
    model_name: str
    explicit_user_requirements: list[str]
    model_assumptions: list[str]
    dsl_elements: list[DslElement]
    compliance_spec: dict[str, Any]
    coverage_report: CoverageReport
    validation_result: ValidationResult | None = None
    critic_feedback: str | None = None

    def to_dict(self) -> dict:
        return {
            "model_name": self.model_name,
            "explicit_user_requirements": self.explicit_user_requirements,
            "model_assumptions": self.model_assumptions,
            "dsl_elements": [vars(e) for e in self.dsl_elements],
            "compliance_spec": self.compliance_spec,
            "coverage_report": {
                "dsl_items_total":         self.coverage_report.dsl_items_total,
                "dsl_items_mapped":        self.coverage_report.dsl_items_mapped,
                "dsl_items_not_checkable": self.coverage_report.dsl_items_not_checkable,
                "unmapped_items":          self.coverage_report.unmapped_items,
                "items": [vars(ci) for ci in self.coverage_report.items],
            },
            "validation_result": {
                "status": self.validation_result.status,
                "errors": self.validation_result.errors,
                "warnings": self.validation_result.warnings,
            } if self.validation_result else None,
            "critic_feedback": self.critic_feedback,
        }
