# Trust Framework — Routing Reference (continuous update)

A living, step-by-step reference for the trust framework. Each step states its
inputs, action, outputs, and exact routing rules — where the step goes on
success, on each kind of failure, and on each integrity-guard trip. Keep this
file in sync with the framework code as new defences land.

---

## Changelog

| Version | Date       | Change                                                                                                             |
|---------|------------|--------------------------------------------------------------------------------------------------------------------|
| 1.0     | 2026-05-30 | Initial routing reference covering Stages A–E + preflight gates + integrity guards.                                |
|         |            | Includes §1.4 (trace-metric), §1.5 (emission contract), §4.2.3 (barrier), §4.2.6 (rule evidence), `dsl_code_coverage`, `description_dsl_coverage`. |
| 1.1     | 2026-06-06 | Added `residual_diagnostics.py` — residual-violation fingerprinting + architectural-implication tagging on §4.2.3 barrier check. STRUCTURAL CEILING signal short-circuits parametric tuning loops on mechanism-bound failure modes. |
| 1.2     | 2026-06-06 | Complete checks catalog: every check across Phase 0 (A1–A8, B01–B41), Phase 1 (§1.1.x, §1.2.x, §1.3.x, §1.4.x, §1.5), Phase 2 (§2.1–§2.3), Phase 3 (§3.1.x–§3.4.x), Phase 4 (§4.1–§4.4 incl. §4.2.1–§4.2.6), Phase 4.5 (7 canonical scenarios), Phase 5 (typed flags), preflight gates, integrity guards. |
| 1.3     | 2026-06-25 | Added `dynamics` field to DSL schema for transition-bearing elements (continuous_hazard / discrete_check / event_driven / unspecified). Added §1.6 trace-temporal-distribution check that verifies empirical inter-event distribution against declared dynamics (KS-test for hazard, cadence-cluster for discrete, trigger-co-occurrence for event-driven). Extended A4 description→DSL coverage gate with dynamics-mechanism keyword scan (catches descriptions that use continuous-hazard language when the DSL declares only discrete checks). Motivated by the ICU discharge case where the LLM implemented per-hour Bernoulli discharge for what the user intended as a continuous transition rate. |
| 1.7     | 2026-07-02 | **Wave 3 canonical-library, guard-hardening, and diagnostic fixes** (12/12 targeted tests + full regression). **H8**: canonical_workload_response uses a `metric_synonyms` set — the harness resolves the first name present in the sim's report and emits ONE result (single INCONCLUSIVE only if none present), removing the guaranteed-WARN ceiling on brief-compliant sims with include_canonical=True. **H9**: canonical_queue_discipline now targets `wait_time_p95.<queue>.high_priority` (direction down — theoretically defensible) instead of the queue's overall p95 (which typically RISES under priority discipline; the old claim could FAIL correct models); per-class metric documented in CODEGEN_BRIEF. **H6**: B41 accepts both `[lo,hi]` window pairs (mapper-emitted) and `{start_hour,end_hour}` dicts. **C3**: import-required modules (vvuq_utils, process_contracts, semantic_checks, run_vvuq) moved to `_REQUIRED_CHECKERS` — their absence now trips the harness-provenance STOP instead of a generic Phase 0 crash. **C2 (partial)**: at stamp time, the ledger's claimed per-phase verdicts are cross-checked against the on-disk phase report files; any mismatch (missing report, unreadable report, or report recording FAIL/BLOCK) refuses the stamp with a ledger-inconsistency STOP — a forged ledger alone no longer stamps VERIFIED (per review guidance, cryptographic signing was rejected as ineffective under the framework's adversary model). **M1**: trace-fidelity guard flags >50% count suppression within a surviving event class (baseline ≥20) — partial suppression previously passed if the class still existed; legitimate shifts route through the existing --accept-trace-change. **M3**: Phase 3 crashed perturbed/replication runs now append their BLOCK records instead of being silently dropped — a sim that crashes only under perturbation no longer looks cleaner than one that runs and WARNs. **M4**: §4.2.3 excludes the terminal preempt of never-completed (abandoned) segments from the barrier-ratio test — its ratio was 1.0 by construction (unfalsifiable) — and reports them as UNVERIFIED-unresolvable (WARN when they are the only preempts). **M5**: residual_diagnostics marks clusters structural by fingerprint alone; share now only routes the implication (≥75% structural → MECHANISM, >25% → MIXED, else PARAMETRIC) — a 70%-structural residual previously produced 'parametric tuning promising', the opposite of correct guidance, and the MIXED branch was unreachable. |
| 1.6     | 2026-07-02 | **Wave 2 vacuous-check and crash fixes** (from the reviewed gap audit; 9/9 reproduce-then-fix tests). **H1**: B10 now enforces `expected_preempt_count=0` as an exact assertion (PASS iff zero preempts, else FAIL) — the strongest form of the non-preemption contract was previously INFO-skipped by compare_ratio; fixed locally per review guidance, compare_ratio untouched. **H2**: B12 rewritten to enforce the POSITIVE entity-type constraint the mapper actually emits, with per-process union semantics for multi-type shared processes (the ER pattern); the vacuous `not_entity_type`-vs-None comparison is applied only when non-null. **H3**: Layer B (B01–B17) checkers now run inside per-checker try/except → BLOCK (matching the B24–B41 wrapper); one malformed spec entry no longer kills the whole gate with no verdict. **H4/H5**: B02 null-cadence and B08 one-sided-precedence now INFO-skip instead of crashing (TypeError / AttributeError class). **H7**: B26 routing restricts next-event attribution to declared targets ∪ fallback ∪ terminals; interleaved concurrent service_starts (hourly checks, monitoring) are skipped and counted separately instead of corrupting branch fractions. **H10**: the DSL→code coverage preflight now fails CLOSED — an exception in the checker is a STOP (return 3), not a "skipped, proceeding" bypass; only the absence of --dsl legitimately skips. **C4**: scenario_ingest forces provenance=auto_generated unconditionally — generator output claiming sme_approved is ignored with a warning (the legitimate SME path is a human editing the DSL, which never passes through ingest); also clamps generator-supplied evaluation blocks (confidence ≥ 0.90, replications ≥ 1000) so a generated scenario cannot weaken its own statistical test (audit F4). |
| 1.5     | 2026-07-02 | **Wave 1 trust-integrity fixes** (from the independently-reviewed internal gap audit; all five reproduce-then-fix verified). **C1**: model-identity guard no longer auto-rebaselines on DSL-hash change — it STOPs with new exit code 6 ("model identity changed") and requires explicit `--reset`, closing the bypass where a DSL edit silently disarmed the parameter-fingerprint and trace-fidelity guards. **C6**: B15, B21, and §1.1.2 flow-conservation checks rewritten with non-exclusive terminal counts — the previous formulations were algebraic identities that could never fail; all three now FAIL/BLOCK explicitly on double-terminated entities (departure AND loss). **C7**: a scenario with an ABSENT direction now routes to NOT_A_CLAIM instead of defaulting to "unchanged" (which passed trivially under paired seeds); explicit "unchanged" claims unaffected. **C8**: Stage E credibility roll-up fails closed — unknown/blank statuses and missing phases map to NOT_EXERCISED (not PASS), all five phases required, Phase 0's `gate` report shape now translated (BLOCKED→BLOCK, OPEN+WARN-tally→WARN), corrupt reports recorded as UNREADABLE instead of silently skipped. **N1**: the B18–B23 exception wrapper now emits BLOCK instead of WARN — a crashing process-contract validator previously counted as validated (fail-open). |
| 1.4     | 2026-07-01 | Terminating-system awareness. Added `simulation_regime` field to DSL schema (steady_state / terminating / burst) and propagated through Phase0Context (`is_terminating`, `rate_measurement_window_seconds`). Updated Phase 0 B01 arrival-rate check to measure over declared arrival window rather than run_length for terminating/burst regimes. Updated §1.3 utilization identity to use windowed λ and downgrade FAIL→WARN for terminating regimes (Little's Law utilization identity is a steady-state property). Updated §2.3.1 extreme_zero_capacity to skip with INFO for terminating regimes (finite bursts drain eventually even at cap=1). Updated §3.1.3 MSER-5 to skip for terminating regimes (no steady state to warm past). Updated §4.2.1 preemption_fires to respect declared `can_initiate=False` (matches B10's existing logic). Fixed compliance_mapper↔semantic_scenarios soft-pool field-name drift (`type` vs `pool_type`). Fixed canonical_preemption_causality and canonical_transition_sensitivity to read the field names the mapper actually emits. Documented `duration_scale`, `queue_discipline`, `eligibility_enforced`, `transition_disabled` overrides and `mean_duration.<proc>`, `wait_time_p95.<queue>`, `transitions.<name>`, `preemption_count`, `eligibility_violations` metric names in CODEGEN_BRIEF. Added coverage_validator cross-check for coordination-element `process` names matching declared service_processes. Fixed missing `defaultdict` import in description_dsl_coverage.py. Motivated by the MASCAL Tele-EM triage case (60 casualties over 45 minutes, 225-minute total run) where every Phase 0–4.5 check that assumes stationary steady-state arrivals produced a defensible WARN despite a correctly-modeled terminating simulation. |

