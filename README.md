# Trust Framework for LLM-Generated Simulations

A credibility framework for discrete-event simulation models whose construction 
is mediated by large language models, packaged as a Claude Code / Claude Agent SDK **skill**.

This skill produces a non-collapsing three-dimension credibility verdict —
**Credibility, Causal Validity, Decision Validity** — and refuses to credential
a model whose declared mechanisms are not operationally honoured by the
simulation code. Trust-bearing decisions are computed by deterministic Python;
LLM judgement is reserved for advisory and generative roles.

---

## What this skill does

End-to-end pipeline across five stages:

| Stage | Purpose | Realization |
|---|---|---|
| **A** — Specification | Natural-language description → typed DSL → A2 alignment review → A4 description-to-DSL coverage gate | LLM + deterministic Python |
| **B** — Implementation | DSL → simulation code → B2 alignment review → declared-element implementation manifest | LLM + deterministic Python |
| **C** — Credibility | Harness-provenance + DSL-to-code coverage preflights → four integrity guards → Phases 0–4 (73 + 30 deterministic checks) → self-heal repair loop | Deterministic Python |
| **D** — Causal Validity | Phase 4.5 what-if scenario harness with declared + canonical scenarios, provenance-weighted aggregate | Deterministic Python |
| **E** — Judgment | Phase 5 plausibility (advisory flags) → three-dimension verdict | LLM + deterministic Python |

The framework defends against eight empirically-observed failure modes of
LLM-mediated simulation construction:

1. Conceptual-to-coded model verification gaps (silent mechanism substitution).
2. Calibration overfitting to declared targets.
3. Event-log manipulation to satisfy checks.
4. Modification of the V&V apparatus by the LLM.
5. Non-independence of code generator and verifier.
6. Iterative tuning to validation residuals without recognising structural error.
7. Mechanism-dynamics substitution (continuous hazard vs discrete check vs event-driven).
8. Description correctness assumed rather than enforced.

---

## Installation

### Option 1 — Direct download (recommended)

1. Download the latest `.skill` file from the [Releases page](../../releases) of this repository.
2. Open the file in Claude Code or Cowork. The chat interface will render a *"Save skill"* install button.
3. Click to install. The skill becomes available at `~/.claude/skills/trust-framework-vvuq/`.

### Option 2 — Clone the repository

```bash
git clone https://github.com/<owner>/trust-framework-vvuq.git ~/.claude/skills/trust-framework-vvuq
```

After installation, restart Claude Code or reload the session.

---

## Usage

In Claude Code or Cowork, invoke by describing your task. The skill triggers
automatically on requests involving V&V, validation, credibility assessment,
or what-if evidence for LLM-generated simulations. Example invocations:

- *"Turn this ER description into a typed simulation specification."*
- *"Verify this simulation against my DSL — should I trust it for the staffing decision?"*
- *"Run the Phase 4.5 harness on the polisher-capacity scenario and give me the three-dimension verdict."*

The skill includes a worked Emergency Room example under
[`examples/simple_er/`](examples/simple_er/).

---

## Repository layout

```
trust-framework-vvuq/
├── SKILL.md                      # entry point (Claude reads this first)
├── scripts/                      # deterministic Python infrastructure
│   ├── verify_and_run.py         #   main orchestrator
│   ├── validation.py             #   Phase 1–4 validators
│   ├── phase0_vvuq.py            #   Phase 0 + dispatcher
│   ├── process_contracts.py      #   B18–B23
│   ├── semantic_checks.py        #   B24–B41
│   ├── semantic_scenarios.py     #   Phase 4.5 harness
│   ├── decision_validity.py      #   Stage E verdict
│   ├── description_dsl_coverage.py  #  A4 gate
│   ├── dsl_code_coverage.py      #   B3 / C1 gate
│   ├── residual_diagnostics.py   #   §4.2.3 augmentation
│   ├── self_heal_orchestrator.py #   repair loop
│   ├── compliance_mapper.py
│   ├── dsl_schema.py
│   ├── coverage_validator.py
│   └── vvuq_utils.py
├── prompts/
│   └── CODEGEN_BRIEF.md          # Stage B codegen contract
├── references/                   # human-facing documentation
│   ├── RUN_GUIDE.md
│   ├── FRAMEWORK_ROUTING.md
│   ├── error_taxonomy_coverage.md
│   └── framework_step_realization.md
└── examples/
    └── simple_er/
        └── description.docx      # Emergency Room worked example
```

---

## Architectural commitments

The framework's claim of trustworthiness rests on six theoretical commitments:

1. **Trust factors into three orthogonal dimensions** — Credibility, Causal Validity, Decision Validity — and never collapses into one.
2. **Trust-bearing decisions are independent and deterministic** — implemented as Python checks the LLM cannot persuade.
3. **Evidence carries pedigree** — automatically generated scenarios are advisory; declared and SME-approved scenarios are authoritative.
4. **Falsifiability cascades across stages** — each downstream artefact exposes upstream claims to a more concrete oracle.
5. **Diagnostics distinguish structural model error from parameter calibration error** — so the self-healing loop does not waste iterations on the wrong fix.
6. **Description correctness is a user responsibility; description-flaw visibility is a framework obligation.**

See [`references/framework_step_realization.md`](references/framework_step_realization.md)
for a per-step map of Python vs. LLM realization and
[`references/error_taxonomy_coverage.md`](references/error_taxonomy_coverage.md)
for the framework's coverage of the simulation V&V error taxonomy.

---

## Citation

If you use this framework in academic work, please cite:

```bibtex
@article{trust-framework-vvuq-2026,
  title  = {{Trust AI for Simulation: A Framework for Credibility of LLM-Generated Discrete-Event Models}},
  author = {<Grace Yao Hou, Xiang Zhong>},
  year   = {2026},
  note   = {Manuscript in preparation}
}
```

---

## License

See [LICENSE](LICENSE).

---

## Contributing

Issues and pull requests are welcome. When proposing a new check or
amendment to an existing one, follow the framework's iterative-refinement
discipline: every check must be deterministic; LLM judgement is allowed in
advisory roles only; no validator may be modified to clear a failing case
without an explicit `VALIDATOR_FIX` escalation. See the failure-mode catalog
in `references/error_taxonomy_coverage.md` before proposing changes.
