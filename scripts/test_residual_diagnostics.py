"""test_residual_diagnostics.py — Synthetic tests reproducing the barrier
case study's four-round iteration arc.

We construct three violation patterns, one per "round" of the empirical case
study, and verify the residual fingerprinting + architectural-implication
tagging correctly classifies each:

  Round 1 — Naive aggregate misread.  51 violations distributed across the
            served-time range (no pre-wait yet, raw PreemptiveResource).
            Expected verdict: PARAMETRIC_TUNING_PROMISING (dispersed
            residual; not yet hitting the structural ceiling).

  Round 3 — Pre-wait + cluster pre-wait + retry buffers.  26 residual
            violations, all same-tick races (served_so_far near zero).
            Expected verdict: MECHANISM_INTERCEPTION_REQUIRED with
            STRUCTURAL CEILING signal — the diagnostic that would have
            short-circuited rounds 2 and 3 of the case study.

  Round 4 — Custom _BarrierResource pattern.  Zero residual violations.
            Expected: no clusters, no implication tag.

We additionally test a MIXED case (some same-tick + some dispersed) to
verify the MIXED_STRUCTURAL_AND_PARAMETRIC pathway, and confirm the
diagnostic_extension text is suitable for embedding in a CheckRecord.
"""

from __future__ import annotations

import os
import sys

# Make this script runnable from anywhere in the repository.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from residual_diagnostics import (
    ViolationRecord,
    architectural_implication_for_barrier,
    classify_residual_barrier,
    diagnostic_extension,
    render_clusters,
    render_implication,
)


BARRIER = 0.5


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic violation generators — each reproduces a round of the case study
# ─────────────────────────────────────────────────────────────────────────────


def round1_violations() -> list[ViolationRecord]:
    """Round 1: naive misread, 51 violations dispersed across served-time.

    The agent had not yet introduced a pre-wait. Preempts fire across the
    full range of served-time; there is no structural ceiling because the
    mechanism has room for parametric improvement (the same-tick races
    haven't dominated yet).
    """
    # 51 violations distributed across served fractions [0.0, BARRIER)
    out: list[ViolationRecord] = []
    n = 51
    for i in range(n):
        # Spread served_so_far evenly through [0.01, 0.49]
        ratio = 0.01 + (BARRIER - 0.02) * (i / (n - 1))
        served = ratio  # total_served = 1.0
        out.append(
            ViolationRecord(
                entity_id=f"pt_{i:03d}",
                segment_id=f"seg_{i:03d}",
                time_of_violation=float(i),
                served_so_far=served,
                total_served=1.0,
                ratio=ratio,
                segment_start_time=float(i) - served,
            )
        )
    return out


def round3_violations() -> list[ViolationRecord]:
    """Round 3: pre-wait + cluster pre-wait + retry. Residual: 26
    violations, all same-tick races (served_so_far ≈ 0).

    The pre-wait pattern has reduced violations from 51 to 26, but every
    remaining violation is a same-tick race between holder registration
    and the library's synchronous preempt step. This is the structural
    ceiling the case study took rounds 2 and 3 to discover.
    """
    out: list[ViolationRecord] = []
    n = 26
    for i in range(n):
        # served_so_far in [0, 1e-9] — same-tick by construction
        served = 1e-10 * i  # all below _SAME_TICK_EPS (1e-6)
        out.append(
            ViolationRecord(
                entity_id=f"pt_{i:03d}",
                segment_id=f"seg_{i:03d}",
                time_of_violation=float(i),
                served_so_far=served,
                total_served=1.0,
                ratio=served / 1.0,
                segment_start_time=float(i) - served,
            )
        )
    return out


def round4_violations() -> list[ViolationRecord]:
    """Round 4: custom _BarrierResource — zero residual."""
    return []