When you add a defence or change a routing rule, append a row here with the date
and a one-line summary, and update the step table below.

---

## High-level routing diagram

```
   ┌─────────────────┐
   │ user description│
   └────────┬────────┘
            ▼
        ┌───────┐  ALIGNED   ┌───────┐  ALIGNED   ┌──────────┐
        │Stage A├───────────▶│Stage B├───────────▶│verify_and│
        └───┬───┘            └───┬───┘            │   _run   │
            │ NEEDS_REVISION    │ NEEDS_REVISION  └────┬─────┘
            └◀──loop until──────┘                      │
                                                       ▼
                                           ┌──────────────────────┐
                                           │ Preflight #1: harness│
                                           │       provenance     │
                                           └──────────┬───────────┘
                                            STOP─◀── │missing? ──▶OK
                                                      ▼
                                           ┌──────────────────────┐
                                           │ Preflight #2: DSL→   │
                                           │   code coverage      │
                                           └──────────┬───────────┘
                                            STOP─◀── │missing? ──▶OK
                                                      ▼
                                          ┌────────────────────────┐
                                          │ Orchestrator pipeline  │
                                          │ Phases 0–4 with        │
                                          │ integrity guards on    │
                                          │ EVERY invocation       │
                                          └──────────┬─────────────┘
                                                     │ PASS
                                                     ▼
                                              ┌────────────┐
                                              │  VERIFIED  │
                                              │  manifest  │
                                              └─────┬──────┘
                                                    ▼
                                              ┌──────────┐
                                              │ Stage D  │ Phase 4.5
                                              │ harness  │
                                              └─────┬────┘
                                                    ▼
                                              ┌──────────┐
                                              │ Stage E  │ Phase 5 + Synthesis
                                              │ verdict  │ Three dimensions
                                              └──────────┘
```

---

## Stage A — Specification

### Step A1: DSL generation by LLM #1

| | |
|---|---|
| **Input** | User's plain-English description |
| **Action** | LLM #1 produces a typed DSL using the 25-element vocabulary. Each declared element gets an ID (A1, R1, PR1, …). |
| **Output** | Draft `my_dsl.json` |
| **Routes to** | **A2** (always) |

### Step A2: DSL Alignment Review by LLM #2

| | |
|---|---|
| **Input** | Draft `my_dsl.json`, original description |
| **Action** | Independent LLM reviewer assesses completeness, internal consistency, grounding in the description, executability. |
| **Output** | Verdict `ALIGNED` or `NEEDS_REVISION` |
| **Routes to (ALIGNED)** | **A3** (or A4 if description has reference tables) |
| **Routes to (NEEDS_REVISION)** | **A1** (revise and re-review; loop continues until ALIGNED) |

### Step A3: Declare `emission_contract` on competing-risks arcs *(optional but recommended)*

| | |
|---|---|
| **Input** | `my_dsl.json` |
| **Action** | For every `competing_risks` entry under `state_transition_rules`, declare `emission_contract` ∈ {`silent_state_change`, `resource_event`, `both`}. |
| **Output** | Augmented `my_dsl.json` |
| **Routes to** | **A4** if description has KPI table; **B1** otherwise |
| **Why** | Without this, §1.5 in Stage C will emit INFO with a recommendation rather than verify the per-direction contract. |

### Step A4: Description→DSL coverage gate *(optional but recommended)*

| | |
|---|---|
| **Input** | `your_description.docx`, `my_dsl.json` |
| **Action** | `python description_dsl_coverage.py --docx ... --dsl my_dsl.json` parses tables with confidence intervals, extracts KPI rows, matches each to a DSL `aggregate_target` (with %↔fraction, days↔hours normalization), and verifies values are consistent with the description's value/CI. |
| **Output** | PASS or FAIL report |
| **Routes to (PASS)** | **B1** (Stage B) |
| **Routes to (FAIL — KPI rows without matching aggregate_target)** | **A1** — add the missing `aggregate_target` entries to the DSL, re-review |
| **Routes to (FAIL — placeholder values)** | **A1** — promote `assumed_default: true` entries to real values from the description |
| **Routes to (FAIL — value disagrees)** | **A1** — reconcile the DSL value with the description's value/CI |

---

## Stage B — Implementation

### Step B1: Simulation generation by LLM #1 (blind to validators)

| | |
|---|---|
| **Input** | `my_dsl.json`, `CODEGEN_BRIEF.md` |
| **Action** | LLM #1 generates `my_sim.py` (SimPy, conforming to the trace contract) plus a `MANIFEST` block. **Forbidden** from reading: `validation.py`, `phase0_vvuq.py`, `process_contracts.py`, `semantic_checks.py`, `vvuq_utils.py`, `coverage_validator.py`, `semantic_scenarios.py`. |
| **Output** | Draft `my_sim.py` with `MANIFEST` |
| **Routes to** | **B2** (always) |

### Step B2: Code Alignment Review by LLM #2

