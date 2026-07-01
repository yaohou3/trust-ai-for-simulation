# Framework Coverage of the Simulation V&V Error Taxonomy

A mapping of every error class in the Sargent-derived taxonomy (plus the LLM-era extensions) to the specific framework mechanisms that address it. Each row gives the framework's coverage level, the specific checks/guards/stages involved, and notes on residual gaps.

**Coverage levels used throughout:**

- **Strong** — deterministic, trace-bound, or fingerprint-bound check catches this class directly.
- **Partial** — framework catches the symptom but cannot establish the root cause without user input.
- **Visibility-only** — framework makes the error visible to the user but cannot resolve it; user is responsible for closing the gap.
- **Out-of-scope** — framework architecturally cannot address this class; it must be handled by the user before or after framework execution.

---

## 1. Specification Errors

*"Did we capture the right problem and the right system logic?"*

| Error class | Coverage | Framework mechanism | Notes / residual gap |
|---|---|---|---|
| **Problem formulation error** (wrong problem / decision context) | Visibility-only | Stage E Decision Validity caps the verdict at WARN when the decision-relevant claim is not covered by an authoritative Phase 4.5 scenario; Phase 5 `decision_risk` and `communication_risk` advisory flags surface mismatches between the model's scope and the user's stated decision | The framework cannot verify that the user has identified the right problem; it can only refuse to credential the model for a decision the user has not declared and has not exercised counterfactual evidence for |
| **Requirement extraction error** (description says X, formal spec captures ¬X) | Strong | A2 DSL Alignment Review (independent LLM reviewer assesses grounding in description); A4 description→DSL coverage gate (`description_dsl_coverage.py` matches description KPI rows to DSL `aggregate_target` entries with units/CI reconciliation); A3 `emission_contract` declaration requirement on competing-risks arcs | The A4 gate specifically caught Reference-Table-6 promotions in the ICU case: 8 of 11 KPIs were absent from the DSL until the gate forced their promotion |
| **Assumption error** (invalid simplifying assumption) | Partial | Typed DSL forcing function makes assumptions explicit; `assumed_default: true` placeholder mechanism marks values the description did not pin; Phase 5 `realism_gap` advisory flags surface domain-implausible assumptions | The framework makes assumptions visible but cannot verify domain plausibility without reference data; reliance on SME-vetted DSL placeholders is the residual control |
| **Conceptual model error** (incorrect representation of system logic) | Partial | A2 DSL Alignment Review; A4 description→DSL coverage; Phase 5 `realism_gap` flag; Stage E Decision Validity caps verdict when conceptual coverage is insufficient | Cannot verify against the real system in the absence of external reference data; the framework's role is to surface the gap, not to close it |

**Out-of-scope cases:** the user identifying the wrong decision question; the description being factually incorrect about the real system (without external reference data); the SME's domain judgement about plausibility.

---

## 2. Input Errors

*"Are the data, parameters, and calibration correct?"*

| Error class | Coverage | Framework mechanism | Notes / residual gap |
|---|---|---|---|
| **Data error** (incorrect / biased / incomplete inputs) | Visibility-only when reference data exists; out-of-scope when it does not | A4 description→DSL coverage gate cross-checks KPI rows against DSL `aggregate_target` values and CIs; Phase 0 B16 verifies aggregate targets against the trace | Cannot verify the description's data against the world; if the user supplies wrong reference values, the framework treats them as ground truth |
| **Parameter error** (wrong parameter values entered into the model) | Strong | G2 parameter-fingerprint guard refuses to run when declared parameters change between approval and execution; Phase 0 B01 (arrival rates), B02 (recurring cadences), B03 (service rates per station) verify trace behaviour against declared rates; B16 verifies aggregate targets within declared ranges; §1.4.1 `recurring_cadence` flags free-parameter drift | The classical "5 vs 50 minute service time" case is caught at B03 if the trace's mean is checked against the declaration; G2 prevents the LLM from silently editing the declared value |
| **Calibration error** (overfitting parameters to validation aggregates) | Strong | §4.2.3 `preemption_barrier` flags `INERT_PARAMETER` when a declared value is read but not gating execution; §1.4.1 `recurring_cadence` catches free-parameter drift (the ICU discharge-cadence-tuned-to-LOS case fired here); G2 parameter-fingerprint guard blocks tuned-value substitution; provenance tiering blocks auto-generated values from conferring authoritative trust | This is the simulation-V&V analogue of the LLM "calibrated to hit the reference" phrase pattern; the framework's defence is structural, not prose-based |

---

## 3. Implementation Errors

*"Does the code implement the conceptual model correctly?"*

