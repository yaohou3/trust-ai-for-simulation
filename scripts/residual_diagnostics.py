"""residual_diagnostics.py — Residual-violation categorization and architectural-implication tagging.

WHY THIS EXISTS
---------------
The validators in validation.py report violation COUNTS but not violation
STRUCTURE. When an iterative repair loop reduces a violation count from 51
to 36 to 26 across rounds, the count trend looks like progress — but if the
residual violations all share the same trace-level fingerprint, no amount
of parametric tuning will close the remaining gap. The agent reading
"26 violations, min ratio 0.03" interprets it as borderline error; the
structural truth is that 26 violations of the SAME fingerprint indicate a
ceiling that requires a categorically different mechanism, not tighter
parameters on the existing one.

This module is the deterministic counterpart to the agent's progress-trajectory
self-evaluation. It generalizes the reviewer-generator collusion defence —
locating trust-bearing diagnoses in non-colludable substrates — to the
single-agent iterative-repair case, where the "reviewer" and "generator"
collapse into one process that exhibits the same Goodhart pattern when
optimizing against monotonically-decreasing surface counts.

WHAT IT PROVIDES
----------------
Two services, both pure functions over violation records:

  1. classify_residual_*() — clusters violation records by trace fingerprint
     and reports the cluster structure. When one cluster dominates the
     residual (share >= dominance_threshold), the cluster is flagged as
     STRUCTURAL, signalling the agent that further parametric tuning will
     not converge.

  2. architectural_implication_*() — maps the dominant cluster to a
     prescriptive ImplicationTag classifying what KIND of fix is required
     (mechanism replacement, policy adjustment, parametric tuning) and what
     KIND is incompatible. Tags are curated per check semantic; the set is
     intended to grow with case-study experience rather than to be
     comprehensive a priori.

CHECK COVERAGE
--------------
  - §4.2.3 preemption_barrier (barrier-aware preemption residual)
  - extensible: add classify_residual_X / architectural_implication_for_X
    pairs as new check semantics gain case-study evidence.

DESIGN NOTES
------------
Each ImplicationTag carries:
  - tag: a small set of named classes (PARAMETRIC_CEILING,
         MECHANISM_INTERCEPTION_REQUIRED, POLICY_GATING_MISCONFIGURED,
         PARAMETRIC_TUNING_PROMISING, MIXED_STRUCTURAL_AND_PARAMETRIC,
         UNCATEGORIZED).
  - description: what the tag means in plain language.
  - compatible_patterns: implementation approaches that can satisfy this
    check semantic given the observed residual.
  - incompatible_patterns: approaches that empirically cannot close the
    gap, so the agent should stop iterating on them.
  - iteration_signal: a single-line message intended for the agent's next
    repair decision, naming the structural-vs-parametric verdict.

The compatible/incompatible lists are deliberately small and named —
they are not a tutorial. Their purpose is to redirect the agent away from
known-failing mechanism choices, not to prescribe an implementation.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Per-violation record (producer fills whichever fields apply; downstream
# categorizers are robust to missing fields)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ViolationRecord:
    """A single violation event suitable for clustering. Producing checks
    populate whichever fields they can measure; the categorizers tolerate
    missing fields and fall back to UNCATEGORIZED when signal is absent.
    """

    entity_id: str = ""
    segment_id: str = ""
    time_of_violation: float = 0.0
    # §4.2.3 barrier check fields
    served_so_far: float = 0.0
    total_served: float = 0.0
    ratio: float = 0.0
    segment_start_time: float | None = None
    # General trace context
    preemptor_priority: int | None = None
    holder_priority: int | None = None
    holder_role: str | None = None
    preemptor_role: str | None = None
    # Open slot for check-specific signals
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClusterReport:
    """One fingerprint group within the residual."""

    fingerprint: str
    description: str
    count: int
    share: float
    examples: list[dict]
    structural: bool


@dataclass
class ImplicationTag:
    """Architectural-implication classification for a residual."""

    tag: str
    description: str
    compatible_patterns: list[str]
    incompatible_patterns: list[str]
    iteration_signal: str


# ─────────────────────────────────────────────────────────────────────────────
# §4.2.3 — barrier-aware preemption residual
# ─────────────────────────────────────────────────────────────────────────────

_SAME_TICK_EPS = 1e-6  # served_so_far below this is "same instant as start"


def _fingerprint_barrier_violation(
    v: ViolationRecord, barrier: float
) -> tuple[str, str]:
    """Compute (fingerprint_key, human_description) for one §4.2.3 violation.

    The fingerprint encodes how early in the segment the preempt fired,
    relative to the declared barrier. Fingerprints near zero indicate a
    same-tick race between the preemptor's arrival and the holder's first
    serving instant — the structural failure mode that pre-wait patterns
    cannot close. Fingerprints near the barrier indicate the preempt fired
    close to but below the declared cutoff — a parametric residual.
    """
    served = v.served_so_far
    if served < _SAME_TICK_EPS:
        return (
            "same_tick+served_zero",
            "preempt fires at the same simulation instant as the segment's start; "
            "holder served effectively zero time",
        )
    if barrier > 0 and served < barrier * 0.10:
        return (
            "near_tick+served_below_10pct_of_barrier",
            "preempt fires very early in the segment, served_so_far < 10% of "
            "declared barrier",
        )
    if barrier > 0 and served < barrier * 0.50:
        return (
            "early_preempt+served_below_50pct_of_barrier",
            "preempt fires in the first half of the barrier window",
        )
    if barrier > 0 and served < barrier:
        return (
            "late_preempt+served_close_to_barrier",
            "preempt fires close to but below the declared barrier",
        )
    return (
        "over_barrier",
        "served_so_far ≥ barrier (should not be a violation — possible numerical "
        "edge case)",
    )


# Fingerprints that, when dominant, indicate a structural ceiling that
# parametric tuning cannot close.
_STRUCTURAL_BARRIER_FINGERPRINTS = frozenset(
    {
        "same_tick+served_zero",
        "near_tick+served_below_10pct_of_barrier",
    }
)


def classify_residual_barrier(
    violations: list[ViolationRecord],
    barrier: float,
    dominance_threshold: float = 0.75,
) -> list[ClusterReport]:
    """Cluster §4.2.3 violation records by fingerprint.

    Returns clusters sorted by count descending.

    AUDIT FIX (M5): a cluster is marked ``structural=True`` by FINGERPRINT
    ALONE — whether the residual pattern is structural-class is a property
    of the failure mechanism, not of how much of the residual it currently
    explains. The previous coupling (structural required share ≥ 0.75)
    made a 70%-same-tick residual read as "no structural cluster", which
    routed the implication to PARAMETRIC_TUNING_PROMISING — the OPPOSITE
    of correct guidance — and left the MIXED branch unreachable dead code.
    Share is now used only downstream, by
    :func:`architectural_implication_for_barrier`, to split dominant-
    structural from mixed from parametric.
    """
    if not violations:
        return []
    groups: dict[str, list[ViolationRecord]] = defaultdict(list)
    descriptions: dict[str, str] = {}
    for v in violations:
        fp_key, desc = _fingerprint_barrier_violation(v, barrier)
        groups[fp_key].append(v)
        descriptions[fp_key] = desc
    total = len(violations)
    clusters: list[ClusterReport] = []
    for fp, vs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        count = len(vs)
        share = count / total
        structural = fp in _STRUCTURAL_BARRIER_FINGERPRINTS
        examples = [
            {
                "entity_id": v.entity_id,
                "segment_id": v.segment_id,
                "time": v.time_of_violation,
                "served_so_far": v.served_so_far,
                "total_served": v.total_served,
                "ratio": v.ratio,
            }
            for v in vs[:5]
        ]
        clusters.append(
            ClusterReport(
                fingerprint=fp,
                description=descriptions[fp],
                count=count,
                share=share,
                examples=examples,
                structural=structural,
            )
        )
    return clusters


def architectural_implication_for_barrier(
    clusters: list[ClusterReport],
) -> ImplicationTag:
    """Map residual cluster structure to an architectural-implication tag for §4.2.3.

    Decision rules (AUDIT FIX M5 — share thresholds live HERE, not in the
    cluster classification, so a sub-dominant structural residual routes to
    MIXED instead of the previously-wrong PARAMETRIC_TUNING_PROMISING):
      - total structural share ≥ 0.75 AND the dominant cluster is
        structural → MECHANISM_INTERCEPTION_REQUIRED (per-fingerprint text)
      - total structural share in (0.25, 0.75) → MIXED_STRUCTURAL_AND_PARAMETRIC
      - total structural share ≤ 0.25 → PARAMETRIC_TUNING_PROMISING
    """
    if not clusters:
        return ImplicationTag(
            tag="UNCATEGORIZED",
            description="No residual violations to classify.",
            compatible_patterns=[],
            incompatible_patterns=[],
            iteration_signal="",
        )
    dominant = clusters[0]
    structural_share = sum(c.share for c in clusters if c.structural)
    dominant_structural = dominant.structural and structural_share >= 0.75

    if dominant_structural and dominant.fingerprint == "same_tick+served_zero":
        return ImplicationTag(
            tag="MECHANISM_INTERCEPTION_REQUIRED",
            description=(
                "Residual violations are dominated by same-tick preempts where the "
                "holder served effectively zero time. This indicates the "
                "preempt-decision step runs synchronously inside the resource "
                "library (e.g. SimPy's PreemptiveResource._do_put) between the "
                "preemptor's arrival and the holder's interrupt — there is no "
                "external window in which a pre-wait pattern can enforce "
                "barrier_fraction."
            ),
            compatible_patterns=[
                "Custom resource where eviction is driven by the requester's own "
                "Process.interrupt() call, fired only after measuring holder "
                "served-time against barrier_dur.",
                "Holder process owns its own barrier check and exposes a "
                "cooperative preempt-allowed event the requester awaits.",
            ],
            incompatible_patterns=[
                "simpy.PreemptiveResource.request(preempt=True) with any external "
                "pre-wait pattern in the requester process — the pre-wait has a "
                "same-tick race with the library's synchronous preempt step that "
                "cannot be closed by tighter buffers.",
                "Snapshot-and-sleep over a holder dict followed by request(preempt=True).",
                "Retry-with-buffer loops around request(preempt=True).",
            ],
            iteration_signal=(
                f"STRUCTURAL CEILING REACHED: {dominant.share:.0%} of residual "
                f"violations share fingerprint '{dominant.fingerprint}'. Parametric "
                f"tuning of pre-wait durations, buffer sizes, or retry counts CANNOT "
                f"reduce this residual. A categorical change of preempt-decision "
                f"mechanism is required."
            ),
        )

    if (
        dominant_structural
        and dominant.fingerprint == "near_tick+served_below_10pct_of_barrier"
    ):
        return ImplicationTag(
            tag="MECHANISM_INTERCEPTION_REQUIRED",
            description=(
                "Residual violations cluster very close to segment start "
                "(served_so_far < 10% of declared barrier). A pre-wait pattern is "
                "partially intercepting the preempt-decision but losing to "
                "same-tick races between holder registration and the library's "
                "synchronous preempt step."
            ),
            compatible_patterns=[
                "Custom resource owning the preempt-decision step, as above.",
            ],
            incompatible_patterns=[
                "Any pre-wait pattern in the requester process — the race window "
                "cannot be closed by tighter buffers.",
            ],
            iteration_signal=(
                f"NEAR-STRUCTURAL CEILING: {dominant.share:.0%} of residual "
                f"violations fingerprint as same-tick races. Further parametric "
                f"tuning will not close this gap."
            ),
        )

    if structural_share > 0.25:
        return ImplicationTag(
            tag="MIXED_STRUCTURAL_AND_PARAMETRIC",
            description=(
                "Residual contains a structural component (same-tick races) "
                f"summing to {structural_share:.0%} of violations plus a "
                "parametric tail. Address the structural component first by "
                "changing the preempt-decision mechanism; the parametric tail "
                "may then close under the new mechanism, or remain as a "
                "smaller residual that parametric tuning can address."
            ),
            compatible_patterns=[
                "Replace the preempt-decision mechanism first; re-measure parametric "
                "residual under the new mechanism."
            ],
            incompatible_patterns=[
                "Tuning parameters alone cannot close the structural component."
            ],
            iteration_signal=(
                f"Residual mixes structural ({structural_share:.0%}) and "
                f"parametric violations. Mechanism change required before "
                f"further parametric tuning — do NOT read a declining count "
                f"as convergence while the structural share persists."
            ),
        )

    # Structural share negligible (≤ 25%) — residual is parametric-dominated
    return ImplicationTag(
        tag="PARAMETRIC_TUNING_PROMISING",
        description=(
            "Residual violations are distributed across the served-time range "
            "(no single structural fingerprint dominates). This is consistent "
            "with a mechanism that could plausibly satisfy the barrier with "
            "further parametric adjustment (longer pre-wait, more conservative "
            "buffer, additional retry rounds)."
        ),
        compatible_patterns=[
            "Continue adjusting the existing mechanism's parameters.",
        ],
        incompatible_patterns=[],
        iteration_signal=(
            f"Residual is dispersed (top cluster share={dominant.share:.0%}, "
            f"fingerprint='{dominant.fingerprint}'). Parametric tuning may continue "
            f"to reduce violations."
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rendering helpers (used by validation.py to extend diagnostic text)
# ─────────────────────────────────────────────────────────────────────────────


def render_clusters(clusters: list[ClusterReport]) -> str:
    """Plain-text rendering of cluster structure, one line per cluster."""
    if not clusters:
        return "  (no residual violations to categorize)"
    lines: list[str] = []
    for c in clusters:
        marker = "▮ STRUCTURAL" if c.structural else "  parametric "
        lines.append(
            f"  {marker}  [{c.count} viol, {c.share:.0%}]  fingerprint='{c.fingerprint}'"
        )
        lines.append(f"      {c.description}")
    return "\n".join(lines)


def render_implication(tag: ImplicationTag) -> str:
    """Plain-text rendering of an architectural-implication tag."""
    out: list[str] = [
        f"  Architectural implication: {tag.tag}",
        f"    {tag.description}",
    ]
    if tag.compatible_patterns:
        out.append("    Compatible patterns:")
        for p in tag.compatible_patterns:
            out.append(f"      • {p}")
    if tag.incompatible_patterns:
        out.append("    Incompatible patterns (do not iterate on these):")
        for p in tag.incompatible_patterns:
            out.append(f"      • {p}")
    if tag.iteration_signal:
        out.append(f"    Iteration signal: {tag.iteration_signal}")
    return "\n".join(out)


def diagnostic_extension(
    clusters: list[ClusterReport], tag: ImplicationTag
) -> str:
    """Compact single-block string to append to a CheckRecord.diagnostic.

    Designed to be informative when read by a code-agent looking at the check
    output as part of an iterative-repair loop: structural-vs-parametric verdict
    first, incompatible patterns second, compatible patterns third.
    """
    if not clusters:
        return ""
    lines = [
        "",
        "─── Residual analysis ───",
        render_clusters(clusters),
        "",
        render_implication(tag),
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Serialization helpers (for CheckRecord.evidence)
# ─────────────────────────────────────────────────────────────────────────────


def clusters_to_dict(clusters: list[ClusterReport]) -> list[dict]:
    return [asdict(c) for c in clusters]


def implication_to_dict(tag: ImplicationTag) -> dict:
    return asdict(tag)
