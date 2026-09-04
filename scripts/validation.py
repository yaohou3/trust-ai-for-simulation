"""
validation.py — Generic Phase 1-4 validators for the V²UQ trust framework.

Companion to phase0_vvuq.py. This module implements Phases 1-4 in a
case-agnostic form: each validator takes a generated simulation module
and emits standardized JSON records per check.

Standardized record shape
=========================
    CheckRecord = {
      "check_id":           str,                   # e.g. "1.2.bed_queue.fifo"
      "status":             "PASS" | "WARN" | "FAIL" | "BLOCK" | "INFO",
      "diagnostic":         str,                   # human-readable
      "evidence":           dict,                  # numeric supporting data
      "suspected_location": str | None,            # repair hint
    }

    PhaseReport = {
      "phase_id": str,                             # e.g. "phase1"
      "status":   str,                             # aggregate
      "tally":    {PASS, WARN, FAIL, BLOCK, INFO},
      "elapsed_s": float,
      "results":  [CheckRecord, ...],
      "notes":    str,
    }

Coverage matches the depth of the case-specific Phase 1-3 validators that
the framework produces for individual studies, generalised so the same
checks fire on any simulation that emits the canonical trace contract.

Self-healing intentionally is NOT inside the validators here. The validator
detects and localises the failure; an external orchestrator
(self_heal_orchestrator.py) loops { run validator → on FAIL hand the
structured failure to an LLM (Claude Code) → patch sim → re-run validator
→ require deterministic PASS }. Keeping repair out of the validator means
the LLM cannot mark its own homework: only this file can issue PASS.

Phase coverage
==============

Phase 1 — Mathematical verification
  1.1 SYSTEM
      1.1.1 trace_integrity        — required fields + monotone time + strict seq
      1.1.2 set_based_conservation — A = D + L_terminal + I(T)
      1.1.3 entity_lifecycle       — exactly one arrival/entity; ordering
      1.1.4 little_law_system      — L = λW within tolerance
  1.2 QUEUE  (per discovered queue)
      1.2.{q}.conservation         — enters / exits balance; no dup/orphan
      1.2.{q}.fifo                 — exit order matches enter order
      1.2.{q}.little               — Lq = λq × W̄q within tolerance
  1.3 RESOURCE (per discovered resource)
      1.3.{r}.state_machine        — service_start/end paired by entity
      1.3.{r}.capacity             — peak concurrency ≤ declared capacity
      1.3.{r}.util_identity        — ρ ≈ λ × E[S] / c
      1.3.{r}.throughput_bound     — λ ≤ c / E[S]
      1.3.{r}.service_pairing      — no orphan service_end

Phase 2 — Validation
  2.1 mmc_applicability            — auto-gate; INFO if non-MMC
  2.1.{r} (when gated)             — Erlang-C on the resource
  2.2.1 limiting_zero_load         — λ → 0  ⇒  W → E[S], Wq → 0
  2.2.2 limiting_heavy_load        — ρ ≈ 0.95 ⇒ Wq ≥ 5·E[S]
  2.3.1 extreme_zero_capacity      — cap=0 on a resource ⇒ either losses
                                     or queue blowup with no service starts
  2.3.2 extreme_infinite_capacity  — cap=∞ ⇒ Wq → 0

Phase 3 — Uncertainty Quantification
  3.1.1 warmup_config_support      — config exposes warmup_time
  3.1.2 welch_method               — Welch's procedure on N replications,
                                     locate cumulative-mean stabilization
  3.1.3 mser5_method               — MSER-5 truncation point on mean trace
  3.2.1 independent_seeds          — different seeds → different per-entity
                                     sojourn statistics
  3.2.2 ci_construction            — multi-rep CI half-width / mean ratio
  3.3.1 same_seed_determinism      — same seed → identical sojourn map
  3.3.2 paired_seed_crn_arrivals   — paired runs share an arrival prefix
  3.3.3 crn_variance_reduction     — paired-seed variance < unpaired
  3.4.1 perturbable_parameters     — canonical config keys exposed
  3.4.2 sensitivity_arrival_rate   — +20% λ produces measurable W increase

Phase 4 — Sensitivity & face validity
  4.1 oat_arrival_rate             — sweep arrival rate; monotone non-decreasing W
  4.2 oat_capacity                 — sweep one resource's capacity; monotone non-increasing W
  4.3 face_validity                — utilization ∈ [0,1]; departures ≤ arrivals; times ≥ 0
  4.4 effect_size                  — INFO summary of OAT response sizes

Anything that requires domain reasoning (Phase 4 scenario-gap identification,
Phase 5 plausibility) intentionally is NOT in this file — those are LLM
phases by design.

Usage
=====
    from validation import (
        validate_phase1, validate_phase2, validate_phase3, validate_phase4,
        report_to_json, report_to_markdown
    )
    import importlib.util as iu
    spec = iu.spec_from_file_location("sim", "/path/to/generated_sim.py")
    sim = iu.module_from_spec(spec); spec.loader.exec_module(sim)
    rep = validate_phase1(sim)
    print(report_to_markdown(rep))
"""

from __future__ import annotations

import json
import math
import statistics
import time
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Iterable

# Residual-violation categorization + architectural-implication tagging.
# Lazy-imported inside the checks that use it so older deployments that
# lack residual_diagnostics.py do not fail to load validation.py.
try:
    from residual_diagnostics import (
        ViolationRecord,
        classify_residual_barrier,
        architectural_implication_for_barrier,
        diagnostic_extension as _residual_diag_extension,
        clusters_to_dict as _clusters_to_dict,
        implication_to_dict as _implication_to_dict,
    )
    _RESIDUAL_DIAG_AVAILABLE = True
except Exception:  # pragma: no cover — fall through to legacy behaviour
    _RESIDUAL_DIAG_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Standardized record shape
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CheckRecord:
    """One finding from any validator. Shape designed to match the JSON
    schema the orchestrator and the JSX parser both consume."""
    check_id: str
    status: str   # PASS | WARN | FAIL | BLOCK | INFO
    diagnostic: str
    evidence: dict[str, Any] = field(default_factory=dict)
    suspected_location: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PhaseReport:
    """Top-level report for a single phase."""
    phase_id: str
    status: str
    tally: dict[str, int]
    results: list[CheckRecord]
    elapsed_s: float = 0.0
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["results"] = [r if isinstance(r, dict) else r for r in d["results"]]
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _tally(records: list[CheckRecord]) -> dict[str, int]:
    c = Counter(r.status for r in records)
    return {k: c.get(k, 0) for k in ("PASS", "INFO", "WARN", "FAIL", "BLOCK")}


def _aggregate_status(tally: dict[str, int]) -> str:
    if tally.get("BLOCK", 0) > 0: return "BLOCK"
    if tally.get("FAIL", 0) > 0:  return "FAIL"
    if tally.get("WARN", 0) > 0:  return "WARN"
    return "PASS"


def _safe_run(sim_module, config: dict | None,
              check_id: str, label: str) -> tuple[Any, CheckRecord | None]:
    try:
        cfg = config or getattr(sim_module, "DEFAULT_CONFIG", {})
        result = sim_module.run_simulation(dict(cfg))
        return result, None
    except Exception as exc:  # noqa: BLE001
        rec = CheckRecord(
            check_id=check_id,
            status="BLOCK",
            diagnostic=f"run_simulation raised {type(exc).__name__}: {exc} (label={label})",
            evidence={"traceback": traceback.format_exc(limit=3)},
            suspected_location="sim_module.run_simulation",
        )
        return None, rec


def _resources_from_config(cfg: dict) -> dict[str, dict]:
    """Best-effort extraction of resource definitions from a config dict.
    Accepts both `{"server": 1}` and `{"server": {"capacity": 1}}` shapes."""
    raw = cfg.get("resources", {}) or {}
    norm: dict[str, dict] = {}
    for name, spec in raw.items():
        if isinstance(spec, dict):
            norm[name] = dict(spec)
        else:
            norm[name] = {"capacity": spec}
    return norm


def _simulation_regime(cfg: dict) -> dict:
    """Return the declared simulation regime as a normalized dict.

    Reads ``cfg['simulation_regime']`` and returns a dict with at least
    ``type`` present. Defaults to ``{'type': 'steady_state'}`` when the
    field is absent, preserving backward compatibility with existing
    models that do not declare a regime.

    Downstream checks that assume steady-state behaviour (Little's Law
    utilization identity §1.3, extreme-capacity divergence §2.3.1,
    warm-up detection §3.1.3, arrival-rate windowing B01) should consult
    this via :func:`_is_terminating` to decide whether to apply their
    steady-state variant, apply a terminating-aware variant, or skip
    with INFO.

    See the Sargent-canonical distinction between terminating and
    non-terminating simulations. MASCAL / mass-casualty and other
    single-incident burst systems are terminating; ICU digital-twin
    and manufacturing continuous-flow systems are non-terminating.
    """
    reg = cfg.get("simulation_regime") or {}
    if not isinstance(reg, dict):
        return {"type": "steady_state"}
    t = (reg.get("type") or "steady_state").lower()
    if t not in ("steady_state", "terminating", "burst"):
        t = "steady_state"
    return {
        "type": t,
        "arrival_window_seconds": reg.get("arrival_window_seconds"),
        "total_entities": reg.get("total_entities"),
        "rationale": reg.get("rationale"),
    }


def _is_terminating(cfg: dict) -> bool:
    """True if the simulation regime is ``terminating`` or ``burst``.

    A helper distinct from :func:`_simulation_regime` so that the intent
    at the call site is clear: *"skip the steady-state variant of this
    check when the declared regime is terminating."*"""
    return _simulation_regime(cfg)["type"] in ("terminating", "burst")


def _arrival_window_seconds(cfg: dict) -> float | None:
    """For burst regimes, return the declared arrival window in seconds.

    Rate-based checks (B01, §1.3 utilization) that need to distinguish
    the arrival-active period from a subsequent drain phase use this to
    select a measurement basis narrower than the full ``run_length``.
    Returns None when no window is declared."""
    reg = _simulation_regime(cfg)
    w = reg.get("arrival_window_seconds")
    if w is None:
        return None
    try:
        return float(w)
    except (TypeError, ValueError):
        return None


def _resource_capacity(cfg: dict, name: str) -> int | None:
    res = _resources_from_config(cfg)
    if name in res:
        return res[name].get("capacity")
    return None


def _set_capacity(resources: dict, name: str, cap) -> dict:
    """Return a copy of `resources` with `name`'s capacity set to `cap`,
    PRESERVING the original entry's representation: a scalar stays a scalar
    (so a sim that declared resources as plain ints receives an int), a dict
    stays a dict. The validator must not impose a shape the sim didn't
    declare — handing an int-shape sim a {"capacity": ...} dict crashes its
    int(cap) before any phase can run. When the resource is new (absent),
    default to a scalar, the simplest shape."""
    rs = dict(resources or {})
    entry = rs.get(name)
    if isinstance(entry, dict):
        new = dict(entry); new["capacity"] = cap; rs[name] = new
    else:
        rs[name] = cap
    return rs


def _trace_horizon(trace: list[dict]) -> tuple[float, float]:
    """Return (t_first, t_last) across the trace; (0, 0) if empty."""
    if not trace:
        return (0.0, 0.0)
    ts = [ev.get("time", 0.0) for ev in trace]
    return (min(ts), max(ts))


def _entity_sojourn(trace: list[dict]) -> dict[str, float]:
    arr: dict[str, float] = {}
    dep: dict[str, float] = {}
    for ev in trace:
        eid = ev.get("entity_id")
        if not eid:
            continue
        if ev.get("event") == "system_arrival":
            arr.setdefault(eid, ev["time"])
        elif ev.get("event") == "system_departure":
            dep.setdefault(eid, ev["time"])
    return {eid: dep[eid] - arr[eid] for eid in dep if eid in arr}


def _avg_census(trace: list[dict], horizon_end: float | None = None) -> float:
    """Time-weighted mean number of in-system entities."""
    deltas: list[tuple[float, int]] = []
    for ev in trace:
        if ev.get("event") == "system_arrival":
            deltas.append((ev["time"], +1))
        elif ev.get("event") in ("system_departure", "loss"):
            deltas.append((ev["time"], -1))
    if not deltas:
        return 0.0
    deltas.sort()
    t_start = deltas[0][0]
    t_end = horizon_end if horizon_end is not None else deltas[-1][0]
    horizon = max(t_end - t_start, 1e-9)
    census = 0
    weighted = 0.0
    last_t = t_start
    for t, d in deltas:
        weighted += census * (t - last_t)
        census += d
        last_t = t
    return weighted / horizon


def _discover_queues(trace: list[dict]) -> list[str]:
    """Distinct queue names from queue_enter / queue_exit events."""
    qs = {ev.get("queue") for ev in trace
          if ev.get("event") in ("queue_enter", "queue_exit") and ev.get("queue")}
    qs.discard(None)
    return sorted(qs)


def _discover_resources(trace: list[dict]) -> list[str]:
    """Distinct resources from service_start / service_end / preempt / resume events."""
    rs = {ev.get("resource") for ev in trace
          if ev.get("event") in ("service_start", "service_end", "preempt", "resume")
          and ev.get("resource")}
    rs.discard(None)
    return sorted(rs)


def _service_intervals(trace: list[dict], resource: str | None = None
                       ) -> list[tuple[float, float, str]]:
    """Per-entity (start, end, entity_id) intervals on `resource` (or all
    resources if None). Unpaired starts are dropped; unpaired ends are
    counted by the caller via _orphan_service_ends."""
    open_starts: dict[tuple[str, str], float] = {}
    intervals: list[tuple[float, float, str]] = []
    for ev in sorted(trace, key=lambda e: (e.get("time", 0.0), e.get("seq", 0))):
        evt = ev.get("event")
        res = ev.get("resource")
        eid = ev.get("entity_id")
        if resource is not None and res != resource:
            continue
        if evt in ("service_start", "resume"):
            open_starts[(res, eid)] = ev["time"]
        elif evt in ("service_end", "preempt"):
            t0 = open_starts.pop((res, eid), None)
            if t0 is not None:
                intervals.append((t0, ev["time"], eid))
    return intervals


def _orphan_service_ends(trace: list[dict], resource: str | None = None) -> int:
    open_starts: dict[tuple[str, str], int] = defaultdict(int)
    orphans = 0
    for ev in sorted(trace, key=lambda e: (e.get("time", 0.0), e.get("seq", 0))):
        evt = ev.get("event")
        res = ev.get("resource")
        eid = ev.get("entity_id")
        if resource is not None and res != resource:
            continue
        if evt in ("service_start", "resume"):
            open_starts[(res, eid)] += 1
        elif evt in ("service_end", "preempt"):
            if open_starts[(res, eid)] > 0:
                open_starts[(res, eid)] -= 1
            else:
                orphans += 1
    return orphans


def _peak_concurrent(intervals: list[tuple[float, float, str]]) -> int:
    if not intervals:
        return 0
    events: list[tuple[float, int]] = []
    for s, e, _ in intervals:
        events.append((s, +1))
        events.append((e, -1))
    events.sort()
    cur = peak = 0
    for _t, d in events:
        cur += d
        peak = max(peak, cur)
    return peak


def _busy_time(intervals: list[tuple[float, float, str]]) -> float:
    """Total busy time across all engaged units (sum of interval lengths)."""
    return sum(max(e - s, 0.0) for s, e, _ in intervals)


def _post_warmup_busy_time(intervals: list[tuple[float, float, str]],
                            warmup: float) -> float:
    """Sum of interval lengths CLIPPED to [warmup, ∞). Used by the trace-vs-
    metrics consistency checks (§1.4) so that the trace-derived busy time is
    comparable to the metrics-reported busy time, which is also computed post-
    warmup."""
    if warmup <= 0:
        return _busy_time(intervals)
    total = 0.0
    for s, e, _ in intervals:
        s_c = max(s, warmup)
        if e > s_c:
            total += e - s_c
    return total


def _admitted_post_warmup(trace: list[dict], warmup: float) -> int:
    """Count of system_arrival events post-warmup that subsequently produced any
    service event other than the arrival itself (i.e. were actually admitted).
    Falls back to system_arrival count if no admission marker is present.
    Used by §1.4 cadence checks as the per-patient denominator."""
    arrivals = [ev for ev in trace
                if ev.get("event") == "system_arrival"
                and float(ev.get("time", 0.0)) >= warmup]
    if not arrivals:
        return 0
    admitted_eids: set[str] = set()
    for ev in trace:
        if ev.get("event") not in ("queue_enter", "service_start"):
            continue
        eid = ev.get("entity_id")
        if eid:
            admitted_eids.add(eid)
    n_admitted = sum(1 for a in arrivals if a.get("entity_id") in admitted_eids)
    return n_admitted or len(arrivals)


def _discover_recurring_obligations(cs: dict, dsl: dict | None = None) -> list[dict]:
    """Return a normalized list of declared recurring activities, regardless of
    where in the spec they live. Looks at, in order:
        compliance_spec.recurring_obligations   (canonical)
        compliance_spec.recurring_activities    (alias)
        dsl.dsl_elements[*].element_type == 'recurring_activity'  (fallback)
    Each entry is normalized to a dict with keys:
        id, process, role (optional), every_hours, expected_per_admitted_per_day,
        tolerance.
    """
    out: list[dict] = []
    src = (cs.get("recurring_obligations") or cs.get("recurring_activities") or [])
    for e in src:
        freq = e.get("frequency") or {}
        eh = e.get("every_hours") or freq.get("every_hours")
        if eh is None:
            continue
        out.append({
            "id": (e.get("source_ids") or [e.get("name", "?")])[0],
            "process": e.get("process") or "",
            "role": e.get("role") or "",
            "every_hours": float(eh),
            "expected_per_admitted_per_day": float(
                e.get("expected_per_admitted_patient_per_day", 24.0 / float(eh))),
            "tolerance": float(e.get("tolerance", 0.15)),
        })
    if out:
        return out
    # Fallback: scan dsl_elements directly.
    elements = (dsl or {}).get("dsl_elements", []) or []
    for e in elements:
        if e.get("element_type") != "recurring_activity":
            continue
        freq = e.get("frequency") or {}
        eh = freq.get("every_hours")
        if eh is None:
            continue
        out.append({
            "id": e.get("id", e.get("name", "?")),
            "process": e.get("process") or "",
            "role": e.get("role") or "",
            "every_hours": float(eh),
            "expected_per_admitted_per_day": float(
                e.get("expected_per_admitted_patient_per_day", 24.0 / float(eh))),
            "tolerance": float(e.get("tolerance", 0.15)),
        })
    return out


def _implicit_hold_intervals(trace: list[dict]) -> list[tuple[float, float, str]]:
    """Reconstruct (acquire, release, entity_id) holding intervals for a
    resource that is held implicitly across the whole sojourn — i.e. a
    resource with no service_start/service_end events of its own (the
    canonical example is an ICU bed, held from admission to departure).

    Acquire = queue_exit time if the entity queued, else system_arrival.
    Release = system_departure (or loss). Entities still in-system at the
    run end are left open-ended at the last event time.

    This is the generic equivalent of the case-specific 'beds' block that
    hand-written validators reconstruct: it lets the same per-resource
    checks fire on resources held implicitly, without naming the resource."""
    arrive: dict[str, float] = {}
    release: dict[str, float] = {}
    queue_exit: dict[str, float] = {}
    for ev in trace:
        eid = ev.get("entity_id")
        if not eid:
            continue
        e = ev.get("event")
        if e == "system_arrival":
            arrive.setdefault(eid, ev["time"])
        elif e == "queue_exit":
            queue_exit[eid] = ev["time"]
        elif e in ("system_departure", "loss"):
            release.setdefault(eid, ev["time"])
    intervals: list[tuple[float, float, str]] = []
    for eid, t_arr in arrive.items():
        acquire_t = queue_exit.get(eid, t_arr)
        release_t = release.get(eid)
        if release_t is not None and release_t >= acquire_t:
            intervals.append((acquire_t, release_t, eid))
    return intervals


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — Mathematical verification
# ─────────────────────────────────────────────────────────────────────────────

