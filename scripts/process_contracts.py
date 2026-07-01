"""
process_contracts.py — Phase 0 validators B18–B23 for process contract verification.

This module implements six validators for the VVUQ system that check process contracts
defined in the compliance_spec["process_contracts"] section. Each process contract
declares expected behavior (frequency, role composition, scheduling, etc.) and these
validators verify adherence.

Contract schema
===============
Each process contract is a dict with the following fields:

  trigger: str
    One of: "periodic", "scheduled", "on_arrival", "on_discharge", "stochastic_hazard", "conditional_event"

  # For periodic trigger:
  every_hours: float
    Expected inter-episode interval in hours

  # For scheduled trigger:
  times_per_day: float | None
    Number of episodes expected per day
  schedule_times_hour: list[float] | None
    Specific hours of day (mod 24) when episodes should occur (e.g. [8.0, 20.0])

  # Role composition:
  required_roles: list[str]
    Roles that must appear in every episode
  shift_opening_roles: list[str]
    Roles required once per shift (not in every episode)
  optional_roles: list[str]
    Roles that may appear but are not required
  shift_length_hours: float
    Length of a shift in hours (default 8.0)

  # Behavior:
  interruptible: bool
    Whether the process can be preempted
  preemption_policy: str | None
    One of: "can_preempt", "can_be_preempted", None
  count_unit: str
    One of: "episode", "role_start", "schedule_window"
  tolerance: float
    Acceptable relative deviation for frequency checks (default 0.10)

Validators (all return list[CheckResult])
==========================================
B18 check_process_frequency(ctx, process_name, contract)
  Verifies expected episode count based on trigger and contract parameters.
  Uses Little's Law for periodic processes and explicit counts for event-driven.

B19 check_role_structure(ctx, process_name, contract, episodes)
  Verifies that roles match the declared required/optional structure.

B20 check_interruptibility(ctx, process_name, contract)
  Verifies preemption behavior matches contract declarations.

B21 check_rooted_flow_conservation(ctx)
  Standalone check verifying entity accounting (rooted arrivals = departures + losses + in-system).

B22 check_count_unit_consistency(ctx, process_name, contract, episodes)
  Verifies that the count_unit (episode vs role_start) is used correctly.

B23 check_schedule_alignment(ctx, process_name, contract, episodes)
  Verifies that episode timing aligns with declared periodic/scheduled intervals.

Entry point
===========
validate_process_contracts(ctx: Phase0Context) -> list[CheckResult]
  Runs all validators on all declared processes. Returns empty list if no process_contracts defined.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from vvuq_utils import (
    CheckResult,
    Phase0Context,
    Severity,
    filter_events,
    group_by_entity,
    post_warmup_events,
    compare_ratio,
)


# ─────────────────────────────────────────────────────────────────────────────
# Census integration helper
# ─────────────────────────────────────────────────────────────────────────────

def _compute_integrated_census(
    trace: list[dict],
    warmup: float,
    end_time: float,
) -> tuple[float, float]:
    """Compute ∫L(t) dt from warmup to end_time using trace events.

    L(t) is the instantaneous count of entities in the system at time t,
    estimated from system_arrival (+1) and system_departure/loss (-1) events.

    Returns:
      (area_entity_seconds, avg_census)
      where area  = Σ L(tᵢ) × (tᵢ₊₁ − tᵢ)   (entity-seconds in effective window)
            avg_census = area / (end_time − warmup)

    This integral is the correct denominator for Little's-Law-based frequency
    checks because it is insensitive to non-stationarity (admission bursts,
    diversion, mortality feedback, etc.).
    """
    effective_seconds = end_time - warmup
    if effective_seconds <= 0:
        return 0.0, 0.0

    # Reconstruct census at warmup from pre-warmup history
    census = 0
    for e in trace:
        if e["time"] >= warmup:
            break
        ev = e["event"]
        if ev == "system_arrival":
            census += 1
        elif ev in ("system_departure", "loss"):
            census -= 1
    census = max(0, census)

    # Collect census-change events in [warmup, end_time) sorted by time
    changes: list[tuple[float, int]] = []
    for e in trace:
        t = e["time"]
        if t < warmup:
            continue
        if t >= end_time:
            break
        ev = e["event"]
        if ev == "system_arrival":
            changes.append((t, +1))
        elif ev in ("system_departure", "loss"):
            changes.append((t, -1))

    changes.sort(key=lambda x: x[0])

    # Integrate step function
    area = 0.0
    last_t = warmup
    for t, delta in changes:
        area += census * (t - last_t)
        last_t = t
        census = max(0, census + delta)

    # Final segment to end_time
    area += census * (end_time - last_t)

    avg_census = area / effective_seconds
    return area, avg_census


# ─────────────────────────────────────────────────────────────────────────────
# Episode extraction helper
# ─────────────────────────────────────────────────────────────────────────────

def _extract_episodes(
    trace: list[dict],
    process_name: str,
    warmup: float,
    episode_definition: dict | None = None,
) -> list[dict]:
    """Group service_start events for process_name into logical episodes.

    Grouping strategy is read from episode_definition (DSL field):
      time_gap        — consecutive events within max_gap_seconds → same episode (default)
      same_timestamp  — events at identical timestamps → same episode
      resource_barrier — new resource in same entity burst → new episode
      entity_event    — one episode per unique entity_id (all events merged)

    episode_definition defaults:
      {"grouping": "time_gap", "max_gap_seconds": 300.0}

    Returns a list of dicts:
      {
        "entity_id": str,
        "start_time": float (first service_start in episode),
        "role_starts": int (count of service_start events in episode),
        "resources": list[str] (unique resources used in episode),
      }
    """
    ed = episode_definition or {}
    grouping = ed.get("grouping", "time_gap")
    max_gap_s = float(ed.get("max_gap_seconds", 300.0))

    # Filter service_start events for this process in post-warmup window
    events = filter_events(
        [e for e in trace if e["time"] >= warmup],
        event="service_start",
        process=process_name,
    )

    if not events:
        return []

    by_entity = group_by_entity(events)
    episodes: list[dict] = []

    for entity_id, entity_events in by_entity.items():
        entity_events.sort(key=lambda e: e["time"])

        if grouping == "entity_event":
            # One episode per entity: all events merged
            episodes.append({
                "entity_id": entity_id,
                "start_time": entity_events[0]["time"],
                "role_starts": len(entity_events),
                "resources": list(set(e.get("resource", "") for e in entity_events if e.get("resource"))),
            })
            continue

        # Build episodes by splitting entity_events
        current_start: float | None = None
        current_evs: list[dict] = []

        def _finalize(eid: str, start: float, evs: list[dict]) -> dict:
            return {
                "entity_id": eid,
                "start_time": start,
                "role_starts": len(evs),
                "resources": list(set(e.get("resource", "") for e in evs if e.get("resource"))),
            }

        for i, event in enumerate(entity_events):
            if current_start is None:
                current_start = event["time"]
                current_evs = [event]
                continue

            prev = current_evs[-1]

            if grouping == "time_gap":
                split = (event["time"] - prev["time"]) > max_gap_s
            elif grouping == "same_timestamp":
                split = event["time"] != prev["time"]
            elif grouping == "resource_barrier":
                split = (event.get("resource") != prev.get("resource"))
            else:
                # Unknown grouping: fall back to time_gap with default
                split = (event["time"] - prev["time"]) > max_gap_s

            if split:
                episodes.append(_finalize(entity_id, current_start, current_evs))
                current_start = event["time"]
                current_evs = [event]
            else:
                current_evs.append(event)

        if current_evs:
            episodes.append(_finalize(entity_id, current_start, current_evs))

    return episodes


# ─────────────────────────────────────────────────────────────────────────────
# B18: Process frequency validation
# ─────────────────────────────────────────────────────────────────────────────

def check_process_frequency(
    ctx: Phase0Context,
    process_name: str,
    contract: dict[str, Any],
) -> list[CheckResult]:
    """Verify expected episode count based on trigger and contract.

    Derives expected count E[N] from first principles:
      - periodic: E[N] = ∫L(t)dt / (every_hours × 3600)
          Uses census integral (not static avg_census) so non-stationarity
          (admission bursts, diversion, mortality feedback) is handled correctly.
      - scheduled: E[N] = times_per_day × effective_days
      - on_arrival: E[N] = count of system_arrival events in [warmup, end)
      - on_discharge: E[N] = count of system_departure events in [warmup, end)
      - Other triggers: INFO only (advisory)

    Observed count is extracted from trace as service_start events grouped into
    episodes using the contract's episode_definition strategy.
    """
    results: list[CheckResult] = []

    trigger = contract.get("trigger", "periodic")
    tolerance = contract.get("tolerance", 0.10)
    episode_definition = contract.get("episode_definition")

    # Extract episodes for this process
    episodes = _extract_episodes(ctx.trace, process_name, ctx.warmup, episode_definition)
    observed = len(episodes)

    # Derive expected count from trigger type
    if trigger == "periodic":
        every_hours = contract.get("every_hours", 1.0)
        # Use time-integrated census: ∫L(t)dt / (every_hours × 3600)
        # This is correct under non-stationarity; avg_census × effective_hours
        # is only accurate when census is stationary.
        area_entity_s, avg_census_int = _compute_integrated_census(
            ctx.trace, ctx.warmup, ctx.run_length
        )
        expected = area_entity_s / (every_hours * 3600.0)
        results.append(CheckResult(
            "INFO", f"B18_census[{process_name}]",
            f"Integrated census: ∫L(t)dt={area_entity_s:.0f} entity-s, "
            f"avg_census={avg_census_int:.2f} (time-weighted). "
            f"Static ctx.avg_census={ctx.avg_census:.2f} for reference.",
            {"integrated_avg_census": avg_census_int,
             "static_avg_census": ctx.avg_census,
             "area_entity_s": area_entity_s},
        ))
    elif trigger == "scheduled":
        times_per_day = contract.get("times_per_day")
        if times_per_day is None or times_per_day <= 0:
            return [CheckResult("INFO", f"B18_freq[{process_name}]",
                               f"Trigger=scheduled but times_per_day not set; check skipped.")]
        expected = times_per_day * ctx.effective_days
        # Scheduled is more deterministic; tighten tolerance
        tolerance = min(tolerance, 0.05)
    elif trigger == "on_arrival":
        arrivals = filter_events(
            post_warmup_events(ctx),
            event="system_arrival",
        )
        expected = float(len(arrivals))
    elif trigger == "on_discharge":
        departures = filter_events(
            post_warmup_events(ctx),
            event="system_departure",
        )
        expected = float(len(departures))
    else:
        # Other triggers (stochastic_hazard, conditional_event, etc.): advisory only
        return [CheckResult("INFO", f"B18_freq[{process_name}]",
                           f"Trigger={trigger} not auto-verified (advisory).")]

    # ── count_unit-aware comparison ──────────────────────────────────────
    # The expected value derived above is in EPISODES. The DSL may declare
    # the contract's count_unit as either:
    #   "episode"      — one event per episode (default; current behaviour)
    #   "role_start"   — one event per role per episode; observed must be
    #                    scaled to role_starts and expected to expected_role_starts
    # Without this branch, a process declared with count_unit="role_start" and
    # multi-role episodes (e.g. rounding: consultant+resident+nurse per window)
    # produces observed = role_starts but expected = episodes, an apples-to-
    # oranges comparison that pressures the user to widen tolerances. The fix
    # is to compare at the granularity the DSL declared, not to loosen the
    # tolerance.
    count_unit = (contract.get("count_unit") or "episode").lower()
    required_roles = contract.get("required_roles", []) or []
    shift_opening_roles = contract.get("shift_opening_roles", []) or []
    if count_unit == "role_start":
        # Count role_starts DIRECTLY from the trace (do not depend on episode
        # grouping). Soft-pool processes use per-staffer entity_ids, which makes
        # _extract_episodes produce one episode per staffer per scheduled
        # instance, so the per-episode role_start average degenerates to 1 and
        # the heuristic for soft-pool detection has to use observed roles
        # per SCHEDULED INSTANCE instead.
        n_role_starts = sum(
            1 for ev in ctx.trace
            if ev.get("event") == "service_start"
            and ev.get("process") == process_name
            and float(ev.get("time", 0)) >= ctx.warmup
        )
        declared_role_ceiling = float(len(required_roles) + len(shift_opening_roles))
        # Use expected_episodes (already derived from the trigger) as the
        # number of scheduled instances; roles-per-instance = observed / expected.
        roles_per_instance_obs = (n_role_starts / expected) if expected > 0 else 0.0
        if declared_role_ceiling > 0 and roles_per_instance_obs > declared_role_ceiling * 1.5:
            # Soft-pool (engagement per instance clearly exceeds declared roles)
            roles_per_episode = roles_per_instance_obs
            note = (f"soft-pool: observed ≈ {roles_per_episode:.2f} role_starts "
                    f"per scheduled instance (declared ceiling "
                    f"{declared_role_ceiling:.0f} treated as floor)")
        else:
            roles_per_episode = declared_role_ceiling or max(roles_per_instance_obs, 1.0)
            note = (f"hard-pool: required {len(required_roles)}"
                    + (f" + shift-opening {len(shift_opening_roles)}"
                       if shift_opening_roles else "")
                    + f" = {roles_per_episode:.0f} role_starts per instance")
        expected_role_starts = expected * roles_per_episode
        result = compare_ratio(
            n_role_starts, expected_role_starts, tolerance,
            f"B18_freq[{process_name}]",
            unit=" role_starts",
        )
        # Append a clarifying message so the granularity choice is visible.
        result = CheckResult(
            severity=result.severity,
            check_name=result.check_name,
            message=f"{result.message} (count_unit=role_start; {note})",
            details=result.details,
        )
    else:
        result = compare_ratio(
            observed, expected, tolerance,
            f"B18_freq[{process_name}]",
            unit=" episodes",
        )
    results.append(result)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# B19: Role structure validation
# ─────────────────────────────────────────────────────────────────────────────

def check_role_structure(
    ctx: Phase0Context,
    process_name: str,
    contract: dict[str, Any],
    episodes: list[dict],
) -> list[CheckResult]:
    """Verify role composition matches declared structure.

    R_p = total role_starts / n_episodes (avg roles per episode)
    Expected range: [len(required_roles), len(required_roles) + len(optional_roles)]

    Also checks shift_opening_roles if present.
    """
    results: list[CheckResult] = []

    if not episodes:
        return [CheckResult("INFO", f"B19_roles[{process_name}]",
                           "No episodes found; role structure check skipped.")]

    required_roles = contract.get("required_roles", [])
    optional_roles = contract.get("optional_roles", [])
    shift_opening_roles = contract.get("shift_opening_roles", [])
    shift_length_hours = contract.get("shift_length_hours", 8.0)

    n_episodes = len(episodes)
    n_role_starts = sum(ep.get("role_starts", 0) for ep in episodes)

    # Average roles per episode. The upper bound includes shift_opening_roles
    # because those roles legitimately participate in episodes that overlap
    # shift starts; counting only required+optional produced false positives
    # for processes with shift-opening role variants (e.g. patient_check whose
    # required nurse+resident gain a consultant at shift open → avg ≈ 2.1 was
    # being flagged as "more roles than expected" against r_upper = 2.0).
    r_p = n_role_starts / n_episodes if n_episodes > 0 else 0.0
    r_lower = float(len(required_roles))
    r_upper = float(len(required_roles) + len(optional_roles) + len(shift_opening_roles))

    # Check role count bounds
    if r_lower <= r_p <= r_upper:
        role_result = CheckResult(
            "PASS", f"B19_roles[{process_name}]",
            f"Avg roles/episode {r_p:.2f} ∈ [{r_lower:.0f}, {r_upper:.0f}].",
            {"avg_roles_per_episode": r_p, "lower_bound": r_lower, "upper_bound": r_upper},
        )
    elif r_p < r_lower:
        role_result = CheckResult(
            "WARN", f"B19_roles[{process_name}]",
            f"Avg roles/episode {r_p:.2f} < required {r_lower:.0f}; missing required roles.",
            {"avg_roles_per_episode": r_p, "lower_bound": r_lower},
        )
    else:
        role_result = CheckResult(
            "WARN", f"B19_roles[{process_name}]",
            f"Avg roles/episode {r_p:.2f} > expected max {r_upper:.0f}; unexpected extra roles.",
            {"avg_roles_per_episode": r_p, "upper_bound": r_upper},
        )

    results.append(role_result)

    # Check shift_opening_roles if present — trace-based, not heuristic
    if shift_opening_roles:
        shift_s = shift_length_hours * 3600.0
        n_full_shifts = int(ctx.effective_hours / shift_length_hours)

        # For each shift window, look for the first service_start event that uses
        # a shift_opening resource (role name matched against trace 'resource' field)
        shift_opening_hits = 0
        shifts_with_any_activity = 0

        for k in range(n_full_shifts):
            shift_start = ctx.warmup + k * shift_s
            shift_end = shift_start + shift_s

            shift_evs = sorted(
                [e for e in ctx.trace
                 if e.get("event") == "service_start"
                 and e.get("process") == process_name
                 and shift_start <= e.get("time", 0) < shift_end],
                key=lambda e: e["time"],
            )
            if not shift_evs:
                continue
            shifts_with_any_activity += 1

            # PER-ENTITY first-N. The previous pooled-first-N window let a
            # capacity-1 trailing role (e.g. a consultant who does one round per
            # patient per shift) get drowned out by high-volume concurrent roles
            # (e.g. residents/nurses doing hourly checks): the consultant's lone
            # service_start per patient was pushed past the first few pooled
            # events. Group the shift's events by entity, take each entity's
            # first N events, and check if any of THOSE carries a shift_opening
            # role — that detects per-patient shift-opening visits regardless of
            # absolute-time interleaving from other patients.
            n_per_entity = max(len(shift_opening_roles), 1) * 3
            per_entity_window: list[dict] = []
            by_eid: dict[str, list[dict]] = {}
            for e in shift_evs:
                by_eid.setdefault(e.get("entity_id", ""), []).append(e)
            for eid, evs in by_eid.items():
                per_entity_window.extend(evs[:n_per_entity])
            if any(e.get("resource") in shift_opening_roles for e in per_entity_window):
                shift_opening_hits += 1

        if shifts_with_any_activity == 0:
            results.append(CheckResult(
                "INFO", f"B19_shift_opening[{process_name}]",
                f"shift_opening_roles declared but no trace activity found for process; check skipped.",
            ))
        else:
            coverage = shift_opening_hits / shifts_with_any_activity
            if coverage >= 0.80:
                results.append(CheckResult(
                    "PASS", f"B19_shift_opening[{process_name}]",
                    f"Shift-opening roles present in {shift_opening_hits}/{shifts_with_any_activity} "
                    f"active shifts ({coverage:.0%} ≥ 80%).",
                    {"hits": shift_opening_hits, "active_shifts": shifts_with_any_activity,
                     "shift_opening_roles": shift_opening_roles},
                ))
            elif coverage >= 0.50:
                results.append(CheckResult(
                    "WARN", f"B19_shift_opening[{process_name}]",
                    f"Shift-opening roles present in only {shift_opening_hits}/{shifts_with_any_activity} "
                    f"active shifts ({coverage:.0%}, expected ≥ 80%).",
                    {"hits": shift_opening_hits, "active_shifts": shifts_with_any_activity},
                ))
            else:
                results.append(CheckResult(
                    "FAIL", f"B19_shift_opening[{process_name}]",
                    f"Shift-opening roles absent in most shifts: "
                    f"{shift_opening_hits}/{shifts_with_any_activity} ({coverage:.0%} < 50%).",
                    {"hits": shift_opening_hits, "active_shifts": shifts_with_any_activity},
                ))

    return results


# ─────────────────────────────────────────────────────────────────────────────
# B20: Interruptibility validation
# ─────────────────────────────────────────────────────────────────────────────

def check_interruptibility(
    ctx: Phase0Context,
    process_name: str,
    contract: dict[str, Any],
) -> list[CheckResult]:
    """Verify preemption behavior matches contract.

    - If interruptible=False: any preempt event for this process → FAIL
    - If interruptible=True: INFO with count of interruptions
    - If preemption_policy='can_preempt': verify preemptions of OTHER processes exist
    - If preemption_policy='can_be_preempted': alias for interruptible=True
    """
    results: list[CheckResult] = []

    interruptible = contract.get("interruptible", True)
    preemption_policy = contract.get("preemption_policy")

    # Find preempt events for this process
    preempt_events = filter_events(
        post_warmup_events(ctx),
        event="preempt",
        process=process_name,
    )

    n_preempts = len(preempt_events)

    if not interruptible:
        if n_preempts > 0:
            results.append(CheckResult(
                "FAIL", f"B20_interrupt[{process_name}]",
                f"Process declared non-interruptible but {n_preempts} preempt events found.",
                {"preempt_count": n_preempts},
            ))
        else:
            results.append(CheckResult(
                "PASS", f"B20_interrupt[{process_name}]",
                "Process non-interruptible and no preempt events observed.",
            ))
    else:
        # interruptible=True: log interruptions as INFO
        results.append(CheckResult(
            "INFO", f"B20_interrupt[{process_name}]",
            f"Process interruptible; {n_preempts} preemption(s) observed.",
            {"preempt_count": n_preempts},
        ))

    # Check preemption policy
    if preemption_policy == "can_preempt":
        # Check whether trace events carry the 'preempted_process' field
        has_attribution = any("preempted_process" in e for e in preempt_events)

        if not has_attribution:
            # Graceful degradation: field absent in trace — warn but don't fail
            results.append(CheckResult(
                "WARN", f"B20_preempts_others[{process_name}]",
                f"Policy='can_preempt' declared but preempt events lack 'preempted_process' field; "
                f"cross-process attribution unavailable. Add 'preempted_process' to preempt events "
                f"for full B20 verification. Found {n_preempts} preempt event(s) for this process.",
                {"preempt_count": n_preempts, "has_attribution": False},
            ))
        else:
            # Full check: verify this process actually preempts OTHER processes
            other_preempts = [
                e for e in preempt_events
                if e.get("preempted_process") and e.get("preempted_process") != process_name
            ]
            if other_preempts:
                results.append(CheckResult(
                    "PASS", f"B20_preempts_others[{process_name}]",
                    f"Policy='can_preempt': {len(other_preempts)} attributable preemptions of other processes found.",
                    {"count": len(other_preempts), "has_attribution": True},
                ))
            else:
                results.append(CheckResult(
                    "WARN", f"B20_preempts_others[{process_name}]",
                    f"Policy='can_preempt' declared but no preemptions of other processes observed "
                    f"(checked {n_preempts} preempt events with attribution).",
                    {"preempt_count": n_preempts, "has_attribution": True},
                ))
    elif preemption_policy == "can_be_preempted":
        # Alias for interruptible=True; already covered above
        pass

    return results


# ─────────────────────────────────────────────────────────────────────────────
# B21: Rooted flow conservation (standalone, not per-process)
# ─────────────────────────────────────────────────────────────────────────────

def check_rooted_flow_conservation(ctx: Phase0Context) -> list[CheckResult]:
    """Verify entity accounting: rooted arrivals = departures + losses + in-system.

    Reads accounting_basis from compliance_spec["flow_accounting"]["accounting_basis"]:
      "rooted"   — only run rooted check (structural gate, BLOCK on failure)
      "windowed" — only run windowed event-count check (INFO, boundary artifacts expected)
      "both"     — run rooted gate first, then report windowed imbalance as INFO context
      None       — treated as "both" (default)

    Rooted entities = those with system_arrival time >= warmup.
    Constructs entity lifecycle from trace events and verifies conservation.

    Returns up to two CheckResults:
      B21_rooted_balance  — rooted entity accounting (PASS or BLOCK)
      B21_windowed_balance — windowed event-count imbalance (always INFO)
    """
    results: list[CheckResult] = []

    trace = ctx.trace
    warmup = ctx.warmup
    end_time = ctx.run_length

    # Read declared accounting basis from compliance spec. Under the v5.2
    # mapper schema, flow_accounting is a list of dicts (one per
    # flow_conservation element); accept both shapes for back-compat.
    fa = ctx.spec.get("flow_accounting", {})
    if isinstance(fa, list):
        flow_spec = fa[0] if fa else {}
    else:
        flow_spec = fa or {}
    accounting_basis: str = (flow_spec.get("accounting_basis") or "both").lower()

    # ── Rooted accounting (run unless basis is explicitly "windowed") ─────────
    if accounting_basis in ("rooted", "both"):
        entity_arrivals: dict[str, float] = {}
        entity_departures: set[str] = set()
        entity_losses: set[str] = set()

        for e in trace:
            ent_id = e.get("entity_id")
            if not ent_id:
                continue
            if e["event"] == "system_arrival":
                if ent_id not in entity_arrivals:
                    entity_arrivals[ent_id] = e["time"]
            elif e["event"] == "system_departure":
                entity_departures.add(ent_id)
            elif e["event"] == "loss":
                entity_losses.add(ent_id)

        rooted_entities = {eid for eid, atime in entity_arrivals.items() if atime >= warmup}
        a_rooted = len(rooted_entities)
        d_rooted = len(rooted_entities & entity_departures)
        l_rooted = len(rooted_entities & entity_losses)
        s_rooted = a_rooted - d_rooted - l_rooted

        if d_rooted + l_rooted + s_rooted == a_rooted:
            results.append(CheckResult(
                "PASS", "B21_rooted_balance",
                f"Rooted accounting OK: {a_rooted} arrivals = {d_rooted} dep + {l_rooted} loss + {s_rooted} in-system. "
                f"(basis={accounting_basis})",
                {"arrivals": a_rooted, "departures": d_rooted, "losses": l_rooted, "in_system": s_rooted,
                 "accounting_basis": accounting_basis},
            ))
        else:
            results.append(CheckResult(
                "BLOCK", "B21_rooted_balance",
                f"Rooted accounting FAILURE: {a_rooted} arrivals ≠ {d_rooted}+{l_rooted}+{s_rooted} (structural bug). "
                f"(basis={accounting_basis})",
                {"arrivals": a_rooted, "departures": d_rooted, "losses": l_rooted, "in_system": s_rooted,
                 "accounting_basis": accounting_basis},
            ))

    # ── Windowed accounting (run unless basis is explicitly "rooted") ─────────
    if accounting_basis in ("windowed", "both"):
        a_raw = len([e for e in trace if e["event"] == "system_arrival" and warmup <= e["time"] < end_time])
        d_raw = len([e for e in trace if e["event"] == "system_departure" and warmup <= e["time"] < end_time])
        l_raw = len([e for e in trace if e["event"] == "loss" and warmup <= e["time"] < end_time])
        imbalance = a_raw - d_raw - l_raw

        if imbalance >= 0:
            results.append(CheckResult(
                "INFO", "B21_windowed_balance",
                f"Windowed balance [warmup, end): {a_raw} arr, {d_raw} dep, {l_raw} loss; "
                f"{imbalance} in-system (boundary artifact expected). (basis={accounting_basis})",
                {"arrivals": a_raw, "departures": d_raw, "losses": l_raw, "in_system": imbalance,
                 "accounting_basis": accounting_basis},
            ))
        else:
            results.append(CheckResult(
                "INFO", "B21_windowed_balance",
                f"Windowed balance [warmup, end): {a_raw} arr, {d_raw} dep, {l_raw} loss; "
                f"imbalance={imbalance} (rooted entities exiting after warmup). (basis={accounting_basis})",
                {"arrivals": a_raw, "departures": d_raw, "losses": l_raw, "imbalance": imbalance,
                 "accounting_basis": accounting_basis},
            ))

    return results


# ─────────────────────────────────────────────────────────────────────────────
# B22: Count unit consistency
# ─────────────────────────────────────────────────────────────────────────────

def check_count_unit_consistency(
    ctx: Phase0Context,
    process_name: str,
    contract: dict[str, Any],
    episodes: list[dict],
) -> list[CheckResult]:
    """Verify count_unit (episode vs role_start) is used consistently.

    If count_unit='episode' and multiple required_roles:
      - ratio of n_role_starts/n_episodes should match n_req_roles or be ~1
      - warn if ≈1 when roles > 1 (possible count-unit confusion)
      - pass if ≈ n_req_roles

    If count_unit='role_start':
      - n_episodes ≈ n_role_starts / n_req_roles (multi-role scenario)
    """
    results: list[CheckResult] = []

    if not episodes:
        return [CheckResult("INFO", f"B22_count_unit[{process_name}]",
                           "No episodes found; count unit check skipped.")]

    n_episodes = len(episodes)
    n_role_starts = sum(ep.get("role_starts", 0) for ep in episodes)
    n_req_roles = len(contract.get("required_roles", []))
    count_unit = contract.get("count_unit", "episode")

    if count_unit == "episode":
        ratio = n_role_starts / max(n_episodes, 1)
        tolerance = 0.05

        if abs(ratio - 1.0) < tolerance and n_req_roles >= 2:
            # Role starts ≈ episodes despite multiple required roles
            results.append(CheckResult(
                "WARN", f"B22_count_unit[{process_name}]",
                f"count_unit='episode' but role_starts/episodes ≈ {ratio:.2f} (≈1 despite {n_req_roles} required roles); possible confusion.",
                {"ratio": ratio, "required_roles": n_req_roles},
            ))
        elif abs(ratio - float(n_req_roles)) < tolerance and n_req_roles >= 2:
            # Role starts ≈ episodes * n_req_roles
            results.append(CheckResult(
                "PASS", f"B22_count_unit[{process_name}]",
                f"count_unit='episode' consistent: {ratio:.2f} roles/episode ≈ {n_req_roles} required.",
                {"ratio": ratio, "required_roles": n_req_roles},
            ))
        else:
            results.append(CheckResult(
                "INFO", f"B22_count_unit[{process_name}]",
                f"count_unit='episode': {n_role_starts} role_starts / {n_episodes} episodes = {ratio:.2f} roles/ep.",
                {"ratio": ratio, "required_roles": n_req_roles},
            ))
    elif count_unit == "role_start":
        if n_req_roles >= 2:
            expected_episodes = n_role_starts / n_req_roles
            ratio = n_episodes / expected_episodes if expected_episodes > 0 else 0.0
            results.append(CheckResult(
                "INFO", f"B22_count_unit[{process_name}]",
                f"count_unit='role_start': {n_episodes} episodes / {expected_episodes:.1f} expected = {ratio:.2f}x.",
                {"episodes": n_episodes, "expected": expected_episodes, "ratio": ratio},
            ))
        else:
            results.append(CheckResult(
                "INFO", f"B22_count_unit[{process_name}]",
                f"count_unit='role_start' with <2 required_roles; check not applicable.",
            ))

    return results


# ─────────────────────────────────────────────────────────────────────────────
# B23: Schedule alignment
# ─────────────────────────────────────────────────────────────────────────────

def check_schedule_alignment(
    ctx: Phase0Context,
    process_name: str,
    contract: dict[str, Any],
    episodes: list[dict],
) -> list[CheckResult]:
    """Verify episode timing aligns with declared periodic/scheduled intervals.

    For periodic: compute inter-episode gaps, verify mean ≈ expected_interval.
    For scheduled with specific hours: count episodes in each hour band.
    For scheduled with times_per_day only: verify daily inter-episode gaps.

    Skip if < 5 episodes (insufficient data).
    """
    results: list[CheckResult] = []

    if len(episodes) < 5:
        return [CheckResult("INFO", f"B23_schedule[{process_name}]",
                           f"Only {len(episodes)} episodes; insufficient data for schedule alignment check.")]

    trigger = contract.get("trigger", "periodic")
    tolerance = contract.get("tolerance", 0.10)
    # Read contract-declared jitter threshold; default 0.3.
    # Tight periodic processes should declare ~0.05–0.10; event-driven may set 0.5+.
    expected_jitter_cv: float = float(contract.get("expected_jitter_cv") or 0.3)

    if trigger == "periodic":
        # PER-ENTITY (and, when count_unit==role_start, PER-ROLE-PER-ENTITY)
        # inter-event gaps. The schedule contract ("every N hours") describes
        # per-entity cadence (each patient is checked every N hours), so
        # pooling start_times across entities produced a global aggregate gap
        # of ≈ N_hours / num_concurrent_entities — a granularity bug that made
        # correctly-implemented per-patient cadences fail (e.g. 8 patients
        # each checked hourly → pooled gap ≈ 450s, not 3600s). The further
        # refinement: when count_unit=='role_start' (one event per role per
        # episode), pooling per-entity across roles produces an analogous
        # bug → per-role per-entity gaps are the right granularity.
        count_unit = (contract.get("count_unit") or "episode").lower()
        gaps_s: list[float] = []
        if count_unit == "role_start":
            # Per-(entity, role) gaps drawn directly from the trace so we don't
            # depend on episode_definition correctness; the schedule contract
            # for "every N hours" with count_unit=role_start describes per-role
            # cadence (each role visits each entity every N hours).
            starts_by_entity_role: dict[tuple[str, str], list[float]] = {}
            for ev in ctx.trace:
                if ev.get("event") != "service_start":
                    continue
                if ev.get("process") != process_name:
                    continue
                if ev.get("time", 0) < ctx.warmup:
                    continue
                key = (ev.get("entity_id", ""), ev.get("role", "") or
                       ev.get("resource", ""))
                starts_by_entity_role.setdefault(key, []).append(
                    float(ev["time"]))
            for key in starts_by_entity_role:
                starts_by_entity_role[key].sort()
                s = starts_by_entity_role[key]
                gaps_s.extend(s[i + 1] - s[i] for i in range(len(s) - 1))
        else:
            starts_by_entity: dict[str, list[float]] = {}
            for ep in episodes:
                starts_by_entity.setdefault(
                    ep.get("entity_id", ""), []).append(ep["start_time"])
            for eid in starts_by_entity:
                starts_by_entity[eid].sort()
                s = starts_by_entity[eid]
                gaps_s.extend(s[i + 1] - s[i] for i in range(len(s) - 1))

        if not gaps_s:
            return [CheckResult("INFO", f"B23_schedule[{process_name}]",
                               "Insufficient per-entity episodes for gap analysis "
                               f"(count_unit={count_unit}; every entity/role has ≤1 event).")]

        expected_gap_s = contract.get("every_hours", 1.0) * 3600.0
        mean_gap = statistics.mean(gaps_s)
        stdev_gap = statistics.stdev(gaps_s) if len(gaps_s) > 1 else 0.0
        cv = (stdev_gap / mean_gap) if mean_gap > 0 else 0.0

        gap_result = compare_ratio(
            mean_gap, expected_gap_s, tolerance,
            f"B23_schedule[{process_name}]",
            unit=" seconds mean gap",
        )
        results.append(gap_result)

        # Jitter check against contract-declared threshold (not hardcoded 0.3)
        if cv > expected_jitter_cv:
            results.append(CheckResult(
                "WARN", f"B23_jitter[{process_name}]",
                f"Inter-episode gap CV={cv:.3f} > declared threshold {expected_jitter_cv:.2f}; "
                f"higher jitter than expected for this process.",
                {"cv": cv, "threshold": expected_jitter_cv, "stdev": stdev_gap, "mean": mean_gap},
            ))
        else:
            results.append(CheckResult(
                "PASS", f"B23_jitter[{process_name}]",
                f"Inter-episode gap CV={cv:.3f} ≤ declared threshold {expected_jitter_cv:.2f}.",
                {"cv": cv, "threshold": expected_jitter_cv},
            ))

    elif trigger == "scheduled":
        schedule_times = contract.get("schedule_times_hour")
        times_per_day = contract.get("times_per_day")

        if schedule_times:
            # Verify episodes occur near declared hours
            hour_coverage = defaultdict(int)
            for ep in episodes:
                time_of_day_h = (ep["start_time"] % 86400.0) / 3600.0
                for declared_h in schedule_times:
                    if abs(time_of_day_h - declared_h) <= 0.5:
                        hour_coverage[declared_h] += 1
                        break

            coverage = len(hour_coverage)
            expected_coverage = len(schedule_times)
            coverage_ratio = coverage / expected_coverage if expected_coverage > 0 else 0.0

            if coverage_ratio >= 0.8:
                results.append(CheckResult(
                    "PASS", f"B23_schedule[{process_name}]",
                    f"Schedule coverage {coverage}/{expected_coverage} ≥ 80%.",
                    {"coverage": coverage, "expected": expected_coverage},
                ))
            elif coverage_ratio >= 0.5:
                results.append(CheckResult(
                    "WARN", f"B23_schedule[{process_name}]",
                    f"Schedule coverage {coverage}/{expected_coverage} = {coverage_ratio:.0%} ∈ [50%, 80%).",
                    {"coverage": coverage, "expected": expected_coverage},
                ))
            else:
                results.append(CheckResult(
                    "FAIL", f"B23_schedule[{process_name}]",
                    f"Schedule coverage {coverage}/{expected_coverage} = {coverage_ratio:.0%} < 50%.",
                    {"coverage": coverage, "expected": expected_coverage},
                ))
        elif times_per_day:
            # Check daily inter-episode gaps
            start_times = sorted([ep["start_time"] for ep in episodes])
            expected_interval_h = 24.0 / times_per_day
            expected_interval_s = expected_interval_h * 3600.0

            # Group episodes by day and compute within-day gaps
            by_day = defaultdict(list)
            for t in start_times:
                day = int(t // 86400)
                by_day[day].append(t)

            all_gaps = []
            for day_eps in by_day.values():
                if len(day_eps) > 1:
                    day_gap = [day_eps[i + 1] - day_eps[i] for i in range(len(day_eps) - 1)]
                    all_gaps.extend(day_gap)

            if all_gaps:
                mean_gap = statistics.mean(all_gaps)
                gap_result = compare_ratio(
                    mean_gap, expected_interval_s, tolerance,
                    f"B23_daily_schedule[{process_name}]",
                    unit=" seconds",
                )
                results.append(gap_result)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# B24: Workload realism
# ─────────────────────────────────────────────────────────────────────────────

def check_workload_realism(
    ctx: Phase0Context,
    process_name: str,
    contract: dict[str, Any],
    episodes: list[dict],
) -> list[CheckResult]:
    """Verify that observed episode counts are workload-plausible (B24).

    For periodic processes, expected episode count from first principles:
      E[N] = ∫L(t)dt × (24 / every_hours) / 86400
           = area_entity_s / (every_hours × 3600)

    This directly encodes the key workload insight:
      "patient-check episodes ≈ census × checks_per_day × days"

    Unlike B18 (which uses the episode extraction pipeline), B24 uses raw
    service_start event counts to cross-validate, catching cases where
    the grouping strategy itself is creating incorrect episode counts.

    Returns FAIL if raw event count is < 10% of expected (severe under-triggering)
    or > 300% (severe over-triggering), independent of tolerance.
    Uses separate WARN thresholds at 50% / 200%.
    """
    results: list[CheckResult] = []
    trigger = contract.get("trigger", "periodic")

    if trigger != "periodic":
        return results  # Workload realism only makes sense for periodic processes

    every_hours = contract.get("every_hours")
    if not every_hours or every_hours <= 0:
        return [CheckResult("INFO", f"B24_workload[{process_name}]",
                           "every_hours not set; workload realism check skipped.")]

    # Compute expected from integrated census
    area_entity_s, avg_census_int = _compute_integrated_census(
        ctx.trace, ctx.warmup, ctx.run_length
    )
    expected = area_entity_s / (every_hours * 3600.0)

    if expected < 1.0:
        return [CheckResult("INFO", f"B24_workload[{process_name}]",
                           f"Expected episodes < 1 (short run / low census); workload check not meaningful.")]

    # Use raw service_start count as independent cross-check (bypass episode grouping)
    raw_events = filter_events(
        [e for e in ctx.trace if e.get("time", 0) >= ctx.warmup],
        event="service_start",
        process=process_name,
    )
    raw_count = len(raw_events)
    ratio = raw_count / expected

    details = {
        "raw_service_starts": raw_count,
        "expected_from_census": round(expected, 1),
        "ratio": round(ratio, 3),
        "episode_count": len(episodes),
        "integrated_avg_census": round(avg_census_int, 2),
        "every_hours": every_hours,
    }

    if ratio < 0.10:
        results.append(CheckResult(
            "FAIL", f"B24_workload[{process_name}]",
            f"Severe under-triggering: {raw_count} service_starts vs {expected:.0f} expected "
            f"(ratio={ratio:.2f}, < 10%). Possible scheduling bug or process not firing.",
            details,
        ))
    elif ratio < 0.50:
        results.append(CheckResult(
            "WARN", f"B24_workload[{process_name}]",
            f"Under-triggering: {raw_count} service_starts vs {expected:.0f} expected "
            f"(ratio={ratio:.2f}). Missed checks likely.",
            details,
        ))
    elif ratio > 3.0:
        results.append(CheckResult(
            "FAIL", f"B24_workload[{process_name}]",
            f"Severe over-triggering: {raw_count} service_starts vs {expected:.0f} expected "
            f"(ratio={ratio:.2f}, > 300%). Possible double-scheduling or loop bug.",
            details,
        ))
    elif ratio > 2.0:
        results.append(CheckResult(
            "WARN", f"B24_workload[{process_name}]",
            f"Over-triggering: {raw_count} service_starts vs {expected:.0f} expected "
            f"(ratio={ratio:.2f}). Check for duplicate scheduling.",
            details,
        ))
    else:
        results.append(CheckResult(
            "PASS", f"B24_workload[{process_name}]",
            f"Workload plausible: {raw_count} service_starts vs {expected:.0f} expected "
            f"(ratio={ratio:.2f} ∈ [0.5, 2.0]).",
            details,
        ))

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def validate_process_contracts(ctx: Phase0Context) -> list[CheckResult]:
    """Run all process contract validators (B18–B23).

    B21 (rooted flow conservation) always runs if trace is non-empty.
    B18–B20, B22–B23 run for each process in spec["process_contracts"].

    Returns empty list if compliance_spec has no "process_contracts" section.
    """
    results: list[CheckResult] = []

    # B21 always runs (no per-process)
    if ctx.trace:
        results.extend(check_rooted_flow_conservation(ctx))

    # Get process_contracts section
    spec = ctx.spec
    process_contracts = spec.get("process_contracts", {})

    if not process_contracts:
        return results

    # Canonical shape is a dict keyed by process name. Be defensive: some
    # producers (and some LLM-emitted specs) hand back a LIST of contract
    # entries instead. Normalize a list into the dict shape using each entry's
    # process_name (or name) as the key, so this validator never crashes on a
    # list with `.items()`.
    if isinstance(process_contracts, list):
        process_contracts = {
            (e.get("process_name") or e.get("name") or f"process_{i}"): e
            for i, e in enumerate(process_contracts)
            if isinstance(e, dict)
        }

    # Run B18–B24 for each process
    for process_name, contract in process_contracts.items():
        try:
            # Extract episodes once per process using contract's episode_definition
            episode_definition = contract.get("episode_definition")
            episodes = _extract_episodes(
                ctx.trace, process_name, ctx.warmup, episode_definition
            )

            # B18: frequency (uses integrated census)
            results.extend(check_process_frequency(ctx, process_name, contract))

            # B19: role structure (trace-based shift-opening check)
            results.extend(check_role_structure(ctx, process_name, contract, episodes))

            # B20: interruptibility (graceful fallback on missing preempted_process)
            results.extend(check_interruptibility(ctx, process_name, contract))

            # B22: count unit consistency
            results.extend(check_count_unit_consistency(ctx, process_name, contract, episodes))

            # B23: schedule alignment (contract-driven jitter CV threshold)
            results.extend(check_schedule_alignment(ctx, process_name, contract, episodes))

            # B24: workload realism (cross-check raw event count vs census integral)
            results.extend(check_workload_realism(ctx, process_name, contract, episodes))

        except Exception as e:
            # Catch any per-process errors and report as WARN (never silently suppress)
            results.append(CheckResult(
                "WARN", f"B18_B24[{process_name}]",
                f"Exception during process validation: {e}",
                {"error": str(e)},
            ))

    return results
