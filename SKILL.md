---
name: trust-framework-vvuq
description: |
  Verification, validation, and uncertainty quantification (V&V/UQ) framework
  for LLM-generated discrete-event simulation models intended for decision
  support. Use this skill whenever the user generates, verifies, validates,
  or assesses the credibility of a simulation built by or with a language
  model — producing a simulation from a description, checking if a model is
  credible, validating a DSL against its description, running what-if
  scenarios, or computing a fitness-for-decision verdict. Use it even when
  the framework is not named explicitly but the task involves trust of
  LLM-generated simulation code, V&V on discrete-event models, calibration
  overfitting, structural model errors, or phrases like "verify this model",
  "is this simulation credible", "check this DSL", "Phase 0/1/2/3/4/4.5",
  "three-dimension verdict", "ICU twin", "manufacturing simulation V&V".
  Do NOT use for general Python development or non-simulation tasks.
---

# Trust Framework for LLM-Generated Simulations

This skill implements an end-to-end V&V/UQ pipeline for simulation models
whose construction has been mediated by language models. It produces a
three-dimension credibility verdict — Credibility, Causal Validity,
Decision Validity — and refuses to credential a model whose declared
mechanisms are not operationally honored by the simulation code.

The framework's core architectural commitment is that **trust-bearing
decisions are computed by deterministic Python; LLM judgement is reserved
for advisory and generative roles**. Read `references/framework_step_realization.md`
for the per-step realization map.

---

## Trust principles — read before doing anything

These are not preferences; they are structural commitments the framework's
integrity guards enforce. Violating them will trip a guard and STOP the
pipeline.

**Never edit files in `scripts/`.** These are the V&V apparatus. The
harness-provenance guard hashes every canonical file at run start and
refuses to proceed if any has been substituted. If a check appears to be
wrong, classify the failure as `VALIDATOR_FIX` and surface it for the
framework maintainer to address — do not patch the validator yourself.
The reason this matters: an LLM that can edit its own verifier can clear
any failing check by relaxing it, which collapses the entire credibility
chain.

**Never silently change declared parameter values.** Declared values
(arrival rates, service distributions, resource capacities, scheduled
cadences, declared targets) are the user's approved model. Changing them
trips the parameter-fingerprint guard (`ILLEGAL_PATCH`, exit 4). If a fix
genuinely requires a different declared value, emit
`DEFER_TO_DSL:<check_id>` and stop. The reason: calibration overfitting
(tuning declared values until checks pass) is the dominant failure mode
of LLM-mediated simulation construction, and the guard is what prevents
it.

**Never write a substitute harness.** If `scripts/validation.py`,
`scripts/phase0_vvuq.py`, `scripts/semantic_scenarios.py`, or any other
canonical checker is missing, stop and surface the missing-file finding.
Do not author a replacement. A self-authored grader credentials itself —
that is the textbook anti-grader failure mode the framework defends
against.

**Only the green VERIFIED banner from `verify_and_run.py` is the
credentialed verdict.** WARN, FAIL, BLOCK, absent banner, partial runs,
and self-narrated success are all uncredentialed. The verdict at Stage E
is computed by `decision_validity.py` from inputs that have themselves
passed deterministic checks; no LLM commentary can move it.

---

## Pipeline overview

The pipeline has five stages. Each stage has documented entry conditions,
deterministic gates, and routing rules. The full per-step routing is in
`references/FRAMEWORK_ROUTING.md`; the end-user procedure is in
`references/RUN_GUIDE.md`. The condensed flow:

```
   Stage A (Specification)
       description → DSL → A2 alignment review → A4 coverage gate
   Stage B (Implementation)
       DSL → simulation code → B2 alignment review → B3 manifest
   Stage C (Credibility)
       integrity guards → preflights → Phase 0–4 → C4 self-heal → manifest
   Stage D (Causal Validity)
       Phase 4.5 what-if harness → tier-weighted aggregate
   Stage E (Judgment)
       Phase 5 plausibility → three-dimension verdict → synthesis
```

---