def validate_phase1(sim_module, config: dict | None = None,
                    rel_tol: float = 0.10) -> PhaseReport:
    t0 = time.time()
    records: list[CheckRecord] = []

    result, block = _safe_run(sim_module, config, "1.0_simulation_run", "phase1_baseline")
    if block:
        records.append(block)
        return PhaseReport("phase1", "BLOCK", _tally(records), records, time.time() - t0)

    trace = result.get("trace", [])
    cfg_back = result.get("config", config or getattr(sim_module, "DEFAULT_CONFIG", {}))
    t_first, t_last = _trace_horizon(trace)
    horizon = max(t_last - t_first, 1e-9)

    # ── 1.1 SYSTEM ─────────────────────────────────────────────────────────

    # 1.1.1 trace_integrity
    required = ("time", "event", "entity_id")
    bad = sum(1 for ev in trace if not all(k in ev for k in required))
    times = [ev.get("time") for ev in trace if "time" in ev]
    monotone = all(times[i] <= times[i+1] for i in range(len(times) - 1))
    seqs = [ev.get("seq") for ev in trace if "seq" in ev]
    seq_strict = all(seqs[i] < seqs[i+1] for i in range(len(seqs) - 1)) if seqs else True
    issues_111: list[str] = []
    if bad: issues_111.append(f"{bad} events missing required fields")
    if not monotone: issues_111.append("time not monotone non-decreasing")
    if seqs and not seq_strict: issues_111.append("seq not strictly increasing")
    records.append(CheckRecord(
        check_id="1.1.1_trace_integrity",
        status="PASS" if not issues_111 else "FAIL",
        diagnostic=("; ".join(issues_111) if issues_111
                    else f"all {len(trace):,} events: required fields present, time monotone, seq strict"),
        evidence={"event_count": len(trace), "missing_fields": bad,
                  "monotone_time": monotone, "strict_seq": seq_strict},
        suspected_location="sim_module (trace emission)" if issues_111 else None,
    ))

    # 1.1.2 set_based_conservation
    arr_set = {ev["entity_id"] for ev in trace if ev.get("event") == "system_arrival"}
    dep_set = {ev["entity_id"] for ev in trace if ev.get("event") == "system_departure"}
    loss_set = {ev["entity_id"] for ev in trace if ev.get("event") == "loss"}
    # Only count departures/losses for entities that actually arrived, so an
    # exit event without a matching arrival can't push the balance negative
    # and FAIL a legitimate trace. A genuine orphan exit (exit with no arrival)
    # is itself a defect — surface it explicitly rather than as a balance error.
    # AUDIT FIX (C6): the previous formulation derived terminal_loss and
    # in_system as an exact partition of arr_set, so bal == 0 held by
    # construction and only orphan_exits could fail the check. A
    # double-terminated entity (both departure and loss) was silently
    # absorbed into D. We now surface double-termination explicitly and
    # count terminals non-exclusively so the balance is a real constraint.
    dep_arrived = dep_set & arr_set
    loss_arrived = loss_set & arr_set
    orphan_exits = (dep_set | loss_set) - arr_set
    double_terminated = dep_arrived & loss_arrived
    in_system = arr_set - dep_arrived - loss_arrived
    A, D, L, I = (len(arr_set), len(dep_arrived), len(loss_arrived),
                  len(in_system))
    # Non-exclusive counting: D + L double-counts the overlap, so the true
    # identity is A = D + L - |overlap| + I. With the overlap required to be
    # empty, this reduces to the declared A = D + L + I.
    bal = A - D - L + len(double_terminated) - I
    cons_ok = (bal == 0 and not orphan_exits and not double_terminated)
    diag = f"A={A} = D={D} + L_terminal={L} + I(T)={I}; balance={bal}"
    if orphan_exits:
        diag += f"; {len(orphan_exits)} exit event(s) with no matching arrival"
    if double_terminated:
        diag += (f"; {len(double_terminated)} entit"
                 f"{'y' if len(double_terminated)==1 else 'ies'} terminated "
                 f"BOTH by departure and loss (double-termination)")
    records.append(CheckRecord(
        check_id="1.1.2_set_based_conservation",
        status="PASS" if cons_ok else "FAIL",
        diagnostic=diag,
        evidence={"A": A, "D": D, "L_terminal": L, "I_T": I, "balance": bal,
                  "orphan_exits": len(orphan_exits),
                  "double_terminated": len(double_terminated)},
        suspected_location=("sim_module (resource leak, duplicate departure, "
                            "double-terminated entity, or exit without arrival)")
                            if not cons_ok else None,
    ))

    # 1.1.3 entity_lifecycle
    arr_c = Counter(); dep_c = Counter()
    for ev in trace:
        if ev.get("event") == "system_arrival": arr_c[ev["entity_id"]] += 1
        elif ev.get("event") == "system_departure": dep_c[ev["entity_id"]] += 1
    multi_arr = sum(1 for v in arr_c.values() if v != 1)
    multi_dep = sum(1 for v in dep_c.values() if v > 1)
    out_of_order = 0
    arr_t: dict[str, float] = {}
    for ev in trace:
        if ev.get("event") == "system_arrival":
            arr_t.setdefault(ev["entity_id"], ev["time"])
    for ev in trace:
        if ev.get("event") == "system_departure":
            t0_arr = arr_t.get(ev["entity_id"])
            if t0_arr is not None and ev["time"] < t0_arr:
                out_of_order += 1
    issues_113 = []
    if multi_arr: issues_113.append(f"{multi_arr} entities with !=1 arrivals")
    if multi_dep: issues_113.append(f"{multi_dep} entities with >1 departures")
    if out_of_order: issues_113.append(f"{out_of_order} entities depart before arrive")
    records.append(CheckRecord(
        check_id="1.1.3_entity_lifecycle",
        status="PASS" if not issues_113 else "FAIL",
        diagnostic=("; ".join(issues_113) if issues_113 else
                    f"{len(arr_c)} entities each with one arrival; "
                    f"{len(dep_c)} departed; ordering valid"),
        evidence={"entities_arrived": len(arr_c), "entities_departed": len(dep_c),
                  "multi_arrival": multi_arr, "multi_departure": multi_dep,
                  "out_of_order": out_of_order},
        suspected_location="sim_module (entity_id reuse or arrival/departure logic)" if issues_113 else None,
    ))

    # 1.1.4 little_law_system
    sojourns = _entity_sojourn(trace)
    if not sojourns:
        records.append(CheckRecord(
            check_id="1.1.4_little_law_system",
            status="WARN",
            diagnostic="No completed entities — cannot compute system Little's Law",
            evidence={"completed": 0},
        ))
    else:
        L_obs = _avg_census(trace, horizon_end=t_last)
        W_obs = statistics.mean(sojourns.values())
        lam = len(arr_set) / horizon
        L_pred = lam * W_obs
        # symmetric denominator (matches the per-queue Little's Law check) so a
        # tiny observed L can't inflate the relative error
        rel = abs(L_obs - L_pred) / max(L_obs, L_pred, 1e-9)
        if rel <= rel_tol: status = "PASS"
        elif rel <= 2 * rel_tol: status = "WARN"
        else: status = "FAIL"
        records.append(CheckRecord(
            check_id="1.1.4_little_law_system",
            status=status,
            diagnostic=f"L_obs={L_obs:.3f} vs λW={L_pred:.3f} (λ={lam:.6f}, W={W_obs:.3f}); "
                       f"rel_err {rel:.1%}, tolerance {rel_tol:.0%}",
            evidence={"L_observed": L_obs, "L_predicted": L_pred,
                      "lambda": lam, "W_mean": W_obs, "rel_error": rel,
                      "n_completed": len(sojourns)},
            suspected_location="sim_module (warmup boundary or census measurement)"
                                if status == "FAIL" else None,
        ))

    # ── 1.2 QUEUE (per discovered queue) ───────────────────────────────────
    queues = _discover_queues(trace)
    if not queues:
        records.append(CheckRecord(
            check_id="1.2_queues",
            status="INFO",
            diagnostic="No queue_enter/queue_exit events in trace — per-queue checks skipped",
            evidence={},
        ))
    for q in queues:
        enters = [(ev["time"], ev["entity_id"]) for ev in trace
                  if ev.get("event") == "queue_enter" and ev.get("queue") == q]
        exits = [(ev["time"], ev["entity_id"]) for ev in trace
                 if ev.get("event") == "queue_exit" and ev.get("queue") == q]

        # Pair queue visits per entity. A re-entrant system (rework loops,
        # retries, feedback) legitimately has an entity enter/exit a queue
        # MORE THAN ONCE, so we must NOT treat repeated entity_ids as
        # duplicates/violations. For each entity, sort its enters and exits by
        # time and pair them positionally (k-th enter ↔ k-th exit); each pair
        # is one visit. This is robust to re-entry and to multiple servers.
        by_ent_enter: dict = defaultdict(list)
        by_ent_exit: dict = defaultdict(list)
        for t, eid in enters: by_ent_enter[eid].append(t)
        for t, eid in exits: by_ent_exit[eid].append(t)
        visits: list[tuple[float, float, str]] = []   # (enter_t, exit_t, entity)
        orphan_exits = 0
        for eid in set(by_ent_enter) | set(by_ent_exit):
            es = sorted(by_ent_enter.get(eid, []))
            xs = sorted(by_ent_exit.get(eid, []))
            if len(xs) > len(es):
                orphan_exits += len(xs) - len(es)   # left more times than entered
            for k in range(min(len(es), len(xs))):
                visits.append((es[k], xs[k], eid))

        # 1.2.{q}.conservation — balance + no orphan exits (re-entry allowed)
        n_enter, n_exit = len(enters), len(exits)
        in_queue_end = n_enter - n_exit
        reentrant = sum(1 for v in by_ent_enter.values() if len(v) > 1)
        cons_ok = (orphan_exits == 0 and in_queue_end >= 0)
        records.append(CheckRecord(
            check_id=f"1.2.{q}.conservation",
            status="PASS" if cons_ok else "FAIL",
            diagnostic=(f"{q}: {n_enter} enters, {n_exit} exits, {in_queue_end} in queue at end; "
                        f"{reentrant} entities re-entered (rework/retry — expected); no orphan exits"
                        if cons_ok else
                        f"{q}: orphan_exits={orphan_exits} (exited more than entered) or "
                        f"negative in-queue ({in_queue_end})"),
            evidence={"enters": n_enter, "exits": n_exit, "in_queue_end": in_queue_end,
                      "reentrant_entities": reentrant, "orphan_exits": orphan_exits},
            suspected_location=f"sim_module (queue '{q}' enter/exit emission)" if not cons_ok else None,
        ))

        # 1.2.{q}.fifo — order-preservation across VISITS: sorting visits by
        # enter time, exit times must be non-decreasing (first in → first out).
        # Pairing per visit (not by first occurrence of an entity_id) is what
        # makes this correct under re-entry.
        if visits:
            vs = sorted(visits, key=lambda v: v[0])
            exit_seq = [v[1] for v in vs]
            inversions = sum(1 for i in range(len(exit_seq) - 1)
                             if exit_seq[i] > exit_seq[i + 1] + 1e-9)
            fifo_ok = inversions == 0
            records.append(CheckRecord(
                check_id=f"1.2.{q}.fifo",
                status="PASS" if fifo_ok else "FAIL",
                diagnostic=(f"{q}: visit exit order matches enter order ({len(vs)} visits)"
                            if fifo_ok else
                            f"{q}: {inversions} FIFO order inversions across {len(vs)} visits"),
                evidence={"visits": len(vs), "inversions": inversions},
                suspected_location=f"sim_module (queue '{q}' service discipline)" if not fifo_ok else None,
            ))
        else:
            records.append(CheckRecord(
                check_id=f"1.2.{q}.fifo",
                status="INFO",
                diagnostic=f"{q}: no completed visits — cannot verify FIFO",
                evidence={},
            ))

        # 1.2.{q}.little — waits computed per visit (handles re-entry: each
        # visit contributes its own wait, instead of differencing only the
        # last enter/exit of a re-entrant entity).
        waits = [x - e for e, x, _ in visits]
        # time-weighted queue length
        qd = [(t, +1) for t, _ in enters] + [(t, -1) for t, _ in exits]
        qd.sort()
        Q = 0; aq = 0.0; t_prev = t_first
        for t, d in qd:
            aq += Q * (t - t_prev); Q += d; t_prev = t
        aq += Q * (t_last - t_prev)
        Lq = aq / horizon
        lam_q = n_enter / horizon
        Wq = statistics.mean(waits) if waits else 0.0
        Lq_pred = lam_q * Wq
        err = abs(Lq - Lq_pred) / max(Lq, Lq_pred, 1e-9)
        if len(waits) >= 5 and err <= rel_tol:
            status_q = "PASS"
        elif len(waits) < 5:
            status_q = "INFO"
        elif err <= 2 * rel_tol:
            status_q = "WARN"
        else:
            status_q = "FAIL"
        records.append(CheckRecord(
            check_id=f"1.2.{q}.little",
            status=status_q,
            diagnostic=f"{q}: Lq={Lq:.4f}, λq×W̄q={Lq_pred:.4f}, rel_err={err*100:.1f}% "
                       f"({len(waits)} completed waits)",
            evidence={"Lq": Lq, "Lq_pred": Lq_pred, "lambda_q": lam_q,
                      "Wq_mean": Wq, "rel_error": err, "n_waits": len(waits)},
            suspected_location=(f"sim_module (queue '{q}' arrival/departure ordering)"
                                if status_q == "FAIL" else None),
        ))

    # ── 1.3 RESOURCE (per discovered resource) ─────────────────────────────
    resources = _discover_resources(trace)
    res_cfg = _resources_from_config(cfg_back)
    if not resources:
        records.append(CheckRecord(
            check_id="1.3_resources",
            status="INFO",
            diagnostic="No service_start/service_end events in trace — per-resource checks skipped",
            evidence={},
        ))
    for r in resources:
        intervals = _service_intervals(trace, resource=r)
        cap = _resource_capacity(cfg_back, r)

        # 1.3.{r}.state_machine
        orphans = _orphan_service_ends(trace, resource=r)
        records.append(CheckRecord(
            check_id=f"1.3.{r}.state_machine",
            status="PASS" if orphans == 0 else "FAIL",
            diagnostic=(f"{r}: state machine clean ({len(intervals)} paired intervals)"
                        if orphans == 0 else
                        f"{r}: {orphans} orphan service_end event(s)"),
            evidence={"intervals": len(intervals), "orphan_ends": orphans},
            suspected_location=f"sim_module (resource '{r}' grant/release accounting)" if orphans else None,
        ))

        # 1.3.{r}.capacity
        peak = _peak_concurrent(intervals)
        if cap is None:
            records.append(CheckRecord(
                check_id=f"1.3.{r}.capacity",
                status="INFO",
                diagnostic=f"{r}: no declared capacity in config — peak concurrency observed = {peak}",
                evidence={"peak": peak},
            ))
        else:
            records.append(CheckRecord(
                check_id=f"1.3.{r}.capacity",
                status="PASS" if peak <= cap else "FAIL",
                diagnostic=(f"{r}: peak concurrency {peak} ≤ capacity {cap}" if peak <= cap
                            else f"{r}: peak {peak} > capacity {cap}"),
                evidence={"peak": peak, "capacity": cap},
                suspected_location=f"sim_module (resource '{r}' over-grant)" if peak > cap else None,
            ))

        # 1.3.{r}.util_identity and throughput_bound
        if intervals and cap and cap > 0:
            durations = [e - s for s, e, _ in intervals]
            E_S = statistics.mean(durations)
            n_starts = sum(1 for ev in trace
                           if ev.get("event") == "service_start" and ev.get("resource") == r)
            # Terminating/burst regime: the arrival process is not stationary
            # over the full horizon. λ×S̄/c is a steady-state identity that
            # assumes constant λ; comparing against a burst-arrival system's
            # measured utilization produces a boundary artifact rather than a
            # meaningful discrepancy. Windowed λ (arrivals ÷ window) restores
            # apples-to-apples with the observed utilization computed over
            # the same window.
            regime = _simulation_regime(base_cfg)
            arrival_window = _arrival_window_seconds(base_cfg)
            terminating = regime["type"] in ("terminating", "burst")
            window = (arrival_window if (terminating and arrival_window
                                          and arrival_window > 0)
                      else horizon)
            n_starts_window = (
                sum(1 for ev in trace
                    if ev.get("event") == "service_start"
                    and ev.get("resource") == r
                    and float(ev.get("time", 0.0)) <= warmup_t + window)
                if terminating and arrival_window else n_starts)
            lam_r = n_starts_window / window
            rho_pred = lam_r * E_S / cap
            busy = _busy_time(intervals)
            rho_obs = busy / (cap * horizon)
            err_u = abs(rho_obs - rho_pred)
            if terminating:
                # For terminating regimes, the identity is advisory rather
                # than trust-bearing. WARN is the max severity even for
                # large discrepancies; the burden of proof shifts to the
                # windowed check reported below.
                if err_u <= 0.05:
                    util_status = "PASS"
                else:
                    util_status = "WARN"
                diag_regime_note = (
                    f" [terminating regime: λ measured over arrival window "
                    f"of {window:.0f}s; ρ_obs measured over full horizon "
                    f"{horizon:.0f}s — a residual is expected and does not "
                    f"indicate a defect unless it far exceeds the drain-tail "
                    f"contribution]"
                )
            else:
                if err_u <= 0.005:
                    util_status = "PASS"
                elif err_u <= 0.05:
                    util_status = "WARN"
                else:
                    util_status = "FAIL"
                diag_regime_note = ""
            records.append(CheckRecord(
                check_id=f"1.3.{r}.util_identity",
                status=util_status,
                diagnostic=(f"{r}: util={rho_obs:.4f} = λ×S̄/c = ({n_starts_window}/{window:.2f})"
                            f"×{E_S:.3f}/{cap} = {rho_pred:.4f}; abs_err={err_u:.4f}"
                            f"{diag_regime_note}"),
                evidence={"util_observed": rho_obs, "util_predicted": rho_pred,
                          "abs_error": err_u, "n_starts": n_starts_window,
                          "E_S": E_S, "capacity": cap,
                          "regime": regime["type"],
                          "measurement_window_seconds": window},
                suspected_location=(f"sim_module (resource '{r}' service-time emission "
                                    "or busy-time accounting)") if util_status == "FAIL" else None,
            ))
            bound = cap / E_S
            tb_status = "PASS" if lam_r <= bound + 1e-9 else "FAIL"
            records.append(CheckRecord(
                check_id=f"1.3.{r}.throughput_bound",
                status=tb_status,
                diagnostic=f"{r}: λ={lam_r:.5f} ≤ c/E[S]={bound:.5f} (c={cap}, E[S]={E_S:.3f})"
                           if tb_status == "PASS" else
                           f"{r}: λ={lam_r:.5f} > bound c/E[S]={bound:.5f}",
                evidence={"lambda": lam_r, "bound": bound, "capacity": cap, "E_S": E_S},
                suspected_location=f"sim_module (resource '{r}' over-utilized; queue must be unbounded)"
                                    if tb_status == "FAIL" else None,
            ))
        else:
            records.append(CheckRecord(
                check_id=f"1.3.{r}.util_identity",
                status="INFO",
                diagnostic=f"{r}: insufficient data for utilization identity",
                evidence={"intervals": len(intervals), "capacity": cap},
            ))

    # ── 1.3 IMPLICIT-HOLD RESOURCES ────────────────────────────────────────
    # A resource declared in config but with NO service_start/service_end
    # events is held implicitly across the entity's sojourn (canonical case:
    # an ICU bed seized from admission to departure). Auto-discovery above
    # only finds service-emitting resources, so we cover the rest here using
    # occupancy reconstruction — same per-resource checks, no hardcoded names.
    implicit_resources = [name for name in res_cfg if name not in resources]
    if implicit_resources:
        held = _implicit_hold_intervals(trace)
    # acquire/release bookkeeping shared by the implicit-hold state-machine check
    _imp_arrived = {ev["entity_id"] for ev in trace if ev.get("event") == "system_arrival"}
    _imp_rel_counts = Counter(ev["entity_id"] for ev in trace
                              if ev.get("event") in ("system_departure", "loss"))
    for name in implicit_resources:
        cap = res_cfg[name].get("capacity") if isinstance(res_cfg[name], dict) else res_cfg[name]

        # 1.3.{name}.state_machine — acquire/release pairing for an implicitly
        # held resource (no service_start/service_end to pair, so we verify the
        # acquire→release lifecycle instead): every release must follow an
        # acquire, and no entity may release more than once.
        n_acquire = len(_imp_arrived)
        n_release = sum(_imp_rel_counts.values())
        orphan_releases = len([eid for eid in _imp_rel_counts if eid not in _imp_arrived])
        dup_releases = sum(1 for c in _imp_rel_counts.values() if c > 1)
        sm_ok = (orphan_releases == 0 and dup_releases == 0)
        records.append(CheckRecord(
            check_id=f"1.3.{name}.state_machine",
            status="PASS" if sm_ok else "FAIL",
            diagnostic=(f"{name} (implicit-hold): {n_acquire} acquires, {n_release} releases; "
                        "no orphan or duplicate releases"
                        if sm_ok else
                        f"{name} (implicit-hold): orphan_releases={orphan_releases}, "
                        f"dup_releases={dup_releases}"),
            evidence={"acquires": n_acquire, "releases": n_release,
                      "orphan_releases": orphan_releases, "dup_releases": dup_releases,
                      "mode": "implicit_hold"},
            suspected_location=f"sim_module (resource '{name}' acquire/release accounting)" if not sm_ok else None,
        ))

        # 1.3.{name}.capacity (occupancy peak ≤ capacity)
        peak = _peak_concurrent(held)
        if cap is None:
            records.append(CheckRecord(
                check_id=f"1.3.{name}.capacity",
                status="INFO",
                diagnostic=f"{name}: declared resource with no service events; "
                           f"reconstructed peak occupancy = {peak} (no capacity declared)",
                evidence={"peak": peak, "mode": "implicit_hold"},
            ))
        else:
            records.append(CheckRecord(
                check_id=f"1.3.{name}.capacity",
                status="PASS" if peak <= cap else "FAIL",
                diagnostic=(f"{name} (implicit-hold): peak occupancy {peak} ≤ capacity {cap}"
                            if peak <= cap else
                            f"{name} (implicit-hold): peak occupancy {peak} > capacity {cap}"),
                evidence={"peak": peak, "capacity": cap, "mode": "implicit_hold"},
                suspected_location=f"sim_module (resource '{name}' over-allocation)" if peak > cap else None,
            ))
        # 1.3.{name}.util_identity and throughput_bound via reconstructed holds
        if held and cap and cap > 0:
            durations = [e - s for s, e, _ in held]
            E_hold = statistics.mean(durations)
            n_acq = len(held)
            lam_n = n_acq / horizon
            rho_pred = lam_n * E_hold / cap
            rho_obs = _busy_time(held) / (cap * horizon)
            err_u = abs(rho_obs - rho_pred)
            # implicit-hold reconstruction omits in-flight (still-held) entities,
            # so a slightly looser tolerance is appropriate at the boundary.
            if err_u <= 0.05:
                util_status = "PASS"
            elif err_u <= 0.10:
                util_status = "WARN"
            else:
                util_status = "FAIL"
            records.append(CheckRecord(
                check_id=f"1.3.{name}.util_identity",
                status=util_status,
                diagnostic=(f"{name} (implicit-hold): util={rho_obs:.4f} vs λ×E[hold]/c="
                            f"{rho_pred:.4f}; abs_err={err_u:.4f} "
                            f"(reconstructed from {n_acq} completed holds; in-flight excluded)"),
                evidence={"util_observed": rho_obs, "util_predicted": rho_pred,
                          "abs_error": err_u, "n_acquisitions": n_acq,
                          "E_hold": E_hold, "capacity": cap, "mode": "implicit_hold"},
                suspected_location=(f"sim_module (resource '{name}' acquire/release accounting)"
                                    if util_status == "FAIL" else None),
            ))
            bound = cap / E_hold
            tb_status = "PASS" if lam_n <= bound + 1e-9 else "FAIL"
            records.append(CheckRecord(
                check_id=f"1.3.{name}.throughput_bound",
                status=tb_status,
                diagnostic=(f"{name} (implicit-hold): λ={lam_n:.6f} ≤ c/E[hold]={bound:.6f}"
                            if tb_status == "PASS" else
                            f"{name} (implicit-hold): λ={lam_n:.6f} > c/E[hold]={bound:.6f}"),
                evidence={"lambda": lam_n, "bound": bound, "capacity": cap,
                          "E_hold": E_hold, "mode": "implicit_hold"},
                suspected_location=f"sim_module (resource '{name}' saturated)" if tb_status == "FAIL" else None,
            ))

    # ─────────────────────────────────────────────────────────────────────
    # § 1.4  TRACE-VS-METRICS CONSISTENCY
    #
    # These checks assert that the trace and the metrics dict agree on the same
    # underlying quantity. They are designed to catch the failure mode in which
    # the simulation maintains TWO parallel views — a (lossy or compressed)
    # trace, and a separately-bookkept metrics aggregate — that are numerically
    # consistent with each other but diverge from each other in granularity.
    # See spec_code_audit.md "rounding trace compression" for the motivating
    # case study (Goodhart's-law / checker-driven trace compression).
    # ─────────────────────────────────────────────────────────────────────
    cs = (cfg_back.get("compliance_spec") or {}) if isinstance(cfg_back, dict) else {}
    warmup_t = float(cfg_back.get("warmup_time",
                                  cfg_back.get("warmup_seconds", 0.0))) \
                if isinstance(cfg_back, dict) else 0.0
    _, horizon_end = _trace_horizon(trace)
    # Attempt to read the DSL alongside the sim, for the fallback path that
    # finds recurring_activity elements not yet mapped into compliance_spec.
    dsl_doc: dict | None = None
    try:
        import json as _json, os as _os
        sim_path = getattr(sim_module, "__file__", "") or ""
        dsl_path = _os.path.join(_os.path.dirname(sim_path), "my_dsl.json")
        if _os.path.isfile(dsl_path):
            with open(dsl_path) as f:
                dsl_doc = _json.load(f)
    except Exception:
        dsl_doc = None

    # ── 1.4.1  Recurring-activity cadence (per declared activity) ───────────
    # For each declared `recurring_activity` / `recurring_obligation`, the
    # event count per admitted patient per post-warmup day must match the
    # declared `expected_per_admitted_patient_per_day` (== 24/every_hours
    # by default). This catches the cadence-drift failure mode in which the
    # implementation uses a free-parameter interval (e.g. 2.5h) tuned to a
    # target metric while the declaration still says hourly (DSL R4 case).
    declared_recurring = _discover_recurring_obligations(cs, dsl_doc)
    if not declared_recurring:
        records.append(CheckRecord(
            check_id="1.4.1_recurring_cadence",
            status="INFO",
            diagnostic="No recurring activities declared in compliance_spec or DSL.",
            evidence={},
        ))
    else:
        n_admitted = _admitted_post_warmup(trace, warmup_t)
        post_days = max((horizon_end - warmup_t) / 86400.0, 1e-9)
        admitted_patient_days = n_admitted * post_days
        for ra in declared_recurring:
            # Match by process name (and role when declared, to avoid double-
            # counting multi-role episodes — R1 nurse vs R2 resident etc.).
            def _match(ev, ra=ra):
                if ev.get("event") not in ("service_start", "queue_enter"):
                    return False
                if ev.get("event") == "queue_enter":
                    return False  # count one event per episode → service_start
                if ev.get("process") != ra["process"]:
                    return False
                if ra["role"] and ev.get("role") and ev.get("role") != ra["role"]:
                    return False
                return float(ev.get("time", 0.0)) >= warmup_t
            n_events = sum(1 for ev in trace if _match(ev))
            if admitted_patient_days <= 0:
                records.append(CheckRecord(
                    check_id=f"1.4.1.{ra['id']}_cadence",
                    status="INFO",
                    diagnostic=f"{ra['id']}: no admitted patient-days post-warmup; check skipped.",
                    evidence={"n_admitted": n_admitted, "post_days": post_days},
                ))
                continue
            observed = n_events / admitted_patient_days
            expected = ra["expected_per_admitted_per_day"]
            rel = abs(observed - expected) / max(expected, 1e-9)
            tol = ra["tolerance"]
            if rel <= tol:
                status = "PASS"
            elif rel <= 2 * tol:
                status = "WARN"
            else:
                status = "FAIL"
            records.append(CheckRecord(
                check_id=f"1.4.1.{ra['id']}_cadence",
                status=status,
                diagnostic=(
                    f"{ra['id']} (process={ra['process']!r}"
                    + (f", role={ra['role']!r}" if ra["role"] else "")
                    + f"): observed {observed:.3f} events/admitted-patient/day vs "
                    f"declared {expected:.3f} (every_hours={ra['every_hours']:.3f}); "
                    f"rel_err={rel:.3f}, tol={tol:.2f}"
                ),
                evidence={
                    "observed_per_admitted_per_day": observed,
                    "expected_per_admitted_per_day": expected,
                    "every_hours_declared": ra["every_hours"],
                    "n_events": n_events,
                    "n_admitted": n_admitted,
                    "post_warmup_days": post_days,
                    "relative_error": rel,
                    "tolerance": tol,
                },
                suspected_location=(
                    f"sim_module (recurring activity {ra['id']} cadence — declared "
                    f"every_hours={ra['every_hours']} but realized cadence "
                    f"corresponds to every_hours≈{24.0/observed if observed>0 else float('inf'):.3f})"
                ) if status == "FAIL" else None,
            ))

    # ── 1.4.2  Scheduled-window count (per declared scheduled_windows entry) ─
    # For each declared scheduled_process / scheduled_window, the number of
    # window-marker events per post-warmup day must match the declared windows
    # per day.
    sched = cs.get("scheduled_windows") or []
    if not sched:
        records.append(CheckRecord(
            check_id="1.4.2_scheduled_windows",
            status="INFO",
            diagnostic="No scheduled_windows declared in compliance_spec.",
            evidence={},
        ))
    else:
        post_days = max((horizon_end - warmup_t) / 86400.0, 1e-9)
        for sw in sched:
            proc = sw.get("process", "")
            windows_hours = sw.get("windows_hours") or []
            expected_per_day = float(sw.get("times_per_day", len(windows_hours) or 0))
            # Window markers can take a couple of shapes — prefer an explicit
            # "<process>_window_open" event, fall back to queue_enter records
            # whose process names that marker.
            marker_proc = f"{proc}_window_open"
            n_markers = sum(
                1 for ev in trace
                if float(ev.get("time", 0.0)) >= warmup_t
                and ev.get("process") in (marker_proc, proc)
                and ev.get("event") in ("queue_enter", "service_start", "window_open")
                and "rounding_episode" in (ev.get("entity_type") or "")
            )
            # If the above didn't match, fall back to a coarser count: any
            # queue_enter whose process matches the marker name.
            if n_markers == 0:
                n_markers = sum(
                    1 for ev in trace
                    if float(ev.get("time", 0.0)) >= warmup_t
                    and ev.get("process") == marker_proc
                    and ev.get("event") == "queue_enter"
                )
            observed_per_day = n_markers / post_days
            if expected_per_day <= 0:
                status = "INFO"
            else:
                rel = abs(observed_per_day - expected_per_day) / expected_per_day
                tol = float(sw.get("tolerance", 0.10))
                status = "PASS" if rel <= tol else ("WARN" if rel <= 2*tol else "FAIL")
            records.append(CheckRecord(
                check_id=f"1.4.2.{proc}_window_count",
                status=status,
                diagnostic=(
                    f"{proc}: observed {observed_per_day:.3f} windows/day vs "
                    f"declared {expected_per_day:.3f} (windows_hours={windows_hours}); "
                    f"n_markers={n_markers} over {post_days:.2f} post-warmup days."
                ),
                evidence={
                    "observed_per_day": observed_per_day,
                    "expected_per_day": expected_per_day,
                    "n_markers": n_markers,
                    "post_warmup_days": post_days,
                },
                suspected_location=(
                    f"sim_module (scheduled process '{proc}' window-open emission)"
                ) if status == "FAIL" else None,
            ))

    # ── 1.4.3  Per-resource trace-vs-metrics busy-time consistency ──────────
    # For each resource for which the metrics dict reports a utilization
    # statistic, the implied busy time (utilization × capacity × post-warmup
    # duration) must agree with the sum of service_end − service_start
    # intervals derived from the trace (clipped to post-warmup). A divergence
    # here exposes the rounding-compression failure mode: a coarse trace
    # surface with the metrics maintained by parallel bookkeeping.
    metrics = (result.get("metrics") or {}) if isinstance(result, dict) else {}
    res_for_check = sorted(set(_discover_resources(trace) +
                                list(_resources_from_config(cfg_back).keys())))
    horizon_dur_post = max(horizon_end - warmup_t, 1e-9)
    flagged_any = False
    for r in res_for_check:
        util_key = f"utilization_{r}"
        if util_key not in metrics:
            continue
        cap = _resource_capacity(cfg_back, r) or 0
        if cap <= 0:
            continue
        try:
            util = float(metrics[util_key])
        except (TypeError, ValueError):
            continue
        reported_busy = util * cap * horizon_dur_post
        intervals = _service_intervals(trace, r)
        trace_busy = _post_warmup_busy_time(intervals, warmup_t)
        denom = max(reported_busy, trace_busy, 1.0)
        rel = abs(reported_busy - trace_busy) / denom
        # Tight: 5% PASS, 15% WARN, otherwise FAIL.
        if rel <= 0.05:
            status = "PASS"
        elif rel <= 0.15:
            status = "WARN"
        else:
            status = "FAIL"
        if status == "FAIL":
            flagged_any = True
        records.append(CheckRecord(
            check_id=f"1.4.3.{r}_busy_consistency",
            status=status,
            diagnostic=(
                f"{r}: trace-derived busy={trace_busy:.1f}s vs metric-implied busy="
                f"{reported_busy:.1f}s (utilization={util:.4f}, capacity={cap}, "
                f"post-warmup duration={horizon_dur_post:.1f}s); rel_err={rel:.4f}"
            ),
            evidence={
                "reported_busy_seconds": reported_busy,
                "trace_busy_seconds": trace_busy,
                "utilization_reported": util,
                "capacity": cap,
                "post_warmup_seconds": horizon_dur_post,
                "n_intervals": len(intervals),
                "relative_error": rel,
            },
            suspected_location=(
                f"sim_module (resource '{r}': trace events do not account for the "
                "busy time the metrics report — coarser trace surface than the "
                "bookkeeping, or stale metric accounting)"
            ) if status == "FAIL" else None,
        ))
    if not flagged_any and not [r for r in res_for_check if f"utilization_{r}" in metrics]:
        records.append(CheckRecord(
            check_id="1.4.3_busy_consistency",
            status="INFO",
            diagnostic="No resource utilization metrics present; trace-vs-metrics busy-time consistency check skipped.",
            evidence={},
        ))

    # ─────────────────────────────────────────────────────────────────────
    # § 1.5  STATE-TRANSITION EMISSION CONTRACT
    #
    # Each competing_risks arc in a state_transition_rule may declare an
    # `emission_contract` field stating what trace events the transition
    # MUST emit. Allowed values:
    #   silent_state_change — only a state_change event; no resource events
    #   resource_event      — service events (e.g. a hard-pool team response);
    #                          state_change may or may not accompany
    #   both                — state_change AND resource events together
    # When the contract is declared, the check verifies the trace's events
    # match per arc-direction. This addresses the audit's "recovery emission
    # asymmetry" finding by making per-direction emission semantics testable.
    # If the contract is unspecified for an arc, the check emits INFO with a
    # recommendation to declare it.
    # ─────────────────────────────────────────────────────────────────────
    str_rules = cs.get("state_transition_rules") or []
    # Build state_change events index by (entity_id, from_state, next_state)
    sc_events = [ev for ev in trace
                 if ev.get("event") == "state_change"
                 and float(ev.get("time", 0.0)) >= warmup_t]
    if not str_rules:
        records.append(CheckRecord(
            check_id="1.5_state_transition_emission",
            status="INFO",
            diagnostic="No state_transition_rules declared; emission contract check skipped.",
            evidence={},
        ))
    else:
        SC_WINDOW = 60.0   # seconds — paired service events must fire within this window
        # Pre-index service_starts by entity for fast neighbourhood lookup.
        services_by_entity: dict[str, list[dict]] = defaultdict(list)
        for ev in trace:
            if ev.get("event") not in ("service_start", "resume"):
                continue
            if float(ev.get("time", 0.0)) < warmup_t:
                continue
            eid = ev.get("entity_id")
            if eid:
                services_by_entity[eid].append(ev)
        for evs in services_by_entity.values():
            evs.sort(key=lambda e: (e.get("time", 0.0), e.get("seq", 0)))

        for rule in str_rules:
            if not isinstance(rule, dict):
                continue
            rid = (rule.get("source_ids") or [rule.get("state", "?")])[0]
            from_state = rule.get("state")
            for arc in rule.get("competing_risks", []) or []:
                if not isinstance(arc, dict):
                    continue
                next_state = arc.get("next_state")
                direction = arc.get("direction", "")
                contract = (arc.get("emission_contract") or "").lower().strip()
                arc_label = f"{rid}.{from_state}_to_{next_state}"
                # Find state_change events matching this arc.
                matching_sc = [
                    ev for ev in sc_events
                    if ev.get("from_state") == from_state
                    and ev.get("next_state") == next_state
                ]
                # Count how many of those state_changes are accompanied by a
                # resource event (a service_start on the same entity within
                # the pairing window).
                accompanied = 0
                for sc in matching_sc:
                    eid = sc.get("entity_id")
                    t = float(sc.get("time", 0.0))
                    nearby = services_by_entity.get(eid, [])
                    if any(abs(float(svc.get("time", 0.0)) - t) <= SC_WINDOW
                            for svc in nearby):
                        accompanied += 1
                n_sc = len(matching_sc)
                ratio = (accompanied / n_sc) if n_sc > 0 else 0.0

                if not contract:
                    # No declared contract — INFO with a recommendation. Avoid
                    # silently passing or failing; the user must choose.
                    if n_sc == 0:
                        records.append(CheckRecord(
                            check_id=f"1.5.{arc_label}_emission",
                            status="INFO",
                            diagnostic=(
                                f"{arc_label}: no transitions occurred in trace "
                                f"(direction={direction!r}); emission_contract not "
                                f"declared. No check performed."
                            ),
                            evidence={"arc": arc_label, "direction": direction,
                                      "n_state_changes": 0},
                        ))
                    else:
                        records.append(CheckRecord(
                            check_id=f"1.5.{arc_label}_emission",
                            status="INFO",
                            diagnostic=(
                                f"{arc_label}: {n_sc} state_changes observed "
                                f"(direction={direction!r}); {accompanied} accompanied "
                                f"by resource events (ratio={ratio:.2f}). "
                                f"emission_contract NOT DECLARED — declare one of "
                                f"'silent_state_change' / 'resource_event' / 'both' "
                                f"on this competing_risks arc to enable verification."
                            ),
                            evidence={"arc": arc_label, "direction": direction,
                                      "n_state_changes": n_sc,
                                      "accompanied_by_resource_event": accompanied,
                                      "ratio": ratio,
                                      "emission_contract_declared": False},
                        ))
                    continue

                # Contract declared — verify.
                if n_sc == 0:
                    # No transitions in this run; can't verify either way.
                    records.append(CheckRecord(
                        check_id=f"1.5.{arc_label}_emission",
                        status="INFO",
                        diagnostic=(
                            f"{arc_label}: emission_contract={contract!r} declared, "
                            f"but no transitions of this kind occurred in the trace "
                            f"(direction={direction!r}). Verification deferred."
                        ),
                        evidence={"arc": arc_label, "direction": direction,
                                  "emission_contract": contract, "n_state_changes": 0},
                    ))
                    continue
                if contract == "silent_state_change":
                    # Strict: NO accompanying resource events.
                    if accompanied == 0:
                        records.append(CheckRecord(
                            check_id=f"1.5.{arc_label}_emission",
                            status="PASS",
                            diagnostic=(
                                f"{arc_label}: emission_contract=silent_state_change; "
                                f"{n_sc} transitions emitted with no accompanying "
                                f"resource events (as declared)."
                            ),
                            evidence={"arc": arc_label, "direction": direction,
                                      "emission_contract": contract,
                                      "n_state_changes": n_sc,
                                      "accompanied_by_resource_event": 0,
                                      "ratio": 0.0},
                        ))
                    else:
                        records.append(CheckRecord(
                            check_id=f"1.5.{arc_label}_emission",
                            status="FAIL",
                            diagnostic=(
                                f"{arc_label}: emission_contract=silent_state_change "
                                f"but {accompanied}/{n_sc} ({ratio:.0%}) "
                                f"state_changes accompanied by resource events. "
                                f"Sim emits clinical work for a transition that the "
                                f"DSL declared silent."
                            ),
                            evidence={"arc": arc_label, "direction": direction,
                                      "emission_contract": contract,
                                      "n_state_changes": n_sc,
                                      "accompanied_by_resource_event": accompanied,
                                      "ratio": ratio},
                            suspected_location=(
                                f"sim_module (state_change for {from_state}→{next_state} "
                                f"emits resource events; either remove the events or "
                                f"change emission_contract in DSL to 'resource_event' "
                                f"or 'both')"
                            ),
                        ))
                elif contract in ("resource_event", "both"):
                    # Strict: nearly all state_changes must be accompanied by resource events.
                    PASS_THRESHOLD = 0.90
                    if ratio >= PASS_THRESHOLD:
                        records.append(CheckRecord(
                            check_id=f"1.5.{arc_label}_emission",
                            status="PASS",
                            diagnostic=(
                                f"{arc_label}: emission_contract={contract!r}; "
                                f"{accompanied}/{n_sc} ({ratio:.0%}) state_changes "
                                f"accompanied by resource events (≥ "
                                f"{PASS_THRESHOLD:.0%} threshold)."
                            ),
                            evidence={"arc": arc_label, "direction": direction,
                                      "emission_contract": contract,
                                      "n_state_changes": n_sc,
                                      "accompanied_by_resource_event": accompanied,
                                      "ratio": ratio},
                        ))
                    else:
                        records.append(CheckRecord(
                            check_id=f"1.5.{arc_label}_emission",
                            status="FAIL",
                            diagnostic=(
                                f"{arc_label}: emission_contract={contract!r} "
                                f"but only {accompanied}/{n_sc} ({ratio:.0%}) "
                                f"state_changes accompanied by resource events. "
                                f"DSL declares the transition fires clinical work; "
                                f"trace mostly silent."
                            ),
                            evidence={"arc": arc_label, "direction": direction,
                                      "emission_contract": contract,
                                      "n_state_changes": n_sc,
                                      "accompanied_by_resource_event": accompanied,
                                      "ratio": ratio},
                            suspected_location=(
                                f"sim_module (state_change for {from_state}→{next_state} "
                                f"fires silently; DSL declared resource events should "
                                f"accompany)"
                            ),
                        ))
                else:
                    records.append(CheckRecord(
                        check_id=f"1.5.{arc_label}_emission",
                        status="INFO",
                        diagnostic=(
                            f"{arc_label}: emission_contract={contract!r} not "
                            f"recognised (use silent_state_change / resource_event "
                            f"/ both)."
                        ),
                        evidence={"arc": arc_label, "emission_contract": contract},
                    ))

    # ─────────────────────────────────────────────────────────────────────
    # §1.6 trace-temporal-distribution — verify that the empirical
    # inter-event distribution of each declared transition matches its
    # declared dynamics-mechanism (continuous_hazard / discrete_check /
    # event_driven). This catches the substitution where a DSL declares
    # a continuous hazard but the code implements a per-hour Bernoulli
    # check (or vice versa). The two implementations produce the same
    # average behaviour but operationally distinct trace structures:
    # continuous hazards yield exponentially-distributed inter-event
    # times, discrete checks cluster events at multiples of the cadence.
    # The check reads each transition's `dynamics.type` from the
    # compliance_spec and applies the corresponding empirical test.
    # ─────────────────────────────────────────────────────────────────────
    def _collect_dynamics_elements(spec: dict) -> list[tuple[str, dict, str]]:
        """Return (element_id, dynamics_dict, transition_label) tuples for
        every DSL element whose compliance entry carries a dynamics block.
        Sections searched: state_transition_rules, terminal_outcomes,
        recurring_obligations. Elements with no dynamics field or with
        dynamics.type='unspecified' are skipped here (they are surfaced
        by the description→DSL coverage gate at Stage A instead)."""
        out: list[tuple[str, dict, str]] = []
        for section in ("state_transition_rules", "terminal_outcomes",
                        "recurring_obligations"):
            for entry in spec.get(section, []) or []:
                if not isinstance(entry, dict):
                    continue
                dyn = entry.get("dynamics")
                if not isinstance(dyn, dict):
                    continue
                dtype = (dyn.get("type") or "").lower().strip()
                if dtype in ("", "unspecified"):
                    continue
                eid = (entry.get("source_ids") or
                       [entry.get("id") or entry.get("name") or "?"])[0]
                label = entry.get("name") or entry.get("event") or eid
                out.append((eid, dyn, f"{section}/{label}"))
        return out

    def _entity_transition_times(
        trace_in: list, target_event: str | None,
        target_next_state: str | None,
    ) -> list[float]:
        """Return the wall-clock times of transitions of interest. If
        target_event is set, match events of that name. If
        target_next_state is set, match state_change events with that
        next_state. Both can be set (state_change to a named next_state).
        Filters to post-warmup events; returns in trace order."""
        out: list[float] = []
        for ev in trace_in:
            if float(ev.get("time", 0.0)) < warmup_t:
                continue
            if target_next_state is not None:
                if ev.get("event") != "state_change":
                    continue
                if ev.get("next_state") != target_next_state:
                    continue
            if target_event is not None:
                if ev.get("event") != target_event:
                    continue
            out.append(float(ev.get("time", 0.0)))
        return sorted(out)

    def _ks_exponential(samples: list[float], rate_per_unit: float) -> float:
        """Single-sample KS statistic vs Exp(rate). Returns max sup-norm
        gap between empirical CDF and theoretical CDF. Smaller is better."""
        if not samples or rate_per_unit <= 0:
            return 1.0
        s = sorted(samples)
        n = len(s)
        from math import exp
        d = 0.0
        for i, x in enumerate(s):
            f_theory = 1.0 - exp(-rate_per_unit * x)
            f_lo = i / n
            f_hi = (i + 1) / n
            d = max(d, abs(f_theory - f_lo), abs(f_theory - f_hi))
        return d

    def _hour_cluster_fraction(times: list[float], window_s: float = 60.0) -> float:
        """Fraction of events that fall within ±window_s of an integer-hour
        boundary (in seconds). High value (>0.5) signals discrete-check
        clustering; low value (≈window_s × 2 / 3600) signals temporal
        uniformity consistent with a continuous hazard."""
        if not times:
            return 0.0
        hits = 0
        for t in times:
            phase = t % 3600.0
            if phase <= window_s or phase >= 3600.0 - window_s:
                hits += 1
        return hits / len(times)

    dyn_elements = _collect_dynamics_elements(cs)
    if not dyn_elements:
        records.append(CheckRecord(
            check_id="1.6_trace_temporal_distribution",
            status="INFO",
            diagnostic=(
                "No declared dynamics found on any state-transition / "
                "terminal-outcome / recurring-obligation element. §1.6 skipped. "
                "Consider declaring a `dynamics` block on transition-bearing "
                "elements to enable temporal-structure verification."
            ),
            evidence={},
        ))
    else:
        for eid, dyn, label in dyn_elements:
            dtype = (dyn.get("type") or "").lower()
            # Resolve the transition's trace footprint. Prefer next_state
            # for state_transition arcs; fall back to terminal_event name.
            target_event = dyn.get("trigger_event")  # event_driven only
            target_next_state = dyn.get("to_state") or dyn.get("next_state")
            terminal_event_name = dyn.get("terminal_event")
            # Inter-event times (in seconds). We use entity-level
            # inter-transition timing rather than wall-clock so a busy
            # period with many concurrent entities does not look like a
            # discrete-check cluster.
            times = _entity_transition_times(
                trace,
                target_event=(target_event or terminal_event_name),
                target_next_state=target_next_state)
            n = len(times)
            if n < 30:
                records.append(CheckRecord(
                    check_id=f"1.6.{eid}_temporal_distribution",
                    status="INFO",
                    diagnostic=(
                        f"{label} declared dynamics.type={dtype!r}; only {n} "
                        f"transitions observed in trace (need ≥ 30 for "
                        f"distribution test). Verification deferred."
                    ),
                    evidence={"element": eid, "label": label, "type": dtype,
                              "n_observed": n},
                ))
                continue
            # Inter-event times in seconds
            iet = [times[i + 1] - times[i] for i in range(n - 1)
                   if times[i + 1] - times[i] > 0]
            iet_hours = [x / 3600.0 for x in iet]
            hour_cluster = _hour_cluster_fraction(times)

            if dtype == "continuous_hazard":
                rate_per_hour = dyn.get("hazard_rate_per_hour")
                rate_per_day = dyn.get("hazard_rate_per_day")
                if rate_per_hour is None and rate_per_day is not None:
                    rate_per_hour = rate_per_day / 24.0
                if rate_per_hour is None or rate_per_hour <= 0:
                    records.append(CheckRecord(
                        check_id=f"1.6.{eid}_temporal_distribution",
                        status="FAIL",
                        diagnostic=(
                            f"{label} dynamics.type=continuous_hazard but "
                            f"no positive hazard_rate_per_hour declared. "
                            f"Declare a positive rate or change the type."
                        ),
                        evidence={"element": eid, "type": dtype,
                                  "hazard_rate_per_hour": rate_per_hour},
                        suspected_location="DSL (dynamics.hazard_rate_per_hour)",
                    ))
                    continue
                ks = _ks_exponential(iet_hours, rate_per_hour)
                # KS critical value for α=0.05, n=30 is ~0.24; tighten with n.
                ks_threshold = max(0.15, 1.36 / (n ** 0.5))
                ks_pass = ks <= ks_threshold
                cluster_pass = hour_cluster <= 0.25
                status = "PASS" if (ks_pass and cluster_pass) else "FAIL"
                diag_extra = ""
                if not cluster_pass:
                    diag_extra = (f" Inter-event times also show hour-boundary "
                                  f"clustering ({hour_cluster:.0%} of events "
                                  f"within ±60s of integer hours) — strong "
                                  f"signal that the sim implements a discrete "
                                  f"per-hour check rather than a continuous hazard.")
                records.append(CheckRecord(
                    check_id=f"1.6.{eid}_temporal_distribution",
                    status=status,
                    diagnostic=(
                        f"{label}: dynamics.type=continuous_hazard with rate "
                        f"{rate_per_hour:.4f}/hr. KS gap vs Exp(rate)="
                        f"{ks:.3f} (threshold {ks_threshold:.3f}); "
                        f"hour-cluster fraction={hour_cluster:.2%} "
                        f"(threshold 25%). {n} transitions."
                        f"{diag_extra}"
                    ),
                    evidence={"element": eid, "label": label, "type": dtype,
                              "hazard_rate_per_hour": rate_per_hour,
                              "n_observed": n,
                              "ks_gap": ks, "ks_threshold": ks_threshold,
                              "hour_cluster_fraction": hour_cluster,
                              "ks_pass": ks_pass, "cluster_pass": cluster_pass},
                    suspected_location=(
                        "sim_module (transition implemented as discrete "
                        "per-hour Bernoulli check; replace with exponential "
                        "time-to-event sampling for continuous-hazard semantics) "
                        "OR DSL (relax dynamics.type to 'discrete_check' if "
                        "discrete is the intended mechanism)"
                    ) if status == "FAIL" else None,
                ))
            elif dtype == "discrete_check":
                cadence_h = dyn.get("check_cadence_hours")
                if cadence_h is None or cadence_h <= 0:
                    records.append(CheckRecord(
                        check_id=f"1.6.{eid}_temporal_distribution",
                        status="FAIL",
                        diagnostic=(
                            f"{label} dynamics.type=discrete_check but no "
                            f"positive check_cadence_hours declared."
                        ),
                        evidence={"element": eid, "type": dtype,
                                  "check_cadence_hours": cadence_h},
                        suspected_location="DSL (dynamics.check_cadence_hours)",
                    ))
                    continue
                cadence_s = cadence_h * 3600.0
                window_s = max(60.0, cadence_s * 0.02)
                cluster_frac = _hour_cluster_fraction(
                    times, window_s=window_s) if cadence_h == 1.0 else None
                # Generic cluster fraction: events that fall within window_s
                # of any integer multiple of the cadence.
                hits = 0
                for t in times:
                    phase = t % cadence_s
                    if phase <= window_s or phase >= cadence_s - window_s:
                        hits += 1
                cadence_cluster_frac = hits / n
                cluster_pass = cadence_cluster_frac >= 0.5
                status = "PASS" if cluster_pass else "FAIL"
                records.append(CheckRecord(
                    check_id=f"1.6.{eid}_temporal_distribution",
                    status=status,
                    diagnostic=(
                        f"{label}: dynamics.type=discrete_check with cadence "
                        f"{cadence_h}h. {cadence_cluster_frac:.0%} of {n} "
                        f"transitions fall within ±{window_s:.0f}s of an "
                        f"integer-cadence boundary (need ≥ 50% for "
                        f"discrete-check consistency)."
                    ),
                    evidence={"element": eid, "label": label, "type": dtype,
                              "check_cadence_hours": cadence_h,
                              "n_observed": n,
                              "cadence_cluster_fraction": cadence_cluster_frac,
                              "cluster_pass": cluster_pass},
                    suspected_location=(
                        "sim_module (transition fires continuously rather "
                        "than at the declared discrete cadence) OR DSL "
                        "(relax dynamics.type to 'continuous_hazard' if "
                        "continuous is the intended mechanism)"
                    ) if status == "FAIL" else None,
                ))
            elif dtype == "event_driven":
                trig = dyn.get("trigger_event")
                if not trig:
                    records.append(CheckRecord(
                        check_id=f"1.6.{eid}_temporal_distribution",
                        status="FAIL",
                        diagnostic=(
                            f"{label} dynamics.type=event_driven but no "
                            f"trigger_event declared."
                        ),
                        evidence={"element": eid, "type": dtype,
                                  "trigger_event": trig},
                        suspected_location="DSL (dynamics.trigger_event)",
                    ))
                    continue
                # Co-occurrence test: each transition should have a
                # trigger_event on the same entity within a short window.
                # (Approximate; precise pairing is a Phase 0 contract.)
                CO_WINDOW = 60.0
                trig_times: dict[str, list[float]] = defaultdict(list)
                for ev in trace:
                    if ev.get("event") == trig and float(ev.get("time", 0.0)) >= warmup_t:
                        eid_ev = ev.get("entity_id")
                        if eid_ev:
                            trig_times[eid_ev].append(float(ev.get("time", 0.0)))
                transition_evs = [
                    ev for ev in trace
                    if (ev.get("event") == (target_event or terminal_event_name)
                        or (ev.get("event") == "state_change"
                            and ev.get("next_state") == target_next_state))
                    and float(ev.get("time", 0.0)) >= warmup_t
                ]
                accompanied = 0
                for tev in transition_evs:
                    eid_ev = tev.get("entity_id")
                    t = float(tev.get("time", 0.0))
                    nearby = trig_times.get(eid_ev, [])
                    if any(abs(tt - t) <= CO_WINDOW for tt in nearby):
                        accompanied += 1
                ratio = accompanied / max(1, len(transition_evs))
                cluster_pass = ratio >= 0.85
                status = "PASS" if cluster_pass else "FAIL"
                records.append(CheckRecord(
                    check_id=f"1.6.{eid}_temporal_distribution",
                    status=status,
                    diagnostic=(
                        f"{label}: dynamics.type=event_driven with "
                        f"trigger_event={trig!r}. {accompanied}/"
                        f"{len(transition_evs)} ({ratio:.0%}) transitions "
                        f"co-occur with the trigger (need ≥ 85%)."
                    ),
                    evidence={"element": eid, "label": label, "type": dtype,
                              "trigger_event": trig,
                              "n_transitions": len(transition_evs),
                              "accompanied": accompanied, "ratio": ratio,
                              "cluster_pass": cluster_pass},
                    suspected_location=(
                        "sim_module (transitions fire without the declared "
                        "trigger event nearby — either add the trigger "
                        "co-emission or revise dynamics.type)"
                    ) if status == "FAIL" else None,
                ))
            else:
                records.append(CheckRecord(
                    check_id=f"1.6.{eid}_temporal_distribution",
                    status="INFO",
                    diagnostic=(
                        f"{label}: dynamics.type={dtype!r} not recognised "
                        f"(use continuous_hazard / discrete_check / "
                        f"event_driven / unspecified)."
                    ),
                    evidence={"element": eid, "type": dtype},
                ))

    tally = _tally(records)
    return PhaseReport("phase1", _aggregate_status(tally), tally, records, time.time() - t0)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Validation (limiting / extreme regimes; M/M/c gated)