| Error class | Coverage | Framework mechanism | Notes / residual gap |
|---|---|---|---|
| **Logic error** (correct concept, wrong implementation logic) | Strong | Phase 0 Layer B (B01–B41) declarative contracts each test one declared logical claim against the trace; B07 coordination patterns (hard_pool / soft_pool / role_anchored); B08 sequencing; B10 preemption pairing; B11 state transitions; B26 routing probabilities; B27 assignment continuity; B31 queue discipline (FIFO/priority); B41 soft-pool composition; §4.2.1–§4.2.6 preemption semantics | The classical "priority queue implemented as FIFO" case is caught at B31; preemption-substitution cases are caught at the §4.2 cluster |
| **Algorithm error** (wrong numerical or simulation algorithm) | Strong | Phase 1 §1.1.1–§1.1.4 system invariants (trace integrity, conservation, Little's Law); §1.2.{q} per-queue invariants (conservation, FIFO, Little's Law); §1.3.{r} per-resource invariants (state machine, capacity, utilization identity, throughput bound, service pairing); §2.1 M/M/c gate with Erlang-C consistency | Algorithm errors typically manifest as conservation or invariant violations, which the Phase 1 invariant checks surface deterministically |
| **Syntax error** (program does not compile/parse) | Strong | Pre-pipeline harness-provenance guard refuses if checker files are missing; if `my_sim.py` does not parse, `_safe_run` emits a BLOCK record at A2 `result_structure` | Caught at the first attempt to import the sim module; the framework reports a clear load-failure with the underlying exception type |
| **Runtime error** (execution failure: division by zero, null reference) | Strong | `_safe_run` wrapper catches exceptions at every sim invocation across Phases 0–4 and Phase 4.5, emits a BLOCK record with the exception type and label, and routes to the self-heal loop with a structured repair prompt | The framework treats runtime errors as first-class failures (BLOCK) rather than allowing the sim to crash silently or partially succeed |
| **Numerical error** (floating-point / discretization / rounding) | Strong | §4.3 `face_validity` checks utilization ∈ [0,1], departures ≤ arrivals, times ≥ 0; B15 flow accounting with tolerance; B21 rooted-flow conservation; Phase 1 §1.1.4 Little's Law within tolerance; §1.4.3 busy-time consistency between trace intervals and sim-reported metrics | The face-validity check catches the gross numerical-error class (negative times, impossible utilizations); finer-grained discretization errors require user-declared tolerance |

---

## 4. Experimental Errors

*"Was the experimental design sound?"*

| Error class | Coverage | Framework mechanism | Notes / residual gap |
|---|---|---|---|
| **Initialization error** (starting conditions wrong, no warm-start) | Strong | §3.1.1 `warmup_config_support` checks that `warmup_time` is exposed; §3.1.2 Welch's method locates cumulative-mean stabilisation; §3.1.3 MSER-5 truncation; B38 `warmup_handling` verifies that aggregate targets declaring `exclude_warmup` were computed on the post-warmup sample | The classical "all queues empty when warm-start required" case is caught by the Welch/MSER stabilisation check |
| **Random number error** (poor RNG, shared streams destroy CRN) | Strong | §3.3.1 `same_seed_determinism` (same seed → identical sojourn map); §3.3.2 `paired_seed_crn_arrivals` (paired runs share an arrival prefix); §3.3.3 `crn_variance_reduction` (paired-seed variance < unpaired-seed) | The classical "shared stream destroys CRN design" case is caught at §3.3.3 directly |
| **Warmup error** (warmup duration too short / not applied) | Strong | §3.1.2 Welch's method; §3.1.3 MSER-5; B38 `warmup_handling` | Welch and MSER-5 each independently locate the truncation point; if either disagrees with the declared `warmup_time`, the user is told to revise |
| **Replication design error** (one replication used for stochastic model; CI too wide) | Strong | §3.2.1 `independent_seeds` (different seeds produce different statistics); §3.2.2 `ci_construction` (CI half-width / mean ratio within bounds); Phase 4.5 defaults to 12 paired seeds with bootstrap CI; INCONCLUSIVE verdict explicitly flags "low power: N seeds < recommended N₀" | The classical "one replication used for stochastic model" case is caught at §3.2.1; the Phase 4.5 INCONCLUSIVE-with-low-power message is the user-facing recommendation to increase seeds |

---

## 5. Credibility Errors

*"Does the credibility assessment itself reach the right conclusion?"*