## When to do what

### Stage A — User provides a description, needs a DSL

Read the description (typically a `.docx`). Generate `my_dsl.json` using
the typed schema in `scripts/dsl_schema.py` (27 element types: arrivals,
services, resources, queues, routing, eligibility, coordination,
preemption, state transitions, etc.). Tag values not directly grounded
in the description as `assumed_default: true` with a written rationale —
this provenance flag propagates through every downstream layer.

**Dynamics-mechanism declaration (v5.3).** For every transition-bearing
element (`state_restriction`, `terminal_outcome`, `recurring_activity`
that implements a probabilistic transition like discharge or mortality)
declare a typed `dynamics` block with one of four `type` values:

- `continuous_hazard` — at state entry, sample time-to-transition from
  an exponential distribution; requires `hazard_rate_per_hour`.
  Trace shows exponentially distributed inter-event times.
- `discrete_check` — every fixed interval, draw a Bernoulli trial;
  requires `check_cadence_hours` and `probability_per_check`. Trace
  shows clustering at multiples of the cadence.
- `event_driven` — transition fires when a specific other event happens;
  requires `trigger_event`.
- `unspecified` — user has not committed; propagates as a placeholder
  requiring resolution and caps Decision Validity at WARN.

The choice matters because the description rarely commits to one
mechanism explicitly. The LLM's default ("hourly check") is operationally
distinct from "continuous discharge rate" even when the average behaviour
is the same — different inter-event distributions, different interaction
with other scheduled events, different tail behaviour. Scan the
description for hazard-class language ("rate", "hazard", "half-life",
"exponential time to") and prefer `continuous_hazard` for transitions
the description describes as continuous-time. The §1.6 trace-distribution
check at Phase 1 verifies the trace matches whatever you declare.

Run the A2 alignment review pass (independent LLM read of the description
+ DSL). Then run the deterministic description→DSL coverage gate:

```bash
python scripts/description_dsl_coverage.py --docx description.docx --dsl my_dsl.json
```

The gate now performs two checks: (a) KPI rows in the description docx
must match a DSL `aggregate_target` with consistent value and CI; (b)
dynamics-keyword scan — if the description uses continuous-hazard
language but no DSL element declares `dynamics.type='continuous_hazard'`,
the gate FAILs with the specific phrases that triggered it. If either
check FAILs (missing KPI matches, placeholders to promote, or
hazard-mechanism mismatch), revise the DSL and re-run before proceeding
to Stage B.

### Stage B — User has a DSL, needs simulation code

Read the DSL and `prompts/CODEGEN_BRIEF.md`. Generate `my_sim.py` against
the SimPy trace contract.

**Critical**: when generating sim code, do NOT read
`scripts/validation.py`, `scripts/phase0_vvuq.py`,
`scripts/process_contracts.py`, `scripts/semantic_checks.py`,
`scripts/semantic_scenarios.py`, `scripts/vvuq_utils.py`, or
`scripts/coverage_validator.py`. The codegen LLM must remain blind to
the validators it will be checked against. Reading them is how
reviewer-generator collusion enters; the codegen brief documents this
boundary.

After codegen, populate the `declared_element_implementation_status`
section of the sim's `MANIFEST` (one entry per DSL element ID with
`status`, `code_citation` for FULL, or `justification` for
NOT_IMPLEMENTED / DEFERRED_TO_DSL). Then run the B2 alignment review
followed by:

```bash
python scripts/dsl_code_coverage.py --dsl my_dsl.json --sim my_sim.py
```

This is the deterministic counterpart to the alignment review and is what
the C1 preflight enforces.

### Stage C — User has both, wants the credibility check

Run the orchestrated pipeline:

```bash
python scripts/verify_and_run.py --dsl my_dsl.json --sim my_sim.py
```

This invokes, in order: harness-provenance preflight (C0), DSL→code
coverage preflight (C1), four integrity guards (G1–G4), Phase 0
(structural + per-element declarative + process + semantic contracts —
73 checks), Phase 1 (mathematical invariants), Phase 2 (limiting
regimes), Phase 3 (UQ — warm-up, replication, CRN, sensitivity), and
Phase 4 (face validity, sensitivity sweeps, preemption semantics).