# ─────────────────────────────────────────────────────────────────────────────

def _looks_like_mmc(cfg: dict) -> tuple[bool, dict]:
    resources = _resources_from_config(cfg)
    if len(resources) != 1:
        return False, {}
    arrival = (cfg.get("arrival_distribution") or {})
    if not isinstance(arrival, dict):
        return False, {}
    arr_type = (arrival.get("type") or arrival.get("distribution") or "").lower()
    if arr_type not in ("exponential", "poisson"):
        return False, {}
    if cfg.get("preemption_rules") or cfg.get("coordination_patterns"):
        return False, {}
    rname, rinfo = next(iter(resources.items()))
    cap = rinfo.get("capacity", 1)
    svc = (cfg.get("service_distributions") or {}).get(rname) or {}
    svc_type = (svc.get("type") or svc.get("distribution") or "").lower()
    if svc_type != "exponential":
        return False, {}
    lam = arrival.get("rate") or arrival.get("lambda")
    mu = svc.get("rate") or (1.0 / svc["mean"] if svc.get("mean") else None)
    if lam is None or mu is None:
        return False, {}
    return True, {"lam": lam, "mu": mu, "c": cap, "resource": rname}


def _erlang_c_wq(lam: float, mu: float, c: int) -> float:
    rho = lam / (c * mu)
    if rho >= 1:
        return float("inf")
    a = lam / mu
    s = sum((a ** n) / math.factorial(n) for n in range(c))
    s += (a ** c) / (math.factorial(c) * (1 - rho))
    P0 = 1.0 / s
    Pq = (a ** c) / (math.factorial(c) * (1 - rho)) * P0
    return Pq / (c * mu - lam)


