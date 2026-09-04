"""
semantic_scenarios.py — Phase 4.5: Counterfactual (treatment-vs-control) harness.

Phase 0 (phase0_vvuq.py) verifies that a single trace is structurally and
semantically consistent with the DSL/compliance_spec.  Phase 4.5 verifies
properties that only become visible by comparing two runs — things like:

  • emergent patterns        : "when demand rises 30%, blocking should rise"
  • inter-process coupling   : "when rounds run, discharge rate should drop"
  • observation-delay effects: "with 15-min delay, miss rate should rise vs 0"
  • control-policy behaviour : "switching reactive → scheduled flattens peak util"
  • policy-threshold claims  : "when queue>N, spin-up happens within T seconds"
  • escalation claims        : "when SLA is breached, escalation fires"
  • workload-response claims : "at 1.5× arrivals, loss_rate rises ≥ 5%"
  • stress / boundary tests  : "at 2× load the system should not deadlock"

The harness runs a `control` configuration and a `treatment` configuration
(both derived from a `base_config`) and asserts that a chosen metric moves
in the declared direction by the declared magnitude.  Each scenario emits
a CheckResult-like record; a pipeline can gate code acceptance on the
aggregate verdict.

Inputs are pulled from compliance_spec (each list is optional):

    semantic_scenarios    # generic control/treatment/expectation
    coupling_rules        # toggle source_process on/off
    control_policies      # delay / cadence perturbations
    policy_thresholds     # toggle response_event on/off
    escalations           # toggle escalation outcome on/off
    workload_responses    # scale arrival rate by factor

Design (v5.2)
=============
• Evaluation is a hypothesis test, not a single-run comparison.  The default
  method bootstraps the mean difference (T − C) over `replications` resamples
  and a claim's `direction_pass` is true iff the 95% CI excludes zero in the
  declared direction.  `magnitude_pass` is true iff |relative_change| ≥
  expectation.min_effect_size.  A PASS requires both.
• Paired Common-Random-Numbers (`paired_crn_median_diff`) is available as an
  alternative method when seeds are held identical across C/T — bootstrap
  the within-seed median difference for variance reduction.
• Deep-merge by default: nested dicts in `overrides` are recursively merged
  into a deep copy of `base_config`.  A per-key opt-out sentinel
  (`"__shallow__": True`) forces replacement instead of merge.
• Per-claim DSL shape (GPT template):
      {
        "name": "...",
        "scenario_category": "emergent_pattern | coupling | control_policy |
                              policy_threshold | escalation | workload_response",
        "control":    {"parameters": { ... }},
        "treatment":  {"parameters": { ... }},
        "metrics":    ["loss_rate"]              # or [{"name": "...", "direction": "up",
                                                 #      "min_effect_size": 0.05}]
        "expectation":{"direction": "up" | "down" | "unchanged",
                       "min_effect_size": 0.05},
        "evaluation": {"method": "bootstrap_mean_diff" | "paired_crn_median_diff",
                       "confidence": 0.95,
                       "replications": 2000},
      }
  Legacy shape (`expected_directional_effect` with `min_magnitude`/`max_magnitude`)
  is tolerated by the expectation/metric normalizers.

Usage
=====

    from semantic_scenarios import run_phase4_5, print_phase4_5_summary

    report = run_phase4_5(
        base_config=cfg,
        compliance_spec=cfg["compliance_spec"],
        run_simulation=run_simulation,
        metric_extractor=lambda result: result["metrics"],
        seeds=[42, 43, 44],
        include_canonical=True,          # also synthesize the 7 canonical claims
    )
    print_phase4_5_summary(report)
"""

from __future__ import annotations

import copy
import math
import random
import statistics
from dataclasses import dataclass, field, asdict
from typing import Any, Callable


# ─────────────────────────────────────────────────────────────────────────────
# Records
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ScenarioResult:
    """Outcome of one (claim, metric) comparison under a stated method."""
    name: str
    category: str                              # emergent_pattern | coupling | ...
    metric: str
    method: str                                # bootstrap_mean_diff | paired_crn_median_diff
    expected_direction: str                    # "up" | "down" | "unchanged"
    expected_magnitude: float | None           # min_effect_size (fractional)
    delta_mean: float                          # point est. of (T − C) under `method`
    relative_change: float                     # delta_mean / |control ref|
    ci_95: tuple[float, float]                 # bootstrap CI on the point estimate
    confidence: float                          # nominal CI level (default 0.95)
    direction_pass: bool                       # CI excludes zero in declared direction
    magnitude_pass: bool                       # |relative_change| ≥ min_effect_size
    verdict: str                               # "PASS" | "FAIL" | "INCONCLUSIVE" | "BLOCK"
    message: str = ""
    n_seeds: int = 0
    # Provenance tier — governs evidence weight (see aggregate_verdict):
    #   "declared"      DSL-declared by the user / Phase-0 mapper  → authoritative
    #   "sme_approved"  LLM-proposed then approved by a human SME  → authoritative,
    #                   may bear a magnitude claim
    #   "canonical"     framework-synthesized theory scenario      → authoritative
    #   "auto_generated" LLM-proposed, NOT human-approved           → advisory only:
    #                   forced direction-only, can flag problems but cannot raise
    #                   the aggregate verdict to PASS on its own.
    provenance: str = "declared"
    advisory: bool = False                     # True ⇔ provenance == "auto_generated"
    control_samples: list[float] = field(default_factory=list)
    treatment_samples: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ci_95"] = list(self.ci_95) if self.ci_95 else None
        return d


# Provenance tiers
_AUTHORITATIVE_PROVENANCE = {"declared", "sme_approved", "canonical"}
_ADVISORY_PROVENANCE = {"auto_generated"}

# Paired-CRN power: the number of seeds is the number of paired differences the
# bootstrap sees, so it is the binding constraint on detecting an effect. Three
# seeds (the historical default) cannot resolve anything but the largest effects
# and yields CIs that straddle zero for moderate ones. Default to a power-bearing
# count and warn when a caller drops below the recommended floor.
_DEFAULT_SEEDS = list(range(101, 113))   # 12 seeds
_MIN_RECOMMENDED_SEEDS = 10


