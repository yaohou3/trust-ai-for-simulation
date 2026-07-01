# Framework Step Realization Reference

A mapping of every framework step to its realization mechanism. The column **Realization** uses four categories:

- **Python (deterministic)** — pure code; no LLM judgement involved in the trust-bearing decision.
- **Prompt (LLM)** — a structured prompt the LLM responds to; the LLM's output is the artefact or verdict.
- **Hybrid** — both Python infrastructure and LLM judgement; the LLM produces content the Python pipeline then validates or operates on deterministically.
- **Schema (declarative)** — typed declarations in Python that constitute the contract rather than executing logic.

The "trust-bearing" column indicates whether the step's verdict can credential the model: deterministic Python is trust-bearing; LLM judgement is advisory (its worst-case outcome is iteration, not a credentialed model).

---

## Stage A — Specification

| Step | What it does | Realization | Files | Trust-bearing? |
|---|---|---|---|---|
| A1 | DSL generation from natural language | **Prompt (LLM)** | `simulation_trust_framework_v5_3.jsx` (canonical prompts); LLM produces draft `my_dsl.json` | No — output is iterated, not credentialed |
| A2 | DSL alignment review (independent LLM) | **Prompt (LLM)** | `simulation_trust_framework_v5_3.jsx` (reviewer prompt); produces `ALIGNED` / `NEEDS_REVISION` verdict | No — advisory verdict that triggers iteration |
| A3 | Declare `emission_contract` on competing-risks arcs | **Schema (declarative)** | `dsl_schema.py` typed field on `state_transition_rules` | No — declaration only, validated downstream by §1.5 |
| A4 | Description→DSL coverage gate (KPI tables) | **Python (deterministic)** | `description_dsl_coverage.py` parses docx tables, matches to `aggregate_target` entries, normalizes units, checks CIs | **Yes** — gate blocks pipeline on FAIL |

## Stage B — Implementation

| Step | What it does | Realization | Files | Trust-bearing? |
|---|---|---|---|---|
| B1 | Simulation code generation (blind to validators) | **Prompt (LLM)** | `CODEGEN_BRIEF.md` (codegen contract); `simulation_trust_framework_v5_3.jsx` (canonical prompt); LLM produces `my_sim.py` | No — output is iterated and validated downstream |
| B2 | Code alignment review (independent LLM) | **Prompt (LLM)** | `simulation_trust_framework_v5_3.jsx`; produces `ALIGNED` / `NEEDS_REVISION` | No — advisory verdict triggering iteration |
| B3 | Declared-element implementation manifest | **Hybrid** | LLM populates `MANIFEST["declared_element_implementation_status"]` in `my_sim.py`; `dsl_code_coverage.py` validates the manifest deterministically | **Yes** — the validation is trust-bearing |

## Stage C — Credibility

### C0–C2: Preflight and integrity guards

| Step | What it does | Realization | Files | Trust-bearing? |
|---|---|---|---|---|
| C0 | Harness-provenance guard (canonical checker files present) | **Python (deterministic)** | `verify_and_run.py` checks file hashes against expected set | **Yes** — STOP (exit 3) on missing files |
| C1 | DSL→code coverage preflight | **Python (deterministic)** | `dsl_code_coverage.py` enumerates DSL elements vs manifest entries | **Yes** — STOP on incomplete coverage |
| C2.G1 | Model-identity guard | **Python (deterministic)** | SHA-256 of `my_dsl.json` vs stored baseline; auto-rebaseline on change | **Yes** — controls when the credibility chain restarts |
| C2.G2 | Parameter-fingerprint guard | **Python (deterministic)** | Fingerprint of declared parameters from `DEFAULT_CONFIG` vs baseline | **Yes** — STOP (exit 4, `ILLEGAL_PATCH`) on declared-parameter change |
| C2.G3 | Trace-fidelity guard | **Python (deterministic)** | Counter of event-type set under baseline run vs stored profile | **Yes** — STOP (exit 5, `SUSPICIOUS_PATCH`) on whole-event-class change |
| C2.G4 | Progress-aware budget | **Python (deterministic)** | Compare `sim_hash` and failure set across self-heal attempts | **Yes** — controls iteration budget consumption |

