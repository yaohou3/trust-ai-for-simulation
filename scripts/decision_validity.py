"""
decision_validity.py — the three-dimension trust verdict (Stage E roll-up).

WHY THIS EXISTS
---------------
A single collapsed "PASS" invites the worst real-world failure mode: "the model
passed, so we trust it blindly." This module keeps the verdict in THREE separate,
non-collapsing dimensions, and computes them deterministically so an LLM's Stage-E
prose cannot quietly merge them:

  Credibility       — is the model implemented correctly?      (Phases 0–4)
  Causal Validity   — does it respond correctly to interventions? (Phase 4.5)
  Decision Validity — is it safe to use for THIS decision?      (gated roll-up)

GATING RULES (the heart of it)
------------------------------
  • Dimensions are MONOTONIC: Decision Validity can never be better than the worse
    of Credibility and Causal Validity. A non-credible or causally-invalid model
    cannot be decision-valid, full stop.
  • Decision Validity requires AUTHORITATIVE causal coverage of the decision-
    relevant claim — a declared or SME-approved Phase 4.5 PASS. Advisory
    (auto-generated) evidence or no coverage caps Decision Validity at WARN. This
    is the PASS_ADVISORY_ONLY / PASS_NO_SCENARIOS distinction, enforced.
  • Phase 5 is ADVISORY. Its flags can pull Decision Validity DOWN to WARN, but
    can never FAIL a model on their own — only the deterministic lower layers
    (Credibility, Causal Validity) can FAIL or BLOCK. This preserves the rule that
    LLM judgement never mints (or destroys) a verdict by itself.

This is the operationalization of fitness-for-intended-use (cf. NASA-STD-7009
credibility assessment, ASME V&V 10/40, Sargent's validation taxonomy): the
concept is established; what's enforced here is the provenance-weighted, gameable-
LLM-resistant, non-collapsing roll-up.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# Verdict ordinal — higher is worse. NOT_EXERCISED sits between PASS and WARN:
# it is not a pass (no evidence) but it is not a contradiction either.
_RANK = {"PASS": 0, "NOT_EXERCISED": 1, "WARN": 2, "FAIL": 3, "BLOCK": 4}
_BY_RANK = {v: k for k, v in _RANK.items()}

_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}
_FLAG_CATEGORIES = {"emergent_behavior", "realism_gap", "decision_risk",
                    "communication_risk"}


def _worst(*verdicts: str) -> str:
    """Return the worst (highest-rank) verdict among the arguments."""
    return _BY_RANK[max(_RANK.get(v, _RANK["WARN"]) for v in verdicts)]


# ── Credibility (Phases 0–4) ───────────────────────────────────────────────
def credibility(phase_statuses: dict) -> dict:
    """Worst-of across Phases 0–4. ``phase_statuses`` keys may be '0'..'4' or
    'phase0'..'phase4'; values are PASS/WARN/FAIL/BLOCK (INFO treated as PASS).

    AUDIT FIX (C8): this roll-up must FAIL CLOSED, not open.
      • A phase whose status is unknown / blank / unparseable maps to
        NOT_EXERCISED, never PASS — an unrecognised status is absence of
        evidence, not evidence.
      • ALL FIVE phases must be present. A missing phase also maps to
        NOT_EXERCISED (with its own reason), so ``{"0": "PASS"}`` alone can
        no longer yield "All of Phases 0–4 PASS."
    INFO alone remains PASS: it is an affirmative "check ran, informational
    only" status emitted by the validators, not an absence of evidence.
    """
    norm: dict[str, str] = {}
    for k, v in (phase_statuses or {}).items():
        key = str(k).replace("phase", "")
        norm[key] = (v or "").upper()
    considered = {k: v for k, v in norm.items() if k in {"0", "1", "2", "3", "4"}}
    if not considered:
        return {"verdict": "NOT_EXERCISED",
                "reasons": ["No Phase 0–4 results provided."]}
    worst = "PASS"
    bad: list[str] = []
    for k in ("0", "1", "2", "3", "4"):
        if k not in considered:
            v = "NOT_EXERCISED"
            bad.append(f"Phase {k}=MISSING (treated as NOT_EXERCISED)")
        else:
            raw = considered[k]
            if raw == "INFO":
                v = "PASS"
            elif raw in _RANK:
                v = raw
            else:
                v = "NOT_EXERCISED"
                bad.append(f"Phase {k}={raw or 'BLANK'!r} (unrecognised status; "
                           f"treated as NOT_EXERCISED)")
        if _RANK[v] > _RANK[worst]:
            worst = v
        if v in ("WARN", "FAIL", "BLOCK"):
            bad.append(f"Phase {k}={v}")
    reasons = ([f"Worst of Phases 0–4: {', '.join(bad)}."] if bad
               else ["All of Phases 0–4 PASS."])
    return {"verdict": worst, "reasons": reasons}


# ── Causal Validity (Phase 4.5) ────────────────────────────────────────────
_CAUSAL_MAP = {
    "PASS": "PASS",
    "WARN": "WARN",
    "FAIL": "FAIL",
    "BLOCK": "BLOCK",
    "PASS_ADVISORY_ONLY": "WARN",     # only advisory evidence → not a clean pass
    "PASS_NO_SCENARIOS": "NOT_EXERCISED",
}


def causal_validity(phase45_aggregate: str) -> dict:
    """Map the Phase 4.5 aggregate verdict to the Causal Validity dimension."""
    agg = (phase45_aggregate or "").upper()
    v = _CAUSAL_MAP.get(agg, "WARN")
    reasons = {
        "PASS": ["Phase 4.5: authoritative scenarios verified causal response."],
        "WARN": [f"Phase 4.5 aggregate '{agg}' — inconclusive or advisory-only "
                 "causal evidence."],
        "FAIL": ["Phase 4.5: the model contradicted a declared causal claim."],
        "BLOCK": ["Phase 4.5: harness/sim error."],
        "NOT_EXERCISED": ["Phase 4.5 ran no scenarios — causal response untested."],
    }.get(v, [f"Phase 4.5 aggregate '{agg}'."])
    return {"verdict": v, "reasons": reasons}


# ── Decision Validity (gated roll-up) ──────────────────────────────────────
def decision_validity(cred: dict, causal: dict, phase5_flags: list[dict],
                      *, decision_claim_covered_authoritatively: bool) -> dict:
    """Gate the decision verdict by the lower layers, authoritative coverage,
    and advisory Phase 5 flags (which can only cap at WARN, never FAIL)."""
    floor = _worst(cred["verdict"], causal["verdict"])
    reasons: list[str] = []

    if floor in ("FAIL", "BLOCK"):
        reasons.append(
            f"Capped by lower layers (credibility={cred['verdict']}, "
            f"causal_validity={causal['verdict']}): a non-credible or causally-"
            "invalid model cannot be decision-valid.")
        return {"verdict": floor, "reasons": reasons}

    if floor != "PASS":  # WARN or NOT_EXERCISED
        reasons.append(
            f"Capped at WARN by lower layers (credibility={cred['verdict']}, "
            f"causal_validity={causal['verdict']}).")
        return {"verdict": "WARN", "reasons": reasons}

    # Lower layers are clean PASS. Now coverage + advisory flags can only lower.
    if not decision_claim_covered_authoritatively:
        reasons.append(
            "Decision-relevant claim is not covered by an authoritative "
            "(declared / SME-approved) Phase 4.5 PASS — advisory or no coverage "
            "is insufficient to certify a decision.")
        return {"verdict": "WARN", "reasons": reasons}

    high = [f for f in (phase5_flags or [])
            if _SEVERITY_RANK.get((f.get("severity") or "info").lower(), 0) >= 3
            and (f.get("category") in ("decision_risk", "realism_gap"))]
    if high:
        reasons.append(
            f"{len(high)} high-severity advisory Phase 5 flag(s) open: "
            + "; ".join((f.get("message") or f.get("category") or "")[:80]
                        for f in high)
            + " (advisory — caps Decision Validity at WARN, never FAIL).")
        return {"verdict": "WARN", "reasons": reasons}

    reasons.append(
        "Credible, causally valid, decision claim authoritatively verified, and "
        "no high-severity plausibility flags.")
    return {"verdict": "PASS", "reasons": reasons}


# ── Phase 5 typed-flag parsing ─────────────────────────────────────────────
_FLAG_RE = re.compile(
    r"===PHASE5_FLAG_START===\s*(\{.*?\})\s*===PHASE5_FLAG_END===", re.DOTALL)


def parse_phase5_flags(text: str) -> tuple[list[dict], list[str]]:
    """Extract typed Phase 5 flags from the plausibility-review response.
    Flags are ALWAYS advisory; unknown categories/severities are normalized,
    not trusted. Returns (flags, warnings)."""
    flags: list[dict] = []
    warnings: list[str] = []
    for i, raw in enumerate(_FLAG_RE.findall(text or ""), 1):
        try:
            f = json.loads(raw)
        except json.JSONDecodeError as exc:
            warnings.append(f"Flag block {i}: invalid JSON ({exc}); skipped.")
            continue
        cat = (f.get("category") or "").lower()
        if cat not in _FLAG_CATEGORIES:
            warnings.append(f"Flag {i}: unknown category '{cat}' → treated as "
                            "realism_gap (advisory).")
            cat = "realism_gap"
        sev = (f.get("severity") or "info").lower()
        if sev not in _SEVERITY_RANK:
            warnings.append(f"Flag {i}: unknown severity '{sev}' → 'low'.")
            sev = "low"
        flags.append({
            "category": cat,
            "severity": sev,
            "message": str(f.get("message") or "").strip(),
            "evidence": f.get("evidence"),
            "advisory": True,
        })
    return flags, warnings


# ── Synthesis ──────────────────────────────────────────────────────────────
def synthesize(*, phase_statuses: dict, phase45_aggregate: str,
               phase5_flags: list[dict] | None,
               decision_claim_covered_authoritatively: bool) -> dict:
    cred = credibility(phase_statuses)
    causal = causal_validity(phase45_aggregate)
    dec = decision_validity(
        cred, causal, phase5_flags or [],
        decision_claim_covered_authoritatively=decision_claim_covered_authoritatively)
    return {"credibility": cred, "causal_validity": causal,
            "decision_validity": dec,
            "phase5_flag_count": len(phase5_flags or [])}


def render(report: dict) -> str:
    """Human-readable three-dimension block (never a single collapsed verdict)."""
    lines = ["=" * 70, "  TRUST VERDICT — three dimensions (do not collapse)", "=" * 70]
    for dim in ("credibility", "causal_validity", "decision_validity"):
        d = report[dim]
        lines.append(f"  {dim.replace('_', ' ').title():18s}: {d['verdict']}")
        for r in d["reasons"]:
            lines.append(f"      · {r}")
    lines.append("=" * 70)
    return "\n".join(lines)


def _statuses_from_vvuq_out(out_dir: str) -> dict:
    """Read per-phase statuses from a vvuq_out directory.

    AUDIT FIX (C8): two fail-open behaviours corrected.
      • Phase 0 reports do not carry a top-level ``status`` field — they
        carry ``gate`` ("OPEN" or "BLOCKED (…)") plus a nested
        ``phase0.tally``. The old reader looked only at ``status``, got
        "", and the roll-up then scored a genuinely BLOCKED Phase 0 as
        PASS. We now translate the gate/tally shape.
      • A corrupt/unreadable report was silently skipped (bare except →
        the phase vanished from the roll-up). It is now recorded as
        "UNREADABLE", which credibility() maps to NOT_EXERCISED, and the
        parse error is printed.
    """
    import os
    statuses: dict[str, str] = {}
    for ph in ("0", "1", "2", "3", "4"):
        path = os.path.join(out_dir, f"phase{ph}_report.json")
        if not os.path.isfile(path):
            continue    # credibility() treats the missing phase as NOT_EXERCISED
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception as exc:
            print(f"[decision_validity] cannot parse {path}: {exc}",
                  file=sys.stderr)
            statuses[ph] = "UNREADABLE"
            continue
        status = (data.get("status") or "").upper()
        if not status:
            # Phase 0 gate shape: gate == "OPEN" means the contract gate
            # passed; anything else ("BLOCKED (structural)", …) is a BLOCK.
            gate = (data.get("gate") or "").upper()
            if gate:
                if gate == "OPEN":
                    tally = (data.get("phase0") or {}).get("tally") or {}
                    status = "WARN" if tally.get("WARN", 0) > 0 else "PASS"
                else:
                    status = "BLOCK"
        statuses[ph] = status or "UNRECOGNISED"
    return statuses


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Compute the three-dimension trust verdict (Credibility / "
                    "Causal Validity / Decision Validity).")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--phases", help='JSON like {"0":"PASS","1":"PASS",...}')
    g.add_argument("--vvuq-out", help="dir with phaseN_report.json files")
    p.add_argument("--causal", required=True,
                   help="Phase 4.5 aggregate verdict (PASS/WARN/FAIL/BLOCK/"
                        "PASS_ADVISORY_ONLY/PASS_NO_SCENARIOS)")
    p.add_argument("--phase5", default=None,
                   help="file with the Phase 5 response text (typed flags parsed)")
    p.add_argument("--decision-claim-authoritative", default="false",
                   help="true if the decision-relevant claim was verified by a "
                        "declared/SME-approved Phase 4.5 PASS")
    args = p.parse_args(argv)

    phase_statuses = (json.loads(args.phases) if args.phases
                      else _statuses_from_vvuq_out(args.vvuq_out))
    flags: list[dict] = []
    if args.phase5:
        try:
            with open(args.phase5) as f:
                flags, warns = parse_phase5_flags(f.read())
            for w in warns:
                print(f"[decision_validity] {w}")
        except OSError as exc:
            print(f"[decision_validity] cannot read {args.phase5}: {exc}",
                  file=sys.stderr)

    covered = str(args.decision_claim_authoritative).lower() in ("true", "1", "yes")
    report = synthesize(phase_statuses=phase_statuses, phase45_aggregate=args.causal,
                        phase5_flags=flags,
                        decision_claim_covered_authoritatively=covered)
    print(render(report))
    # Exit nonzero if Decision Validity is not a clean PASS, so callers can gate.
    return 0 if report["decision_validity"]["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
