"""
vvuq_utils.py — Shared infrastructure for the generic VVUQ pipeline.

Two tiers of tooling live here:

Tier 1 — Generic Phase 0 framework (new)
=========================================
The generic framework lets a model author declare expected behavior in a
standardized `compliance_spec` section of the simulation config, then have
Phase 0 verify those declarations against the trace without any domain
knowledge hard-coded in the checker.

Key types:
  CheckResult       — single finding from any check
  Phase0Context     — run data + helpers passed to every checker
  compare_ratio     — standard deviation-based severity helper
  post_warmup_events, filter_events, group_by_entity  — trace helpers

Tier 2 — Phase 1-5 backward-compatible helpers (retained)
==========================================================
  PhaseResults      — record/summary boilerplate used by phases 1-5
  compute_avg_census — time-weighted census from trace events only

Usage in Phase 0:
    from vvuq_utils import CheckResult, Phase0Context, compare_ratio
    from vvuq_utils import post_warmup_events, filter_events, group_by_entity

Usage in Phases 1-5 (unchanged):
    from vvuq_utils import PhaseResults
    phase = PhaseResults()
    phase.record("PASS", "my_check", "All good")
    phase.print_summary("PHASE 1")
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

# ─────────────────────────────────────────────────────────────────────────────
# Tier 1 — Generic types
# ─────────────────────────────────────────────────────────────────────────────

Severity = Literal["PASS", "INFO", "WARN", "FAIL", "BLOCK"]


@dataclass
class CheckResult:
    """One finding from any Phase 0 check.

    severity  : PASS / INFO / WARN / FAIL / BLOCK
    check_name: short identifier used in reports
    message   : human-readable explanation
    details   : optional structured data for downstream use
    """
    severity: Severity
    check_name: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.severity:5s}] {self.check_name}: {self.message}"


@dataclass
class Phase0Context:
    """Carries all run data needed by every Phase 0 checker.

    Instantiate once from run_simulation() output and pass to all check
    functions so they share the same pre-filtered event lists.
    """
    trace: list[dict[str, Any]]
    metrics: dict[str, Any]
    config: dict[str, Any]
    spec: dict[str, Any]          # compliance_spec section of config
    run_length: float
    warmup: float

    # ── Computed properties ──────────────────────────────────────────────────

    @property
    def effective_time(self) -> float:
        """Post-warmup window length in simulation-time units (seconds)."""
        return max(self.run_length - self.warmup, 1e-9)

    @property
    def effective_days(self) -> float:
        """Post-warmup window expressed as days (÷ 86 400 s)."""
        return self.effective_time / 86_400.0

    @property
    def simulation_regime(self) -> dict[str, Any]:
        """Return the declared simulation regime as a normalized dict.

        For terminating/burst simulations, rate-based checks (e.g. B01
        arrival rate) should measure over the declared arrival window
        rather than the full effective_time — otherwise the drain-tail
        dilutes the observed rate. Defaults to steady_state when the
        field is absent."""
        reg = (self.config or {}).get("simulation_regime") or {}
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

    @property
    def is_terminating(self) -> bool:
        """True if the declared regime is terminating or burst."""
        return self.simulation_regime["type"] in ("terminating", "burst")

    @property
    def rate_measurement_window_seconds(self) -> float:
        """Time window over which rate-based checks (arrivals, throughput)
        should measure. For steady-state regimes this is the full
        effective_time. For terminating/burst regimes with a declared
        arrival window, this is the arrival window itself — measuring
        arrival rate over a full run that extends past the arrival window
        into the drain phase produces an artifactually low observed rate
        that has no defensible interpretation.
        """
        if self.is_terminating:
            reg = self.simulation_regime
            w = reg.get("arrival_window_seconds")
            if w is not None:
                try:
                    return max(float(w), 1e-9)
                except (TypeError, ValueError):
                    pass
        return self.effective_time

    @property
    def rate_measurement_window_days(self) -> float:
        """Rate-measurement window in days."""
        return self.rate_measurement_window_seconds / 86_400.0

    @property
    def effective_hours(self) -> float:
        """Post-warmup window expressed as hours (÷ 3 600 s)."""
        return self.effective_time / 3_600.0

    @property
    def avg_census(self) -> float:
        """Time-weighted average number of entities in system.

        Priority order:
          1. metrics["avg_census"] or metrics["metric_avg_census"]
          2. metrics["avg_wip"]  (common in manufacturing / queueing models)
          3. Computed from trace arrivals/departures/losses (generic fallback)
        """
        for key in ("avg_census", "metric_avg_census", "avg_wip"):
            val = self.metrics.get(key)
            if val is not None:
                return float(val)
        return compute_avg_census(self.trace, self.warmup, self.run_length)

    # ── Convenience constructors ─────────────────────────────────────────────

    @classmethod
    def from_result(cls, result: dict[str, Any]) -> "Phase0Context":
        """Build a Phase0Context from a run_simulation() return value."""
        cfg = result["config"]
        return cls(
            trace=result["trace"],
            metrics=result["metrics"],
            config=cfg,
            spec=cfg.get("compliance_spec", {}),
            run_length=float(cfg.get("run_length", 0)),
            warmup=float(cfg.get("warmup_time", 0)),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Tier 1 — Trace filter helpers
# ─────────────────────────────────────────────────────────────────────────────

def post_warmup_events(ctx: Phase0Context) -> list[dict]:
    """Return all trace events at or after warmup_time."""
    return [e for e in ctx.trace if e["time"] >= ctx.warmup]


def filter_events(
    events: Iterable[dict],
    *,
    event: str | None = None,
    entity_type: str | None = None,
    resource: str | None = None,
    process: str | None = None,
    loss_type: str | None = None,
    state: str | None = None,
) -> list[dict]:
    """Return events matching all supplied keyword filters (None = wildcard)."""
    out = []
    for e in events:
        if event       is not None and e.get("event")       != event:       continue
        if entity_type is not None and e.get("entity_type") != entity_type: continue
        if resource    is not None and e.get("resource")    != resource:     continue
        if process     is not None and e.get("process")     != process:      continue
        if loss_type   is not None and e.get("loss_type")   != loss_type:    continue
        if state       is not None and e.get("state")       != state:        continue
        out.append(e)
    return out


def group_by_entity(events: Iterable[dict]) -> dict[str, list[dict]]:
    """Return a dict mapping entity_id → time-sorted list of events."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        groups[e["entity_id"]].append(e)
    for eid in groups:
        groups[eid].sort(key=lambda x: (x["time"], x.get("seq", 0)))
    return dict(groups)