### C3: Phase pipeline

| Phase | What it checks | Realization | Files | Trust-bearing? |
|---|---|---|---|---|
| Phase 0 — Layer A (A1–A8) | Structural trace integrity, determinism, clean shutdown | **Python (deterministic)** | `phase0_vvuq.py` | **Yes** |
| Phase 0 — Layer B (B01–B17) | Per-element declarative contracts (arrivals, service rates, coordination, etc.) | **Python (deterministic)** | `phase0_vvuq.py` | **Yes** |
| Phase 0 — Layer B Extended (B18–B23) | Process contracts with `count_unit`-aware grouping | **Python (deterministic)** | `process_contracts.py` | **Yes** |
| Phase 0 — Layer B Extended (B24–B41) | Semantic contracts (eligibility, coverage, routing, escalation, dwell, etc.) | **Python (deterministic)** | `semantic_checks.py` | **Yes** |
| Phase 1 — §1.1 system invariants | Conservation, Little's Law, lifecycle | **Python (deterministic)** | `validation.py:validate_phase1` | **Yes** |
| Phase 1 — §1.2 queue invariants | Per-queue conservation, FIFO, Little's Law | **Python (deterministic)** | `validation.py:validate_phase1` | **Yes** |
| Phase 1 — §1.3 resource invariants | Per-resource state machine, capacity, utilization identity | **Python (deterministic)** | `validation.py:validate_phase1` | **Yes** |
| Phase 1 — §1.4 trace-metric consistency | Cadence, scheduled-window count, busy-time consistency | **Python (deterministic)** | `validation.py:validate_phase1` | **Yes** |
| Phase 1 — §1.5 emission contract | Per-direction emission semantics on competing-risks arcs | **Python (deterministic)** | `validation.py:validate_phase1` | **Yes** |
| Phase 2 — limit checks | M/M/c applicability gate, zero-load, heavy-load, zero-capacity (with cap=1 fallback), infinite-capacity | **Python (deterministic)** | `validation.py:validate_phase2` | **Yes** |
| Phase 3 — UQ checks | Warm-up (Welch + MSER-5), CI construction, CRN, sensitivity | **Python (deterministic)** | `validation.py:validate_phase3` | **Yes** |
| Phase 4 — §4.1 OAT sweeps | Arrival-rate and capacity sensitivity directions | **Python (deterministic)** | `validation.py:validate_phase4` | **Yes** |
| Phase 4 — §4.2.x preemption | Preemption fires, priority order, barrier enforcement, resume pairing, non-preemptible, rule evidence | **Python (deterministic)** | `validation.py:validate_phase4` | **Yes** |
| Phase 4 — §4.2.3 residual diagnostics | Residual-fingerprint clustering, architectural-implication tagging | **Python (deterministic)** | `residual_diagnostics.py` | Augments §4.2.3's verdict; trust-bearing via that gate |
| Phase 4 — §4.3 face validity | Utilization ∈ [0,1], departures ≤ arrivals, non-negative times | **Python (deterministic)** | `validation.py:validate_phase4` | **Yes** |
| Phase 4 — §4.4 effect size | INFO summary of OAT response sizes | **Python (deterministic)** | `validation.py:validate_phase4` | No (INFO-only) |

### C4–C6: Self-heal, re-review, manifest stamping