def _provenance(spec: dict, default: str = "declared") -> str:
    """Read a scenario's provenance tier, tolerating ``source``/``provenance``
    keys and the ``sme_approved: true`` shorthand. Unknown values fall back to
    the most conservative interpretation for a non-declared scenario:
    ``auto_generated`` (advisory)."""
    raw = (spec.get("provenance") or spec.get("source") or "").strip().lower()
    if spec.get("sme_approved") is True:
        return "sme_approved"
    if not raw:
        return default
    if raw in _AUTHORITATIVE_PROVENANCE or raw in _ADVISORY_PROVENANCE:
        return raw
    # Any other label (e.g. "llm", "generated", "candidate") is treated as
    # advisory — an unrecognised provenance must never be trusted as ground truth.
    return "auto_generated"


# ─────────────────────────────────────────────────────────────────────────────
# Config override helpers
# ─────────────────────────────────────────────────────────────────────────────

def _apply_overrides(base: dict, overrides: dict) -> dict:
    """Recursive deep-merge overrides into a deep copy of `base`.

    Rules:
      • Nested dicts are merged key-by-key.
      • Non-dict values (including lists) replace the corresponding base value.
      • Per-key opt-out: if an override value is a dict containing
        ``"__shallow__": True``, the sentinel is stripped and the resulting
        dict REPLACES the base value (no recursive merge).
    """
    out = copy.deepcopy(base)
    for k, v in (overrides or {}).items():
        if isinstance(v, dict) and v.get("__shallow__") is True:
            v = {kk: vv for kk, vv in v.items() if kk != "__shallow__"}
            out[k] = v
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _apply_overrides(out[k], v)
        else:
            out[k] = v
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Metric extraction & cell execution
# ─────────────────────────────────────────────────────────────────────────────

def _extract_metric(metrics: dict, name: str) -> float | None:
    """Dotted-path lookup: 'losses.renege' → metrics['losses']['renege']."""
    cur: Any = metrics
    for part in name.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    try:
        return float(cur)
    except (TypeError, ValueError):
        return None


def _run_cell(run_simulation, cfg: dict, seeds: list[int],
              metric_extractor: Callable[[dict], dict]) -> dict[str, list[float]]:
    """Run the simulation across `seeds`, return {metric: [values...]}.

    For dotted-path metrics, the extractor's output is flattened on the fly
    so callers can subscript with 'utilization.nurse', etc.
    """
    # Run every seed first, keeping results index-aligned to `seeds`, then build
    # one column per metric with exactly len(seeds) entries (NaN where a metric
    # is missing/non-numeric for that seed). This keeps index i ↔ seeds[i] so
    # the paired-CRN estimator can difference matched seeds; the previous
    # append-on-success approach desynchronised control/treatment whenever a
    # metric was present for different seed subsets.
    per_seed: list[dict] = []
    for seed in seeds:
        run_cfg = copy.deepcopy(cfg)
        run_cfg["seed"] = seed
        result = run_simulation(run_cfg)
        per_seed.append(_flatten(metric_extractor(result) or {}))

    keys: set[str] = set()
    for d in per_seed:
        keys.update(d.keys())

    samples: dict[str, list[float]] = {}
    for k in keys:
        col: list[float] = []
        for d in per_seed:
            try:
                col.append(float(d.get(k)))
            except (TypeError, ValueError):
                col.append(math.nan)
        samples[k] = col
    return samples


def _flatten(d: dict, prefix: str = "") -> dict:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
            # keep first-level scalars under their bare key too
            if not prefix:
                out[k] = v
        else:
            out[key] = v
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Statistical helpers
# ─────────────────────────────────────────────────────────────────────────────

def _bootstrap_ci(
    control_vals: list[float],
    treatment_vals: list[float],
    replications: int = 2000,
    confidence: float = 0.95,
    statistic: str = "mean",
    rng: random.Random | None = None,
) -> tuple[float, tuple[float, float]]:
    """Return (point_estimate, (ci_low, ci_high)) for (treatment − control).

    `statistic` is "mean" or "median"; resampling is independent between
    control and treatment (unpaired bootstrap).
    """
    rng = rng or random.Random(0xBEEF)
    # drop NaN placeholders (seeds where the metric was missing/non-numeric)
    control_vals = [x for x in control_vals if not math.isnan(x)]
    treatment_vals = [x for x in treatment_vals if not math.isnan(x)]
    if not control_vals or not treatment_vals:
        return 0.0, (0.0, 0.0)

    stat = statistics.mean if statistic == "mean" else statistics.median
    est = stat(treatment_vals) - stat(control_vals)

    nc, nt = len(control_vals), len(treatment_vals)
    deltas: list[float] = []
    for _ in range(replications):
        c = [control_vals[rng.randrange(nc)] for _ in range(nc)]
        t = [treatment_vals[rng.randrange(nt)] for _ in range(nt)]
        deltas.append(stat(t) - stat(c))

    alpha = 1.0 - confidence
    deltas.sort()
    lo_i = max(0, min(int(math.floor((alpha / 2) * len(deltas))),       len(deltas) - 1))
    hi_i = max(0, min(int(math.ceil((1 - alpha / 2) * len(deltas))) - 1, len(deltas) - 1))
    return est, (deltas[lo_i], deltas[hi_i])


def _paired_crn_median_diff(
    control_vals: list[float],
    treatment_vals: list[float],
    confidence: float = 0.95,
    replications: int = 2000,
    rng: random.Random | None = None,
) -> tuple[float, tuple[float, float]]:
    """Paired bootstrap on within-seed differences (T_i − C_i); returns
    (median_diff, (ci_low, ci_high)).

    Assumes samples are already aligned by seed (the harness holds the seed
    list fixed across control/treatment cells).
    """
    rng = rng or random.Random(0xBEEF)
    n = min(len(control_vals), len(treatment_vals))
    # difference only the seeds present (non-NaN) in BOTH arms, so a metric
    # missing for some seeds can't pair mismatched seeds
    paired = [treatment_vals[i] - control_vals[i] for i in range(n)
              if not (math.isnan(control_vals[i]) or math.isnan(treatment_vals[i]))]
    if not paired:
        return 0.0, (0.0, 0.0)
    est = statistics.median(paired)
    n = len(paired)

    meds: list[float] = []
    for _ in range(replications):
        s = [paired[rng.randrange(n)] for _ in range(n)]
        meds.append(statistics.median(s))

    alpha = 1.0 - confidence
    meds.sort()
    lo_i = max(0, min(int(math.floor((alpha / 2) * len(meds))),       len(meds) - 1))
    hi_i = max(0, min(int(math.ceil((1 - alpha / 2) * len(meds))) - 1, len(meds) - 1))
    return est, (meds[lo_i], meds[hi_i])