def match_event_pattern(e: dict, pattern: dict) -> bool:
    """Return True if event dict satisfies every field in pattern."""
    for k, v in pattern.items():
        if e.get(k) != v:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Tier 1 — Severity helper
# ─────────────────────────────────────────────────────────────────────────────

def compare_ratio(
    observed: float,
    expected: float,
    tolerance: float,
    name: str,
    unit: str = "",
) -> CheckResult:
    """Standard ratio-based severity assignment.

    deviation = |observed/expected - 1|
      ≤ tolerance            → PASS
      ≤ tolerance * 2        → WARN
      > tolerance * 2        → FAIL
    """
    # Guard against a None/non-numeric expected (mappers can emit None when a
    # rate/target was not declared) — `None <= 0` would raise TypeError and
    # crash the whole phase. Treat it as a skip.
    if expected is None or not isinstance(expected, (int, float)):
        return CheckResult(
            "INFO", name,
            f"Expected value not declared ({expected!r}); ratio check skipped.",
            {"expected": expected, "observed": observed},
        )
    if expected <= 0:
        return CheckResult(
            "INFO", name,
            f"Expected value ≤ 0 ({expected}); ratio check skipped.",
            {"expected": expected, "observed": observed},
        )

    ratio = observed / expected
    dev = abs(ratio - 1.0)

    if dev <= tolerance:
        sev: Severity = "PASS"
    elif dev <= tolerance * 2:
        sev = "WARN"
    else:
        sev = "FAIL"

    return CheckResult(
        sev, name,
        f"Observed {observed:.4f}{unit} vs expected {expected:.4f}{unit} "
        f"(ratio={ratio:.3f}, tol=±{tolerance:.0%}).",
        {"observed": observed, "expected": expected, "ratio": ratio, "deviation": dev},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tier 1 — Summary printer for CheckResult lists
# ─────────────────────────────────────────────────────────────────────────────

_TAGS: dict[str, str] = {
    "PASS": "✓", "WARN": "⚠", "FAIL": "✗", "BLOCK": "⊘", "INFO": "ℹ",
}


def print_phase0_summary(results: list[CheckResult], phase_label: str = "PHASE 0") -> str:
    """Print formatted summary of Phase 0 results and return status string."""
    sc = Counter(r.severity for r in results)

    print(f"\n{'=' * 72}")
    print(f"{phase_label} RESULTS")
    print("=" * 72)
    for r in results:
        tag = _TAGS.get(r.severity, "?")
        print(f"  {tag} {r}")

    print(
        f"\nFindings: PASS={sc.get('PASS', 0)}  WARN={sc.get('WARN', 0)}  "
        f"FAIL={sc.get('FAIL', 0)}  BLOCK={sc.get('BLOCK', 0)}  "
        f"INFO={sc.get('INFO', 0)}"
    )

    if sc.get("BLOCK", 0) > 0:
        status = "BLOCKED"
        print("\nGATE: BLOCKED (structural failure)")
    elif sc.get("FAIL", 0) > 0:
        status = "FAILED"
        print(f"\nGATE: BLOCKED — {sc['FAIL']} contract failure(s)")
    elif sc.get("WARN", 0) > 0:
        status = "PASSED_WITH_WARNINGS"
        print(f"\nGATE: PASSED WITH WARNINGS ({sc.get('PASS', 0)} pass, {sc.get('WARN', 0)} warn)")
    else:
        status = "PASSED"
        print(f"\nGATE: PASSED ({sc.get('PASS', 0)} pass, {sc.get('INFO', 0)} info)")

    return status


# ─────────────────────────────────────────────────────────────────────────────
# Tier 2 — Backward-compatible PhaseResults (used by phases 1-5)
# ─────────────────────────────────────────────────────────────────────────────

class PhaseResults:
    """record() / print_summary() boilerplate shared by Phases 1-5.

    Phases 1-5 import and use this class unchanged.  Phase 0 uses the new
    CheckResult / Phase0Context infrastructure instead.
    """

    def __init__(self) -> None:
        self._findings: list[dict] = []

    def record(self, severity: str, check: str, msg: str) -> None:
        self._findings.append({"severity": severity, "check": check, "message": msg})
        print(f"[{severity}] {check}: {msg}")

    @property
    def findings(self) -> list[dict]:
        return list(self._findings)

    def counts(self) -> Counter:
        return Counter(f["severity"] for f in self._findings)

    @property
    def is_blocked(self) -> bool:
        c = self.counts()
        return c.get("BLOCK", 0) > 0 or c.get("FAIL", 0) > 0

    @property
    def has_block(self) -> bool:
        return any(f["severity"] == "BLOCK" for f in self._findings)

    def print_summary(self, phase_label: str) -> str:
        c = self.counts()
        print(f"\n{'=' * 72}")
        print(f"{phase_label} RESULTS")
        print("=" * 72)

        for f in self._findings:
            tag = _TAGS.get(f["severity"], "?")
            print(f"  {tag} [{f['severity']:5s}] {f['check']}: {f['message']}")

        print(
            f"\nFindings: PASS={c.get('PASS', 0)} WARN={c.get('WARN', 0)} "
            f"FAIL={c.get('FAIL', 0)} BLOCK={c.get('BLOCK', 0)} INFO={c.get('INFO', 0)}"
        )

        if c.get("BLOCK", 0) > 0:
            status = "BLOCKED"
            print("\nGATE: BLOCKED")
        elif c.get("FAIL", 0) > 0:
            status = "FAILED"
            print(f"\nGATE: BLOCKED — {c['FAIL']} compliance failure(s)")
        elif c.get("WARN", 0) > 0:
            status = "PASSED_WITH_WARNINGS"
            print(f"\nGATE: PASSED WITH WARNINGS ({c.get('PASS', 0)} pass, {c.get('WARN', 0)} warn)")
        else:
            status = "PASSED"
            print(f"\nGATE: PASSED ({c.get('PASS', 0)} pass, {c.get('INFO', 0)} info)")

        return status


# ─────────────────────────────────────────────────────────────────────────────
# Tier 2 — Trace-based census (used by Phase 0 avg_census and externally)
# ─────────────────────────────────────────────────────────────────────────────

def compute_avg_census(trace: list[dict], warmup: float, end_time: float) -> float:
    """Time-weighted average number of entities in system during [warmup, end_time].

    Generic: works from system_arrival / system_departure / loss events only,
    with no dependency on model-specific metric keys.
    """
    events = [
        (e["time"], e["event"])
        for e in trace
        if e["event"] in ("system_arrival", "system_departure", "loss")
        and e["time"] >= warmup
    ]
    events.sort(key=lambda x: x[0])

    if not events:
        return 0.0

    n = 0
    last_t = warmup
    total_weighted = 0.0

    for t, evt in events:
        dt = t - last_t
        total_weighted += n * dt
        last_t = t
        if evt == "system_arrival":
            n += 1
        elif evt in ("system_departure", "loss"):
            n = max(0, n - 1)

    total_weighted += n * (end_time - last_t)
    duration = end_time - warmup
    return total_weighted / duration if duration > 0 else 0.0