| Step | What it does | Realization | Files | Trust-bearing? |
|---|---|---|---|---|
| C4 | Self-heal loop (classify failure, route to correct artefact) | **Hybrid** | `self_heal_orchestrator.py` writes structured prompt; LLM classifies (`SIM_CODE` / `DSL_SPEC` / `VALIDATOR_FIX`) and (for `SIM_CODE`) patches `my_sim.py`; orchestrator re-runs verification | LLM classification is advisory; the post-patch re-run is what re-establishes trust |
| C5 | Step 4 re-review (post-patch intent check) | **Hybrid** | `verify_and_run.py` records `step4_approved_sim_hash` flag; user re-runs Code Alignment Review (LLM prompt) when sim hash drifts | Advisory; declared parameters are guaranteed unchanged by G2 |
| C6 | Manifest stamping | **Python (deterministic)** | `verify_and_run.py` writes `vvuq_out/verified_manifest.json` with per-phase verdicts, hashes, timestamps, disclaimer | **Yes** — the manifest is the credentialing artefact |

## Stage D — Causal Validity

| Step | What it does | Realization | Files | Trust-bearing? |
|---|---|---|---|---|
| D0 | Scenario ingestion (auto-generated scenarios) | **Hybrid** | `scenGenP` prompt produces JSON scenarios; `scenario_ingest.py` parses, normalises provenance (`auto_generated` → magnitude stripped, direction-only), rejects `direction: "any"`, routes by `scenario_category` | Trust-bearing: the deterministic normalisation prevents auto-generated scenarios from earning authoritative trust |
| D1 | Phase 4.5 harness — run scenarios | **Python (deterministic)** | `semantic_scenarios.py:run_phase4_5` runs declared scenarios from `compliance_spec` + canonical scenarios from `canonical_scenarios()` (universal `canonical_workload_response` + 7 element-conditional claims); each scenario runs treatment vs control across paired seeds (default 12), computes bootstrap CI, gates verdict on CI excluding zero in expected direction | **Yes** — per-scenario verdicts and aggregate are deterministic |
| D2 | Tier-weighted aggregate | **Python (deterministic)** | `semantic_scenarios.py:aggregate_verdict` rolls per-scenario verdicts with provenance weighting; emits `PASS` / `WARN` / `FAIL` / `BLOCK` / `PASS_ADVISORY_ONLY` / `PASS_NO_SCENARIOS` | **Yes** |

## Stage E — Judgment

| Step | What it does | Realization | Files | Trust-bearing? |
|---|---|---|---|---|
| E1 | Phase 5 plausibility review | **Prompt (LLM)** | LLM emits typed flags (`emergent_behavior` / `realism_gap` / `decision_risk` / `communication_risk`) with `severity` and citation; advisory by construction (no FAIL) | Advisory only; can cap Decision Validity at WARN |
| E2 | Three-dimension verdict (`decision_validity.py`) | **Python (deterministic)** | `decision_validity.py` computes Credibility (worst-of Phases 0–4), Causal Validity (mapped from Phase 4.5 aggregate), Decision Validity (gated rollup with monotonicity, authoritative-coverage requirement, Phase 5 high-severity cap) | **Yes** — the verdict layer is the methodology's primary credentialing output |
| E3 | Final synthesis | **Hybrid** | LLM produces a synthesis report; the synthesis prompt *requires* the LLM to invoke `decision_validity.py` rather than narrate its own verdict; the deterministic verdict is the trust-bearing fact, the synthesis is the human-readable wrapper | Trust-bearing fact is the verdict; the synthesis is advisory framing |

---

## Cross-cutting infrastructure