| | |
|---|---|
| **Input** | Draft `my_sim.py`, `my_dsl.json`, original description |
| **Action** | Independent reviewer LLM checks code↔DSL alignment, semantic-element honouring, code↔user-intent, compliance_spec embedding, trace contract conformance, manifest accuracy. |
| **Output** | Verdict `ALIGNED` or `NEEDS_REVISION` |
| **Routes to (ALIGNED)** | **B3** |
| **Routes to (NEEDS_REVISION)** | **B1** (revise and re-review; loop until ALIGNED) |

### Step B3: Declared-element implementation manifest

| | |
|---|---|
| **Input** | `my_sim.py`, `my_dsl.json` |
| **Action** | Add `declared_element_implementation_status` to `MANIFEST`. For every DSL element ID, record one of: `FULL` (with `code_citation`), `PARTIAL` (with `gap_description`), `NOT_IMPLEMENTED` (with `justification`), `DEFERRED_TO_DSL` (with `justification`). |
| **Output** | `my_sim.py` with full MANIFEST |
| **Routes to** | **C0** (Stage C entry — preflight) |
| **Optional standalone check** | `python dsl_code_coverage.py --dsl my_dsl.json --sim my_sim.py` |

---

## Stage C — Credibility (verify_and_run.py)

### Step C0: Preflight #1 — harness-provenance guard

| | |
|---|---|
| **Trigger** | Every `verify_and_run.py` invocation |
| **Action** | Compute hashes of every canonical checker/harness file. Required: `validation.py`, `phase0_vvuq.py`, `semantic_scenarios.py`, `run_validators.py`, `self_heal_orchestrator.py`. Optional but recorded: `compliance_mapper.py`, `process_contracts.py`, `semantic_checks.py`, `coverage_validator.py`, `vvuq_utils.py`, `dsl_schema.py`, `scenario_ingest.py`, `decision_validity.py`. |
| **Routes to (all required present)** | **C1** |
| **Routes to (any missing)** | **STOP — required verification files are missing** (exit 3). User must restore the canonical files; **never let an LLM author its own copy of a grader**. |

### Step C1: Preflight #2 — DSL→code coverage gate

| | |
|---|---|
| **Trigger** | When `--dsl` is provided (recommended) |
| **Action** | `dsl_code_coverage.py` enumerates every DSL element and looks up its status in `MANIFEST["declared_element_implementation_status"]`. |
| **Routes to (all elements covered with valid status + required justification)** | **C2** |
| **Routes to (any element MISSING from manifest)** | **STOP — DSL→code coverage preflight failed**. Routes user back to **B3** to add the missing entries. |
| **Routes to (any element NOT_IMPLEMENTED / DEFERRED_TO_DSL without justification)** | **STOP**. Routes user back to **B3** to add the justification. |
| **Routes to (any element PARTIAL without gap_description)** | **STOP**. Routes user back to **B3**. |
| **Routes to (any element with unrecognised status)** | **STOP**. Routes user back to **B3**. |

### Step C2: Per-invocation integrity guards

These fire at the top of every orchestrator invocation, **before any phase
validator runs**. They check the current artefacts; a patch made between runs
is re-checked on the next invocation.

#### Guard G1: Model-identity guard

| | |
|---|---|
| **Action** | Compute SHA-256 of the current `my_dsl.json`. Compare to stored `model_id` baseline. |
| **Routes to (match or no baseline)** | **G2** (proceed) |
| **Routes to (mismatch)** | **STOP — MODEL_CHANGED (exit 6)**. Refuses to proceed: continuing would silently discard the parameter-fingerprint and trace-fidelity baselines, disarming G2/G3 (audit fix C1, v1.5). User must re-run with explicit `--reset` (after Stage A review of the revision) to start a new credibility chain. |

#### Guard G2: Parameter-fingerprint guard

| | |
|---|---|
| **Action** | Compute fingerprint of declared parameters from `DEFAULT_CONFIG`. Compare to stored baseline. |
| **Routes to (match or first run)** | **G3** |
| **Routes to (mismatch)** | **ILLEGAL_PATCH** (exit 4). Refuse to continue. Routes user to revert the parameter change. Write `illegal_patch.md` report. |

#### Guard G3: Trace-fidelity guard

| | |
|---|---|
| **Action** | Run the sim under `DEFAULT_CONFIG`, build a Counter of event types. Compare to stored baseline. |
| **Routes to (event-class set preserved, or first run)** | **G4** |
| **Routes to (whole event class APPEARED or DISAPPEARED with ≥ 5 events)** | **SUSPICIOUS_PATCH** (exit 5). Refuse to continue. Routes user to revert. Write `suspicious_patch.md` report. |
| **Override** | `--accept-trace-change` (rebaselines the trace profile; use only after manual verification). |

#### Guard G4: Progress-aware budget

| | |
|---|---|
| **Action** | Compare current sim hash and failure set to the previous attempt. |
| **Routes to (sim_hash unchanged from last attempt)** | Continue without burning an attempt. Investigation re-run is free. |
| **Routes to (failure set strictly shrunk; new_fail set is empty)** | Continue without burning an attempt. Progress was made. |
| **Routes to (same failure set after a sim edit)** | Burn one attempt — patch had no effect. |
| **Routes to (regression: new failures appeared)** | Burn one attempt + log the new failures. |

After all guards pass, the orchestrator proceeds to **C3 (pipeline mode)**.

### Step C3: Phase pipeline (sequential, per-phase ledger)

The orchestrator iterates phases [0, 1, 2, 3, 4] in order. For each phase, it
checks the **phase ledger** (`phase_ledger[<phase>]` keyed by `sim_hash`):

- If the phase already passed against the **current** sim_hash → **skip** (cached).
- Otherwise → run the phase validator.

Per-phase routing:

| Phase | What runs | Routes to (PASS or WARN) | Routes to (FAIL or BLOCK) |
|-------|-----------|--------------------------|---------------------------|
| **0** | `phase0_vvuq.py` — structural (A0–A8), declarative (B01–B17), process contracts (B18–B23 with count_unit handling), semantic (B24–B41) | Next phase | **C4 (self-heal loop)** |
| **1** | `validation.py validate_phase1` — Little's Law, conservation, queue/resource invariants, **§1.4 trace-metric consistency**, **§1.5 emission contract** | Next phase | **C4** |
| **2** | `validation.py validate_phase2` — M/M/c gate (when applicable), limiting/extreme regimes | Next phase | **C4** |
| **3** | `validation.py validate_phase3` — warm-up (Welch + MSER), replications, CRN | Next phase | **C4** |
| **4** | `validation.py validate_phase4` — sensitivity sweeps, **§4.2.3 barrier_fraction enforcement** (with residual-fingerprint + architectural-implication tagging via `residual_diagnostics.py`), **§4.2.6 preemption-rule evidence** | **C5 (pipeline complete)** | **C4** |

### Step C4: Self-heal loop