def _mean_service_time(cfg: dict) -> float | None:
    means: list[float] = []
    for s in (cfg.get("service_distributions") or {}).values():
        if isinstance(s, dict) and s.get("mean"):
            means.append(s["mean"])
        elif isinstance(s, dict) and s.get("rate"):
            means.append(1.0 / s["rate"])
    return statistics.mean(means) if means else None


def validate_phase2(sim_module, config: dict | None = None,
                    rel_tol: float = 0.20) -> PhaseReport:
    t0 = time.time()
    records: list[CheckRecord] = []
    base_cfg = dict(config or getattr(sim_module, "DEFAULT_CONFIG", {}))

    # 2.1 M/M/c applicability and benchmark
    ok, ingr = _looks_like_mmc(base_cfg)
    records.append(CheckRecord(
        check_id="2.1_mmc_applicability",
        status="INFO",
        diagnostic=("System matches M/M/c gate — Erlang-C benchmark fires below"
                    if ok else
                    "System structure does not match M/M/c gate — Erlang-C skipped"),
        evidence={"qualifies": ok, **ingr},
    ))
    if ok:
        result, block = _safe_run(sim_module, base_cfg, f"2.1.{ingr['resource']}_mmc",
                                  "phase2_mmc")
        if block:
            records.append(block)
        else:
            sojourns = _entity_sojourn(result.get("trace", []))
            if sojourns:
                W_obs = statistics.mean(sojourns.values())
                Wq_pred = _erlang_c_wq(ingr["lam"], ingr["mu"], ingr["c"])
                W_pred = Wq_pred + 1.0 / ingr["mu"]
                rel = abs(W_obs - W_pred) / max(W_obs, 1e-9)
                status = ("PASS" if rel <= rel_tol
                          else "WARN" if rel <= 2 * rel_tol else "FAIL")
                records.append(CheckRecord(
                    check_id=f"2.1.{ingr['resource']}_mmc_benchmark",
                    status=status,
                    diagnostic=f"W_obs={W_obs:.2f} vs Erlang-C W={W_pred:.2f}; rel_err {rel:.1%}",
                    evidence={"W_observed": W_obs, "W_predicted": W_pred, "rel_error": rel, **ingr},
                    suspected_location="sim_module (service or arrival distribution)" if status == "FAIL" else None,
                ))

    # 2.2.1 limiting_zero_load
    arr = dict(base_cfg.get("arrival_distribution") or {})
    if "rate" in arr or "lambda" in arr:
        key = "rate" if "rate" in arr else "lambda"
        original = arr[key]
        arr_zl = dict(arr); arr_zl[key] = max(original * 0.01, 1e-6)
        cfg_zl = dict(base_cfg); cfg_zl["arrival_distribution"] = arr_zl
        result, block = _safe_run(sim_module, cfg_zl, "2.2.1_zero_load", "phase2_zero_load")
        if block:
            records.append(block)
        else:
            sojourns = _entity_sojourn(result.get("trace", []))
            if sojourns:
                W_obs = statistics.mean(sojourns.values())
                E_S = _mean_service_time(cfg_zl)
                if E_S:
                    rel = abs(W_obs - E_S) / max(W_obs, 1e-9)
                    status = "PASS" if rel <= 0.5 else "WARN"
                    records.append(CheckRecord(
                        check_id="2.2.1_limiting_zero_load",
                        status=status,
                        diagnostic=f"At λ→0: W_obs={W_obs:.2f} vs E[S]={E_S:.2f}; rel {rel:.1%}",
                        evidence={"W_obs": W_obs, "E_S": E_S, "rel": rel,
                                  "lambda_factor": 0.01},
                    ))
                else:
                    records.append(CheckRecord(
                        check_id="2.2.1_limiting_zero_load",
                        status="INFO",
                        diagnostic=f"At λ→0 W_obs={W_obs:.2f}; no declared E[S] for comparison",
                        evidence={"W_obs": W_obs},
                    ))
            else:
                records.append(CheckRecord(
                    check_id="2.2.1_limiting_zero_load",
                    status="WARN",
                    diagnostic="At λ→0 the simulation produced no completed entities",
                    evidence={"lambda_factor": 0.01},
                ))
    else:
        records.append(CheckRecord(
            check_id="2.2.1_limiting_zero_load",
            status="INFO",
            diagnostic="No arrival_distribution.rate found — cannot probe zero-load regime",
            evidence={},
        ))

    # 2.2.2 limiting_heavy_load — M/G/c-appropriate criteria
    # The textbook "Wq ≥ 5·E[S] at ρ=0.95" comes from M/M/1; for M/G/c with
    # low-CV service (e.g., triangular) and c >> 1, the multi-server effect
    # combined with the Pollaczek-Khinchine factor (1+CV²)/2 reduces Wq by
    # an order of magnitude. We replace the M/M/1 threshold with substantive
    # M/G/c criteria: (a) ρ within ±0.05 of target, (b) peak occupancy
    # equals capacity, (c) ≥ 50% of arrivals queue, (d) mean Wq ≥ 0.10·E[S],
    # (e) heavy tail (max wait ≥ 5× mean wait).
    arr2 = dict(base_cfg.get("arrival_distribution") or {})
    if "rate" in arr2 or "lambda" in arr2:
        key = "rate" if "rate" in arr2 else "lambda"
        E_S = _mean_service_time(base_cfg)
        if E_S:
            # Target ρ ≈ 0.95. For an M/G/c system ρ = λ·E[S]/c, so to hit
            # ρ=0.95 the arrival rate must be 0.95·c/E[S], NOT 0.95/E[S]
            # (which only loads a single server and leaves a c-server system
            # at ρ≈0.95/c — i.e. light load, defeating the heavy-load test).
            # Use the bottleneck server count: the smallest declared capacity
            # among resources (the stage that saturates first).
            _caps = [r.get("capacity") for r in _resources_from_config(base_cfg).values()
                     if isinstance(r, dict) and isinstance(r.get("capacity"), (int, float)) and r.get("capacity") > 0]
            c_bottleneck = min(_caps) if _caps else 1
            arr_hl = dict(arr2); arr_hl[key] = 0.95 * c_bottleneck / E_S
            cfg_hl = dict(base_cfg); cfg_hl["arrival_distribution"] = arr_hl
            result, block = _safe_run(sim_module, cfg_hl, "2.2.2_heavy_load", "phase2_heavy_load")
            if block:
                records.append(block)
            else:
                trace_hl = result.get("trace", [])
                sojourns = _entity_sojourn(trace_hl)
                if sojourns:
                    W_obs = statistics.mean(sojourns.values())
                    # Collect per-queue waits and queueing fraction
                    waits = []
                    queued_eids = set()
                    arr_set = {ev["entity_id"] for ev in trace_hl
                               if ev.get("event") == "system_arrival"}
                    for q in _discover_queues(trace_hl):
                        enter_t = {}
                        for ev in trace_hl:
                            if ev.get("event") == "queue_enter" and ev.get("queue") == q:
                                enter_t.setdefault(ev["entity_id"], ev["time"])
                                queued_eids.add(ev["entity_id"])
                        for ev in trace_hl:
                            if ev.get("event") == "queue_exit" and ev.get("queue") == q:
                                t0 = enter_t.get(ev["entity_id"])
                                if t0 is not None:
                                    waits.append(ev["time"] - t0)
                    queue_frac = len(queued_eids) / max(len(arr_set), 1)
                    mean_wq = statistics.mean(waits) if waits else 0.0
                    max_wq = max(waits) if waits else 0.0
                    tail_ratio = max_wq / max(mean_wq, 1e-9)
                    # Substantive criteria
                    crit_queue_frac = queue_frac >= 0.50 if waits else None
                    crit_mean_wait = mean_wq >= 0.10 * E_S if waits else None
                    crit_heavy_tail = tail_ratio >= 5.0 if waits else None
                    if not waits:
                        # No queue events emitted — fall back to W vs E[S] check, which
                        # is acceptable for sims that don't emit queue_enter/queue_exit.
                        if W_obs >= 2 * E_S:
                            status = "PASS"
                        elif W_obs >= 1.2 * E_S:
                            status = "WARN"
                        else:
                            status = "FAIL"
                        records.append(CheckRecord(
                            check_id="2.2.2_limiting_heavy_load",
                            status=status,
                            diagnostic=f"At ρ≈0.95: W_obs={W_obs:.2f}, E[S]={E_S:.2f} "
                                       f"(no queue events emitted; using W/E[S] ratio)",
                            evidence={"W_obs": W_obs, "E_S": E_S,
                                      "ratio": W_obs / max(E_S, 1e-9)},
                            suspected_location="sim_module (service-time distribution or queueing)" if status == "FAIL" else None,
                        ))
                    else:
                        passed = sum(c for c in (crit_queue_frac, crit_mean_wait, crit_heavy_tail)
                                     if c is True)
                        if passed >= 2:
                            status = "PASS"
                        elif passed == 1:
                            status = "WARN"
                        else:
                            status = "FAIL"
                        records.append(CheckRecord(
                            check_id="2.2.2_limiting_heavy_load",
                            status=status,
                            diagnostic=(
                                f"At ρ≈0.95: queue_frac={queue_frac:.0%} (≥50%? {crit_queue_frac}), "
                                f"mean Wq={mean_wq:.2f} vs 0.10·E[S]={0.10*E_S:.2f} "
                                f"(≥? {crit_mean_wait}), max/mean={tail_ratio:.1f} "
                                f"(≥5? {crit_heavy_tail}). M/G/c criteria — passed {passed}/3."
                            ),
                            evidence={"W_obs": W_obs, "E_S": E_S,
                                      "queue_fraction": queue_frac,
                                      "mean_wq": mean_wq, "max_wq": max_wq,
                                      "tail_ratio": tail_ratio,
                                      "criteria_passed": passed},
                            suspected_location="sim_module (queueing logic)" if status == "FAIL" else None,
                        ))
                else:
                    records.append(CheckRecord(
                        check_id="2.2.2_limiting_heavy_load",
                        status="WARN",
                        diagnostic="Heavy-load regime produced no completed entities (possibly unstable)",
                        evidence={},
                    ))
        else:
            records.append(CheckRecord(
                check_id="2.2.2_limiting_heavy_load",
                status="INFO",
                diagnostic="No declared E[S] — cannot calibrate heavy-load arrival rate",
                evidence={},
            ))
    else:
        records.append(CheckRecord(
            check_id="2.2.2_limiting_heavy_load",
            status="INFO",
            diagnostic="No arrival_distribution.rate — cannot probe heavy-load",
            evidence={},
        ))

    # 2.3.1 extreme_zero_capacity
    # Classical extreme-condition validation: verify the model exhibits
    # degenerate behaviour (no service / high queue / losses) as capacity → 0.
    #
    # Library limitation handling: many simulation libraries (SimPy among them)
    # structurally refuse Resource(capacity=0) and raise ValueError. When this
    # happens, the check falls back to cap=1 — the smallest capacity the
    # library will represent — and verifies near-degenerate behaviour there
    # (sojourn substantially higher than baseline, OR substantial losses
    # appearing that were absent at baseline). This tests the INTENT of the
    # check (extreme low capacity → degenerate behaviour) on a configuration
    # the library can run, and produces a meaningful PASS/FAIL rather than a
    # WARN that depends on whether the library can represent the boundary
    # point exactly.
    #
    # Regime-awareness: the "unbounded queue divergence at extreme low
    # capacity" expectation is a steady-state property. A terminating or
    # burst simulation has a finite arrival population that drains
    # eventually even at cap=1 — a correctly-modeled MASCAL system does
    # not diverge, so the check cannot distinguish "correctly-modeled
    # terminating system" from "broken model." For terminating regimes,
    # we skip with INFO rather than emit a WARN that has no defensible
    # interpretation.
    _zc_regime = _simulation_regime(base_cfg)
    if _zc_regime["type"] in ("terminating", "burst"):
        records.append(CheckRecord(
            check_id="2.3.1_extreme_zero_capacity",
            status="INFO",
            diagnostic=(
                f"Extreme-zero-capacity divergence is a steady-state "
                f"property; skipped for regime={_zc_regime['type']!r}. "
                f"A finite terminating burst drains eventually even at "
                f"cap=1 without diverging, so this check cannot "
                f"discriminate credible from broken models here."
            ),
            evidence={"regime": _zc_regime["type"]},
        ))
        res_cfg = {}  # skip the loop body below
    else:
        res_cfg = _resources_from_config(base_cfg)
    if res_cfg:
        rname = next(iter(res_cfg))
        cfg_zc = dict(base_cfg)
        cfg_zc["resources"] = _set_capacity(base_cfg.get("resources", {}), rname, 0)
        result, block = _safe_run(sim_module, cfg_zc, "2.3.1_zero_capacity", "phase2_zero_cap")

        # Detect library-style "capacity must be positive" refusals so we
        # can fall back to cap=1 with documentation. We use TWO complementary
        # detection paths because simulation libraries and their wrappers
        # phrase the constraint variably. Concrete observed phrasings:
        #   SimPy:        ValueError: "capacity" must be > 0
        #   SimPy alt:    ValueError: 'capacity' must be > 0
        #   Generic:      capacity must be positive
        #   Custom:       Invalid capacity: 0, Minimum capacity is 1, ...
        # Compound match handles the quoted-parameter-name case that simple
        # substring matching cannot, because the literal token "capacity must
        # be > 0" does not appear in '"capacity" must be > 0' (the quote
        # interrupts the substring). The compound match looks for "capacity"
        # mention anywhere plus a positivity-constraint phrase anywhere.
        block_text = (block.diagnostic or "").lower() if block else ""
        # Direct tokens: full phrase present in the exception text.
        direct_tokens = (
            "capacity must be > 0",
            "capacity must be positive",
            "capacity must be at least",
            "capacity > 0",
            "capacity >= 1",
            "capacity is required to be",
            "minimum capacity",
            "invalid capacity",
            "capacity cannot be zero",
            "capacity cannot be 0",
            "capacity of 0",
            "valueerror: capacity",
            'valueerror: "capacity"',
            "valueerror: 'capacity'",
            "capacity value",
        )
        # Positivity-constraint phrases that, when co-occurring with a
        # "capacity" mention anywhere in the text, identify the BLOCK as a
        # library-class capacity refusal regardless of punctuation around
        # the parameter name.
        constraint_phrases = (
            "must be > 0",
            "must be positive",
            "must be at least 1",
            "must be greater than 0",
            "must be nonzero",
            "must be strictly positive",
            ">= 1",
        )
        mentions_capacity = (
            "capacity" in block_text
            or "cap=" in block_text
            or "cap = " in block_text
        )
        direct_match = any(tok in block_text for tok in direct_tokens)
        compound_match = mentions_capacity and any(
            phrase in block_text for phrase in constraint_phrases
        )
        non_positive_cap_block = bool(block) and (direct_match or compound_match)

        # Structural backstop: if both detection paths missed but cap=1 runs
        # cleanly AND produces near-degenerate behaviour (any measurable
        # sojourn increase or loss escalation), treat the cap=0 refusal as a
        # library-class limitation. The threshold is loosened to 1.5× sojourn
        # (any meaningful increase) or any non-trivial loss appearing — this
        # accommodates sims whose baseline capacity is already low enough
        # that cap=1 is close to baseline.
        if block and not non_positive_cap_block:
            cfg_probe = dict(base_cfg)
            cfg_probe["resources"] = _set_capacity(
                base_cfg.get("resources", {}), rname, 1)
            probe_result, probe_block = _safe_run(
                sim_module, cfg_probe,
                "2.3.1_min_capacity_probe", "phase2_min_cap_probe")
            if (not probe_block) and probe_result is not None:
                base_probe_result, base_probe_block = _safe_run(
                    sim_module, base_cfg,
                    "2.3.1_baseline_for_min_cap_probe",
                    "phase2_min_cap_base_probe")
                if (not base_probe_block) and base_probe_result is not None:
                    s_min = _entity_sojourn(probe_result.get("trace", []))
                    s_base = _entity_sojourn(base_probe_result.get("trace", []))
                    W_min_probe = (statistics.mean(s_min.values())
                                   if s_min else 0.0)
                    W_base_probe = (statistics.mean(s_base.values())
                                    if s_base else 0.0)
                    n_loss_min_probe = sum(
                        1 for ev in probe_result.get("trace", [])
                        if ev.get("event") == "loss")
                    n_loss_base_probe = sum(
                        1 for ev in base_probe_result.get("trace", [])
                        if ev.get("event") == "loss")
                    sojourn_increase = (
                        W_base_probe > 0 and W_min_probe >= 1.5 * W_base_probe)
                    loss_increase = (
                        n_loss_min_probe >= 5
                        and n_loss_min_probe >= 2 * max(n_loss_base_probe, 1))
                    if sojourn_increase or loss_increase:
                        non_positive_cap_block = True

        if block and not non_positive_cap_block:
            records.append(CheckRecord(
                check_id="2.3.1_extreme_zero_capacity",
                status="WARN",
                diagnostic=f"Setting {rname}.capacity=0 caused {block.diagnostic}. "
                           "Sim raised non-library exception; investigate before assuming the "
                           "cap=0 limit is handled gracefully.",
                evidence={"resource": rname, "block": block.diagnostic},
                suspected_location=f"sim_module (no graceful handling of {rname}.capacity=0)",
            ))
        elif non_positive_cap_block:
            # Library refuses cap=0. Fall back to cap=1 and verify
            # near-degenerate behaviour relative to baseline.
            cfg_min = dict(base_cfg)
            cfg_min["resources"] = _set_capacity(
                base_cfg.get("resources", {}), rname, 1)
            result_min, block_min = _safe_run(
                sim_module, cfg_min, "2.3.1_minimum_capacity", "phase2_min_cap")
            # Re-run baseline once for an apples-to-apples sojourn comparison.
            result_base, block_base = _safe_run(
                sim_module, base_cfg, "2.3.1_baseline_for_min_cap",
                "phase2_min_cap_base")
            if block_min or block_base:
                records.append(CheckRecord(
                    check_id="2.3.1_extreme_zero_capacity",
                    status="WARN",
                    diagnostic=(f"Library refused {rname}.capacity=0 "
                                f"({block.diagnostic}); cap=1 fallback also blocked."),
                    evidence={"resource": rname, "zero_block": block.diagnostic,
                              "min_block": (block_min and block_min.diagnostic) or
                                           (block_base and block_base.diagnostic)},
                ))
            else:
                soj_min = _entity_sojourn(result_min.get("trace", []))
                soj_base = _entity_sojourn(result_base.get("trace", []))
                W_min = (statistics.mean(soj_min.values())
                         if soj_min else 0.0)
                W_base = (statistics.mean(soj_base.values())
                          if soj_base else 0.0)
                n_loss_min = sum(1 for ev in result_min.get("trace", [])
                                 if ev.get("event") == "loss")
                n_loss_base = sum(1 for ev in result_base.get("trace", [])
                                  if ev.get("event") == "loss")
                # Near-degenerate behaviour: at cap=1 either sojourn is much
                # higher than baseline (queue-blowup signature) or losses are
                # substantially higher than baseline (loss-handling signature).
                sojourn_blowup = W_base > 0 and W_min >= 3.0 * W_base
                losses_appeared = n_loss_min > 5 and n_loss_min >= 5 * max(n_loss_base, 1)
                if sojourn_blowup or losses_appeared:
                    records.append(CheckRecord(
                        check_id="2.3.1_extreme_zero_capacity",
                        status="PASS",
                        diagnostic=(
                            f"Library refused {rname}.capacity=0 (expected for "
                            f"SimPy-class libraries); fallback at {rname}.capacity=1 "
                            f"exhibits "
                            + (f"sojourn blowup (W_min={W_min:.1f} vs W_base={W_base:.1f})"
                               if sojourn_blowup else
                               f"loss escalation ({n_loss_min} losses vs {n_loss_base} baseline)")
                            + ", consistent with the degenerate-capacity limit."),
                        evidence={"resource": rname, "fallback_capacity": 1,
                                  "W_min": W_min, "W_base": W_base,
                                  "losses_min": n_loss_min, "losses_base": n_loss_base,
                                  "library_refusal": block.diagnostic},
                    ))
                else:
                    records.append(CheckRecord(
                        check_id="2.3.1_extreme_zero_capacity",
                        status="WARN",
                        diagnostic=(
                            f"Library refused {rname}.capacity=0; fallback at "
                            f"{rname}.capacity=1 did not exhibit degenerate behaviour "
                            f"(W_min={W_min:.1f}, W_base={W_base:.1f}, "
                            f"losses_min={n_loss_min}, losses_base={n_loss_base}). "
                            f"Model may not be responding to extreme low capacity as "
                            f"the limiting-regime expectation requires."),
                        evidence={"resource": rname, "fallback_capacity": 1,
                                  "W_min": W_min, "W_base": W_base,
                                  "losses_min": n_loss_min, "losses_base": n_loss_base,
                                  "library_refusal": block.diagnostic},
                        suspected_location=f"sim_module (response to {rname}.capacity=1)",
                    ))
        else:
            trace_zc = result.get("trace", [])
            n_arr = sum(1 for ev in trace_zc if ev.get("event") == "system_arrival")
            n_svc = sum(1 for ev in trace_zc if ev.get("event") == "service_start"
                        and ev.get("resource") == rname)
            n_loss = sum(1 for ev in trace_zc if ev.get("event") == "loss")
            n_dep = sum(1 for ev in trace_zc if ev.get("event") == "system_departure")
            # PASS condition: either no service starts on the zero-cap resource AND
            # entities are accounted for (loss + still-queued = arrivals - departures).
            if n_svc == 0 and (n_loss > 0 or n_arr > n_dep):
                records.append(CheckRecord(
                    check_id="2.3.1_extreme_zero_capacity",
                    status="PASS",
                    diagnostic=f"At {rname}.capacity=0: 0 service_starts; "
                               f"{n_loss} losses, {n_arr - n_dep} entities not departed",
                    evidence={"resource": rname, "service_starts": n_svc,
                              "arrivals": n_arr, "departures": n_dep, "losses": n_loss},
                ))
            elif n_svc > 0:
                records.append(CheckRecord(
                    check_id="2.3.1_extreme_zero_capacity",
                    status="FAIL",
                    diagnostic=f"At {rname}.capacity=0: observed {n_svc} service_starts on a "
                               f"zero-capacity resource",
                    evidence={"resource": rname, "service_starts": n_svc},
                    suspected_location=f"sim_module (capacity check on '{rname}')",
                ))
            else:
                records.append(CheckRecord(
                    check_id="2.3.1_extreme_zero_capacity",
                    status="WARN",
                    diagnostic=f"At {rname}.capacity=0: no service starts but no losses or "
                               "queued entities — sim may have early-aborted arrivals silently",
                    evidence={"resource": rname, "arrivals": n_arr, "departures": n_dep,
                              "losses": n_loss},
                ))

    # 2.3.2 extreme_infinite_capacity
    if res_cfg:
        rname = next(iter(res_cfg))
        cfg_ic = dict(base_cfg)
        cfg_ic["resources"] = _set_capacity(base_cfg.get("resources", {}), rname, 10000)
        result, block = _safe_run(sim_module, cfg_ic, "2.3.2_inf_capacity", "phase2_inf_cap")
        if block:
            records.append(block)
        else:
            sojourns = _entity_sojourn(result.get("trace", []))
            E_S = _mean_service_time(base_cfg)
            if sojourns and E_S:
                W_obs = statistics.mean(sojourns.values())
                # With effectively infinite capacity, W ≈ E[S] (no queueing)
                rel = abs(W_obs - E_S) / max(W_obs, 1e-9)
                status = "PASS" if rel <= 0.5 else "WARN"
                records.append(CheckRecord(
                    check_id="2.3.2_extreme_infinite_capacity",
                    status=status,
                    diagnostic=f"At {rname}.capacity=∞: W_obs={W_obs:.2f} vs E[S]={E_S:.2f}; rel {rel:.1%}",
                    evidence={"resource": rname, "W_obs": W_obs, "E_S": E_S, "rel": rel},
                ))
            else:
                records.append(CheckRecord(
                    check_id="2.3.2_extreme_infinite_capacity",
                    status="INFO",
                    diagnostic="Insufficient data to verify infinite-capacity regime",
                    evidence={},
                ))

    tally = _tally(records)
    return PhaseReport("phase2", _aggregate_status(tally), tally, records, time.time() - t0)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — Uncertainty Quantification