| Error class | Coverage | Framework mechanism | Notes / residual gap |
|---|---|---|---|
| **Verification failure** (model concept correct but implementation wrong) | Strong | Phase 0 (Layer A structural + Layer B declarative) + Phase 1 (mathematical invariants) constitute the verification core; the DSL→code coverage manifest forces every declared element to be cited in code; §4.2.3 residual diagnostics distinguish structural-model vs parameter-calibration error during iterative repair | Verification is the framework's strongest layer; the most consequential extension is §4.2.3 residual fingerprinting, which converts the count-of-violations metric (which conflates structural and parametric error) into a categorical diagnosis |
| **Validation failure** (correct code, wrong representation of reality) | Partial | Phase 4.5 what-if scenarios test responsiveness to declared interventions; §4.3 face validity; Phase 5 `realism_gap` advisory flags; Stage E Causal Validity reports `PASS_NO_SCENARIOS` / `NOT_EXERCISED` distinctly from `PASS` | Operational validation against the real system requires reference data; the framework's role is to expose the gap (Phase 4.5 cap, Phase 5 advisory) rather than close it |
| **Interpretation failure** (drawing incorrect conclusions; correlation as causation) | Strong | Stage E three-dimension verdict refuses to collapse credibility, causal validity, and decision validity into a single scalar; Phase 5 `communication_risk` flags surface specific interpretation traps; the "what would move Decision Validity to PASS" recommendation is generated for every non-PASS verdict | The non-collapsing verdict is structurally important: a user reading "PASS / WARN / NOT_EXERCISED" cannot infer "the model is correct for my use" the way they might infer it from a single-scalar PASS |

---

## 6. LLM-Era Error Classes

*Errors that arise specifically from AI-mediated model construction. These are largely absent from classical V&V because they require an LLM in the loop.*