| Component | What it does | Realization | Files |
|---|---|---|---|
| DSL schema | Typed JSON schema with 27 element types, required fields, enumerations | **Schema (declarative)** | `dsl_schema.py` (`DSL_ELEMENT_SCHEMA`, `PIPELINE_ARTIFACT_SCHEMA`, `DSL_ELEMENT_TO_COMPLIANCE`) |
| Compliance mapper | Maps DSL → `compliance_spec` per the element-type mapping table | **Python (deterministic)** | `compliance_mapper.py` |
| Coverage validator | DSL alignment / element coverage validation including `assumed_default` discipline | **Python (deterministic)** | `coverage_validator.py` |
| VVUQ utilities | Shared helpers (`post_warmup_events`, `group_by_entity`, `_entity_sojourn`, etc.) | **Python (deterministic)** | `vvuq_utils.py` |
| Trace contract | Required and optional event fields; canonical event vocabulary | **Schema (declarative)** | Documented in `vvuq_utils.py` and `CODEGEN_BRIEF.md` |
| Forbidden-file list | Files codegen LLM must not read (the validators it would otherwise game) | **Schema (declarative)** | Documented in `CODEGEN_BRIEF.md` and enforced by harness-provenance guard at C0 |
| Run guide | End-user procedure for running the framework | **Documentation** | `RUN_GUIDE.md` |
| Routing reference | Living routing document per stage / per phase / per check | **Documentation** | `FRAMEWORK_ROUTING.md` |

---

## Summary by realization category

### Trust-bearing decisions are exclusively Python (deterministic)

Every verdict that credentials or refuses to credential the model is computed by deterministic Python:

- The integrity guards (G1–G4)
- The preflight gates (C0–C1)
- Phase 0 (all 73 contract checks)
- Phase 1, 2, 3, 4 (all mathematical, limit, UQ, sensitivity, preemption, face-validity checks)
- Phase 4.5 harness (`run_phase4_5`, `aggregate_verdict`)
- Stage E verdict (`decision_validity.py`)
- Description→DSL coverage gate (A4)
- DSL→code coverage gate (C1)
- Manifest stamping (C6)

This is the operationalization of the methodology's "trust-bearing decisions are located in independent, deterministic infrastructure" principle.

### LLM judgement is reserved for advisory and generative roles

- DSL generation (A1) and code generation (B1) — where the worst-case outcome is downstream verification finding errors, not a credentialed bad model.
- Alignment reviews (A2, B2, C5) — where the worst-case outcome is iteration with a non-aligned verdict.
- Self-heal classification (C4) — where the LLM's classification routes the repair but the post-patch re-run is what re-establishes trust.
- Scenario generation (D0) — where auto-generated scenarios are forced to advisory tier by `scenario_ingest.py`.
- Plausibility review (E1) — advisory by construction; cannot emit FAIL.
- Final synthesis (E3) — the LLM frames the verdict but cannot replace it.

This is the operationalization of the methodology's "LLM judgement is welcome in advisory and iterative-improvement roles; not as the final arbiter of any single trust dimension's verdict" principle.

### Schema-declarative components define contracts that downstream Python validates

The DSL schema, the trace contract, the compliance-section mapping table, the canonical scenario contract (override keys and metric-name conventions), and the forbidden-file list are not algorithmic — they are typed declarations that the deterministic Python pipeline reads and validates against. The schema is the framework's *interface*; the Python code is the framework's *enforcement*.

---

## Methodological observation

The two-axis decomposition (deterministic-vs-judgmental × trust-bearing-vs-advisory) gives a clean way to argue the framework's contribution claim:

- *All* trust-bearing decisions are deterministic.
- *All* LLM judgement is advisory.
- *All* contracts are declaratively schematised so the deterministic checks have something to read.

This separation is the methodology's structural guarantee: no LLM commentary can move a verdict, no LLM revision can edit the verification apparatus, no LLM-generated scenario can earn authoritative trust. The framework's evolution under empirical case studies has tightened this separation rather than weakening it — every framework-evolution vignette in the iterative-refinement narrative has either strengthened a deterministic check (B29 entity_type, A8 counter-pairing, §4.2.3 residual diagnostics, canonical_workload_response) or made an LLM-judgement-dependent step more constrained (DSL→code coverage manifest, residual-diagnostics-driven structural-vs-parametric classification).

For the paper, this table can be referenced directly when reviewers ask "where exactly is LLM judgement in the loop, and where is it deterministic?" The answer per step is in the table; the principle behind the answer is in §3.4 of the methodology (Principle 2: trust-bearing decisions in non-colludable infrastructure).