def _evaluate_claim(
    control_vals: list[float],
    treatment_vals: list[float],
    expectation: dict,
    *,
    method: str = "bootstrap_mean_diff",
    confidence: float = 0.95,
    replications: int = 2000,
) -> dict:
    """Run the directional hypothesis test.

    Returns
    -------
    dict with keys:
        delta_mean, relative_change, ci_95,
        direction_pass, magnitude_pass, verdict, message
    """
    direction = (expectation.get("direction") or "").lower()
    min_effect = expectation.get("min_effect_size")

    if not control_vals or not treatment_vals:
        return {
            "delta_mean":      0.0,
            "relative_change": 0.0,
            "ci_95":           (0.0, 0.0),
            "direction_pass":  False,
            "magnitude_pass":  False,
            "verdict":         "INCONCLUSIVE",
            "message":         "Missing samples in control or treatment.",
        }

    if method == "paired_crn_median_diff":
        est, (lo, hi) = _paired_crn_median_diff(
            control_vals, treatment_vals,
            confidence=confidence, replications=replications,
        )
        c_ref = statistics.median(control_vals)
    else:
        est, (lo, hi) = _bootstrap_ci(
            control_vals, treatment_vals,
            replications=replications, confidence=confidence, statistic="mean",
        )
        c_ref = statistics.mean(control_vals)

    denom = abs(c_ref) if abs(c_ref) > 1e-12 else 1e-12
    rel = est / denom

    # An expectation with no falsifiable direction is NOT A CLAIM. We never let
    # such a scenario PASS — a test that asserts nothing is not evidence. (This
    # is also the guard against "fixing" a failing scenario by relaxing its
    # direction to 'any'.)
    if direction not in ("up", "down", "unchanged"):
        return {
            "delta_mean":      est,
            "relative_change": rel,
            "ci_95":           (lo, hi),
            "direction_pass":  False,
            "magnitude_pass":  False,
            "verdict":         "INCONCLUSIVE",
            "message":         (f"NOT_A_CLAIM: no falsifiable direction declared "
                                f"(direction={direction!r}). A scenario must predict "
                                "'up', 'down', or 'unchanged' to count as evidence."),
        }

    # Direction test: the CI must lie strictly on the expected side of zero.
    # When it does not, we distinguish two very different situations:
    #   • CI lies strictly on the WRONG side  → FAIL  (the model contradicts the
    #     claim — a real, decision-relevant disagreement).
    #   • CI straddles zero                   → INCONCLUSIVE (underpowered or no
    #     detectable effect — usually too few seeds; not a contradiction).
    straddles_zero = (lo <= 0.0 <= hi)
    if direction == "up":
        direction_pass = lo > 0
        wrong_side = hi < 0
    elif direction == "down":
        direction_pass = hi < 0
        wrong_side = lo > 0
    else:  # "unchanged"
        tol = float(min_effect) if min_effect is not None else 0.10
        tol_abs = tol * denom
        direction_pass = (lo >= -tol_abs) and (hi <= tol_abs)
        wrong_side = (lo > tol_abs) or (hi < -tol_abs)
        straddles_zero = False  # not meaningful for an 'unchanged' claim

    # Magnitude test (skip for 'unchanged' — direction test already enforces it)
    if direction == "unchanged":
        magnitude_pass = direction_pass
    elif min_effect is None:
        magnitude_pass = True
    else:
        magnitude_pass = abs(rel) >= float(min_effect)

    if direction_pass and magnitude_pass:
        verdict = "PASS"
    elif direction == "unchanged":
        verdict = "FAIL" if wrong_side else "INCONCLUSIVE"
    elif direction_pass and not magnitude_pass:
        # Effect is real and in the right direction but smaller than claimed —
        # the magnitude claim is falsified.
        verdict = "FAIL"
    elif wrong_side:
        verdict = "FAIL"
    elif straddles_zero:
        verdict = "INCONCLUSIVE"
    else:
        verdict = "FAIL"

    extra = ""
    if verdict == "INCONCLUSIVE" and straddles_zero:
        extra = " — 95% CI straddles zero (no detectable effect; try more seeds)"
    msg = (f"Δ={est:+.4g} ({rel:+.2%}) "
           f"CI95=[{lo:+.4g}, {hi:+.4g}] "
           f"direction={'✓' if direction_pass else '✗'} "
           f"magnitude={'✓' if magnitude_pass else '✗'}{extra}")
    return {
        "delta_mean":      est,
        "relative_change": rel,
        "ci_95":           (lo, hi),
        "direction_pass":  direction_pass,
        "magnitude_pass":  magnitude_pass,
        "verdict":         verdict,
        "message":         msg,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Spec normalizers
# ─────────────────────────────────────────────────────────────────────────────

def _expectation(spec: dict) -> dict:
    """Normalize expectation block — tolerate the legacy
    ``expected_directional_effect`` shape with ``min_magnitude``."""
    exp = dict(spec.get("expectation") or spec.get("expected_directional_effect") or {})
    if "min_effect_size" not in exp and "min_magnitude" in exp:
        exp["min_effect_size"] = exp.get("min_magnitude")
    return exp


def _metrics_list(spec: dict) -> list[str]:
    """Normalize spec['metrics'] to a list of metric-name strings."""
    ms = spec.get("metrics")
    if isinstance(ms, list) and ms:
        out: list[str] = []
        for m in ms:
            if isinstance(m, str):
                out.append(m)
            elif isinstance(m, dict) and m.get("name"):
                out.append(m["name"])
        if out:
            return out
    exp = _expectation(spec)
    if exp.get("metric"):
        return [exp["metric"]]
    return ["loss_rate"]


def _evaluation(spec: dict) -> dict:
    ev = spec.get("evaluation") or {}
    return {
        "method":       ev.get("method", "bootstrap_mean_diff"),
        "confidence":   float(ev.get("confidence", 0.95)),
        "replications": int(ev.get("replications", 2000)),
    }


def _per_metric_expectation(spec: dict, metric_name: str, default_exp: dict) -> dict:
    """Allow `metrics: [{"name": ..., "direction": ..., "min_effect_size": ...}]`
    entries to override the claim-level expectation.

    AUDIT FIX (C7): an ABSENT direction must not default to "unchanged".
    Under paired seeds with empty overrides, control and treatment cells are
    bit-identical, so an "unchanged" claim passes trivially — meaning a bare
    scenario like ``{"name": "x"}`` with no expectation at all would earn an
    authoritative PASS. A scenario that asserts nothing is not evidence.
    Absent direction now resolves to the sentinel "undeclared", which
    _evaluate_claim's existing NOT_A_CLAIM branch rejects (same treatment as
    the explicit direction-relaxation guard for "any"). A scenario that
    EXPLICITLY declares direction "unchanged" is still a real, falsifiable
    equivalence claim and is evaluated as before.
    """
    direction = (default_exp.get("direction") or "undeclared").lower()
    min_effect = default_exp.get("min_effect_size")
    for m in spec.get("metrics", []) or []:
        if isinstance(m, dict) and m.get("name") == metric_name:
            direction = (m.get("direction") or direction).lower()
            if "min_effect_size" in m:
                min_effect = m["min_effect_size"]
            break
    return {"direction": direction, "min_effect_size": min_effect}


# ─────────────────────────────────────────────────────────────────────────────
# Drivers — derive (control_overrides, treatment_overrides) from the spec
# ─────────────────────────────────────────────────────────────────────────────

def _driver_semantic_scenario(spec: dict) -> tuple[dict, dict]:
    """Generic emergent-pattern claim.

    Accepts both GPT shape (``control.parameters`` / ``treatment.parameters``)
    and a flat dict — whichever the Phase-0 mapper emitted.
    """
    c = spec.get("control") or {}
    t = spec.get("treatment") or {}
    c_ovr = c.get("parameters") if isinstance(c, dict) and "parameters" in c else c
    t_ovr = t.get("parameters") if isinstance(t, dict) and "parameters" in t else t
    return dict(c_ovr or {}), dict(t_ovr or {})


def _driver_coupling_rule(spec: dict) -> tuple[dict, dict]:
    """Toggle source_process off in control, on in treatment.

    Config convention the generated sim must honor:
        {"processes_enabled": {"<process_name>": bool}}
    """
    src = spec.get("source_process")
    control   = {"processes_enabled": {src: False}}
    treatment = {"processes_enabled": {src: True}}
    return control, treatment


def _driver_control_policy(spec: dict) -> tuple[dict, dict]:
    """Perturb observation_delay / update_cadence.

    Control:   observation_delay_s = 0 (perfect information)
    Treatment: observation_delay_s = declared value
    """
    delay = spec.get("observation_delay_s") or 0.0
    cadence = spec.get("update_cadence_s")
    control   = {"observation_delay_s": 0.0}
    treatment: dict[str, Any] = {"observation_delay_s": float(delay)}
    if cadence is not None:
        treatment["update_cadence_s"] = float(cadence)
    return control, treatment


def _driver_policy_threshold(spec: dict) -> tuple[dict, dict]:
    """Policy-threshold claim: toggle whether the declared response_event
    fires when the threshold is breached.

    Config convention:
        {"policy_thresholds": {"<name>": {"enabled": bool}}}
    Expect response-linked metric (e.g. response_count, spinup_count,
    max_response_delay_s) to move in the declared direction when enabled
    flips from False → True.
    """
    name = spec.get("name") or "threshold"
    control   = {"policy_thresholds": {name: {"enabled": False}}}
    treatment = {"policy_thresholds": {name: {"enabled": True}}}
    return control, treatment


def _driver_escalation(spec: dict) -> tuple[dict, dict]:
    """Escalation claim: toggle the escalation_outcome on/off.

    Config convention:
        {"escalations": {"<name>": {"enabled": bool}}}
    Expected metric typically reflects the escalation signal (e.g.
    escalation_count, sla_breach_rate).
    """
    name = spec.get("name") or spec.get("escalation_name") or "escalation"
    control   = {"escalations": {name: {"enabled": False}}}
    treatment = {"escalations": {name: {"enabled": True}}}
    return control, treatment


def _driver_workload_response(spec: dict) -> tuple[dict, dict]:
    """Workload-response claim: scale arrival rate by a factor.

    Config convention:
        {"arrival_rate_scale": float}   # sim multiplies its declared rates
    ``spec['scale_factor']`` defaults to 1.3 (a mild stress test).
    """
    factor = float(spec.get("scale_factor") or spec.get("factor") or 1.3)
    control   = {"arrival_rate_scale": 1.0}
    treatment = {"arrival_rate_scale": factor}
    return control, treatment


# Registry of drivers by claim category — used by run_scenario() and
# run_phase4_5(include_canonical=True).
_DRIVERS: dict[str, Callable[[dict], tuple[dict, dict]]] = {
    "emergent_pattern":   _driver_semantic_scenario,
    "coupling":           _driver_coupling_rule,
    "control_policy":     _driver_control_policy,
    "policy_threshold":   _driver_policy_threshold,
    "escalation":         _driver_escalation,
    "workload_response":  _driver_workload_response,
}


# ─────────────────────────────────────────────────────────────────────────────
# Canonical-scenario library (7 claims synthesized from the DSL)
# ─────────────────────────────────────────────────────────────────────────────

def canonical_scenarios(compliance_spec: dict) -> list[dict]:
    """Synthesize the 7 canonical semantic claims from the declarative DSL.

    Each claim uses the GPT-template shape and carries its own
    ``scenario_category`` so ``run_phase4_5(include_canonical=True)`` can
    dispatch to the right driver.  Missing ingredients silently drop a
    claim rather than erroring — the library is best-effort.

    Section-name resolution
    -----------------------
    The framework's ``compliance_mapper`` emits sections under names such as
    ``service_rates``, ``coordination_patterns``, ``state_transition_rules``,
    ``preemption_rules`` (the per-element-type names enumerated in
    ``dsl_schema.DSL_ELEMENT_TO_COMPLIANCE``). Earlier drafts of this
    generator read short-form names — ``processes``, ``resources``, ``queues``,
    ``state_transitions`` — that the mapper does not produce. The result was
    that ``canonical_scenarios()`` returned 0 claims on every artefact run
    through the canonical pipeline, regardless of the model's structure.

    This implementation resolves the section names against the mapper-emitted
    shape first, falls back to the legacy short-form names for backward
    compatibility, and derives resources / queues from ``service_rates``
    entries when no dedicated sections are present.
    """
    out: list[dict] = []
    cs = compliance_spec or {}

    # ── Section resolution ──────────────────────────────────────────────────
    # Processes: mapper emits per-process info under `service_rates` (one
    # entry per service_process DSL element) and `periodic_processes`.
    procs = (
        cs.get("service_rates")
        or cs.get("processes")          # legacy short-form
        or []
    )
    # Augment with periodic_processes for duration/queue derivation.
    procs = list(procs) + list(cs.get("periodic_processes") or [])

    # Resources: when no dedicated `resources` section exists, derive
    # unique resource names from `service_rates` entries' `resources` field
    # or `resource` scalar.
    resources = cs.get("resources") or []
    if not resources:
        seen_res: set[str] = set()
        for p in procs:
            for r in (p.get("resources") or []) or []:
                if r and r not in seen_res:
                    resources.append({"name": r})
                    seen_res.add(r)
            r_scalar = p.get("resource")
            if r_scalar and r_scalar not in seen_res:
                resources.append({"name": r_scalar})
                seen_res.add(r_scalar)

    # State transitions: mapper emits `state_transition_rules`; legacy is
    # `state_transitions`.
    transitions = (
        cs.get("state_transition_rules")
        or cs.get("state_transitions")  # legacy
        or []
    )

    # Queues: no dedicated section is emitted by the mapper; derive one
    # implicit queue per process (the queue feeding that process's resource).
    queues = cs.get("queues") or []
    if not queues:
        for p in procs:
            qname = (p.get("name") or p.get("process")
                     or p.get("resource") or "")
            if qname:
                queues.append({"name": qname, "process": qname})

    # Eligibility and coverage rules: same name in mapper and legacy shapes.
    elig_rules = cs.get("eligibility_rules") or []
    cov_rules  = cs.get("coverage_rules") or []

    # Preemption: mapper emits `preemption_rules` as a section; each entry
    # has higher_priority / lower_priority. Convert to the (source, victim)
    # tuple the preemption_causality claim expects.
    preempt_section = cs.get("preemption_rules") or []
    preempt_procs = [p for p in procs if p.get("preempts")]

    # Soft pools: coordination_patterns entries emitted by compliance_mapper.py
    # carry the coordination kind under the `type` field (values include
    # `soft_pool`, `hard_pool`, `role_anchored`). Earlier drafts of this
    # generator looked only for `pool_type` / `pattern_type`, neither of which
    # the mapper ever emits — so canonical_soft_pool_scaling silently never
    # synthesized for any DSL processed through the framework's own mapper.
    # We now check `type` first (mapper-emitted), then the legacy variants,
    # then fall back to resources with `capacity_mode: soft` for older DSLs.
    coord_patterns = cs.get("coordination_patterns") or []
    def _is_soft_pool(c: dict) -> bool:
        for key in ("type", "pool_type", "pattern_type", "coordination_type"):
            v = (c.get(key) or "").lower()
            if v in ("soft_pool", "soft"):
                return True
        return (c.get("capacity_mode") or "").lower() == "soft"
    soft_pools_coord = [c for c in coord_patterns if _is_soft_pool(c)]
    soft_pools_res = [r for r in resources
                      if (r.get("capacity_mode") or "").lower() == "soft"]
    soft_pools = soft_pools_coord or soft_pools_res

    # ── Claim 0: workload_response (universal) ──────────────────────────────
    # Scale arrivals and expect the system's congestion-side metrics to
    # respond upward. This claim fires whenever an `arrivals` section is
    # declared (i.e. almost any discrete-event model). It uses the universal
    # override convention `arrival_rate_scale` and lists multiple candidate
    # metric names so it survives the sim-convention variation that the
    # duration- and discipline-specific claims below cannot.
    #
    # CANONICAL OVERRIDE CONTRACT
    # ---------------------------
    # For a sim to participate in canonical Phase 4.5 evidence, the
    # `run_simulation(config)` entry point should honour the following
    # override keys when present in `config`:
    #
    #   arrival_rate_scale : float          — multiply arrival rate(s) by this
    #   capacity           : dict[str,int]  — per-resource capacity override
    #   routing_probability: dict[...]      — per-routing-branch probability
    #   duration_scale     : dict[str,float]— per-process duration multiplier
    #   queue_discipline   : dict[str,str]  — per-queue discipline override
    #   processes_enabled  : dict[str,bool] — per-process on/off toggle
    #
    # `arrival_rate_scale` is the most universally-supported convention and
    # is the only override the canonical workload_response claim relies on.
    #
    # CANONICAL METRIC CONTRACT
    # -------------------------
    # The canonical claims read from `result["metrics"]`. The workload-
    # response claim is robust to which specific metric name the sim
    # exposes for system time / sojourn — it lists multiple candidates so
    # that at least one is typically present.
    arrivals_section = cs.get("arrivals") or cs.get("arrival_distribution") or []
    if arrivals_section:
        out.append({
            "name":               "canonical_workload_response",
            "scenario_category":  "workload_response",
            "scale_factor":       1.3,
            # AUDIT FIX (H8): SYNONYM SET, not a metrics list. The previous
            # shape listed 5 candidate metrics, each evaluated as a separate
            # ScenarioResult — but the codegen brief only requires ONE
            # sojourn-class metric, so ≥3 were absent by contract, producing
            # ≥3 authoritative INCONCLUSIVEs that capped the aggregate at
            # WARN on every brief-compliant sim. With metric_synonyms the
            # harness resolves the FIRST name present in the sim's report
            # and emits ONE result; only if none is present does it emit a
            # single INCONCLUSIVE.
            "metric_synonyms": [
                "mean_sojourn", "avg_system_time", "total_time_in_system",
                "mean_wait", "queue_length_mean",
            ],
            "expectation":        {"direction": "up", "min_effect_size": 0.05},
            "evaluation":         {"method": "bootstrap_mean_diff",
                                   "confidence": 0.95, "replications": 2000},
        })

    # ── Claim 1: preemption_causality ───────────────────────────────────────
    # If a process preempts another, turning it off should decrease
    # preemption_count (and typically relax the victim's SLA).
    if preempt_procs:
        proc = preempt_procs[0]
        targets = proc.get("preempts") or []
        victim = targets[0] if targets else None
        out.append({
            "name":               "canonical_preemption_causality",
            "scenario_category":  "coupling",
            "source_process":     proc.get("name"),
            "target_process":     victim,
            "metrics":            [{"name": "preemption_count", "direction": "up"}],
            "expectation":        {"direction": "up", "min_effect_size": 0.10},
            "evaluation":         {"method": "bootstrap_mean_diff",
                                   "confidence": 0.95, "replications": 2000},
        })
    elif preempt_section:
        # Derived from preemption_rules. Different DSL versions and
        # different mapper implementations emit the source/victim pair
        # under different field names. Try in order:
        #   higher_priority/lower_priority (v5.3 explicit pair)
        #   applies_to_processes[0] / preemptible_by[0] (v5.2 default)
        #   name / (first item in competing_risks) (legacy)
        rule = preempt_section[0]
        source_process = (
            rule.get("higher_priority")
            or (rule.get("applies_to_processes") or [None])[0]
            or rule.get("source_process")
            or rule.get("name")
        )
        target_process = (
            rule.get("lower_priority")
            or (rule.get("preemptible_by") or [None])[0]
            or rule.get("target_process")
            or rule.get("victim_process")
        )
        if source_process:
            out.append({
                "name":               "canonical_preemption_causality",
                "scenario_category":  "coupling",
                "source_process":     source_process,
                "target_process":     target_process,
                "metrics":            [{"name": "preemption_count", "direction": "up"}],
                "expectation":        {"direction": "up", "min_effect_size": 0.10},
                "evaluation":         {"method": "bootstrap_mean_diff",
                                       "confidence": 0.95, "replications": 2000},
            })

    # ── Claim 2: soft_pool_scaling ──────────────────────────────────────────
    if soft_pools:
        res = soft_pools[0]
        res_name = res.get("name") or res.get("resource") or "pool"
        out.append({
            "name":               "canonical_soft_pool_scaling",
            "scenario_category":  "workload_response",
            "scale_factor":       1.5,
            "metrics": [
                {"name": f"utilization.{res_name}", "direction": "up"},
                {"name": "loss_rate", "direction": "up", "min_effect_size": 0.05},
            ],
            "expectation":        {"direction": "up", "min_effect_size": 0.05},
            "evaluation":         {"method": "bootstrap_mean_diff",
                                   "confidence": 0.95, "replications": 2000},
        })

    # ── Claim 3: eligibility_enforcement ────────────────────────────────────
    if elig_rules:
        rule = elig_rules[0]
        rname = rule.get("name") or "elig"
        out.append({
            "name":               f"canonical_eligibility_{rname}",
            "scenario_category":  "emergent_pattern",
            "control":    {"parameters": {"eligibility_enforced": {rname: True}}},
            "treatment":  {"parameters": {"eligibility_enforced": {rname: False}}},
            "metrics": [{"name": "eligibility_violations",
                         "direction": "up", "min_effect_size": 0.01}],
            "expectation":        {"direction": "up", "min_effect_size": 0.01},
            "evaluation":         {"method": "bootstrap_mean_diff",
                                   "confidence": 0.95, "replications": 2000},
        })

    # ── Claim 4: duration_semantics ─────────────────────────────────────────
    # Mapper-emitted service_rates entries carry duration info under
    # `mean_service_seconds` / `service_time` / `duration_distribution`
    # depending on the DSL version. Accept any of these as evidence the
    # process has a duration the canonical scenario can scale.
    procs_dur = [p for p in procs
                 if p.get("duration_distribution")
                 or p.get("mean_service_seconds")
                 or p.get("service_time")
                 or p.get("duration_seconds")]
    if procs_dur:
        proc = procs_dur[0]
        pname = (proc.get("name") or proc.get("process")
                 or proc.get("resource") or "proc")
        out.append({
            "name":               f"canonical_duration_{pname}",
            "scenario_category":  "emergent_pattern",
            "control":    {"parameters": {"duration_scale": {pname: 1.0}}},
            "treatment":  {"parameters": {"duration_scale": {pname: 1.5}}},
            "metrics": [{"name": f"mean_duration.{pname}", "direction": "up"}],
            "expectation":        {"direction": "up", "min_effect_size": 0.30},
            "evaluation":         {"method": "paired_crn_median_diff",
                                   "confidence": 0.95, "replications": 2000},
        })

    # ── Claim 5: state_transition_sensitivity ───────────────────────────────
    # The mapper's `state_transition_rules` entries carry the state under
    # `state` (v5.3) with source_ids for provenance. Older schemas used
    # `name` or `from_state`. Try all in order so the claim can synthesize
    # a meaningful label regardless of mapper version.
    if transitions:
        t = transitions[0]
        tname = (
            t.get("name")
            or t.get("from_state")
            or t.get("state")
            or (t.get("source_ids") or [None])[0]
            or "transition"
        )
        out.append({
            "name":               f"canonical_transition_{tname}",
            "scenario_category":  "emergent_pattern",
            "control":    {"parameters": {"transition_disabled": {tname: True}}},
            "treatment":  {"parameters": {"transition_disabled": {tname: False}}},
            "metrics": [{"name": f"transitions.{tname}", "direction": "up"}],
            "expectation":        {"direction": "up", "min_effect_size": 0.05},
            "evaluation":         {"method": "bootstrap_mean_diff",
                                   "confidence": 0.95, "replications": 2000},
        })

    # ── Claim 6: queue_discipline ───────────────────────────────────────────
    # AUDIT FIX (H9): the previous claim expected the queue's OVERALL p95
    # wait to DROP under priority discipline vs FIFO. Queueing theory says
    # otherwise: for a work-conserving non-preemptive discipline switch with
    # service-time-independent priorities, the conservation law fixes the
    # mean wait, priority redistributes wait from high- to low-priority
    # customers, and FIFO minimizes waiting-time variance in this class — so
    # the overall p95 under priority is typically ≥ FIFO's. The old claim
    # could FAIL a CORRECT sim on a wrong-theory expectation. The
    # theoretically defensible claim targets the HIGH-PRIORITY CLASS's p95,
    # which priority scheduling does reduce. Requires the per-class metric
    # `wait_time_p95.<queue>.high_priority` (documented in CODEGEN_BRIEF);
    # sims not exposing it produce a single INCONCLUSIVE, not a FAIL.
    if queues:
        q = queues[0]
        qname = q.get("name") or "queue"
        out.append({
            "name":               f"canonical_queue_discipline_{qname}",
            "scenario_category":  "emergent_pattern",
            "control":    {"parameters": {"queue_discipline": {qname: "FIFO"}}},
            "treatment":  {"parameters": {"queue_discipline": {qname: "priority"}}},
            "metrics": [{"name": f"wait_time_p95.{qname}.high_priority",
                         "direction": "down"}],
            "expectation":        {"direction": "down", "min_effect_size": 0.05},
            "evaluation":         {"method": "paired_crn_median_diff",
                                   "confidence": 0.95, "replications": 2000},
        })

    # ── Claim 7: coverage_guarantees ────────────────────────────────────────
    if cov_rules:
        c = cov_rules[0]
        etype = c.get("entity_type") or "entity"
        out.append({
            "name":               f"canonical_coverage_{etype}",
            "scenario_category":  "workload_response",
            "scale_factor":       2.0,
            "metrics": [{"name": f"coverage.{etype}", "direction": "down"}],
            "expectation":        {"direction": "down", "min_effect_size": 0.05},
            "evaluation":         {"method": "bootstrap_mean_diff",
                                   "confidence": 0.95, "replications": 2000},
        })

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Per-claim orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def _eval_one(
    *,
    spec: dict,
    driver: Callable[[dict], tuple[dict, dict]],
    base_config: dict,
    run_simulation: Callable,
    metric_extractor: Callable[[dict], dict],
    seeds: list[int],
    default_category: str,
    default_provenance: str = "declared",
) -> list[ScenarioResult]:
    """Run ONE claim.  Returns one ScenarioResult per metric listed in spec."""
    name = spec.get("name") or f"{default_category}_{id(spec)}"
    category = spec.get("scenario_category") or default_category
    ev = _evaluation(spec)
    claim_exp = _expectation(spec)
    metric_names = _metrics_list(spec)
    provenance = _provenance(spec, default_provenance)
    advisory = provenance in _ADVISORY_PROVENANCE

    def _block(metric: str, reason: str) -> ScenarioResult:
        return ScenarioResult(
            name=name, category=category, metric=metric, method=ev["method"],
            expected_direction=(claim_exp.get("direction") or "?"),
            expected_magnitude=claim_exp.get("min_effect_size"),
            delta_mean=0.0, relative_change=0.0, ci_95=(0.0, 0.0),
            confidence=ev["confidence"], direction_pass=False, magnitude_pass=False,
            verdict="BLOCK", message=reason, n_seeds=0,
            provenance=provenance, advisory=advisory,
        )

    try:
        c_ovr, t_ovr = driver(spec)
    except Exception as exc:
        return [_block(metric_names[0], f"Driver raised {type(exc).__name__}: {exc}")]

    ctrl_cfg  = _apply_overrides(base_config, c_ovr)
    treat_cfg = _apply_overrides(base_config, t_ovr)

    try:
        ctrl_samples  = _run_cell(run_simulation, ctrl_cfg,  seeds, metric_extractor)
        treat_samples = _run_cell(run_simulation, treat_cfg, seeds, metric_extractor)
    except Exception as exc:
        return [_block(metric_names[0], f"Simulation raised {type(exc).__name__}: {exc}")]

    # Synonym-set resolution (audit H8): a claim may declare
    # `metric_synonyms: [...]` instead of `metrics: [...]`. The names are
    # SYNONYMS for one underlying quantity under different sim naming
    # conventions — evaluate exactly ONE result: the first name present in
    # BOTH cells' samples. Only when none is present does the claim emit a
    # single INCONCLUSIVE (rather than one per absent name, which
    # previously guaranteed authoritative INCONCLUSIVEs that capped the
    # aggregate at WARN for every brief-compliant sim).
    synonyms = spec.get("metric_synonyms")
    if isinstance(synonyms, list) and synonyms:
        resolved = next(
            (m for m in synonyms
             if ctrl_samples.get(m) and treat_samples.get(m)), None)
        if resolved is None:
            return [ScenarioResult(
                name=name, category=category,
                metric=f"(none of {len(synonyms)} synonyms present)",
                method=ev["method"],
                expected_direction=(claim_exp.get("direction") or "?"),
                expected_magnitude=claim_exp.get("min_effect_size"),
                delta_mean=0.0, relative_change=0.0, ci_95=(0.0, 0.0),
                confidence=ev["confidence"], direction_pass=False,
                magnitude_pass=False, verdict="INCONCLUSIVE",
                message=(f"None of the synonym metrics {synonyms} present in "
                         f"the sim's report — expose one of them to enable "
                         f"this claim."),
                n_seeds=len(seeds), provenance=provenance, advisory=advisory,
            )]
        metric_names = [resolved]

    out: list[ScenarioResult] = []
    for metric in metric_names:
        c_vals = ctrl_samples.get(metric) or []
        t_vals = treat_samples.get(metric) or []
        per_exp = _per_metric_expectation(spec, metric, claim_exp)

        # Magnitude licensing: only an authoritative scenario (DSL-declared or
        # SME-approved) may assert a magnitude. An auto-generated (unapproved)
        # scenario is forced direction-only — nobody has vouched for the size of
        # the effect, so a precise magnitude would be a guess the test then
        # conveniently meets.
        if advisory and per_exp.get("min_effect_size") is not None:
            per_exp = {**per_exp, "min_effect_size": None}

        if not c_vals or not t_vals:
            out.append(ScenarioResult(
                name=name, category=category, metric=metric, method=ev["method"],
                expected_direction=per_exp["direction"],
                expected_magnitude=per_exp["min_effect_size"],
                delta_mean=0.0, relative_change=0.0, ci_95=(0.0, 0.0),
                confidence=ev["confidence"], direction_pass=False, magnitude_pass=False,
                verdict="INCONCLUSIVE",
                message=f"Metric '{metric}' absent from metrics dict in one or both cells.",
                n_seeds=len(seeds), provenance=provenance, advisory=advisory,
            ))
            continue

        res = _evaluate_claim(
            c_vals, t_vals, per_exp,
            method=ev["method"], confidence=ev["confidence"],
            replications=ev["replications"],
        )
        message = res["message"]
        if (res["verdict"] == "INCONCLUSIVE" and "straddles zero" in message
                and len(seeds) < _MIN_RECOMMENDED_SEEDS):
            message += (f" [low power: {len(seeds)} seeds < {_MIN_RECOMMENDED_SEEDS} "
                        "recommended]")
        out.append(ScenarioResult(
            name=name, category=category, metric=metric, method=ev["method"],
            expected_direction=per_exp["direction"],
            expected_magnitude=per_exp["min_effect_size"],
            delta_mean=res["delta_mean"], relative_change=res["relative_change"],
            ci_95=res["ci_95"], confidence=ev["confidence"],
            direction_pass=res["direction_pass"],
            magnitude_pass=res["magnitude_pass"],
            verdict=res["verdict"], message=message,
            n_seeds=len(seeds), provenance=provenance, advisory=advisory,
            control_samples=c_vals, treatment_samples=t_vals,
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Public entry points
# ─────────────────────────────────────────────────────────────────────────────

def run_scenario(
    *,
    base_config: dict,
    scenario: dict,
    run_fn: Callable,
    category: str | None = None,
    metric_extractor: Callable[[dict], dict] | None = None,
    seeds: list[int] | None = None,
) -> list[ScenarioResult]:
    """Run a single claim (GPT-template signature).

    The driver is selected by ``scenario['scenario_category']`` or the
    explicit ``category`` argument; unknown categories fall back to
    ``_driver_semantic_scenario``.
    """
    metric_extractor = metric_extractor or (lambda r: r.get("metrics", {}))
    seeds = seeds or list(_DEFAULT_SEEDS)
    cat = category or scenario.get("scenario_category") or "emergent_pattern"
    driver = _DRIVERS.get(cat, _driver_semantic_scenario)
    return _eval_one(
        spec=scenario, driver=driver, base_config=base_config,
        run_simulation=run_fn, metric_extractor=metric_extractor,
        seeds=seeds, default_category=cat,
    )


def run_phase4_5(
    *,
    base_config: dict,
    compliance_spec: dict[str, list],
    run_simulation: Callable,
    metric_extractor: Callable[[dict], dict] | None = None,
    seeds: list[int] | None = None,
    include_canonical: bool = False,
) -> list[ScenarioResult]:
    """Run all declared Phase 4.5 counterfactuals.

    When ``include_canonical=True`` the 7-claim canonical library is also
    synthesized from the DSL and appended.
    """
    metric_extractor = metric_extractor or (lambda r: r.get("metrics", {}))
    seeds = seeds or list(_DEFAULT_SEEDS)

    results: list[ScenarioResult] = []
    claim_slots: list[tuple[str, Callable, str]] = [
        ("semantic_scenarios",  _driver_semantic_scenario, "emergent_pattern"),
        ("coupling_rules",      _driver_coupling_rule,     "coupling"),
        ("control_policies",    _driver_control_policy,    "control_policy"),
        ("policy_thresholds",   _driver_policy_threshold,  "policy_threshold"),
        ("escalations",         _driver_escalation,        "escalation"),
        ("workload_responses",  _driver_workload_response, "workload_response"),
    ]

    # DSL-declared scenarios default to "declared" (authoritative). A scenario
    # may self-label provenance="auto_generated" / "sme_approved" to override.
    for key, driver, cat in claim_slots:
        for spec in compliance_spec.get(key, []) or []:
            results.extend(_eval_one(
                spec=spec, driver=driver, base_config=base_config,
                run_simulation=run_simulation, metric_extractor=metric_extractor,
                seeds=seeds, default_category=cat, default_provenance="declared",
            ))

    if include_canonical:
        for spec in canonical_scenarios(compliance_spec):
            cat = spec.get("scenario_category", "emergent_pattern")
            driver = _DRIVERS.get(cat, _driver_semantic_scenario)
            results.extend(_eval_one(
                spec=spec, driver=driver, base_config=base_config,
                run_simulation=run_simulation, metric_extractor=metric_extractor,
                seeds=seeds, default_category=cat, default_provenance="canonical",
            ))

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────

_STATUS_ICON = {"PASS": "✓", "FAIL": "✗", "INCONCLUSIVE": "?", "BLOCK": "⊘"}


def aggregate_verdict(results: list[ScenarioResult]) -> str:
    """Roll up Phase 4.5 results into a single verdict, weighted by provenance.

    Authoritative scenarios (declared / sme_approved / canonical) govern the
    verdict. Advisory scenarios (auto_generated, LLM-proposed but not human-
    approved) can only *flag* problems — they can raise the result to WARN but
    can never produce a clean PASS on their own and never escalate to FAIL/BLOCK.

    Precedence:
      BLOCK               — any AUTHORITATIVE BLOCK
      FAIL                — any AUTHORITATIVE FAIL
      WARN                — any AUTHORITATIVE INCONCLUSIVE, OR any advisory
                            FAIL/BLOCK/INCONCLUSIVE (advisory issues cap at WARN)
      PASS                — at least one authoritative scenario and all
                            authoritative scenarios PASS (advisory all-PASS too)
      PASS_ADVISORY_ONLY  — only advisory scenarios ran and all PASS (no
                            authoritative evidence — not a clean credibility pass)
      PASS_NO_SCENARIOS   — nothing ran
    """
    if not results:
        return "PASS_NO_SCENARIOS"

    authoritative = [r for r in results if not r.advisory]
    advisory = [r for r in results if r.advisory]

    if any(r.verdict == "BLOCK" for r in authoritative):
        return "BLOCK"
    if any(r.verdict == "FAIL" for r in authoritative):
        return "FAIL"
    # Advisory problems and authoritative inconclusives both cap at WARN.
    if any(r.verdict == "INCONCLUSIVE" for r in authoritative):
        return "WARN"
    if any(r.verdict in ("FAIL", "BLOCK", "INCONCLUSIVE") for r in advisory):
        return "WARN"
    # No problems anywhere → PASS, but only authoritative evidence earns a clean
    # pass. Advisory-only evidence is explicitly weaker.
    if authoritative:
        return "PASS"
    return "PASS_ADVISORY_ONLY"


def print_phase4_5_summary(results: list[ScenarioResult],
                           label: str = "PHASE 4.5") -> str:
    print()
    print("=" * 70)
    print(f"{label} — Semantic Counterfactual Harness ({len(results)} scenarios)")
    print("=" * 70)

    if not results:
        print("  (no semantic claims declared in compliance_spec)")
        return "PASS_NO_SCENARIOS"

    by_cat: dict[str, list[ScenarioResult]] = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r)

    for cat, rs in by_cat.items():
        print(f"\n[{cat}]  ({len(rs)} scenario(s))")
        for r in rs:
            icon = _STATUS_ICON.get(r.verdict, "?")
            lo, hi = r.ci_95 or (0.0, 0.0)
            tier = "advisory" if r.advisory else r.provenance
            print(f"  {icon} {r.name}  metric='{r.metric}' "
                  f"{r.expected_direction}  "
                  f"Δ={r.delta_mean:+.4g} ({r.relative_change:+.2%})  "
                  f"CI95=[{lo:+.4g}, {hi:+.4g}]  "
                  f"[{r.method}; {tier}]")
            print(f"      {r.message}")

    verdict = aggregate_verdict(results)
    n_adv = sum(1 for r in results if r.advisory)
    n_auth = len(results) - n_adv
    n_incon = sum(1 for r in results if r.verdict == "INCONCLUSIVE")
    print()
    print(f"Tiers: {n_auth} authoritative, {n_adv} advisory (auto-generated).")
    if n_incon:
        print(f"Note: {n_incon} scenario(s) INCONCLUSIVE — no detectable effect; "
              "for those, increasing seeds may resolve direction.")
    if verdict == "PASS_ADVISORY_ONLY":
        print("Note: all evidence is advisory (auto-generated) — this is NOT a "
              "clean credibility pass. Have an SME approve the key scenarios to "
              "earn authoritative weight.")
    print()
    print(f"Aggregate Phase 4.5 verdict: {verdict}")
    print("=" * 70)
    return verdict