def mixed_violations() -> list[ViolationRecord]:
    """Hybrid case: 20 same-tick violations + 10 dispersed violations.

    Verifies the MIXED_STRUCTURAL_AND_PARAMETRIC pathway: a structural
    cluster is present but does not dominate the residual.
    """
    out: list[ViolationRecord] = []
    # 20 same-tick (structural cluster, but below dominance threshold of 75%)
    for i in range(20):
        served = 1e-10
        out.append(
            ViolationRecord(
                entity_id=f"st_{i:03d}",
                segment_id=f"st_seg_{i:03d}",
                time_of_violation=float(i),
                served_so_far=served,
                total_served=1.0,
                ratio=served,
            )
        )
    # 10 dispersed (parametric tail)
    for i in range(10):
        ratio = 0.1 + 0.04 * i  # spread across [0.1, 0.46]
        out.append(
            ViolationRecord(
                entity_id=f"sp_{i:03d}",
                segment_id=f"sp_seg_{i:03d}",
                time_of_violation=float(100 + i),
                served_so_far=ratio,
                total_served=1.0,
                ratio=ratio,
            )
        )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_round1_dispersed_residual() -> None:
    """Round 1: dispersed residual → PARAMETRIC_TUNING_PROMISING."""
    viols = round1_violations()
    clusters = classify_residual_barrier(viols, BARRIER)
    tag = architectural_implication_for_barrier(clusters)

    # AUDIT FIX (M5): clusters are structural by FINGERPRINT; a small
    # near-tick tail may legitimately carry structural=True. What matters
    # for the dispersed case is that the TOTAL structural share stays
    # negligible (≤ 25%) so the implication routes to PARAMETRIC.
    structural_share = sum(c.share for c in clusters if c.structural)
    assert structural_share <= 0.25, (
        f"Dispersed residual's structural share should be negligible, got "
        f"{structural_share:.0%}: {[(c.fingerprint, c.share) for c in clusters]}"
    )
    assert tag.tag == "PARAMETRIC_TUNING_PROMISING", (
        f"Expected PARAMETRIC_TUNING_PROMISING, got {tag.tag}"
    )
    print(f"  [PASS] Round 1 (dispersed): tag={tag.tag}")


def test_round3_structural_ceiling() -> None:
    """Round 3: all same-tick → MECHANISM_INTERCEPTION_REQUIRED + STRUCTURAL CEILING."""
    viols = round3_violations()
    clusters = classify_residual_barrier(viols, BARRIER)
    tag = architectural_implication_for_barrier(clusters)

    assert len(clusters) == 1, (
        f"Expected single same-tick cluster, got {len(clusters)}: "
        f"{[c.fingerprint for c in clusters]}"
    )
    assert clusters[0].fingerprint == "same_tick+served_zero", (
        f"Wrong fingerprint: {clusters[0].fingerprint}"
    )
    assert clusters[0].structural, (
        "Round 3 cluster should be flagged structural (100% same-tick)"
    )
    assert clusters[0].share == 1.0
    assert tag.tag == "MECHANISM_INTERCEPTION_REQUIRED", (
        f"Expected MECHANISM_INTERCEPTION_REQUIRED, got {tag.tag}"
    )
    assert "STRUCTURAL CEILING REACHED" in tag.iteration_signal, (
        f"Expected STRUCTURAL CEILING signal, got: {tag.iteration_signal!r}"
    )
    # The incompatible patterns should explicitly name PreemptiveResource
    inc = " ".join(tag.incompatible_patterns).lower()
    assert "preemptiveresource" in inc, (
        "Incompatible-patterns list must explicitly name PreemptiveResource "
        "so the agent receives concrete guidance"
    )
    # The compatible patterns should name custom resource + interrupt()
    cmp_ = " ".join(tag.compatible_patterns).lower()
    assert "interrupt" in cmp_, (
        "Compatible-patterns list must name interrupt()-driven eviction"
    )
    print(f"  [PASS] Round 3 (structural ceiling): tag={tag.tag}")
    print(f"         iteration_signal: {tag.iteration_signal}")


def test_round4_no_violations() -> None:
    """Round 4: zero violations → no implication tag (UNCATEGORIZED)."""
    viols = round4_violations()
    clusters = classify_residual_barrier(viols, BARRIER)
    tag = architectural_implication_for_barrier(clusters)
    assert clusters == [], f"Expected empty clusters, got: {clusters}"
    assert tag.tag == "UNCATEGORIZED"
    print(f"  [PASS] Round 4 (no violations): tag={tag.tag}")


def test_mixed_residual() -> None:
    """Mixed: 20 same-tick + 10 dispersed → MIXED_STRUCTURAL_AND_PARAMETRIC.

    AUDIT FIX (M5): a cluster is structural by FINGERPRINT, independent of
    share; share only routes the implication tag. A 67%-structural residual
    now correctly routes to MIXED — previously it read as 'no structural
    cluster' and produced PARAMETRIC_TUNING_PROMISING, the opposite of the
    correct guidance for a majority-same-tick residual.
    """
    viols = mixed_violations()
    clusters = classify_residual_barrier(viols, BARRIER)
    tag = architectural_implication_for_barrier(clusters)

    same_tick = [c for c in clusters if c.fingerprint == "same_tick+served_zero"]
    assert same_tick, "Expected a same-tick cluster"
    assert same_tick[0].structural, (
        f"Same-tick cluster must be flagged structural by fingerprint "
        f"(share-independent), got structural={same_tick[0].structural}"
    )
    assert tag.tag == "MIXED_STRUCTURAL_AND_PARAMETRIC", (
        f"67% structural share must route to MIXED (not parametric); "
        f"got {tag.tag}"
    )
    assert "do NOT read a declining count" in tag.iteration_signal
    print(f"  [PASS] Mixed (67/33): tag={tag.tag}")