| | |
|---|---|
| **Trigger** | Any phase returns FAIL or BLOCK |
| **Action** | Orchestrator writes a structured repair prompt to `vvuq_out/repair_prompt_phase{N}_attempt{K}.md`. The prompt requires the LLM to: (1) classify each failing check, (2) act according to the classification. |
| **Classification: SIM_CODE** | LLM edits `my_sim.py`. User re-runs `verify_and_run.py`. Routes back to **G1** (integrity guards re-check the patch). |
| **Classification: DSL_SPEC** | LLM emits `DEFER_TO_DSL: <check_id>` and stops. Routes user back to **A1** to revise the DSL; the model-identity guard will auto-rebaseline when the new DSL hash is detected. |
| **Classification: VALIDATOR** | LLM emits `VALIDATOR_FIX: <check_id>` and stops. Out-of-loop escalation; framework maintainer updates the validator code. User re-runs once the framework is updated. |
| **Attempt limit** | `max_attempts` (default 3) per phase, counting only attempts where Guard G4 deemed the run "burnable" (no progress / regression). |
| **Routes to (attempt budget exhausted)** | **Escalation** (exit 2). Writes `escalation_phase{N}.md`. User intervenes manually. |
| **Routes to (3 consecutive STOP-class guard trips on the same patch attempt)** | The triggering guard's specific STOP is the routing (ILLEGAL_PATCH → exit 4, SUSPICIOUS_PATCH → exit 5, harness missing → exit 3). |

### Step C5: Pipeline complete — Step 4 re-review check

| | |
|---|---|
| **Trigger** | All phases [0,1,2,3,4] PASS/WARN against the current sim_hash |
| **Action** | Compare current `sim_hash` to `state["step4_approved_sim_hash"]` (set by user via `--step4-approved` after the Stage B Code Alignment Review). |
| **Routes to (no Step 4 approval recorded)** | Print "NOTE: no Step 4 approval recorded" and continue to **C6**. User should approve once if they have not. |
| **Routes to (step4 hash matches current sim_hash)** | "Step 4 approval current — no re-review needed." Routes to **C6**. |
| **Routes to (mismatch — sim patched since Step 4 approval)** | Print `STEP4_REREVIEW_REQUIRED`. Advisory only — proceeds to **C6**. User is expected to route the patched code through Stage B's Code Alignment Review once before relying on the verdict (declared parameters are guaranteed unchanged by Guard G2, so this is an intent re-review, not a full rebuild). |

### Step C6: Manifest stamping

| | |
|---|---|
| **Action** | `verify_and_run.py` writes `vvuq_out/verified_manifest.json` with: dsl_hash, code_hash, parameter_fingerprint, trace_profile_hash, per-phase verdicts, every checker file hash, timestamp, step4_review_current flag, and the no-claim disclaimer ("provenance-bound and verifier-approved; NOT a claim that the model is true"). |
| **Output** | `verified_manifest.json` |
| **Routes to** | **Stage D** entry (or stop here if Stage D not desired) |
| **Banner** | 🟢 **VERIFIED** prints to screen. |

### STOP banners — routing reference

| Banner | Exit | Cause | User routes to |
|--------|------|-------|----------------|
| 🟢 **VERIFIED** | 0 | Pipeline complete + manifest stamped | Stage D, or accept result |
| 🔴 STOP — check FAILED (repair) | 1 | Phase FAIL, repair prompt written | Apply LLM patch, re-run (loops to G1) |
| 🔴 STOP — repair gave up | 2 | `max_attempts` exhausted | Read `escalation_phase{N}.md`, intervene manually |
| 🔴 STOP — verifier couldn't run | 3 | Cannot load sim, OR harness missing (preflight #1), OR DSL→code coverage failed (preflight #2) | Share message + apply fix per preflight diagnostic |
| 🔴 STOP — ILLEGAL_PATCH | 4 | Declared parameter changed | Revert parameter change; if genuinely needs change, route to **A1** via `DEFER_TO_DSL` |
| 🔴 STOP — SUSPICIOUS_PATCH | 5 | Whole event class appeared/vanished | Revert; failing check is likely a VALIDATOR issue, not a sim defect |
| 🔴 STOP — MODEL_CHANGED | 6 | DSL hash / model identity differs from the baselines' model; proceeding would silently discard the integrity baselines | If the DSL revision was reviewed (Stage A), re-run with `--reset` to explicitly start a new credibility chain; if not intentional, investigate what edited the DSL before trusting anything |

---

## Stage D — Causal Validity (Phase 4.5)

### Step D0: Scenario ingestion *(if using auto-generated scenarios)*

| | |
|---|---|
| **Trigger** | User wants to extend Phase 4.5 beyond declared scenarios |
| **Action** | Run the JSX scenario generator step or `scenGenP` prompt manually. Save the LLM's response (with `===SCENARIO_JSON===` blocks) to a file. Then: `python scenario_ingest.py --from gen_response.txt --dsl my_dsl.json --merge` |
| **Action (inside ingest)** | Parses blocks; normalises (force `provenance: auto_generated` unless explicitly `sme_approved`; force direction-only for auto_generated; reject non-falsifiable "any" direction); routes by `scenario_category` into the correct `compliance_spec` section. |
| **Routes to** | **D1** with the merged DSL |

### Step D1: Run Phase 4.5 harness

| | |
|---|---|
| **Trigger** | After Stage C VERIFIED |
| **Constraint** | Use the **provided** `semantic_scenarios.py`. The harness-provenance guard refuses to start `verify_and_run.py` if it's missing, but Phase 4.5 can be invoked outside that wrapper; the user must enforce the rule manually. |
| **Action** | `run_phase4_5(...)` enumerates declared scenarios from `compliance_spec`, plus canonical scenarios if `include_canonical=True`. For each scenario, runs treatment vs control across paired seeds (default 12), computes a bootstrap CI, gates the per-scenario verdict on whether the CI excludes zero in the expected direction. |
| **Per-scenario verdicts** | `PASS` (CI excludes zero on right side, magnitude met) → counted as positive evidence at provenance tier. **FAIL** (CI strictly on wrong side) → counted as negative evidence. **INCONCLUSIVE** (CI straddles zero) → underpowered or no effect; recommend more seeds. **BLOCK** (harness/sim error) → fatal. **NOT_A_CLAIM** (direction relaxed to "any" or undeclared) → never PASS. |
| **Provenance tier handling** | `declared` / `sme_approved` / `canonical` = authoritative. `auto_generated` = advisory (magnitude stripped, direction-only). |
| **Routes to** | **D2** (aggregate) |

### Step D2: Tier-weighted aggregate

| | |
|---|---|
| **Action** | `aggregate_verdict(results)` rolls per-scenario verdicts up with provenance weighting. |
| **Aggregate values** | `BLOCK` (any authoritative BLOCK) · `FAIL` (any authoritative FAIL) · `WARN` (any authoritative INCONCLUSIVE or any advisory FAIL/BLOCK/INCONCLUSIVE) · `PASS` (at least one authoritative result and all authoritative PASS) · `PASS_ADVISORY_ONLY` (only advisory ran, all PASS — explicit "not a clean credibility pass") · `PASS_NO_SCENARIOS` (nothing ran) |
| **Routes to** | **Stage E** (always) |

---

## Stage E — Judgment

### Step E1: Phase 5 — plausibility review