# ─────────────────────────────────────────────────────────────────────────────

def _replication_kpi_series(sim_module, base_cfg: dict, seeds: list[int],
                            window_size: float | None = None,
                            records: list | None = None) -> list[list[float]]:
    """Run N replications; return per-rep list of bin-averaged in-system census
    over equal-width time windows. Used by Welch's procedure and MSER-5.

    AUDIT FIX (M3): a replication that crashes is no longer silently dropped
    — when a `records` list is supplied, the BLOCK CheckRecord is appended
    so a sim that crashes on some seeds is visible in the phase report
    rather than just thinning the Welch sample."""
    horizon_default = base_cfg.get("run_length", 100.0)
    if window_size is None:
        window_size = horizon_default / 50.0
    series: list[list[float]] = []
    for s in seeds:
        cfg_r = dict(base_cfg); cfg_r["seed"] = s
        result, block = _safe_run(sim_module, cfg_r, f"3.1.x_rep{s}", f"welch_rep_{s}")
        if block:
            if records is not None:
                records.append(block)
            continue
        trace = result.get("trace", [])
        # bin in-system count over windows
        deltas = []
        for ev in trace:
            if ev.get("event") == "system_arrival":
                deltas.append((ev["time"], +1))
            elif ev.get("event") in ("system_departure", "loss"):
                deltas.append((ev["time"], -1))
        deltas.sort()
        if not deltas:
            continue
        t_start = 0.0
        t_end = max(d[0] for d in deltas)
        n_bins = max(int((t_end - t_start) / window_size), 1)
        bins = [0.0] * n_bins
        census = 0
        last_t = t_start
        bin_idx = 0
        for t, d in deltas:
            while bin_idx < n_bins and (bin_idx + 1) * window_size <= t - t_start:
                # accumulate the time-weighted census in this bin up to its end
                bin_end = t_start + (bin_idx + 1) * window_size
                bins[bin_idx] += census * (bin_end - last_t)
                last_t = bin_end
                bin_idx += 1
            if bin_idx >= n_bins:
                break
            bins[bin_idx] += census * (t - last_t)
            last_t = t
            census += d
        # normalise per bin to averages
        bins = [b / window_size for b in bins]
        series.append(bins)
    return series