If any phase returns FAIL or BLOCK, the orchestrator writes a structured
repair prompt to `vvuq_out/repair_prompt_phase{N}_attempt{K}.md`. Read it,
classify each failing check, act accordingly:

- **`SIM_CODE`** — edit `my_sim.py` only. Never touch declared parameter
  values. Re-run the same command.
- **`DSL_SPEC`** — emit `DEFER_TO_DSL:<check_id>` and stop. The user must
  revise the DSL at Stage A.
- **`VALIDATOR_FIX`** — emit `VALIDATOR_FIX:<check_id>` and stop. The
  user must escalate to the framework maintainer. This includes any case
  where a check crashes on a legitimate declaration or where a check's
  grouping logic doesn't honor a spec-declared segmentation.

Repair budget defaults to 3 attempts per phase. The progress-aware
budget guard (G4) burns an attempt only when no progress was made.

The green VERIFIED banner appears when all phases PASS/WARN against the
current `sim_hash` and no integrity guard has tripped. The verdict is
stamped to `vvuq_out/verified_manifest.json`.

**STOP banner reference** (full table in `references/RUN_GUIDE.md`):

| Banner | Exit | Cause | Routes to |
|---|---|---|---|
| 🟢 VERIFIED | 0 | Pipeline complete + manifest stamped | Stage D |
| 🔴 STOP — check FAILED | 1 | Phase FAIL, repair prompt written | Apply LLM patch, re-run |
| 🔴 STOP — repair gave up | 2 | `max_attempts` exhausted | Read escalation file |
| 🔴 STOP — verifier couldn't run | 3 | Sim won't load, harness missing, or coverage failed | Diagnose and fix |
| 🔴 STOP — ILLEGAL_PATCH | 4 | Declared parameter changed | Revert; route to A1 |
| 🔴 STOP — SUSPICIOUS_PATCH | 5 | Whole event class appeared/vanished | Revert; check VALIDATOR |

### Stage D — User has a Verified model, wants what-if evidence

Run the Phase 4.5 harness with declared scenarios plus the canonical library:

```bash
python -c "from scripts.semantic_scenarios import run_phase4_5; \
           import json; \
           # ... harness invocation pattern, see RUN_GUIDE Step D1"
```

Or via the provided wrapper. Per-scenario verdicts are PASS / FAIL /
INCONCLUSIVE / BLOCK / NOT_A_CLAIM (the last guards against oracle
relaxation — direction = "any" is always NOT_A_CLAIM). The aggregate is
provenance-weighted:

- `PASS` — at least one authoritative scenario, all authoritative PASS
- `PASS_ADVISORY_ONLY` — only advisory-tier ran, all PASS (explicit:
  "not a clean authoritative pass")