def test_mixed_residual_high_share() -> None:
    """High structural share but with a parametric tail.

    Tests that when a structural cluster crosses the dominance threshold
    AND a parametric cluster is also present, we still emit
    MECHANISM_INTERCEPTION_REQUIRED (the dominant-cluster pathway), not
    MIXED. The MIXED pathway is reserved for cases where multiple clusters
    are structural without one dominating.
    """
    out: list[ViolationRecord] = []
    # 80 same-tick (structural, dominant at 80%)
    for i in range(80):
        served = 1e-10
        out.append(
            ViolationRecord(
                entity_id=f"st_{i:03d}",
                segment_id=f"st_seg_{i:03d}",
                time_of_violation=float(i),
                served_so_far=served,
                total_served=1.0,
                ratio=served,
            )
        )
    # 20 dispersed
    for i in range(20):
        ratio = 0.1 + 0.018 * i
        out.append(
            ViolationRecord(
                entity_id=f"sp_{i:03d}",
                segment_id=f"sp_seg_{i:03d}",
                time_of_violation=float(100 + i),
                served_so_far=ratio,
                total_served=1.0,
                ratio=ratio,
            )
        )
    clusters = classify_residual_barrier(out, BARRIER)
    tag = architectural_implication_for_barrier(clusters)
    assert tag.tag == "MECHANISM_INTERCEPTION_REQUIRED", (
        f"At 80% same-tick share the dominant cluster is structural; "
        f"expected MECHANISM_INTERCEPTION_REQUIRED, got {tag.tag}"
    )
    print(f"  [PASS] High-share mixed (80/20): tag={tag.tag}")


def test_diagnostic_extension_renders() -> None:
    """The diagnostic_extension text should be non-empty for FAIL cases
    and contain the structural-vs-parametric verdict in a way that an
    agent reading the CheckRecord.diagnostic would see it."""
    viols = round3_violations()
    clusters = classify_residual_barrier(viols, BARRIER)
    tag = architectural_implication_for_barrier(clusters)
    ext = diagnostic_extension(clusters, tag)
    assert "STRUCTURAL" in ext, (
        "diagnostic_extension must surface the STRUCTURAL marker"
    )
    assert "MECHANISM_INTERCEPTION_REQUIRED" in ext
    assert "PreemptiveResource" in ext or "preemptiveresource" in ext.lower()
    assert "Incompatible patterns" in ext
    assert "Compatible patterns" in ext
    print(f"  [PASS] diagnostic_extension contains agent-facing structural verdict")


def test_serialization_round_trips() -> None:
    """clusters_to_dict and implication_to_dict must produce JSON-friendly
    output so the residual analysis can be embedded in CheckRecord.evidence."""
    import json
    from residual_diagnostics import (
        clusters_to_dict,
        implication_to_dict,
    )

    viols = round3_violations()
    clusters = classify_residual_barrier(viols, BARRIER)
    tag = architectural_implication_for_barrier(clusters)

    c_dict = clusters_to_dict(clusters)
    t_dict = implication_to_dict(tag)
    # Round-trip through JSON to confirm no non-serializable fields.
    json.dumps({"residual_clusters": c_dict, "architectural_implication": t_dict})
    print(f"  [PASS] Residual evidence serializes to JSON cleanly")


# ─────────────────────────────────────────────────────────────────────────────
# Pretty-print demo
# ─────────────────────────────────────────────────────────────────────────────


def demo_round3_output() -> None:
    """Show what the agent would see at the end of Round 3 with these
    diagnostics wired in. This is the 'would have short-circuited rounds
    2 and 3' demonstration from the case study."""
    print()
    print("─" * 70)
    print("DEMO — what the iterating agent sees at end of Round 3 with")
    print("residual diagnostics enabled:")
    print("─" * 70)
    viols = round3_violations()
    clusters = classify_residual_barrier(viols, BARRIER)
    tag = architectural_implication_for_barrier(clusters)
    print(render_clusters(clusters))
    print()
    print(render_implication(tag))
    print("─" * 70)


# ─────────────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────────────


def main() -> int:
    print("Testing residual_diagnostics against synthetic barrier-case patterns")
    print("=" * 70)
    tests = [
        test_round1_dispersed_residual,
        test_round3_structural_ceiling,
        test_round4_no_violations,
        test_mixed_residual,
        test_mixed_residual_high_share,
        test_diagnostic_extension_renders,
        test_serialization_round_trips,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failures += 1
            print(f"  [FAIL] {t.__name__}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"  [ERROR] {t.__name__}: {type(exc).__name__}: {exc}")
    print("=" * 70)
    if failures == 0:
        print(f"All {len(tests)} tests passed.")
        demo_round3_output()
        return 0
    print(f"{failures} of {len(tests)} tests failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