def _welch_truncation(per_rep_series: list[list[float]], window: int = 5,
                      sustain: int = 3) -> int:
    """Welch's procedure: average across reps, smooth with a moving window,
    return the bin index at which the smoothed series first *sustains*
    stability — `sustain` consecutive bins whose first difference is below 5%
    of the overall mean. Returns -1 if the series never stabilises (the caller
    treats -1 as "warmup could not be located", a real concern, rather than
    silently reporting the last bin as the truncation point)."""
    if not per_rep_series:
        return -1
    n_bins = min(len(s) for s in per_rep_series)
    if n_bins < 2 * window:
        return -1
    avg = [statistics.mean(s[i] for s in per_rep_series) for i in range(n_bins)]
    smooth = [statistics.mean(avg[i - window:i + window + 1])
              for i in range(window, n_bins - window)]
    if len(smooth) <= sustain:
        return -1
    overall = statistics.mean(smooth)
    threshold = 0.05 * max(abs(overall), 1e-9)
    run = 0
    for i in range(1, len(smooth)):
        if abs(smooth[i] - smooth[i - 1]) <= threshold:
            run += 1
            if run >= sustain:
                return (i - sustain + 1) + window  # first bin of the stable run
        else:
            run = 0
    return -1  # never sustained stability


def _mser(time_series: list[float]) -> int:
    """MSER truncation point on a (bin-averaged) series. Returns the index d
    that minimises SS(d) / (n-d)^2, the standard MSER statistic, where SS is
    the sum of squared deviations of the retained tail about its mean.

    Note: this runs on the per-bin averaged trajectory, so it is MSER applied
    at bin granularity rather than MSER-5's fixed batch-of-5 (the binning that
    feeds this already aggregates raw observations); the (n-d)^2 denominator is
    the standard one (no ad-hoc offset)."""
    n = len(time_series)
    if n < 10:
        return 0
    best_d = 0
    best_score = float("inf")
    for d in range(0, n - 1):
        tail = time_series[d:]
        m = statistics.mean(tail)
        ss = sum((x - m) ** 2 for x in tail)
        denom = (len(tail)) ** 2
        if denom <= 0:
            continue
        score = ss / denom
        if score < best_score:
            best_score = score
            best_d = d
    return best_d


def _trace_signature(trace: list[dict], n: int = 200) -> tuple:
    """Stable signature for determinism comparison: first-N (time, event, resource) tuples,
    deliberately ignoring entity_id since some sims allocate IDs from a global counter."""
    sig = []
    for ev in trace[:n]:
        sig.append((round(ev.get("time", 0.0), 6),
                    ev.get("event"),
                    ev.get("resource"),
                    ev.get("queue")))
    return tuple(sig)


def _arrival_times(trace: list[dict], n: int = 200) -> list[float]:
    return [ev["time"] for ev in trace if ev.get("event") == "system_arrival"][:n]


def validate_phase3(sim_module, config: dict | None = None,
                    n_replications: int = 5,
                    rel_ci_threshold: float = 0.30) -> PhaseReport:
    t0 = time.time()
    records: list[CheckRecord] = []
    base_cfg = dict(config or getattr(sim_module, "DEFAULT_CONFIG", {}))

    # 3.1.1 warmup_config_support
    has_warmup = "warmup_time" in base_cfg or "warmup" in base_cfg
    records.append(CheckRecord(
        check_id="3.1.1_warmup_config_support",
        status="PASS" if has_warmup else "WARN",
        diagnostic=("config exposes warmup_time" if has_warmup
                    else "no warmup_time in config — initialization bias may contaminate metrics"),
        evidence={"warmup_keys": [k for k in ("warmup_time", "warmup") if k in base_cfg]},
        suspected_location="sim_module.DEFAULT_CONFIG" if not has_warmup else None,
    ))

    # 3.1.2 welch_method — use all available replications (capped by what was
    # actually requested), not a hard-coded 3.
    welch_seeds = [base_cfg.get("seed", 42) + i for i in range(max(n_replications, 2))]
    series = _replication_kpi_series(sim_module, base_cfg, welch_seeds,
                                     records=records)
    if len(series) >= 2:
        d_welch = _welch_truncation(series, window=5)
        n_bins = min(len(s) for s in series)
        if d_welch < 0:
            # never sustained stability — a real concern, not a "late but found"
            records.append(CheckRecord(
                check_id="3.1.2_welch_method",
                status="WARN",
                diagnostic=f"Welch's procedure: the averaged trajectory never sustained "
                           f"stability across {n_bins} bins — warmup could not be located; "
                           "the run may be too short or the system non-stationary",
                evidence={"truncation_bin": None, "n_bins": n_bins},
                suspected_location="sim_module.DEFAULT_CONFIG (run_length / warmup_time)",
            ))
        else:
            ratio = d_welch / max(n_bins, 1)
            records.append(CheckRecord(
                check_id="3.1.2_welch_method",
                status="PASS" if ratio < 0.5 else "WARN",
                diagnostic=(f"Welch's procedure: stabilization at bin {d_welch} of {n_bins} "
                            f"({ratio:.0%})") + ("" if ratio < 0.5
                            else " — late; declared warmup may be too short"),
                evidence={"truncation_bin": d_welch, "n_bins": n_bins, "ratio": ratio},
                suspected_location=("sim_module.DEFAULT_CONFIG.warmup_time (consider increasing)"
                                    if ratio >= 0.5 else None),
            ))
    else:
        records.append(CheckRecord(
            check_id="3.1.2_welch_method",
            status="WARN",
            diagnostic="Welch's procedure needs ≥2 successful replications",
            evidence={"available_reps": len(series)},
        ))

    # 3.1.3 mser5_method — warm-up truncation detection.
    # Terminating regimes have no meaningful steady state to warm past.
    # A declared warmup_time=0 for a terminating simulation is a correct
    # declaration, not an omission. Applying MSER-5 pushes the "truncation
    # point" arbitrarily late (95%+ in the MASCAL case) rather than
    # recognizing that no truncation is applicable. Skip with INFO.
    _mser_regime = _simulation_regime(base_cfg)
    if _mser_regime["type"] in ("terminating", "burst"):
        records.append(CheckRecord(
            check_id="3.1.3_mser5_method",
            status="INFO",
            diagnostic=(
                f"MSER-5 warm-up truncation is not applicable to "
                f"regime={_mser_regime['type']!r} — no steady state to "
                f"warm past. Skipped."
            ),
            evidence={"regime": _mser_regime["type"]},
        ))
    elif series:
        avg_series = [statistics.mean(s[i] for s in series)
                      for i in range(min(len(s) for s in series))]
        d_mser = _mser(avg_series)
        ratio2 = d_mser / max(len(avg_series), 1)
        records.append(CheckRecord(
            check_id="3.1.3_mser5_method",
            status="PASS" if ratio2 < 0.5 else "WARN",
            diagnostic=f"MSER-5 truncation point at bin {d_mser} of {len(avg_series)} "
                       f"({ratio2:.0%})",
            evidence={"truncation_bin": d_mser, "n_bins": len(avg_series), "ratio": ratio2},
        ))
    else:
        records.append(CheckRecord(
            check_id="3.1.3_mser5_method",
            status="INFO",
            diagnostic="MSER-5 needs ≥1 replication trajectory",
            evidence={},
        ))

    # 3.2.1 independent_seeds
    cfg_a = dict(base_cfg); cfg_a["seed"] = base_cfg.get("seed", 42)
    cfg_b = dict(base_cfg); cfg_b["seed"] = base_cfg.get("seed", 42) + 1000
    ra, ba = _safe_run(sim_module, cfg_a, "3.2.1_seed_a", "indep_seed_a")
    rb, bb = _safe_run(sim_module, cfg_b, "3.2.1_seed_b", "indep_seed_b")
    if ba or bb:
        records.append(ba or bb)
    else:
        sig_a = _trace_signature(ra.get("trace", []))
        sig_b = _trace_signature(rb.get("trace", []))
        independent = sig_a != sig_b
        records.append(CheckRecord(
            check_id="3.2.1_independent_seeds",
            status="PASS" if independent else "FAIL",
            diagnostic=("Different seeds produce different trace signatures (independence holds)"
                        if independent else
                        "Different seeds produce IDENTICAL trace signatures — RNG may not be seed-driven"),
            evidence={"seed_a": cfg_a["seed"], "seed_b": cfg_b["seed"]},
            suspected_location="sim_module (RNG instantiation)" if not independent else None,
        ))

    # 3.2.2 ci_construction
    means: list[float] = []
    for i in range(n_replications):
        cfg_r = dict(base_cfg); cfg_r["seed"] = base_cfg.get("seed", 42) + i
        result, block = _safe_run(sim_module, cfg_r, f"3.2.2_rep{i}", f"ci_rep_{i}")
        if block:
            records.append(block)
            continue
        sojourns = _entity_sojourn(result.get("trace", []))
        if sojourns:
            means.append(statistics.mean(sojourns.values()))
    if len(means) >= 2:
        m = statistics.mean(means)
        sd = statistics.stdev(means)
        half = 2 * sd / math.sqrt(len(means))
        rel = half / max(abs(m), 1e-9)
        if rel <= rel_ci_threshold:
            status = "PASS"
        elif rel <= 2 * rel_ci_threshold:
            status = "WARN"
        else:
            status = "FAIL"
        records.append(CheckRecord(
            check_id="3.2.2_ci_construction",
            status=status,
            diagnostic=f"Across {len(means)} reps: mean={m:.2f}, half-width={half:.2f}, rel={rel:.1%}",
            evidence={"mean": m, "half_width": half, "rel": rel,
                      "per_rep_means": means, "n_reps": len(means)},
        ))
    else:
        records.append(CheckRecord(
            check_id="3.2.2_ci_construction",
            status="WARN",
            diagnostic=f"Could not compute CI — only {len(means)} successful reps",
            evidence={"successful_reps": len(means)},
        ))

    # 3.3.1 same_seed_determinism
    cfg_d1 = dict(base_cfg); cfg_d1["seed"] = base_cfg.get("seed", 42)
    cfg_d2 = dict(base_cfg); cfg_d2["seed"] = base_cfg.get("seed", 42)
    rd1, bd1 = _safe_run(sim_module, cfg_d1, "3.3.1_det_1", "det_1")
    rd2, bd2 = _safe_run(sim_module, cfg_d2, "3.3.1_det_2", "det_2")
    if bd1 or bd2:
        records.append(bd1 or bd2)
    else:
        sig1 = _trace_signature(rd1.get("trace", []))
        sig2 = _trace_signature(rd2.get("trace", []))
        records.append(CheckRecord(
            check_id="3.3.1_same_seed_determinism",
            status="PASS" if sig1 == sig2 else "FAIL",
            diagnostic=("Same seed produces identical trace signature (CRN-ready)"
                        if sig1 == sig2 else
                        "Same seed produces different trace — CRN cannot give correlated samples"),
            evidence={"prefix_compared": 200},
            suspected_location="sim_module (untracked stochastic source or RNG seeding)" if sig1 != sig2 else None,
        ))

    # 3.3.2 paired_seed_crn_arrivals
    if not (bd1 or bd2):
        # Re-run with the same seed under a perturbed parameter and check the arrival prefix matches
        E_S_local = _mean_service_time(base_cfg)
        cfg_pa = dict(base_cfg); cfg_pa["seed"] = base_cfg.get("seed", 42)
        cfg_pb = dict(base_cfg); cfg_pb["seed"] = base_cfg.get("seed", 42)
        # Mild perturbation that shouldn't change arrival stream if RNGs are stream-separated
        if E_S_local and "service_distributions" in cfg_pa:
            sd_pb = dict(cfg_pa.get("service_distributions") or {})
            for k in list(sd_pb.keys())[:1]:
                if isinstance(sd_pb[k], dict) and "mean" in sd_pb[k]:
                    sd_pb[k] = dict(sd_pb[k]); sd_pb[k]["mean"] = sd_pb[k]["mean"] * 1.5
            cfg_pb["service_distributions"] = sd_pb
        ra2, _b1 = _safe_run(sim_module, cfg_pa, "3.3.2_pair_a", "crn_arr_a")
        rb2, _b2 = _safe_run(sim_module, cfg_pb, "3.3.2_pair_b", "crn_arr_b")
        # AUDIT FIX (M3): surface crashed perturbed runs instead of dropping.
        for _blk in (_b1, _b2):
            if _blk:
                records.append(_blk)
        if ra2 and rb2:
            arr_a = _arrival_times(ra2.get("trace", []))
            arr_b = _arrival_times(rb2.get("trace", []))
            n = min(len(arr_a), len(arr_b))
            matches = sum(1 for i in range(n) if abs(arr_a[i] - arr_b[i]) < 1e-9)
            ratio_m = matches / max(n, 1)
            if ratio_m == 1.0:
                status = "PASS"
            elif ratio_m >= 0.8:
                status = "WARN"
            else:
                status = "FAIL"
            records.append(CheckRecord(
                check_id="3.3.2_paired_seed_crn_arrivals",
                status=status,
                diagnostic=f"Paired-seed arrival prefix match: {matches}/{n} ({ratio_m:.0%}). "
                           "Stream separation enables full CRN variance reduction.",
                evidence={"matches": matches, "n_compared": n, "ratio": ratio_m},
                suspected_location="sim_module (single shared RNG across stochastic sources)" if status != "PASS" else None,
            ))

    # 3.3.3 crn_variance_reduction
    paired_diffs: list[float] = []
    unpaired_diffs: list[float] = []
    for i in range(min(3, n_replications)):
        seed = base_cfg.get("seed", 42) + i * 100
        # Paired: same seed for control and treatment
        cfg_c = dict(base_cfg); cfg_c["seed"] = seed
        cfg_t = dict(base_cfg); cfg_t["seed"] = seed
        # Treatment: 1.2× arrival rate
        if "rate" in (cfg_t.get("arrival_distribution") or {}):
            arr_t_dict = dict(cfg_t["arrival_distribution"])
            arr_t_dict["rate"] = arr_t_dict["rate"] * 1.2
            cfg_t["arrival_distribution"] = arr_t_dict
        rc, _b3 = _safe_run(sim_module, cfg_c, "3.3.3_pc", "crn_pair_c")
        rt, _b4 = _safe_run(sim_module, cfg_t, "3.3.3_pt", "crn_pair_t")
        for _blk in (_b3, _b4):
            if _blk:
                records.append(_blk)   # AUDIT FIX (M3)
        if rc and rt:
            sc = _entity_sojourn(rc.get("trace", []))
            st = _entity_sojourn(rt.get("trace", []))
            if sc and st:
                paired_diffs.append(statistics.mean(st.values()) - statistics.mean(sc.values()))
        # Unpaired: different seeds
        cfg_c2 = dict(base_cfg); cfg_c2["seed"] = seed + 7
        cfg_t2 = dict(base_cfg); cfg_t2["seed"] = seed + 11
        if "rate" in (cfg_t2.get("arrival_distribution") or {}):
            arr_t2_dict = dict(cfg_t2["arrival_distribution"])
            arr_t2_dict["rate"] = arr_t2_dict["rate"] * 1.2
            cfg_t2["arrival_distribution"] = arr_t2_dict
        rc2, _b5 = _safe_run(sim_module, cfg_c2, "3.3.3_uc", "crn_unpair_c")
        rt2, _b6 = _safe_run(sim_module, cfg_t2, "3.3.3_ut", "crn_unpair_t")
        for _blk in (_b5, _b6):
            if _blk:
                records.append(_blk)   # AUDIT FIX (M3)
        if rc2 and rt2:
            sc2 = _entity_sojourn(rc2.get("trace", []))
            st2 = _entity_sojourn(rt2.get("trace", []))
            if sc2 and st2:
                unpaired_diffs.append(statistics.mean(st2.values()) - statistics.mean(sc2.values()))
    if len(paired_diffs) >= 2 and len(unpaired_diffs) >= 2:
        var_p = statistics.variance(paired_diffs)
        var_u = statistics.variance(unpaired_diffs)
        ratio_v = var_p / max(var_u, 1e-9)
        if ratio_v < 1.0:
            status = "PASS"
        elif ratio_v < 1.5:
            status = "WARN"
        else:
            status = "INFO"  # CRN didn't help, but isn't a defect — could just be sim shape
        records.append(CheckRecord(
            check_id="3.3.3_crn_variance_reduction",
            status=status,
            diagnostic=f"Variance(paired)={var_p:.3f} vs Variance(unpaired)={var_u:.3f}; "
                       f"ratio={ratio_v:.2f} (PASS if <1)",
            evidence={"variance_paired": var_p, "variance_unpaired": var_u,
                      "ratio": ratio_v, "n_paired": len(paired_diffs), "n_unpaired": len(unpaired_diffs)},
        ))
    else:
        records.append(CheckRecord(
            check_id="3.3.3_crn_variance_reduction",
            status="INFO",
            diagnostic=f"Insufficient paired/unpaired data: paired={len(paired_diffs)}, "
                       f"unpaired={len(unpaired_diffs)}",
            evidence={},
        ))

    # 3.4.1 perturbable_parameters
    canonical = {"seed", "run_length", "warmup_time", "warmup", "resources",
                 "arrival_distribution", "service_distributions"}
    keys_present = sorted(set(base_cfg.keys()) & canonical)
    records.append(CheckRecord(
        check_id="3.4.1_perturbable_parameters",
        status="PASS" if len(keys_present) >= 4 else "WARN",
        diagnostic=f"{len(keys_present)} canonical config keys exposed for perturbation: {keys_present}",
        evidence={"keys_present": keys_present, "all_keys": sorted(base_cfg.keys())},
        suspected_location="sim_module.DEFAULT_CONFIG" if len(keys_present) < 4 else None,
    ))

    # 3.4.2 sensitivity_arrival_rate
    arr_s = base_cfg.get("arrival_distribution") or {}
    if "rate" in arr_s or "lambda" in arr_s:
        key = "rate" if "rate" in arr_s else "lambda"
        cfg_base = dict(base_cfg)
        cfg_pert = dict(base_cfg)
        cfg_pert["arrival_distribution"] = dict(arr_s); cfg_pert["arrival_distribution"][key] = arr_s[key] * 1.2
        rb_, _b7 = _safe_run(sim_module, cfg_base, "3.4.2_base", "sens_base")
        rp_, _b8 = _safe_run(sim_module, cfg_pert, "3.4.2_pert", "sens_pert")
        for _blk in (_b7, _b8):
            if _blk:
                records.append(_blk)   # AUDIT FIX (M3): a sim that crashes
                # only under a +20% arrival perturbation is a robustness red
                # flag, not something to silently omit.
        if rb_ and rp_:
            sb_ = _entity_sojourn(rb_.get("trace", []))
            sp_ = _entity_sojourn(rp_.get("trace", []))
            if sb_ and sp_:
                Wb = statistics.mean(sb_.values()); Wp = statistics.mean(sp_.values())
                rel_change = (Wp - Wb) / max(abs(Wb), 1e-9)
                if rel_change > 0.0:
                    status = "PASS"
                elif rel_change >= -0.05:
                    status = "WARN"
                else:
                    status = "FAIL"
                records.append(CheckRecord(
                    check_id="3.4.2_sensitivity_arrival_rate",
                    status=status,
                    diagnostic=f"+20% λ → W moves from {Wb:.2f} to {Wp:.2f} (Δ {rel_change:+.1%})",
                    evidence={"W_base": Wb, "W_pert": Wp, "rel_change": rel_change},
                    suspected_location="sim_module (arrival or queueing)" if status == "FAIL" else None,
                ))
            else:
                records.append(CheckRecord(
                    check_id="3.4.2_sensitivity_arrival_rate",
                    status="INFO",
                    diagnostic="Insufficient completed entities to measure sensitivity",
                    evidence={},
                ))
    else:
        records.append(CheckRecord(
            check_id="3.4.2_sensitivity_arrival_rate",
            status="INFO",
            diagnostic="No arrival_distribution.rate — cannot probe arrival sensitivity",
            evidence={},
        ))

    # 3.4.3 sensitivity_secondary_parameter
    # Verify that a second declared scalar parameter (mortality rate, scrap
    # probability, breakdown rate, etc.) actually drives a corresponding
    # observed KPI. Auto-discover the first scalar-numeric config key
    # outside the canonical framework keys and perturb it.
    #
    # EXCLUDE contains only the universal framework-level keys. Anything
    # else — including dicts, booleans, and None — is filtered by the
    # `isinstance(v, (int, float)) and not isinstance(v, bool)` type
    # check below, so this stays domain-agnostic: ICU-specific shapes
    # like `processes_enabled` (dict), `coupling_rule_enabled` (bool),
    # `semantic_scenario` (None|str) get skipped automatically without
    # being named here, as do any analogous shapes from other domains.
    EXCLUDE = {"seed", "run_length", "warmup_time", "warmup",
               "resources", "arrival_distribution", "service_distributions",
               "rep_id", "first_arrival_at", "compliance_spec"}
    secondary_key = None
    secondary_value = None
    for k, v in base_cfg.items():
        if k in EXCLUDE:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            secondary_key = k
            secondary_value = float(v)
            break
    if secondary_key is None:
        records.append(CheckRecord(
            check_id="3.4.3_sensitivity_secondary_parameter",
            status="INFO",
            diagnostic="No secondary scalar parameter in DEFAULT_CONFIG to perturb. "
                       "Add a domain-specific scalar (mortality_rate, scrap_prob, "
                       "breakdown_rate, …) for richer sensitivity coverage.",
            evidence={"canonical_excluded": sorted(EXCLUDE), "config_keys": sorted(base_cfg.keys())},
        ))
    else:
        # Choose a meaningful perturbation: ×2 by default, but if v looks like a
        # probability (0 < v < 1) and 2·v would exceed 1, perturb to min(2v, 0.99).
        if 0.0 < secondary_value < 1.0:
            pert = min(secondary_value * 2.0, 0.99)
        elif secondary_value == 0.0:
            pert = 0.1
        else:
            pert = secondary_value * 2.0
        cfg_b3 = dict(base_cfg); cfg_b3["seed"] = base_cfg.get("seed", 42)
        cfg_p3 = dict(base_cfg); cfg_p3["seed"] = base_cfg.get("seed", 42)
        cfg_p3[secondary_key] = pert
        rb3, _b9 = _safe_run(sim_module, cfg_b3, "3.4.3_base", "sens2_base")
        rp3, _b10 = _safe_run(sim_module, cfg_p3, "3.4.3_pert", "sens2_pert")
        for _blk in (_b9, _b10):
            if _blk:
                records.append(_blk)   # AUDIT FIX (M3)
        if rb3 and rp3:
            # Compare every numeric KPI between the two runs; report the
            # largest signed fractional change.
            mb = rb3.get("metrics", {}) or {}
            mp = rp3.get("metrics", {}) or {}
            kpi_changes: list[tuple[str, float, float, float]] = []
            for k in sorted(set(mb.keys()) & set(mp.keys())):
                vb = mb[k]; vp = mp[k]
                if not (isinstance(vb, (int, float)) and isinstance(vp, (int, float))):
                    continue
                if abs(vb) < 1e-9 and abs(vp) < 1e-9:
                    continue
                rel = (vp - vb) / max(abs(vb), 1e-9)
                kpi_changes.append((k, vb, vp, rel))
            # Also include mean sojourn as a fallback KPI
            sb3 = _entity_sojourn(rb3.get("trace", []))
            sp3 = _entity_sojourn(rp3.get("trace", []))
            if sb3 and sp3:
                Wb = statistics.mean(sb3.values()); Wp = statistics.mean(sp3.values())
                kpi_changes.append(("mean_sojourn", Wb, Wp,
                                    (Wp - Wb) / max(abs(Wb), 1e-9)))
            # Largest-magnitude relative change
            kpi_changes.sort(key=lambda t: abs(t[3]), reverse=True)
            if kpi_changes:
                top = kpi_changes[0]
                largest_rel = abs(top[3])
                if largest_rel >= 0.10:
                    status = "PASS"
                elif largest_rel >= 0.02:
                    status = "WARN"
                else:
                    status = "FAIL"
                records.append(CheckRecord(
                    check_id="3.4.3_sensitivity_secondary_parameter",
                    status=status,
                    diagnostic=(f"Perturbed {secondary_key}: {secondary_value} → {pert}. "
                                f"Largest KPI response: {top[0]} {top[1]:.4f}→{top[2]:.4f} "
                                f"(Δ {top[3]:+.1%}). Threshold: ≥10% → PASS."),
                    evidence={
                        "parameter": secondary_key,
                        "base_value": secondary_value,
                        "perturbed_value": pert,
                        "kpi_changes_top5": [{"kpi": k, "base": vb, "pert": vp, "rel": rel}
                                              for k, vb, vp, rel in kpi_changes[:5]],
                    },
                    suspected_location=(f"sim_module (parameter '{secondary_key}' may not be wired through)"
                                        if status == "FAIL" else None),
                ))
            else:
                records.append(CheckRecord(
                    check_id="3.4.3_sensitivity_secondary_parameter",
                    status="INFO",
                    diagnostic=f"Perturbed {secondary_key} but no numeric KPIs available to compare",
                    evidence={"parameter": secondary_key},
                ))

    tally = _tally(records)
    return PhaseReport("phase3", _aggregate_status(tally), tally, records, time.time() - t0)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 — Sensitivity & face validity