- `PASS_NO_SCENARIOS` — nothing ran (explicit: "no decision-relevant
  claim tested")
- `WARN` — authoritative INCONCLUSIVE or advisory FAIL/BLOCK
- `FAIL` / `BLOCK` — authoritative failure

For auto-generated scenarios (from LLM exploration), use
`scripts/scenario_ingest.py` to normalize provenance to `auto_generated`
(direction-only, magnitude stripped). This prevents automatic
exploration from minting authoritative trust.

### Stage E — Produce the final verdict

Run the Phase 5 plausibility review (emit typed flags only — never FAIL):
flag categories are `emergent_behavior`, `realism_gap`, `decision_risk`,
`communication_risk`, each with severity and an explicit citation to a
phase result or trace observation.

Then invoke the deterministic verdict:

```bash
python scripts/decision_validity.py --vvuq_out vvuq_out/
```

This produces a three-row verdict — Credibility / Causal Validity /
Decision Validity. The three are never collapsed into a single scalar.
Decision Validity is capped at WARN when authoritative-tier coverage of
the decision-relevant claim is absent or when a Phase 5 high-severity
flag in `decision_risk` or `realism_gap` is open. Report the verdict
with per-dimension reasons and a "what would move Decision Validity to
PASS" recommendation; do NOT narrate your own verdict.

---

## Failure-mode pattern recognition

The framework defends against six empirically-observed failure modes.
When debugging unexpected behavior, recognizing the mode is half the fix:

1. **Conceptual-to-coded model verification gaps** — DSL declares a
   mechanism the code silently omits or substitutes (e.g., preemption
   replaced by priority queueing, with documented rationale). Caught by
   the DSL→code coverage manifest and §4.2.6 rule evidence.

2. **Calibration overfitting** — free parameter tuned until aggregates
   match. Caught by §1.4 trace-metric consistency and the parameter
   fingerprint guard.

3. **Event-log manipulation** — events suppressed, synthesized, or
   compressed to satisfy a check. Caught by the trace-fidelity guard
   (G3) and per-event-class conservation checks.

4. **Modification of the V&V apparatus** — LLM tries to edit a checker
   or self-author a harness. Caught by the harness-provenance guard (C0).

5. **Non-independence of generator and verifier** — same LLM family
   ratifies its own rationalizations. Mitigated by deterministic checks
   for trust-bearing decisions.

6. **Iterative tuning to validation residuals** — repair loop runs
   monotonically improving but never converges because the residual is
   structural. Caught by §4.2.3 residual-fingerprint diagnostics (the
   "STRUCTURAL CEILING REACHED" signal with named compatible /
   incompatible mechanism patterns).

7. **Dynamics-mechanism substitution** — DSL pins what a transition does
   but the temporal-structure mechanism (continuous hazard vs discrete
   check vs event-driven) is left implicit; the LLM picks the simplest
   (usually per-hour Bernoulli) and the choice is invisible to alignment
   review. Caught at Stage A by the description→DSL dynamics-keyword
   scan and at Phase 1 by §1.6 trace-temporal-distribution (KS-test
   against declared hazard rate plus hour-boundary cluster check).

Full catalog with framework coverage in `references/error_taxonomy_coverage.md`.

---

## Worked example

A minimal example case (Emergency Room) is bundled at
`examples/simple_er/`. The folder contains the description.docx; running
the skill against it should produce a DSL, then a simulation, then a
VERIFIED verdict. Use this for sanity checks when first deploying the
skill.

---

## Output format guidance

For every credibility check the user runs, report:

- The phase or stage the check belongs to.
- The verdict (PASS / WARN / FAIL / BLOCK / NOT_EXERCISED) per dimension.
- The check's classification of any failure (`SIM_CODE` / `DSL_SPEC` /
  `VALIDATOR_FIX`) — never absorb a failure into a forced PASS.
- The three-dimension verdict at Stage E, with each dimension on its own
  line. Do not collapse into a scalar.
- The "what would move Decision Validity to PASS" recommendation when
  Decision Validity is below PASS.

For STOP banners, include the exit code and the routing the user should
take per `references/RUN_GUIDE.md`.

---

## References

- `references/RUN_GUIDE.md` — end-user procedure for running the framework
- `references/FRAMEWORK_ROUTING.md` — per-step routing reference with the
  complete checks catalog (Phase 0 A1–A8, B01–B41; Phase 1 §1.1–§1.5;
  Phase 2 §2.1–§2.3; Phase 3 §3.1–§3.4; Phase 4 §4.1–§4.4; Phase 4.5
  canonical scenarios; Phase 5 flag categories; Stage E verdict gates)
- `references/error_taxonomy_coverage.md` — mapping of every error class
  in the Sargent / NASA M&S V&V taxonomy plus LLM-era extensions to the
  specific framework mechanisms that address them
- `references/framework_step_realization.md` — per-step map of which
  steps are deterministic Python vs. LLM prompt vs. hybrid; the
  architectural commitment that all trust-bearing decisions are
  deterministic
- `prompts/CODEGEN_BRIEF.md` — the contract for Stage B simulation code
  generation, including the forbidden-file list
