"""
semantic_checks.py — Layer B Extended (v5.2): single-trace semantic verifiers
B24 – B41.

These checks consume compliance_spec sections produced by compliance_mapper
v5.2 and verify the trace against semantic properties that go beyond the
original 17 structural contracts.

Each check is defensive: if the trace lacks the optional fields the check
needs (e.g. `server_id`, `priority`), it emits an INFO advisory and skips
rather than failing.  Phase 4.5 (semantic_scenarios, coupling_rules,
control_policies) is verified by semantic_scenarios.py instead.

Check roster
============
  B24  eligibility_rules          — no service_start for ineligible entities
  B25  coverage_rules             — per-entity coverage cadence
  B26  routing_rules              — branch probabilities match declaration
  B27  assignment_continuity      — same-server-for-same-entity rules
  B28  policy_thresholds          — breach triggers declared escalation
  B29  duration_semantics         — duration_scope / setup / teardown
  B30  capacity_consumption       — exclusive / fractional / consumable
  B31  queue_discipline           — FIFO / priority / balk / renege
  B32  batching                   — batched_start / batched_complete alignment
  B33  handoff_gap                — handoff_pair max_gap_s honored
  B34  calendar                   — no activity during breaks/holidays
  B35  loss_conditions            — losses emitted when condition_expr holds
  B36  state_dwell                — mean dwell per state matches dwell_distribution
  B37  composition_bands          — workload split within declared bands
  B38  warmup_handling            — aggregate_targets respect warmup_handling
  B39  kpi_decomposition          — numerator/denominator/basis consistency
  B40  granularity_consistency    — entity-identity unit matches declaration
  B41  pool_composition           — soft_pool composition matches declared
                                    pool_policy (fixed_team vs all_idle_at_start
                                    vs all_available_through_window)

Entry point
===========
    all_results += validate_semantic_contracts(ctx)
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any

from vvuq_utils import (
    CheckResult, Phase0Context, Severity,
    post_warmup_events, filter_events, group_by_entity,
    compare_ratio,
)


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _field_present(trace: list[dict], field: str) -> bool:
    return any(field in ev and ev[field] is not None for ev in trace)


def _info_skip(check_name: str, reason: str) -> CheckResult:
    return CheckResult("INFO", check_name, reason)


def _section_empty(check_name: str, section: str) -> CheckResult:
    return CheckResult("INFO", check_name, f"No {section} specs; skipped.")


def _severity_from_violations(violations: int, fatal: bool = False) -> Severity:
    if violations == 0:
        return "PASS"
    return "FAIL" if fatal else "WARN"


# ─────────────────────────────────────────────────────────────────────────────
# B24  Eligibility rules
# ─────────────────────────────────────────────────────────────────────────────

def verify_eligibility_rules(ctx: Phase0Context) -> list[CheckResult]:
    """B24: No service_start for an ineligible entity."""
    specs = ctx.spec.get("eligibility_rules", [])
    if not specs:
        return [_section_empty("B24_eligibility_rules", "eligibility_rules")]

    pwu = post_warmup_events(ctx)
    groups = group_by_entity(pwu)

    # Build per-entity state history: latest state_change per entity
    # and prerequisite completions.
    completions: dict[str, set[str]] = defaultdict(set)
    for e in pwu:
        if e.get("event") == "service_end" and e.get("process"):
            completions[e["entity_id"]].add(e["process"])

    out: list[CheckResult] = []
    for i, spec in enumerate(specs):
        proc = spec.get("process")
        elig_states = set(spec.get("eligible_states") or [])
        elig_etypes = set(spec.get("eligible_entity_types") or [])
        prereqs     = spec.get("requires_prerequisites") or []

        checked = violations = 0
        for eid, evs in groups.items():
            rel = [e for e in evs if e.get("event") == "service_start"
                   and (proc is None or e.get("process") == proc)]
            if not rel:
                continue
            for first in rel:
                checked += 1
                state_ok = (not elig_states) or (first.get("state") in elig_states)
                etype_ok = (not elig_etypes) or (first.get("entity_type") in elig_etypes)
                # Prereqs must have completed (strict: service_end) before this time
                prereq_ok = True
                if prereqs:
                    earlier = [e for e in evs
                               if e.get("time", 0.0) <= first.get("time", 0.0)
                               and e.get("event") == "service_end"
                               and e.get("process") in prereqs]
                    got = {e.get("process") for e in earlier}
                    prereq_ok = set(prereqs).issubset(got)
                if not (state_ok and etype_ok and prereq_ok):
                    violations += 1
                    break  # one violation per entity is enough

        tol = spec.get("tolerance", 0.0)
        frac = violations / checked if checked else 0.0
        sev: Severity = "PASS" if frac <= tol else "FAIL"
        out.append(CheckResult(
            sev, f"B24_eligibility_rules[{i}]",
            f"process='{proc}': {violations}/{checked} service_starts violated eligibility.",
            {"violations": violations, "checked": checked, "fraction": frac},
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# B25  Coverage cadence
# ─────────────────────────────────────────────────────────────────────────────

def verify_coverage_rules(ctx: Phase0Context) -> list[CheckResult]:
    """B25: Every eligible entity gets covered at declared cadence.

    For each coverage_rule, look at service_start events matching
    `coverage_target` per entity and confirm the max gap between
    successive coverage events (while the entity is in system) does
    not exceed `interval_hours * (1 + tolerance)`.
    """
    specs = ctx.spec.get("coverage_rules", [])
    if not specs:
        return [_section_empty("B25_coverage_rules", "coverage_rules")]

    pwu = post_warmup_events(ctx)
    groups = group_by_entity(pwu)

    out: list[CheckResult] = []
    for i, spec in enumerate(specs):
        target = spec.get("coverage_target")
        etype  = spec.get("entity_type")
        interval_s = (spec.get("interval_hours") or 0.0) * 3_600.0
        tol = spec.get("tolerance", 0.10)
        if interval_s <= 0:
            out.append(_info_skip(f"B25_coverage_rules[{i}]",
                                  "interval_hours missing or ≤0; skipped."))
            continue
        limit = interval_s * (1.0 + tol)

        checked = violations = 0
        max_gap_observed = 0.0
        for _, evs in groups.items():
            if etype is not None and not any(
                e.get("entity_type") == etype for e in evs
            ):
                continue
            arr = next((e for e in evs if e.get("event") == "system_arrival"), None)
            end = next((e for e in evs if e.get("event")
                        in ("system_departure", "loss")), None)
            if arr is None:
                continue
            t_start = arr["time"]
            t_end = end["time"] if end else ctx.trace[-1]["time"]
            cover_times = sorted(
                e["time"] for e in evs
                if e.get("event") == "service_start"
                and (target is None or e.get("process") == target
                     or e.get("resource") == target)
            )
            # Gap from arrival to first, between successives, and to end
            anchors = [t_start] + cover_times + [t_end]
            checked += 1
            for j in range(1, len(anchors)):
                gap = anchors[j] - anchors[j-1]
                if gap > max_gap_observed:
                    max_gap_observed = gap
                if gap > limit + 1e-6:
                    violations += 1
                    break

        sev: Severity = "PASS" if violations == 0 else "FAIL"
        out.append(CheckResult(
            sev, f"B25_coverage_rules[{i}]",
            f"target='{target}': {violations}/{checked} entities breached "
            f"{spec.get('interval_hours')}-hour cadence "
            f"(max observed gap = {max_gap_observed/3600.0:.2f}h).",
            {"violations": violations, "checked": checked,
             "max_gap_hours": max_gap_observed / 3600.0},
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# B26  Routing probabilities
# ─────────────────────────────────────────────────────────────────────────────

def verify_routing_rules(ctx: Phase0Context) -> list[CheckResult]:
    """B26: Branch probabilities from `routing_from` match declaration."""
    specs = ctx.spec.get("routing_rules", [])
    if not specs:
        return [_section_empty("B26_routing_rules", "routing_rules")]

    pwu = post_warmup_events(ctx)
    groups = group_by_entity(pwu)

    out: list[CheckResult] = []
    for i, spec in enumerate(specs):
        src = spec.get("routing_from")
        targets = spec.get("routing_to") or []
        tol = spec.get("tolerance", 0.05)
        if not src or not targets:
            out.append(_info_skip(f"B26_routing_rules[{i}]",
                                  "routing_from or routing_to missing; skipped."))
            continue

        # For each entity, find first service_end at src, then the very next
        # service_start and record its process/resource.
        observed_counts: dict[str, int] = defaultdict(int)
        total = 0
        for _, evs in groups.items():
            evs_sorted = sorted(evs, key=lambda x: (x.get("time", 0.0), x.get("seq", 0)))
            for k, e in enumerate(evs_sorted):
                if e.get("event") == "service_end" and (
                    e.get("process") == src or e.get("resource") == src
                ):
                    # Next service_start
                    nxt = next((ev for ev in evs_sorted[k+1:]
                                if ev.get("event") == "service_start"), None)
                    # Or terminal
                    if nxt is None:
                        term = next((ev for ev in evs_sorted[k+1:]
                                     if ev.get("event")
                                     in ("system_departure", "loss")), None)
                        if term is not None:
                            observed_counts[f"__terminal__{term['event']}"] += 1
                            total += 1
                        continue
                    key = nxt.get("process") or nxt.get("resource") or "_unknown_"
                    observed_counts[key] += 1
                    total += 1

        if total == 0:
            out.append(_info_skip(f"B26_routing_rules[{i}]",
                                  f"No transitions observed out of '{src}'."))
            continue

        # Compare observed fractions to declared probabilities
        violations = []
        for route in targets:
            tgt = route.get("target")
            p_exp = route.get("probability")
            if tgt is None or p_exp is None:
                continue
            observed_frac = observed_counts.get(tgt, 0) / total
            if abs(observed_frac - p_exp) > tol:
                violations.append(
                    f"{tgt}: observed={observed_frac:.3f}, "
                    f"expected={p_exp:.3f} (tol={tol:.3f})"
                )

        sev: Severity = "PASS" if not violations else "FAIL"
        out.append(CheckResult(
            sev, f"B26_routing_rules[{i}]",
            f"from '{src}' ({total} transitions): "
            + ("; ".join(violations) if violations else "all branches within tolerance."),
            {"observed_counts": dict(observed_counts),
             "total_transitions": total, "violations": violations},
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# B27  Assignment continuity
# ─────────────────────────────────────────────────────────────────────────────

def verify_assignment_continuity(ctx: Phase0Context) -> list[CheckResult]:
    """B27: Same server_id persists across the declared continuity scope."""
    specs = ctx.spec.get("assignment_continuity", [])
    if not specs:
        return [_section_empty("B27_assignment_continuity", "assignment_continuity")]

    pwu = post_warmup_events(ctx)
    if not _field_present(pwu, "server_id"):
        return [_info_skip("B27_assignment_continuity",
                           "Trace lacks 'server_id'; cannot verify continuity. "
                           "Add server_id to service_start events to enable B27.")]

    groups = group_by_entity(pwu)
    out: list[CheckResult] = []
    for i, spec in enumerate(specs):
        proc = spec.get("process")
        scope = spec.get("continuity_scope", "episode")
        tol = spec.get("tolerance", 0.05)

        checked = violations = 0
        for _, evs in groups.items():
            rel = [e for e in evs if e.get("event") == "service_start"
                   and (proc is None or e.get("process") == proc)]
            if len(rel) < 2:
                continue

            if scope == "shift":
                # Group by shift_id then assert same server within shift
                buckets = defaultdict(list)
                for e in rel:
                    buckets[e.get("shift_id")].append(e)
                for _, grp in buckets.items():
                    checked += 1
                    servers = {e.get("server_id") for e in grp}
                    if len(servers) > 1:
                        violations += 1
            else:
                # episode / entity_lifetime / unit: single bucket per entity
                checked += 1
                servers = {e.get("server_id") for e in rel}
                if len(servers) > 1:
                    violations += 1

        frac = violations / checked if checked else 0.0
        sev: Severity = "PASS" if frac <= tol else "FAIL"
        out.append(CheckResult(
            sev, f"B27_assignment_continuity[{i}]",
            f"process='{proc}' scope='{scope}': "
            f"{violations}/{checked} episodes broke continuity "
            f"(tol={tol:.2f}).",
            {"violations": violations, "checked": checked, "fraction": frac},
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# B28  Policy thresholds
# ─────────────────────────────────────────────────────────────────────────────

def verify_policy_thresholds(ctx: Phase0Context) -> list[CheckResult]:
    """B28: When wait/sojourn exceeds breach_threshold_seconds, the declared
    escalation_outcome (a loss_type or state_change) must be emitted."""
    specs = ctx.spec.get("policy_thresholds", [])
    if not specs:
        return [_section_empty("B28_policy_thresholds", "policy_thresholds")]

    pwu = post_warmup_events(ctx)
    groups = group_by_entity(pwu)

    out: list[CheckResult] = []
    for i, spec in enumerate(specs):
        threshold = spec.get("breach_threshold_seconds")
        outcome   = (spec.get("escalation_outcome") or "").strip()
        etype     = spec.get("entity_type")
        if threshold is None or not outcome:
            out.append(_info_skip(f"B28_policy_thresholds[{i}]",
                                  "breach_threshold_seconds or escalation_outcome missing."))
            continue

        # Parse outcome: support "emit loss with type=<T>" or
        # "state_change to <S>" simple forms; otherwise treat as loss_type.
        loss_type_match = None
        target_state   = None
        low = outcome.lower()
        if "loss" in low and "type=" in low:
            loss_type_match = outcome.split("type=")[-1].strip().strip(" '\";.")
        elif "state_change" in low and " to " in low:
            target_state = outcome.split(" to ")[-1].strip().strip(" '\";.")
        else:
            loss_type_match = outcome  # assume raw loss_type

        breached = escalated = 0
        for _, evs in groups.items():
            rel = [e for e in evs
                   if etype is None or e.get("entity_type") == etype]
            arr = next((e for e in rel if e.get("event") == "system_arrival"), None)
            if arr is None:
                continue
            # Queue entry time (first queue_enter) or arrival time
            qe = next((e for e in rel if e.get("event") == "queue_enter"),
                      arr)
            ss = next((e for e in rel if e.get("event") == "service_start"),
                      None)
            loss = next((e for e in rel if e.get("event") == "loss"), None)
            # 'wait' = first service_start time - queue_enter time, or
            # 'sojourn' = terminal - arrival, depending on condition_expr text.
            wait = None
            if ss is not None:
                wait = ss["time"] - qe["time"]
            elif loss is not None:
                wait = loss["time"] - qe["time"]

            if wait is None or wait <= threshold:
                continue
            breached += 1

            # Check escalation occurred
            if loss_type_match:
                if loss is not None and loss.get("loss_type") == loss_type_match:
                    escalated += 1
            elif target_state:
                sc = next((e for e in rel if e.get("event") == "state_change"
                           and e.get("next_state") == target_state), None)
                if sc is not None:
                    escalated += 1

        if breached == 0:
            out.append(CheckResult(
                "INFO", f"B28_policy_thresholds[{i}]",
                f"No threshold breaches observed (threshold={threshold:.0f}s).",
            ))
            continue
        rate = escalated / breached
        sev: Severity = "PASS" if rate >= 1.0 - spec.get("tolerance", 0.0) else "FAIL"
        out.append(CheckResult(
            sev, f"B28_policy_thresholds[{i}]",
            f"{escalated}/{breached} threshold breaches escalated to '{outcome}' "
            f"(rate={rate:.1%}).",
            {"breached": breached, "escalated": escalated, "rate": rate},
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# B29  Duration semantics: duration_scope / setup / teardown
# ─────────────────────────────────────────────────────────────────────────────

def verify_duration_semantics(ctx: Phase0Context) -> list[CheckResult]:
    """B29: duration_scope (per_episode vs per_attempt) + setup/teardown.

    For specs with duration_scope='per_episode', the sum of service durations
    across attempts per entity should approximate a single drawn mean_service_seconds
    (we test coefficient of variation of total_service_time vs declared CV).
    For setup/teardown: service_end - service_start >= setup + teardown
    (best-effort; passes as INFO when per-attempt time fields are absent).

    Segmentation: when the spec declares an `entity_type`, only entities of
    that type are counted toward the per-episode mean. This handles the case
    where one process (e.g. ``treatment``) serves multiple acuity classes with
    distinct mean_service_seconds — without segmentation the check pools all
    episodes into a single mix-weighted mean that cannot match any individual
    declared mean. Mirrors the per-type segmentation used by B03 service_rates.
    """
    specs = ctx.spec.get("service_rates", [])
    relevant = [s for s in specs
                if s.get("duration_scope") or s.get("setup_seconds")
                or s.get("teardown_seconds")]
    if not relevant:
        return [_section_empty("B29_duration_semantics",
                               "service_rates with duration semantics")]

    pwu = post_warmup_events(ctx)
    groups = group_by_entity(pwu)

    out: list[CheckResult] = []
    for i, spec in enumerate(relevant):
        res = spec.get("resource")
        mean_s = spec.get("mean_service_seconds")
        scope = spec.get("duration_scope")
        setup = float(spec.get("setup_seconds") or 0.0)
        teardown = float(spec.get("teardown_seconds") or 0.0)
        etype = spec.get("entity_type")

        durations_per_episode: list[float] = []
        short_services = 0
        total_services = 0

        for _, evs in groups.items():
            # If the spec declares an entity_type, restrict the per-episode
            # accounting to entities of that type (same convention as B25/B27/B28).
            if etype is not None and not any(
                    e.get("entity_type") == etype for e in evs):
                continue
            relevant = [e for e in evs
                        if e.get("event") in ("service_start", "service_end")
                        and (res is None or e.get("resource") == res)]
            if not any(e.get("event") == "service_start" for e in relevant):
                continue
            # Pair service_start → service_end by (resource, segment_id) within
            # this entity, in time order. Independent sorting + zip mis-pairs a
            # start to an unrelated end whenever services interleave or one is
            # still open at trace end; keying avoids that. Falls back to FIFO
            # per resource when segment_id is absent.
            open_seg: dict[tuple, float] = {}      # (resource, segment_id) -> start time
            fifo: dict[Any, list] = defaultdict(list)  # resource -> [start times]
            total = 0.0
            for e in sorted(relevant, key=lambda x: (x["time"], x.get("seq", 0))):
                r = e.get("resource")
                seg = e.get("segment_id")
                if e.get("event") == "service_start":
                    if seg is not None:
                        open_seg[(r, seg)] = e["time"]
                    else:
                        fifo[r].append(e["time"])
                else:  # service_end
                    t0 = None
                    if seg is not None and (r, seg) in open_seg:
                        t0 = open_seg.pop((r, seg))
                    elif fifo[r]:
                        t0 = fifo[r].pop(0)
                    if t0 is None:
                        continue
                    dur = e["time"] - t0
                    if dur < setup + teardown - 1e-6:
                        short_services += 1
                    total += dur
                    total_services += 1
            if total > 0:
                durations_per_episode.append(total)

        msgs = []
        sev: Severity = "PASS"
        if setup + teardown > 0 and total_services > 0:
            if short_services > 0:
                sev = "FAIL"
                msgs.append(f"{short_services}/{total_services} service durations "
                            f"shorter than setup+teardown={setup+teardown:.1f}s")
            else:
                msgs.append(f"all {total_services} services ≥ setup+teardown.")
        type_tag = f" (entity_type={etype})" if etype is not None else ""
        if scope == "per_episode" and mean_s and len(durations_per_episode) >= 20:
            mean_obs = statistics.mean(durations_per_episode)
            if abs(mean_obs - mean_s) / mean_s > spec.get("tolerance", 0.25):
                sev = "FAIL" if sev == "PASS" else sev
                msgs.append(f"per_episode mean total duration{type_tag} = "
                            f"{mean_obs:.1f}s, expected≈{mean_s:.1f}s.")
            else:
                msgs.append(f"per_episode mean{type_tag} ≈ {mean_obs:.1f}s (ok).")
        elif scope == "per_episode" and mean_s and len(durations_per_episode) < 20:
            msgs.append(f"per_episode scope{type_tag}: only "
                        f"{len(durations_per_episode)} episodes observed "
                        f"(< 20 threshold); insufficient sample for mean check.")
            sev = "INFO" if sev == "PASS" else sev  # type: ignore
        if scope == "per_attempt":
            msgs.append(f"per_attempt scope{type_tag} — "
                        f"{total_services} attempts observed.")

        if not msgs:
            msgs.append("no duration checks applicable.")
            sev = "INFO"  # type: ignore

        out.append(CheckResult(sev, f"B29_duration_semantics[{i}]",
                               "; ".join(msgs),
                               {"total_services": total_services,
                                "short_services": short_services,
                                "n_episodes": len(durations_per_episode),
                                "entity_type": etype}))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# B30  Capacity consumption
# ─────────────────────────────────────────────────────────────────────────────

def verify_capacity_consumption(ctx: Phase0Context) -> list[CheckResult]:
    """B30: consumption_mode semantics:
       exclusive → max_concurrent respected
       fractional → overlaps allowed, sum ≤ capacity
       consumable → inventory depletes & replenishes per declaration
       shared → supervisory: no blocking on concurrent
    """
    specs = ctx.spec.get("service_rates", [])
    relevant = [s for s in specs if s.get("consumption_mode")]
    if not relevant:
        return [_section_empty("B30_capacity_consumption",
                               "service_rates with consumption_mode")]

    pwu = post_warmup_events(ctx)
    out: list[CheckResult] = []
    for i, spec in enumerate(relevant):
        res = spec.get("resource")
        mode = spec.get("consumption_mode")
        max_conc = spec.get("max_concurrent")

        if mode in ("exclusive", "shared") and max_conc:
            # Simulate occupancy sweep
            events = [(e["time"], +1) for e in pwu
                      if e.get("event") == "service_start" and e.get("resource") == res]
            events += [(e["time"], -1) for e in pwu
                       if e.get("event") == "service_end" and e.get("resource") == res]
            events.sort()
            max_occ = occ = 0
            for _, d in events:
                occ += d
                max_occ = max(max_occ, occ)
            sev: Severity = "PASS" if max_occ <= max_conc else "FAIL"
            out.append(CheckResult(
                sev, f"B30_capacity_consumption[{i}]",
                f"resource='{res}' mode={mode}: peak concurrency={max_occ}, "
                f"max_concurrent={max_conc}.",
                {"peak": max_occ, "max_concurrent": max_conc},
            ))
        elif mode == "consumable":
            out.append(CheckResult(
                "INFO", f"B30_capacity_consumption[{i}]",
                f"resource='{res}' consumable — inventory depletion "
                "check requires metrics counters (deferred)."))
        elif mode == "fractional":
            out.append(CheckResult(
                "INFO", f"B30_capacity_consumption[{i}]",
                f"resource='{res}' fractional — sum-of-fractions check "
                "requires fractional service-time metadata (deferred)."))
        else:
            out.append(_info_skip(f"B30_capacity_consumption[{i}]",
                                  f"unrecognized consumption_mode '{mode}'."))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# B31  Queue discipline
# ─────────────────────────────────────────────────────────────────────────────

def verify_queue_discipline(ctx: Phase0Context) -> list[CheckResult]:
    """B31: discipline enforcement (FIFO/priority), balk/renege counts."""
    specs = ctx.spec.get("service_rates", [])
    relevant = [s for s in specs
                if s.get("discipline") or s.get("balk_renege_policy")]
    if not relevant:
        return [_section_empty("B31_queue_discipline",
                               "service_rates with discipline")]

    pwu = post_warmup_events(ctx)
    out: list[CheckResult] = []
    for i, spec in enumerate(relevant):
        res = spec.get("resource")
        disc = spec.get("discipline")
        brp = spec.get("balk_renege_policy") or {}

        # FIFO: enqueue order should match service_start order per resource
        if disc == "FIFO":
            queue = []   # list of (time, entity_id)
            served_order: list[tuple[float, str]] = []
            enqueue_order: dict[str, int] = {}
            counter = 0
            for e in pwu:
                if e.get("resource") != res:
                    continue
                if e.get("event") == "queue_enter":
                    counter += 1
                    enqueue_order[e["entity_id"]] = counter
                    queue.append(e["entity_id"])
                elif e.get("event") == "service_start":
                    served_order.append((e["time"], e["entity_id"]))

            # Compare served order to enqueue rank
            inversions = 0
            last_rank = 0
            for _, eid in served_order:
                rank = enqueue_order.get(eid, -1)
                if rank > 0 and rank < last_rank:
                    inversions += 1
                if rank > last_rank:
                    last_rank = rank
            sev: Severity = "PASS" if inversions == 0 else "WARN"
            out.append(CheckResult(
                sev, f"B31_queue_discipline[{i}]_FIFO",
                f"resource='{res}': {inversions} FIFO inversions "
                f"over {len(served_order)} services.",
                {"inversions": inversions, "services": len(served_order)},
            ))
        elif disc == "priority":
            # Skip without priority field
            if not _field_present(pwu, "priority"):
                out.append(_info_skip(
                    f"B31_queue_discipline[{i}]_priority",
                    f"resource='{res}' priority — 'priority' field absent in trace."))
            else:
                # Measure: at each service_start, is chosen entity the highest-priority
                # one currently queued? Best-effort scan.
                # At each service_start, was a strictly-higher-priority OTHER
                # entity still queued? Compare the chosen entity's priority
                # against the priorities of the entities still waiting
                # (excluding the chosen one — including it would compare it to
                # itself and mask nothing but adds noise). Convention assumed:
                # higher priority number = higher priority. Reported as WARN
                # because the trace contract does not pin the priority
                # convention; a clean PASS is informative, a non-zero count
                # warrants a look rather than a hard FAIL.
                violations = checked = 0
                queued: dict[str, tuple[float, int]] = {}
                for e in pwu:
                    if e.get("resource") != res:
                        continue
                    if e.get("event") == "queue_enter":
                        queued[e["entity_id"]] = (e["time"], e.get("priority", 0))
                    elif e.get("event") == "service_start" and e["entity_id"] in queued:
                        checked += 1
                        chosen_prio = queued[e["entity_id"]][1]
                        others = [p for eid, (_, p) in queued.items()
                                  if eid != e["entity_id"]]
                        if others and chosen_prio < max(others):
                            violations += 1
                        queued.pop(e["entity_id"], None)
                sev = "PASS" if violations == 0 else "WARN"
                out.append(CheckResult(
                    sev, f"B31_queue_discipline[{i}]_priority",
                    f"resource='{res}': {violations}/{checked} services chose an "
                    "entity while a strictly-higher-priority other entity was "
                    "queued (assumes higher number = higher priority).",
                    {"violations": violations, "checked": checked},
                ))
        else:
            out.append(_info_skip(f"B31_queue_discipline[{i}]",
                                  f"discipline '{disc}' not automatically checkable."))

        # Balk/renege presence checks
        if brp:
            balks = len([e for e in pwu if e.get("event") == "loss"
                         and e.get("loss_type") == "balk"])
            reneges = len([e for e in pwu if e.get("event") == "loss"
                           and e.get("loss_type") == "renege"])
            msg = f"resource='{res}': balks={balks}, reneges={reneges}."
            out.append(CheckResult("INFO", f"B31_queue_discipline[{i}]_balk_renege", msg,
                                   {"balks": balks, "reneges": reneges}))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# B32  Batching
# ─────────────────────────────────────────────────────────────────────────────

def verify_batching(ctx: Phase0Context) -> list[CheckResult]:
    """B32: batched_start → entities in same batch share service_start time."""
    specs = ctx.spec.get("coordination_patterns", [])
    relevant = [s for s in specs
                if s.get("batched_start") or s.get("batched_complete")]
    if not relevant:
        return [_section_empty("B32_batching", "coordination_patterns with batching")]

    pwu = post_warmup_events(ctx)
    out: list[CheckResult] = []
    for i, spec in enumerate(relevant):
        proc = spec.get("process")
        # Bucket service_start events by rounded time; each bucket should have
        # multiple entities if batched.
        buckets: dict[float, set[str]] = defaultdict(set)
        for e in pwu:
            if e.get("event") == "service_start" and (
                proc is None or e.get("process") == proc
            ):
                buckets[round(e["time"], 3)].add(e["entity_id"])
        multi = sum(1 for b in buckets.values() if len(b) >= 2)
        single = sum(1 for b in buckets.values() if len(b) == 1)
        if multi + single == 0:
            out.append(_info_skip(f"B32_batching[{i}]",
                                  "no matching service_start events."))
            continue
        batch_rate = multi / (multi + single)
        sev: Severity = "PASS" if batch_rate > 0.5 else "WARN"
        out.append(CheckResult(
            sev, f"B32_batching[{i}]",
            f"process='{proc}': {multi} batch buckets vs {single} singletons "
            f"(batch_rate={batch_rate:.1%}).",
            {"batch_buckets": multi, "singletons": single,
             "batch_rate": batch_rate},
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# B33  Handoff gap
# ─────────────────────────────────────────────────────────────────────────────

def verify_handoff_gap(ctx: Phase0Context) -> list[CheckResult]:
    """B33: handoff_pair max_gap_s honored between `from_resource` service_end
    and `to_resource` service_start per entity."""
    specs = ctx.spec.get("coordination_patterns", [])
    relevant = [s for s in specs if s.get("handoff_pair")]
    if not relevant:
        return [_section_empty("B33_handoff_gap",
                               "coordination_patterns with handoff_pair")]

    pwu = post_warmup_events(ctx)
    groups = group_by_entity(pwu)
    out: list[CheckResult] = []
    for i, spec in enumerate(relevant):
        hp = spec["handoff_pair"]
        fr = hp.get("from_resource")
        to = hp.get("to_resource")
        max_gap = float(hp.get("max_gap_s") or float("inf"))
        checked = violations = 0
        for _, evs in groups.items():
            evs_s = sorted(evs, key=lambda x: (x["time"], x.get("seq", 0)))
            for k, e in enumerate(evs_s):
                if e.get("event") == "service_end" and e.get("resource") == fr:
                    # Look for next service_start at to
                    nxt = next((ev for ev in evs_s[k+1:]
                                if ev.get("event") == "service_start"
                                and ev.get("resource") == to), None)
                    if nxt is None:
                        continue
                    checked += 1
                    if nxt["time"] - e["time"] > max_gap + 1e-6:
                        violations += 1
        sev: Severity = "PASS" if violations == 0 else "WARN"
        out.append(CheckResult(
            sev, f"B33_handoff_gap[{i}]",
            f"{fr}→{to}: {violations}/{checked} handoffs exceeded "
            f"max_gap_s={max_gap}.",
            {"violations": violations, "checked": checked},
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# B34  Calendar
# ─────────────────────────────────────────────────────────────────────────────

def verify_calendar(ctx: Phase0Context) -> list[CheckResult]:
    """B34: No activity during declared breaks / holidays / maintenance windows.

    Uses `calendar` fields that were passed through on arrivals, service_rates,
    and periodic_processes entries.
    """
    calendars = []
    for section in ("arrivals", "service_rates", "periodic_processes",
                    "recurring_obligations"):
        for j, entry in enumerate(ctx.spec.get(section, [])):
            if entry.get("calendar"):
                calendars.append((section, j, entry["calendar"], entry))

    if not calendars:
        return [_section_empty("B34_calendar", "entries with calendar")]

    pwu = post_warmup_events(ctx)

    def _norm_windows(windows) -> list[tuple[float, float]]:
        """Accept either a list of [lo,hi] pairs or a single flat [lo,hi]."""
        if not windows:
            return []
        # flat [lo, hi] (two numbers) → wrap into one pair
        if (len(windows) == 2 and all(isinstance(x, (int, float)) for x in windows)):
            return [(float(windows[0]), float(windows[1]))]
        out_w = []
        for w in windows:
            if isinstance(w, (list, tuple)) and len(w) == 2:
                out_w.append((float(w[0]), float(w[1])))
        return out_w

    def _in_hour_window(t: float, windows: list) -> bool:
        h = (t % 86_400.0) / 3_600.0
        return any(lo <= h <= hi for lo, hi in _norm_windows(windows))

    out: list[CheckResult] = []
    for k, (section, j, cal, entry) in enumerate(calendars):
        breaks = cal.get("breaks") or []
        maintenance = cal.get("maintenance") or []
        weekdays_only = bool(cal.get("weekdays_only"))
        res = entry.get("resource")

        # Events potentially in the forbidden windows
        candidates = [e for e in pwu
                      if e.get("event") in ("system_arrival", "service_start")
                      and (res is None or e.get("resource") == res)]

        violations = 0
        if breaks + maintenance:
            windows = breaks + maintenance
            violations += sum(1 for e in candidates if _in_hour_window(e["time"], windows))

        # weekdays_only: day-of-week derivation requires simulation start weekday
        # (unknown here) — emit INFO advisory.
        if weekdays_only:
            out.append(CheckResult(
                "INFO", f"B34_calendar[{section}][{j}]_weekdays_only",
                "weekdays_only declared — requires simulation start weekday to verify."))

        if breaks + maintenance:
            sev: Severity = "PASS" if violations == 0 else "FAIL"
            out.append(CheckResult(
                sev, f"B34_calendar[{section}][{j}]",
                f"resource='{res}': {violations} events during declared "
                f"breaks/maintenance.",
                {"violations": violations,
                 "candidates": len(candidates)},
            ))
    return out or [_section_empty("B34_calendar", "actionable calendar windows")]


# ─────────────────────────────────────────────────────────────────────────────
# B35  Loss conditions
# ─────────────────────────────────────────────────────────────────────────────

def verify_loss_conditions(ctx: Phase0Context) -> list[CheckResult]:
    """B35: losses of a given type must occur only when their declared
    condition_expr / threshold_seconds is satisfied (best-effort on
    threshold_seconds)."""
    specs = ctx.spec.get("losses", [])
    relevant = [s for s in specs if s.get("threshold_seconds")
                or s.get("condition_expr")]
    if not relevant:
        return [_section_empty("B35_loss_conditions",
                               "losses with condition_expr/threshold_seconds")]

    pwu = post_warmup_events(ctx)
    groups = group_by_entity(pwu)

    out: list[CheckResult] = []
    for i, spec in enumerate(relevant):
        lt = spec.get("loss_type")
        threshold = spec.get("threshold_seconds")
        losses = [e for e in pwu if e.get("event") == "loss"
                  and e.get("loss_type") == lt]
        if not losses:
            out.append(CheckResult(
                "INFO", f"B35_loss_conditions[{i}]",
                f"loss_type='{lt}' absent; skipped."))
            continue

        if threshold is None:
            out.append(CheckResult(
                "INFO", f"B35_loss_conditions[{i}]",
                f"loss_type='{lt}' has condition_expr='{spec.get('condition_expr')}' "
                "but no threshold_seconds — deferred to manual review."))
            continue

        bad = 0
        for le in losses:
            evs = groups.get(le["entity_id"], [])
            arr = next((e for e in evs if e.get("event") == "system_arrival"), None)
            if arr is None:
                continue
            wait = le["time"] - arr["time"]
            if wait < threshold - 1e-6:
                bad += 1
        sev: Severity = "PASS" if bad == 0 else "FAIL"
        out.append(CheckResult(
            sev, f"B35_loss_conditions[{i}]",
            f"loss_type='{lt}': {bad}/{len(losses)} losses emitted before "
            f"threshold_seconds={threshold}.",
            {"violations": bad, "total_losses": len(losses)},
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# B36  State dwell
# ─────────────────────────────────────────────────────────────────────────────

def verify_state_dwell(ctx: Phase0Context) -> list[CheckResult]:
    """B36: Mean dwell time in each declared state matches dwell_distribution.mean_seconds."""
    specs = ctx.spec.get("state_transition_rules", [])
    relevant = [s for s in specs if s.get("dwell_distribution")]
    if not relevant:
        return [_section_empty("B36_state_dwell",
                               "state_transition_rules with dwell_distribution")]

    pwu = post_warmup_events(ctx)
    if not _field_present(pwu, "state"):
        return [_info_skip("B36_state_dwell",
                           "Trace lacks 'state' field; cannot verify dwell.")]

    # Per entity, build a state-timeline
    groups = group_by_entity(pwu)
    out: list[CheckResult] = []
    for i, spec in enumerate(relevant):
        target = spec.get("state")
        dd = spec.get("dwell_distribution") or {}
        mean_expected = dd.get("mean_seconds")
        if mean_expected is None:
            out.append(_info_skip(f"B36_state_dwell[{i}]",
                                  "dwell_distribution missing mean_seconds."))
            continue

        dwells: list[float] = []
        for _, evs in groups.items():
            evs_s = sorted(evs, key=lambda x: (x["time"], x.get("seq", 0)))
            entered: float | None = None
            for e in evs_s:
                ev = e.get("event")
                st = e.get("state")
                if ev == "state_change":
                    if st == target:
                        entered = e["time"]
                    elif entered is not None:
                        dwells.append(e["time"] - entered)
                        entered = None
                elif ev in ("system_departure", "loss") and entered is not None:
                    dwells.append(e["time"] - entered)
                    entered = None

        if len(dwells) < 5:
            out.append(_info_skip(f"B36_state_dwell[{i}]",
                                  f"state='{target}': only {len(dwells)} dwells observed."))
            continue
        mean_obs = statistics.mean(dwells)
        tol = spec.get("tolerance", 0.25)
        err = abs(mean_obs - mean_expected) / mean_expected if mean_expected else float("inf")
        sev: Severity = "PASS" if err <= tol else "FAIL"
        out.append(CheckResult(
            sev, f"B36_state_dwell[{i}]",
            f"state='{target}': observed mean={mean_obs:.1f}s, "
            f"expected={mean_expected:.1f}s (err={err:.1%}, tol={tol:.1%}).",
            {"mean_observed": mean_obs, "mean_expected": mean_expected,
             "n": len(dwells)},
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# B37  Composition bands
# ─────────────────────────────────────────────────────────────────────────────

def verify_composition_bands(ctx: Phase0Context) -> list[CheckResult]:
    """B37: Workload split (per process or resource) falls within declared bands."""
    specs = ctx.spec.get("aggregate_targets", [])
    relevant = [s for s in specs if s.get("composition_band")]
    if not relevant:
        return [_section_empty("B37_composition_bands",
                               "aggregate_targets with composition_band")]

    pwu = post_warmup_events(ctx)
    service_starts = [e for e in pwu if e.get("event") == "service_start"]
    by_process: dict[str, int] = defaultdict(int)
    by_resource: dict[str, int] = defaultdict(int)
    for e in service_starts:
        if e.get("process"):
            by_process[e["process"]] += 1
        if e.get("resource"):
            by_resource[e["resource"]] += 1
    total = len(service_starts) or 1

    out: list[CheckResult] = []
    for i, spec in enumerate(relevant):
        bands = spec.get("composition_band") or {}
        violations = []
        for key, rng in bands.items():
            if isinstance(rng, (list, tuple)) and len(rng) == 2:
                lo, hi = rng
            else:
                continue
            frac = (by_process.get(key, by_resource.get(key, 0))) / total
            if not (lo - 1e-9 <= frac <= hi + 1e-9):
                violations.append(f"{key}: observed={frac:.2%}, "
                                  f"band=[{lo:.2%}, {hi:.2%}]")
        sev: Severity = "PASS" if not violations else "WARN"
        out.append(CheckResult(
            sev, f"B37_composition_bands[{i}]",
            ("within bands." if not violations else "; ".join(violations)),
            {"violations": violations},
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# B38  Warmup handling
# ─────────────────────────────────────────────────────────────────────────────

def verify_warmup_handling(ctx: Phase0Context) -> list[CheckResult]:
    """B38: aggregate_targets declaring warmup_handling='exclude_warmup' must
    have been computed on the post-warmup sample. This is a metadata check;
    we rely on ctx.metrics carrying an explicit 'warmup_excluded' marker."""
    specs = [s for s in ctx.spec.get("aggregate_targets", [])
             if s.get("warmup_handling")]
    if not specs:
        return [_section_empty("B38_warmup_handling",
                               "aggregate_targets with warmup_handling")]

    flag = ctx.metrics.get("warmup_excluded")
    out: list[CheckResult] = []
    for i, spec in enumerate(specs):
        wh = spec.get("warmup_handling")
        if wh == "exclude_warmup" and flag is False:
            out.append(CheckResult(
                "FAIL", f"B38_warmup_handling[{i}]",
                f"metric '{spec.get('metric')}' requires exclude_warmup but "
                "ctx.metrics['warmup_excluded'] is False."))
        elif wh == "exclude_warmup" and flag is None:
            out.append(CheckResult(
                "WARN", f"B38_warmup_handling[{i}]",
                f"metric '{spec.get('metric')}' requires exclude_warmup but "
                "ctx.metrics lacks a 'warmup_excluded' marker — declare it."))
        else:
            out.append(CheckResult(
                "PASS", f"B38_warmup_handling[{i}]",
                f"metric '{spec.get('metric')}' warmup_handling='{wh}' acknowledged."))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# B39  KPI decomposition (numerator / denominator / basis)
# ─────────────────────────────────────────────────────────────────────────────

def verify_kpi_decomposition(ctx: Phase0Context) -> list[CheckResult]:
    """B39: aggregate_targets with numerator/denominator/basis declared must
    have that decomposition available in ctx.metrics under the same names,
    OR we only emit INFO advising the downstream consumer to verify."""
    specs = [s for s in ctx.spec.get("aggregate_targets", [])
             if s.get("numerator") or s.get("denominator") or s.get("basis")]
    if not specs:
        return [_section_empty("B39_kpi_decomposition",
                               "aggregate_targets with numerator/denominator/basis")]

    out: list[CheckResult] = []
    for i, spec in enumerate(specs):
        metric = spec.get("metric")
        num = spec.get("numerator")
        den = spec.get("denominator")
        basis = spec.get("basis")
        missing: list[str] = []
        if num and num not in ctx.metrics:
            missing.append(f"numerator='{num}'")
        if den and den not in ctx.metrics:
            missing.append(f"denominator='{den}'")
        if missing:
            out.append(CheckResult(
                "WARN", f"B39_kpi_decomposition[{i}]",
                f"metric '{metric}' declares {missing} but they are not in "
                "ctx.metrics — cannot reconstruct decomposition.",
                {"missing": missing}))
        else:
            out.append(CheckResult(
                "INFO", f"B39_kpi_decomposition[{i}]",
                f"metric '{metric}' decomposition declared "
                f"(num='{num}', den='{den}', basis='{basis}')."))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# B40  Granularity consistency
# ─────────────────────────────────────────────────────────────────────────────

def verify_granularity_consistency(ctx: Phase0Context) -> list[CheckResult]:
    """B40: Entity identity unit is consistent across sections that declare it.

    If two arrivals / service_rates / aggregate_targets declare different
    `granularity` values (e.g. one says 'visit' and another says 'episode'),
    warn: the framework treats one entity_id = one unit, so conflicting
    granularity declarations likely mean the counts can't all be compared.
    """
    grans: list[tuple[str, str]] = []
    for section in ("arrivals", "service_rates", "aggregate_targets",
                    "coverage_rules", "policy_thresholds"):
        for entry in ctx.spec.get(section, []):
            g = entry.get("granularity")
            if g:
                grans.append((section, g))
    if not grans:
        return [_section_empty("B40_granularity_consistency",
                               "entries with granularity")]

    unique = {g for _, g in grans}
    if len(unique) == 1:
        return [CheckResult("PASS", "B40_granularity_consistency",
                            f"all sections agree on granularity='{next(iter(unique))}'.")]
    return [CheckResult(
        "WARN", "B40_granularity_consistency",
        f"Inconsistent granularity declarations: {sorted(unique)}. "
        f"Sections: {grans}. Entity identity unit may be ambiguous.",
        {"unique": sorted(unique), "declarations": grans}
    )]


# ─────────────────────────────────────────────────────────────────────────────
# B41  Pool composition
# ─────────────────────────────────────────────────────────────────────────────

def _hour_of_day_seconds(t: float, day_length_s: float = 86400.0) -> float:
    """Sim time -> hour of day in [0, 24). Assumes sim-time origin at midnight."""
    return ((t % day_length_s) / 3600.0)


def _in_window(t: float, start_hour: float, end_hour: float,
               day_length_s: float = 86400.0) -> bool:
    h = _hour_of_day_seconds(t, day_length_s)
    if start_hour <= end_hour:
        return start_hour <= h < end_hour
    return h >= start_hour or h < end_hour  # wrap past midnight


def _window_onsets(trace: list[dict], start_hour: float,
                   day_length_s: float = 86400.0,
                   warmup: float = 0.0) -> list[float]:
    """Return sim times at which the daily window opens, within the trace."""
    if not trace:
        return []
    t_min = max(trace[0].get("time", 0.0), warmup)
    t_max = trace[-1].get("time", 0.0)
    onsets: list[float] = []
    # First onset at or after t_min
    day0 = int(t_min // day_length_s)
    for d in range(day0, day0 + int((t_max - t_min) // day_length_s) + 2):
        onset = d * day_length_s + start_hour * 3600.0
        if t_min <= onset <= t_max:
            onsets.append(onset)
    return onsets


def _busy_servers_at(trace: list[dict], role: str, t: float,
                     resource_to_role: dict[str, str]) -> set[str]:
    """Distinct server_ids of the given role that are mid-service at time t.

    A server is busy at t if a service_start with server_id s has occurred at
    or before t without a matching service_end for the same server_id+entity.
    """
    busy: set[str] = set()
    open_by_server: dict[str, int] = defaultdict(int)
    for ev in trace:
        if ev.get("time", 0.0) > t:
            break
        sid = ev.get("server_id")
        res = ev.get("resource")
        if sid is None or resource_to_role.get(res) != role:
            continue
        if ev.get("event") == "service_start":
            open_by_server[sid] += 1
        elif ev.get("event") == "service_end":
            open_by_server[sid] = max(0, open_by_server[sid] - 1)
    for sid, n in open_by_server.items():
        if n > 0:
            busy.add(sid)
    return busy


def verify_pool_composition(ctx: Phase0Context) -> list[CheckResult]:
    """B41: Soft-pool composition matches declared pool_policy.

    pool_policy = "fixed_team":
        At window onset, exactly pool_size staff of each role (or the per-role
        override) begin the declared process. Over/under by ≥ 1 is a WARN;
        by ≥ 2 is a FAIL.

    pool_policy = "all_idle_at_start":
        Every *idle* staff of each declared role at window onset must fire a
        service_start for the declared process within a small tolerance (60 s).
        Observing strictly fewer joiners than the idle-set is a FAIL — this is
        the canonical "one-of-each instead of all-available" failure mode.

    pool_policy = "all_available_through_window":
        all_idle_at_start PLUS any staff that becomes idle during the window
        must also fire a service_start for the process before the window closes.
        A WARN is emitted per missed late joiner.

    Window-close sub-check (all policies):
        Any service_start for the declared process whose time is at or after
        window_end is a FAIL — the request pattern "submit one non-preempting
        request per capacity unit at window start; cancel residuals at close"
        forbids grants past the window boundary.

    server_id_required sub-check:
        When rule["server_id_required"] is True and the trace lacks server_id
        on service_start/service_end events for this rule's resources, B41
        promotes the otherwise-INFO_SKIP to a FAIL. This lets the DSL author
        declare that composition MUST be mechanically verifiable.
    """
    spec = ctx.spec
    rules = spec.get("pool_composition_rules", [])
    if not rules:
        return [_section_empty("B41_pool_composition", "pool_composition_rules")]

    trace = ctx.trace or []
    trace_has_server_id = _field_present(trace, "server_id")

    warmup = float(getattr(ctx, "warmup", 0.0) or 0.0)
    out: list[CheckResult] = []

    for i, rule in enumerate(rules):
        src = rule.get("source_id", f"rule_{i}")
        process = rule.get("process")
        roles = rule.get("roles") or []
        resources_by_role = rule.get("resources_by_role") or {}
        policy = (rule.get("pool_policy") or "fixed_team").lower()
        size_spec = rule.get("pool_size")
        admits_late = bool(rule.get("admits_late_joiners", policy == "all_available_through_window"))
        windows = rule.get("windows") or []
        onset_tol_s = float(rule.get("onset_tolerance_seconds", 60.0))
        server_id_required = bool(rule.get("server_id_required", False))
        pool_granularity = rule.get("pool_granularity")  # "window" | "per_patient" | None

        if not (process and roles and windows):
            out.append(_info_skip(
                f"B41_pool_composition[{i}]",
                f"Rule {src}: needs process, roles, windows — skipped."))
            continue

        # server_id gate — per-rule so server_id_required can promote to FAIL.
        if not trace_has_server_id:
            if server_id_required:
                out.append(CheckResult(
                    "FAIL",
                    f"B41_pool_composition[{i}]",
                    f"B41[{src}] process={process}: server_id_required=True but "
                    f"trace omits server_id on service_start/service_end. "
                    f"Composition cannot be mechanically verified."))
            else:
                out.append(_info_skip(
                    f"B41_pool_composition[{i}]",
                    f"B41[{src}] process={process}: trace lacks server_id; "
                    f"B41 cannot distinguish co-active staff. Set "
                    f"server_id_required=True to promote this to FAIL, or "
                    f"emit server_id on every service_start/service_end."))
            continue

        # Build resource->role map (for identifying which role a server_id belongs to)
        res_to_role: dict[str, str] = {}
        for role, res_list in resources_by_role.items():
            for r in res_list:
                res_to_role[r] = role

        violations = 0
        fails = 0
        warns = 0
        notes: list[str] = []

        late_grant_tol_s = 60.0  # grace for granting exactly at window_end

        for w_i, window in enumerate(windows):
            sh = float(window.get("start_hour"))
            eh = float(window.get("end_hour"))
            onsets = _window_onsets(trace, sh, warmup=warmup)
            window_len_s = ((eh - sh) % 24) * 3600.0

            # Window-close sub-check: any service_start for `process` whose time
            # falls strictly after (onset + window_len + late_grant_tol) is a
            # FAIL. These grants are post-close arrivals — the "cancel residual
            # requests at window end" invariant is broken.
            for onset in onsets:
                window_end = onset + window_len_s
                for ev in trace:
                    t = ev.get("time", 0.0)
                    if t <= window_end + late_grant_tol_s:
                        continue
                    # Only consider events close enough to implicate this window
                    if t > window_end + window_len_s:
                        break
                    if ev.get("event") != "service_start":
                        continue
                    if ev.get("process") != process:
                        continue
                    role = res_to_role.get(ev.get("resource"))
                    if role is None or role not in roles:
                        continue
                    fails += 1
                    notes.append(
                        f"w{w_i}@{onset:.0f}s post-close grant: service_start "
                        f"for {process} at t={t:.0f}s (window_end={window_end:.0f}s) "
                        f"role={role}")

            for onset in onsets:
                # Joiners at window onset: service_starts for this process
                # within [onset, onset + onset_tol_s]
                joiners_by_role: dict[str, set[str]] = defaultdict(set)
                for ev in trace:
                    t = ev.get("time", 0.0)
                    if t < onset:
                        continue
                    if t > onset + onset_tol_s:
                        break
                    if ev.get("event") != "service_start":
                        continue
                    if ev.get("process") != process:
                        continue
                    sid = ev.get("server_id")
                    role = res_to_role.get(ev.get("resource"))
                    if sid is None or role is None or role not in roles:
                        continue
                    joiners_by_role[role].add(sid)

                for role in roles:
                    role_resources = resources_by_role.get(role, [])
                    role_capacity = len(role_resources)
                    busy = _busy_servers_at(trace, role, onset, res_to_role)
                    idle = set(role_resources) - busy
                    n_joined = len(joiners_by_role.get(role, set()))
                    n_idle = len(idle)

                    if policy == "fixed_team":
                        # Determine declared size for this role
                        decl = size_spec.get(role) if isinstance(size_spec, dict) else size_spec
                        try:
                            decl_n = int(decl)
                        except Exception:
                            decl_n = 1
                        gap = abs(n_joined - decl_n)
                        if gap >= 2:
                            fails += 1
                            notes.append(
                                f"w{w_i}@{onset:.0f}s role={role}: observed "
                                f"{n_joined}, declared fixed_team size {decl_n}")
                        elif gap == 1:
                            warns += 1

                    elif policy in ("all_idle_at_start",
                                    "all_available_through_window"):
                        if n_joined < n_idle:
                            fails += 1
                            notes.append(
                                f"w{w_i}@{onset:.0f}s role={role}: observed "
                                f"{n_joined} joiner(s); idle-set had {n_idle} "
                                f"(capacity {role_capacity}). Likely "
                                f"one-of-each pattern instead of pool_policy="
                                f"{policy}.")
                        # Late-joiner check for all_available_through_window
                        if (policy == "all_available_through_window"
                                and admits_late
                                and window_len_s > onset_tol_s):
                            late_tol = 120.0
                            window_end = onset + window_len_s
                            late_joined = set(joiners_by_role.get(role, set()))
                            # Any role member that became idle during the window
                            # should appear as a later service_start in process.
                            for ev in trace:
                                t = ev.get("time", 0.0)
                                if t <= onset or t >= window_end:
                                    continue
                                if ev.get("event") != "service_end":
                                    continue
                                sid = ev.get("server_id")
                                if (res_to_role.get(ev.get("resource")) != role
                                        or sid is None
                                        or sid in late_joined):
                                    continue
                                # Look forward: did this server later start the process?
                                joined_later = False
                                for ev2 in trace:
                                    t2 = ev2.get("time", 0.0)
                                    if t2 <= t or t2 > t + late_tol:
                                        if t2 > t + late_tol:
                                            break
                                        continue
                                    if (ev2.get("event") == "service_start"
                                            and ev2.get("process") == process
                                            and ev2.get("server_id") == sid):
                                        joined_later = True
                                        late_joined.add(sid)
                                        break
                                if not joined_later and t < window_end - late_tol:
                                    warns += 1
                    else:
                        notes.append(f"Unknown pool_policy={policy!r}")

        total = fails + warns
        if total == 0:
            sev: Severity = "PASS"
            msg = (f"B41[{src}] process={process}: pool composition matches "
                   f"pool_policy={policy}.")
        elif fails > 0:
            sev = "FAIL"
            msg = (f"B41[{src}] process={process}: {fails} composition FAIL(s), "
                   f"{warns} WARN(s). " + " | ".join(notes[:3]))
        else:
            sev = "WARN"
            msg = (f"B41[{src}] process={process}: {warns} composition WARN(s). "
                   + " | ".join(notes[:3]))
        out.append(CheckResult(sev, f"B41_pool_composition[{i}]", msg))

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

_ALL_SEMANTIC_CHECKS = [
    verify_eligibility_rules,            # B24
    verify_coverage_rules,               # B25
    verify_routing_rules,                # B26
    verify_assignment_continuity,        # B27
    verify_policy_thresholds,            # B28
    verify_duration_semantics,           # B29
    verify_capacity_consumption,         # B30
    verify_queue_discipline,             # B31
    verify_batching,                     # B32
    verify_handoff_gap,                  # B33
    verify_calendar,                     # B34
    verify_loss_conditions,              # B35
    verify_state_dwell,                  # B36
    verify_composition_bands,            # B37
    verify_warmup_handling,              # B38
    verify_kpi_decomposition,            # B39
    verify_granularity_consistency,      # B40
    verify_pool_composition,             # B41
]


def validate_semantic_contracts(ctx: Phase0Context) -> list[CheckResult]:
    """Run all B24–B41 semantic checks and return their CheckResults.

    Each check self-skips with INFO if the required trace fields or
    spec sections are absent. This keeps the semantic layer opt-in: a
    DSL that declares no semantic rules produces a no-op (INFO only).
    """
    results: list[CheckResult] = []
    for check in _ALL_SEMANTIC_CHECKS:
        try:
            results += check(ctx)
        except Exception as exc:  # pragma: no cover
            results.append(CheckResult(
                "BLOCK", check.__name__,
                f"Semantic check raised {type(exc).__name__}: {exc}"))
    return results