| | |
|---|---|
| **Trigger** | After Stage D aggregate is computed |
| **Action** | LLM emits typed flags. Each flag has: `category` ∈ {`emergent_behavior`, `realism_gap`, `decision_risk`, `communication_risk`}, `severity` ∈ {`info`, `low`, `medium`, `high`}, `message`, and an **explicit citation** to a phase result, scenario verdict, or trace observation. |
| **Constraint** | Phase 5 is **advisory by construction**. It can emit PASS or WARN, never FAIL. |
| **Routes to** | **E2** (always) |

### Step E2: Three-dimension verdict (`decision_validity.py`)

| | |
|---|---|
| **Action** | Compute three dimensions deterministically. |
| **Credibility** | `worst-of` across Phases 0–4. Possible values: PASS / WARN / FAIL / BLOCK / NOT_EXERCISED. |
| **Causal Validity** | Map Stage D aggregate: `PASS` → PASS, `WARN` → WARN, `FAIL` → FAIL, `BLOCK` → BLOCK, `PASS_ADVISORY_ONLY` → WARN, `PASS_NO_SCENARIOS` → NOT_EXERCISED. |
| **Decision Validity** | Gated rollup. |
| **Decision routing — monotonicity** | Decision can never beat the worst of Credibility and Causal Validity. If either is FAIL → Decision is FAIL. If either is BLOCK → Decision is BLOCK. |
| **Decision routing — authoritative coverage** | If lower layers are PASS but the decision-relevant claim is **not** covered by an authoritative Phase 4.5 PASS (declared or SME-approved) → Decision is capped at WARN. |
| **Decision routing — Phase 5 high-severity advisory** | If lower layers are PASS and authoritative coverage exists but any Phase 5 flag with `severity: high` and `category` ∈ {`decision_risk`, `realism_gap`} is open → Decision is capped at WARN. |
| **Decision routing — clean PASS** | Only when all of the above pass (lower layers clean, authoritative coverage, no high-severity advisory flag). |
| **Output** | A three-row verdict with reasons per row. **The dimensions are never collapsed into one number.** |
| **Routes to** | **E3** (final synthesis) |

### Step E3: Final synthesis

| | |
|---|---|
| **Action** | LLM produces a Stage E synthesis report. The synthesis prompt **requires** the LLM to invoke `decision_validity.py` (not produce the rollup by prose) and to report the three dimensions on separate lines with the reasons. |
| **Output** | Three-dimension verdict + per-dimension reasons + "what would move Decision Validity to PASS" recommendation. |
| **Routes to** | End of pipeline. |

---

## Cross-cutting controls — quick-reference

### Integrity guards (Stage C, every invocation)

| Guard | Fires when | Routes to |
|-------|------------|-----------|
| Harness provenance | Required checker/harness file missing | STOP (exit 3) |
| DSL→code coverage | MANIFEST coverage incomplete or unjustified | STOP (exit 3) |
| Model identity | DSL hash differs from baseline | STOP (exit 6, MODEL_CHANGED) — explicit `--reset` required |
| Parameter fingerprint | Declared parameter value changed | ILLEGAL_PATCH (exit 4) |
| Trace fidelity | Whole event class appeared/vanished | SUSPICIOUS_PATCH (exit 5) |
| Progress-aware budget | Attempt budget bookkeeping | Burn or skip attempt counter |

### Scenario-side integrity (Stage D)

| Rule | Effect |
|------|--------|
| Direction "any" / undeclared | Scored `NOT_A_CLAIM` — never PASS |
| Generator reads sim code | Forbidden — circular oracle |
| `auto_generated` magnitude | Stripped — direction-only |
| Authoritative-only-can-bear-magnitude | `sme_approved` is what licenses a magnitude claim |

### Self-heal classification routing

| Class | LLM action | User next action |
|-------|------------|------------------|
| `SIM_CODE` | Patch `my_sim.py` | Re-run `verify_and_run.py` (routes to G1) |
| `DSL_SPEC` | Emit `DEFER_TO_DSL: <check_id>` and stop | Revise at **A1**; auto-rebaseline on next run |
| `VALIDATOR` | Emit `VALIDATOR_FIX: <check_id>` and stop | Escalate to framework maintainer |

---

## Complete checks catalog

Every check the framework runs, grouped by phase. Each row gives the check ID, what it tests, the highest severity it can return, and where to look for the cause on FAIL.

### Pre-pipeline gates (run before any phase)

| Check | Module | Tests | Max severity | On FAIL |
|---|---|---|---|---|
| Description→DSL coverage (KPI rows in description bound to `aggregate_target` entries in DSL) | `description_dsl_coverage.py` | KPI tables in the docx description have a matching `aggregate_target` in the DSL with consistent value/CI; **plus** dynamics-mechanism keyword scan (catches descriptions using continuous-hazard language when the DSL declares only discrete checks) | STOP | Routes to **A1** to add or reconcile DSL entries, or to declare `dynamics.type='continuous_hazard'` with a rate on the affected transition element |
| Harness provenance (canonical checker/harness files present at expected hashes) | `verify_and_run.py` preflight | The `validation.py`, `phase0_vvuq.py`, `semantic_scenarios.py`, `run_validators.py`, `self_heal_orchestrator.py` files are present and have not been substituted with self-authored versions | STOP (exit 3) | Restore the canonical files; never let an LLM author its own grader |
| DSL→code coverage (every declared DSL element enumerated in `MANIFEST`) | `dsl_code_coverage.py` | Every DSL element id is present in `MANIFEST["declared_element_implementation_status"]` with a valid status (FULL / PARTIAL / NOT_IMPLEMENTED / DEFERRED_TO_DSL) and required justification | STOP (exit 3) | Routes to **B3** to add/justify the manifest entries |

### Integrity guards (run on every orchestrator invocation, before any phase)

| Guard | Tests | Max severity | On FAIL |
|---|---|---|---|
| G1 — Model identity | SHA-256 of `my_dsl.json` matches stored baseline | STOP (exit 6, MODEL_CHANGED) | Requires explicit `--reset` to start a new credibility chain |
| G2 — Parameter fingerprint | Fingerprint of declared parameters from `DEFAULT_CONFIG` matches baseline | STOP (exit 4, ILLEGAL_PATCH) | Revert the parameter change; if intended, route to **A1** |
| G3 — Trace fidelity | Set of event types observed under `DEFAULT_CONFIG` matches baseline | STOP (exit 5, SUSPICIOUS_PATCH) | Revert (failing check is likely a VALIDATOR issue, not a sim defect) |
| G4 — Progress-aware budget | Self-heal attempt budget honoured | None (bookkeeping only) | Burns one attempt only if no progress was made |

### Phase 0 — Layer A: structural gate

Universal preflight on the trace; failures BLOCK phase 0 because no downstream check is meaningful without these.

| Check | Tests | Max severity |
|---|---|---|
| A1 — `config_schema` | Required config sections present | BLOCK |
| A2 — `result_structure` | `run_simulation()` returns `trace`, `metrics`, `config` | BLOCK |
| A3 — `trace_completeness` | Every event has `time`, `event`, `entity_id`; optional fields surfaced as INFO | BLOCK |
| A4 — `chronological_order` | `time` non-decreasing; `seq` (if present) strictly increasing | BLOCK |
| A5 — `canonical_events` | No unknown event names | WARN |
| A6 — `entity_lifecycle` | Exactly one `system_arrival` per `entity_id` | FAIL |
| A7 — `determinism` | Same seed → same trace prefix (50-event compare) | FAIL |
| A8 — `clean_shutdown` | No `service_end` without preceding `service_start` for the same `(resource, entity)` | FAIL |