| Error class | Coverage | Framework mechanism | Notes / residual gap |
|---|---|---|---|
| **Requirement extraction error** (description says "preempt at 50%" but DSL omits the barrier) | Strong | A2 DSL Alignment Review; A3 explicit declaration of `emission_contract` on competing-risks arcs; A4 description→DSL coverage gate | Empirically caught the ICU case where `barrier_fraction` was implicit in the description and would otherwise have been silently absent from the DSL |
| **Semantic fidelity error** (LLM substitutes non-preemptive logic while rationalizing it) | Strong | DSL→code coverage manifest (`dsl_code_coverage.py`) requires every DSL element to be enumerated in `MANIFEST["declared_element_implementation_status"]` with status FULL/PARTIAL/NOT_IMPLEMENTED/DEFERRED_TO_DSL plus code citation or justification; §4.2.6 `preemption_rule_evidence` flags STRUCTURAL_IMPOSSIBILITY when a declared rule cannot fire under the code's gate; §4.2.3 INERT_PARAMETER flags declared-but-not-gating values | The DSL→code coverage manifest is the framework's primary structural defence against semantic-fidelity loss with documented LLM rationalization; the manifest cannot be persuaded by prose |
| **Specification drift** (generated model gradually deviates from original requirements) | Strong | G1 model-identity guard hashes the DSL on every invocation and auto-rebaselines if the DSL changes (signalling intentional drift); G2 parameter-fingerprint guard refuses to run if declared parameter values change between approval and execution; the typed DSL is the immutable specification — the code cannot drift independently | The combination of G1 (DSL hash) and G2 (parameter fingerprint) ensures that drift either becomes explicit (re-baselined) or is blocked (ILLEGAL_PATCH) |
| **Hallucinated mechanism** (LLM invents queue disciplines, resources, transitions not requested) | Strong | Phase 0 B-series checks each verify that observed mechanisms match declared mechanisms (a hallucinated rule produces no DSL declaration, leaving it unjustified in the manifest); G3 trace-fidelity guard flags `SUSPICIOUS_PATCH` when whole event classes appear or disappear between runs; the DSL→code coverage manifest cannot reference an undeclared mechanism | The combination of the manifest's "no undeclared mechanism" rule with G3's "no unexplained event class" rule blocks both static (manifest-side) and dynamic (trace-side) hallucinations |
| **Constraint omission** (resource capacity, preemption barrier, or other declared constraint silently dropped from code) | Strong | DSL→code coverage manifest requires explicit `NOT_IMPLEMENTED` justification (which routes to user review); §4.2.6 `STRUCTURAL_IMPOSSIBILITY` flags rules that cannot fire under the code's priority gate; §4.2.3 `INERT_PARAMETER` flags values that are read but do not gate execution | These three checks together catch the ICU preemption + barrier_fraction case: the substitution would either appear as a NOT_IMPLEMENTED entry the user reviews, as a STRUCTURAL_IMPOSSIBILITY when the rule cannot fire, or as an INERT_PARAMETER when the value is read but never gates |
| **Repair-induced regression** (auto-fix resolves one issue but breaks conservation law) | Strong | G4 progress-aware budget guard detects regression (failure set strictly grew compared to prior attempt) and burns one attempt while logging the new failures; G3 trace-fidelity guard catches the case where a repair causes a whole event class to disappear; SUSPICIOUS_PATCH (exit 5) halts the loop | The self-heal loop's `max_attempts` budget plus G4's regression detection plus G3's class-change detection together prevent the auto-repair loop from drifting through plausible-but-wrong intermediate states |
| **Explanation–reality mismatch** (LLM's prose explanation claims FIFO while code implements priority) | Strong | All B-series checks bind declared logical claims to *trace behaviour*, not to LLM prose; B31 `service_rates_extended` specifically checks queue discipline (FIFO vs priority) against the observed queue exit order; §4.2.3 residual diagnostics distinguish what the LLM says was implemented from what the trace actually shows; the trace is the ground truth, the explanation is advisory | The principle is structural: the framework asks the trace, not the LLM; LLM prose can be persuasive, trace event timing cannot |
| **Iteration-level metric-following** (auto-repair tunes a structural-failure-bound mechanism, reading count-down as progress) | Strong | §4.2.3 residual fingerprinting (via `residual_diagnostics.py`) clusters residual violations by trace fingerprint; when a structural fingerprint (e.g. same-tick preempt race) dominates the residual, emits `STRUCTURAL CEILING REACHED` with named compatible / incompatible mechanism patterns; this short-circuits the iterative-tuning loop on mechanism-bound failure modes | This is the most recent addition to the framework, derived directly from the four-round barrier-enforcement case study; it converts the count-of-violations metric into a categorical structural-vs-parametric verdict |

---

## Summary: framework coverage by error class

### Strong-coverage classes (24 of ~28)

The framework provides deterministic, trace-bound, or fingerprint-bound coverage of:

- requirement extraction error
- parameter error
- calibration error
- logic error
- algorithm error
- syntax error
- runtime error
- numerical error
- initialization error
- random number error
- warmup error
- replication design error
- verification failure
- interpretation failure
- semantic fidelity error
- specification drift
- hallucinated mechanism
- constraint omission
- repair-induced regression
- explanation-reality mismatch
- iteration-level metric-following

### Partial-coverage classes

The framework catches symptoms but cannot establish root cause without user input:

- assumption error (made visible by typed DSL + placeholder mechanism; plausibility verified via Phase 5)
- conceptual model error (made visible by alignment review + coverage gates; reality match requires reference data)
- validation failure (Phase 4.5 + face validity + Phase 5 surface; operational validation against real system requires reference data)

### Visibility-only classes

The framework makes the error visible to the user but cannot resolve it:

- problem formulation error (Decision Validity refuses to credential a model whose decision-relevant claim has not been counterfactually exercised; Phase 5 advisory flags surface mismatches)
- data error (when reference data is supplied, A4 + B16 verify; without reference data, the description's data is treated as ground truth)

### Out-of-scope classes

The framework architecturally cannot address these:

- the user identifying the wrong decision question (no procedure can verify this)
- the description being factually incorrect about the real system without external reference data (no procedure can verify the description against the world)
- SME domain judgement on plausibility (the framework can flag at Phase 5 but cannot replace SME judgement)

---

## What this coverage table demonstrates

Three observations support the methodology's contribution claim.

First, **the framework's coverage is strongest on the LLM-era error classes** — every LLM-specific error in the taxonomy has a strong-coverage deterministic mechanism. This is empirically grounded: each LLM-specific mechanism was added in response to a failure mode observed in the case studies, and each one targets a class that classical V&V does not address because classical V&V was designed for human-authored models.

Second, **the framework's classical V&V coverage matches Sargent's expectations for a credible V&V suite**. Phase 0 + Phase 1 cover verification; Phase 4.5 + Phase 5 cover validation; Stage E covers interpretation. The framework does not displace classical V&V techniques (face validation, predictive validation against historical data, SME review) but extends them into a setting where the developer-side of the V&V is itself produced by an LLM.

Third, **the residual gaps are concentrated on user-responsibility classes**. Problem formulation, factual correctness of the description, and SME judgement on plausibility are out of scope for any verification framework that does not have access to the real system; the user must close these gaps before the framework can credential the model. The methodology's commitment is to make these residual gaps explicit through the verdict structure rather than absorb them silently — the Decision Validity cap on insufficient counterfactual coverage, the Phase 5 advisory ceiling, and the `assumed_default: true` placeholder mechanism together ensure that a user-side responsibility is never disguised as a framework guarantee.

The framework's claim, then, is bounded but precise: it provides strong deterministic coverage of the verification-side, implementation-side, experimental-side, and LLM-specific error classes; partial-but-visibility-strong coverage of the conceptual-model and assumption error classes; and an explicit verdict-layer refusal to credential models whose user-responsibility classes have not been adequately addressed. The empirical case studies in the paper instantiate this coverage map: every failure documented in the two case studies maps to one of the rows above, and every framework mechanism listed in the rows above was added in response to a documented case-study failure.
