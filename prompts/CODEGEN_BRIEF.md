# Code Generation Brief — self-contained

Hand this file + the DSL (`my_dsl.json`) to Claude Code for Stage B. It is
everything needed to generate a correct simulation. **Do NOT read the
validator/checker/harness files** (`validation.py`, `phase0_vvuq.py`,
`process_contracts.py`, `semantic_checks.py`, `vvuq_utils.py`,
`coverage_validator.py`, `semantic_scenarios.py`). The simulation must be
written from the DSL and the trace contract below — never from the checks.
Keeping the generator blind to the checks is what makes the downstream
verification independent and trustworthy.

**Never author or run your own copy of a checker or harness.** These files are
the graders; they are provided. If one is missing when a phase needs it, STOP
and report it — do not re-create the thing that will judge your code. A grader
you wrote will, even unintentionally, be more lenient and pass code the real
grader fails. (Phase 4.5 in particular runs the provided `semantic_scenarios.py`;
never substitute a self-written harness.)

---

## Your task

Generate a complete, runnable SimPy simulation in a file `my_sim.py` that:
- faithfully implements every element declared in `my_dsl.json`, and
- emits the canonical trace contract below.

Expose a function `run_simulation(config)` returning `{trace, metrics, config}`,
and a `DEFAULT_CONFIG` dict (or `default_config()`).

---

## The trace contract (the only spec you need besides the DSL)

```
VVUQ TRACE CONTRACT v5.2: run_simulation(config) must return:
  {"trace":[{"time":float,"event":str,"entity_id":str,"resource":str,"queue":str,
             "loss_type":str,"entity_type":str,"seq":int,"state":str,"process":str,
             "segment_id":str,
             // v5.2 optional fields (required when the corresponding semantic
             //  element_type is declared in the DSL):
             "server_id":str,         // specific server/nurse/agent handling the entity
             "shift_id":str,          // shift/roster instance for calendar checks
             "priority":int|float,    // priority class for queue discipline checks
             "signal_source":str,     // e.g. "sensor", "operator", "alarm" (control_policy)
             "signal_time":float,     // time signal was issued (for observation_delay)
             "threshold_breach":bool, // whether this event crossed a policy_threshold
             "preempted_process":str  // process that was preempted (for coupling_rule)
             }...],
   "metrics":{...},
   "config":{...including compliance_spec}}.
Canonical events: system_arrival, queue_enter, service_start, service_end,
  system_departure, loss, preempt, resume, state_change.
Per-visit entity_ids (never reused). service_start→resource, queue_enter→queue,
loss→loss_type, preempt/resume→segment_id recommended, state_change→state+next_state.
If compliance_spec declares assignment_continuity → service_start must carry server_id.
If compliance_spec declares pool_composition_rules with server_id_required=true → every
  service_start / service_end for that pooled process must carry a deterministic server_id
  (assigned at grant time — B41 uses it to count distinct staff joining at window onset).
  For pool_policy in {"all_idle_at_start","all_available_through_window"}, grants past the
  declared window_end are forbidden: residual resource requests MUST be cancelled at window
  close (B41 FAILs on any service_start for the pooled process after window_end + 60 s).
If compliance_spec declares queue_discipline with priority/aging → queue_enter must carry priority.
If compliance_spec declares control_policy with observation_delay_s → relevant events must carry
  signal_source + signal_time (action event.time minus signal_time = realized delay).
Seed controls ALL randomness. Config must include compliance_spec derived from DSL.
```

---

## Hard rules

- Generate from `my_dsl.json` + this contract ONLY. Do not open the validator
  files; do not tailor the code to make specific checks pass.
- Every declared DSL parameter (distributions, rates, capacities, routing
  probabilities, coordination structure) must appear in the simulation exactly
  as declared. Do not invent or alter declared numbers.
- Emit the canonical events with their required fields. Emit the optional v5.2
  fields (server_id, priority, signal_source/time, threshold_breach,
  preempted_process) only when the corresponding DSL element is declared.
- Assign `seq` in final time order (sort the trace by time, then number it), so
  time is non-decreasing and seq is strictly increasing.
- **Honor the standard scenario config-override keys** so Phase 4.5 (and the
  canonical scenarios) can perturb the model. At config-merge time the sim MUST
  recognise, in addition to its own parameter paths:
    • `arrival_rate_scale` (float) — multiply every declared arrival rate by this
      factor (equivalently divide each inter-arrival `mean_seconds` by it);
      default 1.0. This is how `workload_response` and canonical load scenarios
      work — without it they run treatment==control and come back INCONCLUSIVE.
    • `capacity` ({resource: int}) — override a resource's server count.
    • `routing_probability` ({decision_name: {branch: prob}}) — override a
      declared branch probability.
    • `processes_enabled` ({process: bool}) — toggle an optional process on/off
      (for coupling scenarios), if the model has couplings.
  These are run-time MULTIPLIERS/overrides layered on top of the declared values;
  they do NOT change the declared parameters themselves (DEFAULT_CONFIG keeps the
  declared numbers), so honoring them is implementation, not a model change.
- After the code, output a manifest between ===MANIFEST_START=== and
  ===MANIFEST_END=== listing entity_types, resources, queues, kpis_implemented,
  semantic_fields_emitted, and config_overrides_supported (list every override
  key above that the sim honors, plus the exact metric names it reports — the
  scenario generator reconciles its candidate keys against this manifest).

When done, run the Code Alignment Review against `my_dsl.json` (that review MAY
read the DSL and your code, but still not the validators), revise until ALIGNED,
and save the final file as `my_sim.py`.