### Phase 0 — Layer B: declarative contract gate (B01–B17)

Per-DSL-declaration checks; one entry per declared element under `compliance_spec`.

| Check | Tests | Max severity |
|---|---|---|
| B01 — `arrivals` | Arrival rate per day matches each declared `expected_rate_per_day` | FAIL |
| B02 — `recurring_obligations` | Time-driven recurring activities per entity-hour at declared cadence | FAIL |
| B03 — `service_rates` | Observed mean service time at each resource matches declared rate (revised formula: per-station mean, not pooled) | FAIL |
| B04 — `periodic_processes` | Scheduled process count per effective day matches declared cadence | FAIL |
| B05 — `scheduled_windows` | Declared events occur inside their `time_of_day` windows | FAIL |
| B06 — `handoff_sequences` | Role ordering and occurrence limits within declared cycles | FAIL |
| B07 — `coordination_patterns` | hard_pool / soft_pool / role_anchored coordination semantics enforced | FAIL |
| B08 — `sequencing` | Before/after precedence rules per entity | FAIL |
| B09 — `temporal` | Min/max sojourn before declared terminal events | FAIL |
| B10 — `preemption_rules` | Preempt events paired with resume or terminal exit; count vs declared | FAIL |
| B11 — `state_transitions` | Forbidden events/processes in declared states absent | FAIL |
| B12 — `entity_type_constraints` | Process/entity-type typing constraints | FAIL |
| B13 — `terminal_outcomes` | Each entity ends in exactly one terminal event from the declared union; per-section existence + per-section count |  FAIL |
| B14 — `losses` | Required loss types present; forbidden loss types absent | FAIL |
| B15 — `flow_accounting` | Arrivals = departures + losses + in_system_at_end (rooted from trace, not from sim-reported metrics) | FAIL |
| B16 — `aggregate_targets` | Key metrics within declared expected ranges | FAIL |
| B17 — `scenario_guidance` | Advisory placeholder for declared scenarios (always INFO; real scenarios run at Phase 4.5) | INFO |

### Phase 0 — Layer B Extended: process contracts (B18–B23, `count_unit`-aware)

Per-process contract checks. The `count_unit` field (per-episode vs per-role-start) tells the checker whether to count team episodes or individual role starts; this resolves the multi-role over-count false positives that motivated the granularity fix.

| Check | Tests | Max severity |
|---|---|---|
| B18 — `process_frequency` | Process occurrence count matches `every_hours` declaration, honouring `count_unit` (episode vs role-start) | FAIL |
| B19 — `role_structure` | Roles-per-episode within declared min/max, including shift-opening anchored roles | FAIL |
| B20 — `interruptibility` | `interruptible`/`preempts` declarations honoured; if a process declares `preempts`, observable preempt evidence required | FAIL |
| B21 — `rooted_flow_conservation` | Arrivals = exits across the full simulation; windowed imbalance reported separately as INFO | BLOCK |
| B22 — `count_unit_consistency` | Declared `count_unit` consistent with how the trace emits the process | FAIL |
| B23 — `schedule_alignment` | Per-process activity aligned with declared schedule (calendar position + per-window cadence + jitter) | FAIL |

### Phase 0 — Layer B Extended: semantic contracts (B24–B41)

Higher-level semantic contracts. Each is per-DSL-declaration; missing declarations produce INFO.

| Check | Tests | Max severity |
|---|---|---|
| B24 — `eligibility_rules` | No `service_start` for an ineligible entity | FAIL |
| B25 — `coverage_rules` | Every eligible entity gets covered at declared cadence | FAIL |
| B26 — `routing_rules` | Branch probabilities from `routing_from` match declaration | FAIL |
| B27 — `assignment_continuity` | Same `server_id` persists across declared continuity scope | FAIL |
| B28 — `escalation_rules` | When wait/sojourn > `breach_threshold_seconds`, the declared `escalation_outcome` is emitted | FAIL |
| B29 — `duration_scope` | `duration_scope` (per_episode vs per_attempt) + setup/teardown semantics honoured | FAIL |
| B30 — `consumption_mode` | `consumption_mode` (exclusive / shared / max_concurrent) semantics enforced | FAIL |
| B31 — `service_rates_extended` | Discipline (FIFO/priority), balk/renege counts within declared bounds | FAIL |
| B32 — `batched_start` | Entities in same batch share `service_start` time | FAIL |
| B33 — `handoff_pair` | `max_gap_s` honoured between `from_resource.service_end` and `to_resource.service_start` per entity | FAIL |
| B34 — `breaks_and_holidays` | No declared activity during breaks / holidays / maintenance windows | FAIL |
| B35 — `loss_conditions` | Losses occur only when declared `condition_expr` / `threshold_seconds` satisfied | FAIL |
| B36 — `dwell_distribution` | Mean dwell time in each declared state matches `dwell_distribution.mean_seconds` | FAIL |
| B37 — `workload_splits` | Workload split (per process or resource) falls within declared bands | FAIL |
| B38 — `warmup_handling` | `aggregate_targets` declaring `warmup_handling='exclude_warmup'` computed on the post-warmup sample | FAIL |
| B39 — `metric_decomposition` | Targets with numerator/denominator/basis decomposition match the named items in `ctx.metrics` | FAIL |
| B40 — `identity_unit_consistency` | Entity identity unit consistent across sections that declare it | FAIL |
| B41 — `soft_pool_composition` | Soft-pool composition matches declared `pool_policy` | FAIL |

### Phase 1 — Mathematical verification (`validate_phase1`)

#### §1.1 — System-level invariants

| Check | Tests | Max severity |
|---|---|---|
| 1.1.1 — `trace_integrity` | Required fields + monotone `time` + strict `seq` | FAIL |
| 1.1.2 — `set_based_conservation` | A = D + L_terminal + I(T) | FAIL |
| 1.1.3 — `entity_lifecycle` | Exactly one arrival per entity; ordering | FAIL |
| 1.1.4 — `little_law_system` | L = λ × W within tolerance | FAIL |

#### §1.2 — Per-queue invariants (one set of rows per discovered queue)

| Check | Tests | Max severity |
|---|---|---|
| 1.2.{q}.conservation | Queue enters / exits balance; no duplicates or orphans | FAIL |
| 1.2.{q}.fifo | Exit order matches enter order | FAIL |
| 1.2.{q}.little | Lq = λq × W̄q within tolerance | FAIL |

#### §1.3 — Per-resource invariants (one set of rows per discovered resource)

| Check | Tests | Max severity |
|---|---|---|
| 1.3.{r}.state_machine | `service_start`/`service_end` paired by entity | FAIL |
| 1.3.{r}.capacity | Peak concurrency ≤ declared capacity | FAIL |
| 1.3.{r}.util_identity | ρ ≈ λ × E[S] / c | FAIL |
| 1.3.{r}.throughput_bound | λ ≤ c / E[S] | FAIL |
| 1.3.{r}.service_pairing | No orphan `service_end` | FAIL |