# ─────────────────────────────────────────────────────────────────────────────

def validate_phase4(sim_module, config: dict | None = None) -> PhaseReport:
    t0 = time.time()
    records: list[CheckRecord] = []
    base_cfg = dict(config or getattr(sim_module, "DEFAULT_CONFIG", {}))

    result, block = _safe_run(sim_module, base_cfg, "4.0_baseline", "phase4_baseline")
    if block:
        records.append(block)
        return PhaseReport("phase4", "BLOCK", _tally(records), records, time.time() - t0)
    trace = result.get("trace", [])

    # 4.1 oat_arrival_rate
    arr = dict(base_cfg.get("arrival_distribution") or {})
    if "rate" in arr or "lambda" in arr:
        key = "rate" if "rate" in arr else "lambda"
        baseline_rate = arr[key]
        sweep: list[tuple[float, float]] = []
        for factor in (0.5, 1.0, 1.5):
            cfg_s = dict(base_cfg)
            arr_s = dict(arr); arr_s[key] = baseline_rate * factor
            cfg_s["arrival_distribution"] = arr_s
            r, b = _safe_run(sim_module, cfg_s, f"4.1_arr_{factor}", f"oat_arr_{factor}")
            if b: continue
            soj = _entity_sojourn(r.get("trace", []))
            if soj:
                sweep.append((factor, statistics.mean(soj.values())))
        if len(sweep) >= 2:
            sweep.sort()
            monotonic_up = all(sweep[i][1] <= sweep[i+1][1] for i in range(len(sweep) - 1))
            records.append(CheckRecord(
                check_id="4.1_oat_arrival",
                status="PASS" if monotonic_up else "WARN",
                diagnostic=(f"OAT sweep on arrival rate W moves: {sweep}; "
                            f"{'monotone non-decreasing' if monotonic_up else 'non-monotone'}"),
                evidence={"sweep": sweep, "monotonic_up": monotonic_up},
                suspected_location="sim_module (arrival or queueing logic)" if not monotonic_up else None,
            ))
    else:
        records.append(CheckRecord(
            check_id="4.1_oat_arrival",
            status="INFO",
            diagnostic="No arrival_distribution.rate — cannot sweep",
            evidence={},
        ))

    # 4.1.{r} oat_capacity (per resource that has a capacity) — part of the OAT family
    res_cfg = _resources_from_config(base_cfg)
    for rname, rinfo in res_cfg.items():
        if "capacity" not in rinfo:
            continue
        base_cap = rinfo["capacity"]
        sweep_cap: list[tuple[int, float]] = []
        for cap in (max(1, base_cap - 1), base_cap, base_cap + 1):
            cfg_s = dict(base_cfg)
            cfg_s["resources"] = _set_capacity(base_cfg.get("resources", {}), rname, cap)
            r, b = _safe_run(sim_module, cfg_s, f"4.1_cap_{rname}_{cap}", f"oat_cap_{rname}_{cap}")
            if b: continue
            soj = _entity_sojourn(r.get("trace", []))
            if soj:
                sweep_cap.append((cap, statistics.mean(soj.values())))
        if len(sweep_cap) >= 2:
            sweep_cap.sort()
            monotonic_down = all(sweep_cap[i][1] >= sweep_cap[i+1][1] for i in range(len(sweep_cap) - 1))
            records.append(CheckRecord(
                check_id=f"4.1.{rname}.oat_capacity",
                status="PASS" if monotonic_down else "WARN",
                diagnostic=(f"OAT capacity sweep on {rname}: W as cap rises = {sweep_cap}; "
                            f"{'monotone non-increasing' if monotonic_down else 'non-monotone'}"),
                evidence={"resource": rname, "sweep": sweep_cap, "monotonic_down": monotonic_down},
                suspected_location=(f"sim_module (resource '{rname}' grant/release)"
                                    if not monotonic_down else None),
            ))

    # ── 4.2 PREEMPTION SEMANTICS ───────────────────────────────────────────
    # Fires only when the model declares preemption (preemption_rules in
    # config) AND the trace carries preempt/resume events. These checks
    # verify preemption *semantics* beyond what Phase 0 B10/B20 cover
    # (count + segment_id pairing + non-preemptible). All are generic — they
    # read only the canonical trace fields (progress_fraction, priority,
    # segment_id, resource).
    _cs_for_preempt = (base_cfg.get("compliance_spec") or {}) \
                       if isinstance(base_cfg, dict) else {}
    preempt_declared = bool(base_cfg.get("preemption_rules")
                            or base_cfg.get("preemption")
                            or any("preempt" in str(k).lower() for k in base_cfg)
                            or _cs_for_preempt.get("preemption_rules"))
    preempts = [ev for ev in trace if ev.get("event") == "preempt"]
    resumes = [ev for ev in trace if ev.get("event") == "resume"]

    if not preempt_declared and not preempts:
        records.append(CheckRecord(
            check_id="4.2_preemption",
            status="INFO",
            diagnostic="No preemption declared and no preempt events — preemption checks skipped",
            evidence={},
        ))
    else:
        # 4.2.1 preemption_fires
        # If the declared preemption_rules explicitly forbid initiation
        # (e.g. `can_initiate: False` on every rule, or `interruptible: False`
        # on every service_process), then observing zero preempts is the
        # correct behaviour, not evidence of an omission. B10 already
        # handles this in Phase 0; §4.2.1 mirrors that logic so a declared
        # non-preemptive system does not accumulate a spurious WARN.
        def _preemption_forbidden_by_declaration(cfg: dict) -> bool:
            pr = cfg.get("preemption_rules") or []
            _cs = (cfg.get("compliance_spec") or {}) if isinstance(cfg, dict) else {}
            pr = list(pr) + list(_cs.get("preemption_rules") or [])
            if not pr:
                return False
            # If any rule permits initiation, preemption is expected.
            for rule in pr:
                if not isinstance(rule, dict):
                    continue
                ci = rule.get("can_initiate")
                if ci is True or ci is None:
                    # can_initiate=None means unspecified (defaults to True);
                    # only explicit False on every rule suffices.
                    return False
            return True

        preempts_forbidden = _preemption_forbidden_by_declaration(base_cfg)
        if not preempts and preempts_forbidden:
            fires_status = "PASS"
            fires_diag = (
                "0 preempt events observed — consistent with declaration: "
                "every declared preemption_rule has can_initiate=False "
                "(preemption is forbidden by design, not omitted)."
            )
        elif preempts:
            fires_status = "PASS"
            fires_diag = f"{len(preempts)} preempt + {len(resumes)} resume events observed"
        else:
            fires_status = "WARN"
            fires_diag = (
                "preemption declared but no preempt events observed in this run "
                "(may be a low-contention regime, not necessarily a defect)"
            )
        records.append(CheckRecord(
            check_id="4.2.1_preemption_fires",
            status=fires_status,
            diagnostic=fires_diag,
            evidence={"preempts": len(preempts), "resumes": len(resumes),
                      "declared_non_preemptive": preempts_forbidden},
        ))

        # 4.2.2 preemption_priority_only — the takeover must be higher priority
        # than the victim. Convention: lower priority number = higher priority
        # (matches the framework's preemption_rules ordering). Best-effort: we
        # pair each preempt with the same-resource service_start/resume that
        # takes over at ~the same time, and compare numeric priorities. If
        # priority fields are absent or no takeover can be paired, INFO-skip.
        if preempts and all("priority" in ev for ev in preempts):
            takeovers = sorted(
                (ev for ev in trace if ev.get("event") in ("service_start", "resume")
                 and ev.get("priority") is not None),
                key=lambda e: (e.get("time", 0.0), e.get("seq", 0)))
            EPS_T = 1e-3
            violations = 0
            compared = 0
            for pe in preempts:
                vp = pe.get("priority")
                res = pe.get("resource")
                t = pe.get("time", 0.0)
                # find a takeover on the same resource at ~the same time, by a
                # different entity
                cand = [tk for tk in takeovers
                        if tk.get("resource") == res
                        and abs(tk.get("time", 0.0) - t) <= EPS_T
                        and tk.get("entity_id") != pe.get("entity_id")]
                if not cand or vp is None:
                    continue
                pp = cand[0].get("priority")
                if pp is None:
                    continue
                compared += 1
                if pp >= vp:   # takeover not strictly higher priority than victim
                    violations += 1
            if compared == 0:
                records.append(CheckRecord(
                    check_id="4.2.2_preemption_priority_only",
                    status="INFO",
                    diagnostic="Could not pair preempts with same-resource takeovers — "
                               "priority-ordering not verifiable from this trace",
                    evidence={"preempts": len(preempts)},
                ))
            else:
                records.append(CheckRecord(
                    check_id="4.2.2_preemption_priority_only",
                    status="PASS" if violations == 0 else "FAIL",
                    diagnostic=(f"{compared} preempt/takeover pairs checked; "
                                f"{violations} where takeover priority ≥ victim priority "
                                "(assumes lower number = higher priority)"),
                    evidence={"compared": compared, "violations": violations},
                    suspected_location="sim_module (preemption priority ordering)" if violations else None,
                ))
        else:
            records.append(CheckRecord(
                check_id="4.2.2_preemption_priority_only",
                status="INFO",
                diagnostic="preempt events lack a numeric priority field — priority-ordering "
                           "not verifiable",
                evidence={},
            ))

        # 4.2.3 preemption_barrier — for every barrier_aware preempt, the
        # served fraction at the moment of preempt must be ≥ barrier_fraction.
        # The earlier version of this check trusted a sim-supplied
        # ``progress_fraction`` field and fell through to INFO when the field
        # was absent — which made the check vacuously pass on any sim that
        # never emitted the field, including sims that declared barrier_aware
        # policies but never enforced them ("trivially satisfied because
        # non-abandon tasks complete the full duration via resume"). That
        # failure mode (INERT_PARAMETER) was the headline gap in the ICU audit.
        #
        # The corrected check derives the served fraction from trace event
        # timing directly: for each preempt, served_at_preempt is the sum of
        # the segment's start/resume → preempt intervals up to that moment,
        # and total_served is the same sum extended to the segment's eventual
        # service_end. served_at_preempt / total_served < barrier_fraction
        # means the barrier did not gate this preempt.
        barrier = 0.5
        # Read from preemption_rules — accept either a single value (legacy
        # shape) or per-rule values; for the check we use the MAX declared
        # barrier (the strictest declaration any rule asserts).
        pr = base_cfg.get("preemption_rules")
        declared_barriers: list[float] = []
        if isinstance(pr, dict):
            v = pr.get("barrier_fraction", pr.get("min_progress"))
            if v is not None:
                declared_barriers.append(float(v))
        elif isinstance(pr, list):
            for rule in pr:
                if not isinstance(rule, dict):
                    continue
                pol = (rule.get("policy") or "").lower()
                if pol and pol != "barrier_aware":
                    continue  # only barrier_aware rules constrain the served fraction
                v = rule.get("barrier_fraction", rule.get("min_progress"))
                if v is not None:
                    declared_barriers.append(float(v))
        if declared_barriers:
            barrier = max(declared_barriers)

        # Group all segment events (service_start, resume, preempt, service_end)
        # by (entity_id, segment_id) to reconstruct served intervals per segment.
        seg_events: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for ev in trace:
            if ev.get("event") not in ("service_start", "resume", "preempt", "service_end"):
                continue
            seg_id = ev.get("segment_id") or ""
            eid = ev.get("entity_id") or ""
            if not seg_id:
                continue
            seg_events[(eid, seg_id)].append(ev)

        below = 0
        checked = 0
        min_ratio = None
        abandoned_unresolvable = 0   # AUDIT FIX (M4): terminal preempts of
        # never-completed segments — ratio undefined, reported separately.
        # Per-violation records, retained for residual fingerprinting and
        # architectural-implication tagging. Each entry corresponds to one
        # preempt that violated the declared barrier.
        violation_records: list = []
        for key, evs in seg_events.items():
            evs.sort(key=lambda e: (e.get("time", 0.0), e.get("seq", 0)))
            # Sum interval durations: each start/resume opens, each preempt or
            # service_end closes.
            intervals: list[tuple[float, float, str]] = []  # (start, end, closing_event)
            cur_open: float | None = None
            for ev in evs:
                t = float(ev.get("time", 0.0))
                evt = ev.get("event")
                if evt in ("service_start", "resume"):
                    cur_open = t
                elif evt in ("preempt", "service_end") and cur_open is not None:
                    intervals.append((cur_open, t, evt))
                    cur_open = None
            # total served at segment completion = sum of all interval lengths
            total_served = sum(max(0.0, e - s) for s, e, _ in intervals)
            if total_served <= 0:
                continue
            seg_start_time = intervals[0][0] if intervals else None
            # AUDIT FIX (M4): abandon-path blind spot. When a segment's LAST
            # closing event is a preempt with no subsequent resume →
            # service_end (abandonment), that final preempt has
            # served_so_far == total_served, i.e. ratio 1.0 BY CONSTRUCTION
            # — it could never register as a barrier violation no matter how
            # early it fired, and its 1.0 also polluted min_ratio/INERT
            # detection. The true denominator (intended service duration) is
            # unknowable from the trace for an abandoned segment, so the
            # final preempt of a segment that never completes is EXCLUDED
            # from the ratio test and counted separately as unresolvable.
            segment_completed = intervals[-1][2] == "service_end"
            served_so_far = 0.0
            n_intervals = len(intervals)
            for idx, (s, e, closing) in enumerate(intervals):
                served_so_far += max(0.0, e - s)
                if closing == "preempt":
                    is_terminal_preempt = (idx == n_intervals - 1
                                           and not segment_completed)
                    if is_terminal_preempt:
                        abandoned_unresolvable += 1
                        continue
                    ratio = served_so_far / total_served
                    if min_ratio is None or ratio < min_ratio:
                        min_ratio = ratio
                    checked += 1
                    if ratio < barrier - 1e-9:
                        below += 1
                        if _RESIDUAL_DIAG_AVAILABLE:
                            violation_records.append(ViolationRecord(
                                entity_id=str(key[0]),
                                segment_id=str(key[1]),
                                time_of_violation=e,
                                served_so_far=served_so_far,
                                total_served=total_served,
                                ratio=ratio,
                                segment_start_time=seg_start_time,
                            ))
        if checked == 0:
            records.append(CheckRecord(
                check_id="4.2.3_preemption_barrier",
                status="INFO" if abandoned_unresolvable == 0 else "WARN",
                diagnostic=(
                    "No barrier-bearing preempts could be reconstructed from "
                    "trace event timing (missing segment_id, no completed "
                    "preempt→resume→service_end triples, or no preempts at all)."
                    + (f" NOTE: {abandoned_unresolvable} preempt(s) belong to "
                       f"never-completed (abandoned) segments whose served "
                       f"fraction is unresolvable from the trace — the barrier "
                       f"declaration is UNVERIFIED for these, not satisfied. "
                       f"Emit resume/service_end (or declare the intended "
                       f"duration) to make them checkable."
                       if abandoned_unresolvable else "")),
                evidence={"barrier": barrier, "preempts_seen": len(preempts),
                          "abandoned_unresolvable": abandoned_unresolvable},
            ))
        else:
            status = "PASS" if below == 0 else "FAIL"
            inert_note = ""
            # If the EMPIRICAL minimum served fraction is well below the
            # declared barrier, the barrier rule is INERT — declared but not
            # gating execution. Distinguish this from sporadic violations.
            inert = (min_ratio is not None and min_ratio < barrier * 0.5
                     and below >= max(checked // 4, 1))
            if inert:
                inert_note = (" (INERT_PARAMETER: declared barrier_fraction is "
                              "not gating preempt timing — preempts fire at the "
                              "arrival of the preemptor regardless of served "
                              "fraction)")
            # Residual fingerprinting + architectural-implication tagging.
            # Only meaningful when there are violations to categorize.
            residual_clusters_dict: list[dict] = []
            implication_dict: dict | None = None
            residual_extension = ""
            if (
                _RESIDUAL_DIAG_AVAILABLE
                and status == "FAIL"
                and violation_records
            ):
                _clusters = classify_residual_barrier(violation_records, barrier)
                _tag = architectural_implication_for_barrier(_clusters)
                residual_clusters_dict = _clusters_to_dict(_clusters)
                implication_dict = _implication_to_dict(_tag)
                residual_extension = _residual_diag_extension(_clusters, _tag)
            records.append(CheckRecord(
                check_id="4.2.3_preemption_barrier",
                status=status,
                diagnostic=(
                    f"{checked} preempts checked against declared barrier="
                    f"{barrier:.2f} (max across all barrier_aware rules); "
                    f"{below} violated (min observed served fraction = "
                    f"{min_ratio:.4f}). "
                    f"{'all satisfy barrier' if below == 0 else 'barrier violations present'}"
                    f"{inert_note}"
                    + (f" [{abandoned_unresolvable} terminal preempt(s) of "
                       f"never-completed (abandoned) segments EXCLUDED — "
                       f"their served fraction is unresolvable from the "
                       f"trace; declare the intended duration or emit "
                       f"resume/service_end to make them checkable]"
                       if abandoned_unresolvable else "")
                    + f"{residual_extension}"
                ),
                evidence={
                    "barrier": barrier,
                    "min_served_fraction": min_ratio,
                    "below_count": below,
                    "checked": checked,
                    "abandoned_unresolvable": abandoned_unresolvable,
                    "inert_parameter": inert,
                    "residual_clusters": residual_clusters_dict,
                    "architectural_implication": implication_dict,
                },
                suspected_location=(
                    "sim_module (preemption barrier enforcement — barrier_fraction "
                    "appears declared but not enforced: preempt timing is determined "
                    "by the preemptor's arrival, not by the served fraction)"
                ) if status == "FAIL" else None,
            ))

        # 4.2.4 preemption_resume_pairing — every resume pairs with a prior
        # preempt on (entity_id, segment_id). (Re-asserted at Phase 4; Phase 0
        # B10 also checks this — kept here so the Phase 4 preemption block is
        # self-contained.)
        preempt_keys = {(ev.get("entity_id"), ev.get("segment_id")) for ev in preempts}
        unpaired = sum(1 for ev in resumes
                       if (ev.get("entity_id"), ev.get("segment_id")) not in preempt_keys)
        records.append(CheckRecord(
            check_id="4.2.4_preemption_resume_pairing",
            status="PASS" if unpaired == 0 else "FAIL",
            diagnostic=(f"all {len(resumes)} resume events pair with a prior preempt on "
                        "(entity_id, segment_id)" if unpaired == 0 else
                        f"{unpaired}/{len(resumes)} resume events have no matching preempt"),
            evidence={"resumes": len(resumes), "unpaired": unpaired},
            suspected_location="sim_module (resume without matching preempt)" if unpaired else None,
        ))

        # 4.2.5 non_preemptible — resources declared non-preemptible must have
        # zero preempt events. Declaration form: config["non_preemptible"] list,
        # or a resource entry with preemptible=False.
        non_preemptible = set(base_cfg.get("non_preemptible", []) or [])
        for rname, rinfo in res_cfg.items():
            if isinstance(rinfo, dict) and rinfo.get("preemptible") is False:
                non_preemptible.add(rname)
        if non_preemptible:
            offenders = sorted({ev.get("resource") for ev in preempts
                                if ev.get("resource") in non_preemptible})
            records.append(CheckRecord(
                check_id="4.2.5_non_preemptible",
                status="PASS" if not offenders else "FAIL",
                diagnostic=(f"declared non-preemptible resources {sorted(non_preemptible)} "
                            "have zero preempt events" if not offenders else
                            f"non-preemptible resources preempted: {offenders}"),
                evidence={"non_preemptible": sorted(non_preemptible), "offenders": offenders},
                suspected_location=f"sim_module (preempted non-preemptible {offenders})" if offenders else None,
            ))

        # 4.2.6 preemption_rule_evidence — every declared preemption_rule must
        # produce one of three outcomes in the trace:
        #   (a) POSITIVE evidence — the rule fired (rule's higher_priority
        #       process appears as the preempter of the rule's lower_priority
        #       process at least once);
        #   (b) NEGATIVE evidence — the rule was violated (a no_initiate rule
        #       fired anyway, or a barrier_aware rule fired below barrier);
        #   (c) EXPLICIT not_applicable_under_workload annotation in the sim
        #       manifest, justifying why the rule did not fire in this regime.
        # A rule with NONE of (a), (b), or (c) is a STRUCTURAL-IMPOSSIBILITY
        # finding — the rule is declared but cannot fire under the code's
        # current logic (the PR10-style failure mode from the ICU audit).
        cs = (base_cfg.get("compliance_spec") or {}) if isinstance(base_cfg, dict) else {}
        declared_rules = cs.get("preemption_rules") or []
        # Pull manifest annotations from the sim module if available.
        manifest_ann: dict = {}
        try:
            m_fn = getattr(sim_module, "manifest", None)
            mani = m_fn() if callable(m_fn) else getattr(sim_module, "MANIFEST", {})
            if isinstance(mani, dict):
                manifest_ann = mani.get("preemption_rule_annotations") or {}
        except Exception:
            manifest_ann = {}

        # Build (resource, ~time) → takeover process mapping for pairing.
        EPS_T = 1.0  # seconds — preempts and takeovers should be near-instant
        # Index takeovers (service_start/resume) by resource for fast scan.
        takeovers_by_res: dict[str, list[dict]] = defaultdict(list)
        for ev in trace:
            if ev.get("event") not in ("service_start", "resume"):
                continue
            r = ev.get("resource")
            if r:
                takeovers_by_res[r].append(ev)
        for r in takeovers_by_res:
            takeovers_by_res[r].sort(key=lambda e: (e.get("time", 0.0), e.get("seq", 0)))

        def _preempter_process(preempt_ev: dict) -> str | None:
            """Find the process that initiated this preempt by looking for a
            same-resource service_start/resume at ~the same time by a different
            entity."""
            r = preempt_ev.get("resource")
            t = float(preempt_ev.get("time", 0.0))
            ent = preempt_ev.get("entity_id")
            cand = [tk for tk in takeovers_by_res.get(r, [])
                    if abs(float(tk.get("time", 0.0)) - t) <= EPS_T
                    and tk.get("entity_id") != ent]
            if not cand:
                return None
            return cand[0].get("process")

        # Pre-compute, for every preempt event, (preempter_process, preempted_process).
        labelled_preempts: list[tuple[str | None, str | None, dict]] = []
        for pe in preempts:
            labelled_preempts.append((
                _preempter_process(pe),
                pe.get("preempted_process") or pe.get("process"),
                pe,
            ))

        if not declared_rules:
            records.append(CheckRecord(
                check_id="4.2.6_preemption_rule_evidence",
                status="INFO",
                diagnostic="No preemption_rules declared; rule-evidence check skipped.",
                evidence={},
            ))
        else:
            per_rule_records: list[CheckRecord] = []
            structurally_impossible: list[str] = []
            for rule in declared_rules:
                if not isinstance(rule, dict):
                    continue
                rid = (rule.get("source_ids") or [rule.get("name", "?")])[0]
                hp = rule.get("higher_priority") or rule.get("preempter") or ""
                lp = rule.get("lower_priority") or rule.get("preemptee") or ""
                policy = (rule.get("policy") or "").lower()
                can_initiate = rule.get("can_initiate")
                if can_initiate is None:
                    # Infer from policy if not explicit.
                    can_initiate = (policy != "no_initiate")
                ann = manifest_ann.get(rid) or manifest_ann.get(rule.get("name", "")) or {}
                annotated = bool(ann.get("not_applicable_under_workload"))

                # Positive evidence: a preempt where this rule's preempter is the
                # initiator AND this rule's preemptee is the preempted process.
                pos = [pe for (pp, vp, pe) in labelled_preempts
                       if pp == hp and vp == lp]
                # Negative evidence (only meaningful for no_initiate rules):
                # the rule says hp must NOT initiate against lp, but we find one.
                if not can_initiate:
                    if pos:
                        per_rule_records.append(CheckRecord(
                            check_id=f"4.2.6.{rid}_rule_evidence",
                            status="FAIL",
                            diagnostic=(
                                f"{rid}: rule declares {hp!r} must NOT initiate "
                                f"against {lp!r} (policy={policy}); trace shows "
                                f"{len(pos)} such preempt(s) — rule violated."
                            ),
                            evidence={"rule_id": rid, "higher_priority": hp,
                                      "lower_priority": lp, "policy": policy,
                                      "violations": len(pos)},
                            suspected_location=(
                                f"sim_module (preemption_rule {rid}: {hp} is "
                                f"initiating preempt against {lp} despite "
                                f"policy={policy})"
                            ),
                        ))
                    else:
                        per_rule_records.append(CheckRecord(
                            check_id=f"4.2.6.{rid}_rule_evidence",
                            status="PASS",
                            diagnostic=(
                                f"{rid}: rule declares {hp!r} must NOT initiate "
                                f"against {lp!r}; trace shows 0 such preempts "
                                f"(rule respected)."
                            ),
                            evidence={"rule_id": rid, "higher_priority": hp,
                                      "lower_priority": lp, "policy": policy,
                                      "violations": 0},
                        ))
                else:
                    # can_initiate=True: rule SHOULD fire under contention.
                    if pos:
                        per_rule_records.append(CheckRecord(
                            check_id=f"4.2.6.{rid}_rule_evidence",
                            status="PASS",
                            diagnostic=(
                                f"{rid}: positive evidence — {len(pos)} "
                                f"preempt(s) where {hp!r} interrupted {lp!r}."
                            ),
                            evidence={"rule_id": rid, "higher_priority": hp,
                                      "lower_priority": lp, "policy": policy,
                                      "positive_evidence": len(pos)},
                        ))
                    elif annotated:
                        per_rule_records.append(CheckRecord(
                            check_id=f"4.2.6.{rid}_rule_evidence",
                            status="INFO",
                            diagnostic=(
                                f"{rid}: no positive evidence in trace, but "
                                f"manifest annotates not_applicable_under_workload"
                                + (f" — reason: {ann.get('reason')}" if ann.get('reason') else "")
                                + "."
                            ),
                            evidence={"rule_id": rid, "higher_priority": hp,
                                      "lower_priority": lp, "policy": policy,
                                      "annotated": True},
                        ))
                    else:
                        structurally_impossible.append(rid)
                        per_rule_records.append(CheckRecord(
                            check_id=f"4.2.6.{rid}_rule_evidence",
                            status="FAIL",
                            diagnostic=(
                                f"{rid}: rule declares {hp!r} can_initiate=True "
                                f"against {lp!r} (policy={policy}); trace shows "
                                f"ZERO preempts of this kind and no manifest "
                                f"annotation justifies the absence. STRUCTURAL "
                                f"IMPOSSIBILITY — the rule may be unreachable "
                                f"under the current code path (e.g. a priority "
                                f"gate excludes {hp!r} from initiating preempt)."
                            ),
                            evidence={"rule_id": rid, "higher_priority": hp,
                                      "lower_priority": lp, "policy": policy,
                                      "positive_evidence": 0,
                                      "annotated": False,
                                      "structural_impossibility": True},
                            suspected_location=(
                                f"sim_module (preemption_rule {rid}: declared "
                                f"can_initiate=True but no preempt of {lp} by "
                                f"{hp} ever fires; check priority gate or other "
                                f"code path that may make this rule unreachable)"
                            ),
                        ))
            records.extend(per_rule_records)
            # Roll-up: how many rules are structurally impossible (the
            # PR10-class failure mode).
            if structurally_impossible:
                records.append(CheckRecord(
                    check_id="4.2.6_preemption_rule_evidence_summary",
                    status="FAIL",
                    diagnostic=(
                        f"{len(structurally_impossible)} preemption_rule(s) "
                        f"have NO evidence (positive, negative, or annotated) "
                        f"and may be structurally unreachable: "
                        f"{', '.join(structurally_impossible)}."
                    ),
                    evidence={"structurally_impossible_rules":
                              structurally_impossible,
                              "total_rules": len(declared_rules)},
                    suspected_location=(
                        "sim_module (one or more declared preemption_rules "
                        "appear unreachable; annotate as "
                        "not_applicable_under_workload in MANIFEST or fix the "
                        "code path that prevents them from firing)"
                    ),
                ))
            else:
                records.append(CheckRecord(
                    check_id="4.2.6_preemption_rule_evidence_summary",
                    status="PASS",
                    diagnostic=(
                        f"All {len(declared_rules)} preemption_rule(s) have "
                        f"evidence (positive, negative, or annotated)."
                    ),
                    evidence={"total_rules": len(declared_rules)},
                ))

    # 4.3 face_validity
    arr_set = {ev["entity_id"] for ev in trace if ev.get("event") == "system_arrival"}
    dep_set = {ev["entity_id"] for ev in trace if ev.get("event") == "system_departure"}
    soj = _entity_sojourn(trace)
    neg_times = sum(1 for ev in trace if ev.get("time", 0) < 0)
    neg_sojourns = sum(1 for v in soj.values() if v < 0)
    deps_le_arrs = len(dep_set) <= len(arr_set)
    # also check per-resource utilization in [0,1]
    util_violations = []
    for r in _discover_resources(trace):
        cap = _resource_capacity(base_cfg, r)
        if cap is None or cap <= 0: continue
        intervals = _service_intervals(trace, resource=r)
        if not intervals: continue
        t_first, t_last = _trace_horizon(trace)
        horizon = max(t_last - t_first, 1e-9)
        rho_obs = _busy_time(intervals) / (cap * horizon)
        if rho_obs > 1.0 + 1e-6:
            util_violations.append((r, rho_obs))
    issues = []
    if neg_times: issues.append(f"{neg_times} events with negative time")
    if neg_sojourns: issues.append(f"{neg_sojourns} entities with negative sojourn")
    if not deps_le_arrs: issues.append(f"departures ({len(dep_set)}) > arrivals ({len(arr_set)})")
    for r, ro in util_violations:
        issues.append(f"{r}: utilization {ro:.3f} > 1.0")
    records.append(CheckRecord(
        check_id="4.3_face_validity",
        status="PASS" if not issues else "FAIL",
        diagnostic=("; ".join(issues) if issues else
                    f"times non-negative; sojourns non-negative; departures ({len(dep_set)}) ≤ "
                    f"arrivals ({len(arr_set)}); all resource utilizations ≤ 1"),
        evidence={"arrivals": len(arr_set), "departures": len(dep_set),
                  "neg_times": neg_times, "neg_sojourns": neg_sojourns,
                  "util_violations": util_violations},
        suspected_location="sim_module (event ordering or trace emission)" if issues else None,
    ))

    # 4.4 effect_size (informational summary of the sweeps)
    sweep_summaries = []
    for r in records:
        if r.check_id.startswith("4.1") or "oat_capacity" in r.check_id:
            sw = r.evidence.get("sweep") or []
            if len(sw) >= 2:
                lo = sw[0][1]; hi = sw[-1][1]
                sweep_summaries.append({"check": r.check_id, "low": lo, "high": hi,
                                        "abs_change": abs(hi - lo),
                                        "rel_change": abs(hi - lo) / max(abs(lo), 1e-9)})
    records.append(CheckRecord(
        check_id="4.4_effect_size",
        status="INFO",
        diagnostic=f"OAT effect sizes recorded for {len(sweep_summaries)} sweeps; "
                   "scenario-gap identification stays in the LLM Phase 4 prompt",
        evidence={"sweep_summaries": sweep_summaries},
    ))

    tally = _tally(records)
    return PhaseReport("phase4", _aggregate_status(tally), tally, records, time.time() - t0)


# ─────────────────────────────────────────────────────────────────────────────
# Output formatters
# ─────────────────────────────────────────────────────────────────────────────

def report_to_json(report: PhaseReport) -> dict:
    return {
        "phase_id": report.phase_id,
        "status":   report.status,
        "tally":    report.tally,
        "elapsed_s": report.elapsed_s,
        "notes":    report.notes,
        "results":  [r.to_dict() if isinstance(r, CheckRecord) else r
                     for r in report.results],
    }


def report_to_markdown(report: PhaseReport) -> str:
    p = report.tally
    lines = [
        f"# VVUQ {report.phase_id} — local report",
        "",
        f"**Status:** **{report.status}** · "
        f"PASS {p.get('PASS',0)} · INFO {p.get('INFO',0)} · WARN {p.get('WARN',0)} · "
        f"FAIL {p.get('FAIL',0)} · BLOCK {p.get('BLOCK',0)} · "
        f"Elapsed {report.elapsed_s:.2f}s",
        "",
    ]
    fails = [r for r in report.results if r.status in ("FAIL", "BLOCK")]
    warns = [r for r in report.results if r.status == "WARN"]
    if fails:
        lines.append(f"## FAIL / BLOCK ({len(fails)})")
        for r in fails:
            loc = f" — *suspected:* `{r.suspected_location}`" if r.suspected_location else ""
            lines.append(f"- **[{r.status}] {r.check_id}** — {r.diagnostic}{loc}")
        lines.append("")
    if warns:
        lines.append(f"## WARN ({len(warns)})")
        for r in warns:
            lines.append(f"- **{r.check_id}** — {r.diagnostic}")
        lines.append("")
    lines.append(f"**PHASE {report.phase_id} STATUS: {report.status}** · "
                 f"{{PASS:{p.get('PASS',0)}, WARN:{p.get('WARN',0)}, "
                 f"FAIL:{p.get('FAIL',0)}, BLOCK:{p.get('BLOCK',0)}, "
                 f"INFO:{p.get('INFO',0)}}}")
    return "\n".join(lines)


def report_to_self_heal_payload(report: PhaseReport) -> dict:
    failing = [r for r in report.results if r.status in ("FAIL", "BLOCK")]
    return {
        "phase_id": report.phase_id,
        "n_failing": len(failing),
        "failures": [
            {
                "check_id": r.check_id,
                "status": r.status,
                "diagnostic": r.diagnostic,
                "evidence": r.evidence,
                "suspected_location": r.suspected_location,
            }
            for r in failing
        ],
    }