#### §1.4 — Trace-metric consistency

Closes the calibration-overfitting hole where the trace and sim-reported metrics agree numerically but disagree in operational reality.

| Check | Tests | Max severity |
|---|---|---|
| 1.4.1 — `recurring_cadence` | Declared `every_hours` for each recurring activity matches the empirical cadence observed in the trace; flags free-parameter calibration drift | FAIL |
| 1.4.2 — `scheduled_windows` | Declared per-window event count matches the trace per-window count | FAIL |
| 1.4.3 — `busy_consistency` | Sim-reported busy time consistent with the trace's open `service_start`/`service_end` intervals | FAIL |

#### §1.5 — State-transition emission contract

Closes the asymmetric-emission failure mode where deterioration arcs fire resource events while recovery arcs emit silent state-changes.

| Check | Tests | Max severity |
|---|---|---|
| 1.5 — `state_transition_emission` | For each `competing_risks` arc declaring an `emission_contract`, observed emission shape (silent_state_change / resource_event / both) matches the declaration | FAIL |

#### §1.6 — Trace-temporal-distribution (dynamics-mechanism verification)

| Check | Tests | Max severity |
|---|---|---|
| 1.6.{eid}_temporal_distribution | For every transition-bearing DSL element (state_restriction, terminal_outcome, recurring_activity) that declares a `dynamics` block with `type ∈ {continuous_hazard, discrete_check, event_driven}`, the empirical inter-transition-time distribution in the trace matches the declared mechanism. For `continuous_hazard`: KS-test of inter-event times against Exp(rate), plus an hour-boundary cluster check (high clustering signals a discrete check disguised as continuous). For `discrete_check`: ≥ 50% of transitions cluster at multiples of the declared cadence. For `event_driven`: ≥ 85% of transitions co-occur with the declared trigger event. The check catches the substitution where the DSL declares continuous hazards but the code implements per-hour Bernoulli checks (or vice versa) — two mechanisms with the same average behaviour but operationally distinct trace structures. | FAIL |

### Phase 2 — Validation (`validate_phase2`)

| Check | Tests | Max severity |
|---|---|---|
| 2.1 — `mmc_applicability` | Auto-gate: INFO if model is not M/M/c; else proceeds with per-resource Erlang-C checks | INFO/FAIL |
| 2.1.{r} (gated) | Erlang-C consistency on each resource that passes the M/M/c gate | FAIL |
| 2.2.1 — `limiting_zero_load` | As λ → 0: W → E[S], Wq → 0 | FAIL |
| 2.2.2 — `limiting_heavy_load` | At ρ ≈ 0.95: Wq ≥ 5·E[S] | FAIL |
| 2.3.1 — `extreme_zero_capacity` | At cap = 0: either losses or queue blow-up with no service starts | FAIL |
| 2.3.2 — `extreme_infinite_capacity` | At cap = ∞: Wq → 0 | FAIL |

### Phase 3 — Uncertainty quantification (`validate_phase3`)

#### §3.1 — Warm-up

| Check | Tests | Max severity |
|---|---|---|
| 3.1.1 — `warmup_config_support` | Config exposes `warmup_time` | WARN |
| 3.1.2 — `welch_method` | Welch's procedure across N replications locates cumulative-mean stabilisation | FAIL |
| 3.1.3 — `mser5_method` | MSER-5 truncation point on the mean trace | FAIL |

#### §3.2 — Replication independence + CI construction

| Check | Tests | Max severity |
|---|---|---|
| 3.2.1 — `independent_seeds` | Different seeds → different per-entity sojourn statistics | FAIL |
| 3.2.2 — `ci_construction` | Multi-rep CI half-width / mean ratio within reasonable bounds | FAIL |

#### §3.3 — Determinism + common random numbers (CRN)

| Check | Tests | Max severity |
|---|---|---|
| 3.3.1 — `same_seed_determinism` | Same seed → identical per-entity sojourn map | FAIL |
| 3.3.2 — `paired_seed_crn_arrivals` | Paired runs share an arrival prefix | FAIL |
| 3.3.3 — `crn_variance_reduction` | Paired-seed variance < unpaired-seed variance | FAIL |

#### §3.4 — Parametric sensitivity

| Check | Tests | Max severity |
|---|---|---|
| 3.4.1 — `perturbable_parameters` | Canonical config keys exposed for perturbation | WARN |
| 3.4.2 — `sensitivity_arrival_rate` | +20% arrival rate produces measurable W increase | FAIL |
| 3.4.3 — `sensitivity_secondary_parameter` | Secondary parameter (resource capacity, service rate) sensitivity present | FAIL |

### Phase 4 — Face validity + sensitivity sweeps (`validate_phase4`)

#### §4.1 — One-at-a-time (OAT) sweeps

| Check | Tests | Max severity |
|---|---|---|
| 4.1 — `oat_arrival` | Sweep arrival rate; observed W is monotone non-decreasing | WARN |
| 4.1.{r}.oat_capacity | Sweep resource capacity by ±1; observed W is monotone non-increasing | WARN |

#### §4.2 — Preemption semantics (fires only if preemption declared or preempts observed)

| Check | Tests | Max severity |
|---|---|---|
| 4.2 — `preemption_block` | INFO-skip if no preemption declared and no preempt events | INFO |
| 4.2.1 — `preemption_fires` | At least one `preempt` + `resume` pair observed if preemption was declared | WARN |
| 4.2.2 — `preemption_priority_only` | Each takeover is by a strictly higher-priority claimant than the victim | FAIL |
| 4.2.3 — `preemption_barrier` | For every `barrier_aware` preempt, served fraction at the preempt instant ≥ declared `barrier_fraction`; flags `INERT_PARAMETER` when the value is read but not gating; **augmented with residual fingerprinting and architectural-implication tags** (`STRUCTURAL CEILING REACHED` when residual is dominated by same-tick races, with named compatible/incompatible mechanism patterns — see `residual_diagnostics.py`) | FAIL |
| 4.2.4 — `preemption_resume_pairing` | Every `resume` pairs with a prior `preempt` on `(entity_id, segment_id)` | FAIL |
| 4.2.5 — `non_preemptible` | Resources declared non-preemptible have zero `preempt` events | FAIL |
| 4.2.6.{rid} — `rule_evidence` | Each declared `preemption_rule` produces positive evidence (rule fires), negative evidence (rule violated), or `STRUCTURAL_IMPOSSIBILITY` flag (priority gate excludes the declared pair) | FAIL |
| 4.2.6 — `rule_evidence_summary` | Roll-up across all declared `preemption_rules` | FAIL |

#### §4.3–§4.4 — Face validity + effect size

| Check | Tests | Max severity |
|---|---|---|
| 4.3 — `face_validity` | Utilisation ∈ [0,1]; departures ≤ arrivals; times ≥ 0 | FAIL |
| 4.4 — `effect_size` | INFO summary of OAT response sizes | INFO |

### Phase 4.5 — What-if scenario harness (`semantic_scenarios.py`)

User-declared scenarios from `compliance_spec` plus seven canonical scenarios when `include_canonical=True`. Each scenario runs treatment vs control across paired seeds (default 12), computes a bootstrap CI, and grades on whether the CI excludes zero in the expected direction.

#### Canonical scenarios (auto-synthesized from declared DSL elements)

| Scenario | Tests |
|---|---|
| `canonical_preemption_causality` | If a process preempts another, turning preemption off should decrease `preemption_count` (and typically relax victim SLA) |
| `canonical_soft_pool_scaling` | Ramp arrivals 1.5×; soft-capacity pool utilisation rises; `loss_rate` rises |
| `canonical_eligibility_{rule}` | Disabling the eligibility gate raises `eligibility_violations` |
| `canonical_duration_{proc}` | Stretch service time 1.5× (paired CRN); `mean_duration.{proc}` rises by ≥ 30% |
| `canonical_transition_{name}` | Disabling a declared state transition makes its counter collapse |
| `canonical_queue_discipline_{q}` | Priority discipline lowers prioritised-class `wait_time_p95` vs FIFO (paired CRN) |
| `canonical_coverage_{etype}` | At 2× workload, `coverage.{etype}` degrades by ≥ 5% |

#### Per-scenario verdicts

| Verdict | Meaning |
|---|---|
| PASS | CI excludes zero on the expected side; magnitude (if asserted) met. Counted as positive evidence at the scenario's provenance tier. |
| FAIL | CI strictly on the wrong side. Counted as negative evidence. |
| INCONCLUSIVE | CI straddles zero; underpowered or no effect. Recommend more seeds. |
| BLOCK | Harness or sim error. Fatal for this scenario. |
| NOT_A_CLAIM | Direction relaxed to "any" or undeclared. Never PASS — guard against oracle relaxation. |

#### Aggregate verdicts (provenance-weighted roll-up)

| Aggregate | Meaning |
|---|---|
| BLOCK | Any authoritative BLOCK |
| FAIL | Any authoritative FAIL |
| WARN | Any authoritative INCONCLUSIVE, or any advisory FAIL/BLOCK/INCONCLUSIVE |
| PASS | At least one authoritative result, all authoritative PASS |
| PASS_ADVISORY_ONLY | Only advisory-tier scenarios ran, all PASS — explicit "not a clean authoritative pass" |
| PASS_NO_SCENARIOS | Nothing ran — explicit "no decision-relevant claim has been tested" |

### Phase 5 — Plausibility review (LLM advisory)

Phase 5 is advisory by construction: it can emit PASS or WARN, never FAIL. Each typed flag has a `category`, `severity`, `message`, and an explicit citation to a phase result, scenario verdict, or trace observation.

| Flag category | Tests |
|---|---|
| `emergent_behavior` | Whole-system patterns that match or contradict domain expectation |
| `realism_gap` | Specific declared mechanisms whose realism is questionable for the intended use |
| `decision_risk` | Risks specific to the decision the user wants the model to support |
| `communication_risk` | Risks of misinterpretation in how the verdict is reported to stakeholders |

| Severity | Effect on Decision Validity (Stage E) |
|---|---|
| `info` | None |
| `low` | None directly; surfaced in synthesis |
| `medium` | None directly; surfaced in synthesis |
| `high` | If `category ∈ {decision_risk, realism_gap}` and the flag is open, Decision Validity is capped at WARN |

### Stage E — Three-dimension verdict (`decision_validity.py`)

Not "checks" in the same sense — the verdict layer aggregates the above into a non-collapsing three-dimensional report.

| Dimension | Computed from |
|---|---|
| Credibility | `worst-of` across Phases 0–4. Possible: PASS / WARN / FAIL / BLOCK / NOT_EXERCISED |
| Causal Validity | Phase 4.5 aggregate mapped: PASS → PASS, WARN → WARN, FAIL → FAIL, BLOCK → BLOCK, PASS_ADVISORY_ONLY → WARN, PASS_NO_SCENARIOS → NOT_EXERCISED |
| Decision Validity | Gated rollup; requires authoritative-tier Phase 4.5 coverage of the decision-relevant claim AND no open high-severity advisory flag; can never beat the worst of the lower two |

---

## File map — what each file does at which step

| File | Step(s) | Role |
|------|---------|------|
| `description_dsl_coverage.py` | A4 | Description→DSL coverage gate |
| `compliance_mapper.py` | A (under the hood) | DSL→compliance_spec mapping |
| `dsl_schema.py` | A | DSL element schema definitions |
| `CODEGEN_BRIEF.md` | B1 | Codegen contract |
| `dsl_code_coverage.py` | B3, C1 | DSL→code coverage check |
| `verify_and_run.py` | C0–C6 | Trusted execution wrapper |
| `self_heal_orchestrator.py` | C2–C5 | Self-heal loop + integrity guards |
| `validation.py` | C3 (Phases 1–4) | Phase 1–4 validators incl. §1.4, §1.5, §4.2.3, §4.2.6 |
| `residual_diagnostics.py` | §4.2.3 (within Phase 4) | Residual-violation fingerprinting + architectural-implication tagging. Produces `STRUCTURAL CEILING REACHED` signals and named compatible/incompatible patterns when residual violations cluster on a structural fingerprint (e.g. same-tick preempt races). Short-circuits parametric-tuning iteration on mechanism-bound failure modes. |
| `phase0_vvuq.py` | C3 (Phase 0) | Phase 0 contract gate |
| `process_contracts.py` | C3 (Phase 0 — B18–B23) | Process contract checks (count_unit-aware) |
| `semantic_checks.py` | C3 (Phase 0 — B24–B41) | Semantic compliance checks |
| `coverage_validator.py` | C3 | DSL element coverage validator |
| `vvuq_utils.py` | C3 | Shared helpers |
| `run_validators.py` | C3 (CLI) | Phase 1–4 CLI |
| `run_vvuq.py` | C3 (CLI) | Phase 0 runner |
| `semantic_scenarios.py` | D1, D2 | Phase 4.5 harness |
| `scenario_ingest.py` | D0 | LLM scenarios → DSL connector |
| `decision_validity.py` | E2 | Three-dimension verdict |
| `simulation_trust_framework_v5_3.jsx` | A1, A2, B1, B2, D0, E1, E3 | JSX prompt orchestrator (interactive mode) |
| `RUN_GUIDE.md` | All | End-user guide |
| `FRAMEWORK_ROUTING.md` (this file) | All | Routing reference |

---

## How to extend this document

When you add a new defence, check, or routing rule:

1. Append a row to the **Changelog** with the date and a one-line summary.
2. Add or update a step row in the relevant stage section. State the inputs,
   action, and the **routes-to** rules explicitly for each branch.
3. If the change introduces a new STOP banner or exit code, add a row to the
   **STOP banners — routing reference** table.
4. If it's an integrity guard, add a row to the **Integrity guards** table.
5. If it changes which file does what, update the **File map**.
6. If the routing affects the high-level diagram at the top, update the ASCII
   art to reflect the new edge.

The goal is that anyone — including the user, a new collaborator, or future
Claude reading this cold — can trace any input to any output by reading the
routing tables in order, without having to infer behaviour from code.
