"""description_dsl_coverage.py — Description→DSL reference-data coverage gate.

WHY THIS EXISTS
---------------
The ICU case study's headline gap was that the description's Table 6 declared
eleven reference KPIs with 95% confidence intervals, but only three made it
faithfully into the DSL as `aggregate_target` entries with `expected_value`s
matching the description; the rest were either absent or wide-tolerance
placeholders (`assumed_default: True`). The Phase 4.5 harness has no
authoritative target to falsify against when KPIs are silently dropped on the
way from description to DSL.

This module reads reference tables out of the description docx, identifies the
numeric KPI rows, and requires the DSL to declare a corresponding
`aggregate_target` for each — with an `expected_value` that falls within the
description's stated confidence interval (or, if no CI is provided, within a
declared tolerance). Description rows without a matching DSL element are
flagged as DSL_GAPs.

WHAT THE CHECK READS
--------------------
A reference table is a docx table where AT LEAST ONE column contains values
that parse as numbers (with optional unit and / or trailing CI in parens).
Common shapes the parser handles:

    "3.01 days"                          → value=3.01,  unit="days"
    "0.85"                               → value=0.85
    "96.20%"                             → value=96.20, unit="%"
    "3.01 days | (2.90, 3.13)"           → value=3.01, ci=(2.90, 3.13)
    "6.37 patients/day | (6.09, 6.64)"   → value=6.37, ci=(6.09, 6.64)

For each row the first cell is treated as the KPI name, and the parser scans
the remaining cells for a numeric value and an optional CI.

MATCHING DESCRIPTION ROWS TO DSL aggregate_targets
--------------------------------------------------
A description row matches a DSL aggregate_target when normalized substring
match holds in either direction between the description's KPI name and the
DSL element's `name` or `metric` field. Match is intentionally permissive (the
description tends to use natural-language KPI names; the DSL uses machine
names). Ambiguous matches are reported; unmatched rows are FAIL.

USAGE
-----
    from description_dsl_coverage import check_description_dsl_coverage
    result = check_description_dsl_coverage(docx_path, dsl_doc)
    if result["status"] != "PASS":
        # BLOCK Stage B (DSL alignment)
"""

from __future__ import annotations

import argparse
import json
import re
import sys


# Regex that pulls a leading numeric value (with optional sign and decimal),
# possibly followed by a unit word, possibly followed by a CI tuple in
# parentheses.
_NUM_RE = re.compile(
    r"(?P<val>[-+]?\d+(?:\.\d+)?)"
    r"\s*(?P<unit>[a-zA-Z%/]+(?:\s*[a-zA-Z%/]+)?)?"
)
_CI_RE = re.compile(
    r"\(\s*(?P<lo>[-+]?\d+(?:\.\d+)?)\s*,\s*(?P<hi>[-+]?\d+(?:\.\d+)?)\s*\)"
)


def _parse_numeric_cell(text: str) -> dict | None:
    """Return ``{value, unit, ci_low, ci_high}`` parsed from a cell, or None."""
    if not text:
        return None
    text = text.replace(" ", " ").strip()
    m = _NUM_RE.match(text)
    if not m:
        return None
    try:
        val = float(m.group("val"))
    except ValueError:
        return None
    out: dict = {"value": val, "unit": (m.group("unit") or "").strip()}
    ci = _CI_RE.search(text)
    if ci:
        try:
            out["ci_low"] = float(ci.group("lo"))
            out["ci_high"] = float(ci.group("hi"))
        except ValueError:
            pass
    return out


def _normalize_kpi_name(name: str) -> str:
    """Normalize for permissive matching."""
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    return name.strip("_")


def _table_is_kpi_reference(table) -> bool:
    """A reference KPI table is one whose body declares values with
    confidence intervals or explicit point/range columns. Ordinary
    categorical tables (state meanings, acuity probabilities, priority
    rankings, resource counts) do not.

    Heuristic: at least one data cell contains a parenthesised pair of
    numbers (a CI), OR a header cell mentions "Confidence Interval",
    "95% CI", "Range", or "Mean / 95% CI".
    """
    header_keywords = (
        "confidence interval", "95% ci", "mean / 95%",
        "range", "lower", "upper", "ci low", "ci high",
    )
    saw_ci_cell = False
    saw_header_keyword = False
    for ri, row in enumerate(table.rows):
        for ci, c in enumerate(row.cells):
            text = c.text.strip().lower()
            if not text:
                continue
            if _CI_RE.search(text):
                saw_ci_cell = True
            if ri == 0 and any(kw in text for kw in header_keywords):
                saw_header_keyword = True
            if saw_ci_cell or saw_header_keyword:
                return True
    return False


def extract_reference_rows(docx_path: str) -> list[dict]:
    """Extract candidate KPI rows from a docx file.

    Each row: ``{kpi_name, value, unit, ci_low, ci_high, table_index, row_index}``.
    A row is included only if (a) its containing table looks like a reference
    KPI table (CI columns present), (b) its first cell looks like a KPI name
    (non-empty, not numeric), and (c) at least one other cell parses as a
    number. Ordinary categorical tables are skipped so the gate does not
    produce false-positive failures for the description's state-meaning,
    acuity-mix, or priority-ranking tables.
    """
    try:
        from docx import Document
    except Exception as exc:
        raise RuntimeError(
            "python-docx is required to read the description docx; "
            "install via `pip install python-docx --break-system-packages`."
        ) from exc
    d = Document(docx_path)
    rows: list[dict] = []
    for ti, table in enumerate(d.tables):
        if not _table_is_kpi_reference(table):
            continue
        header_phrases = ("confidence interval", "95% ci", "mean / 95%",
                          "mean value", "ci low", "ci high", "lower", "upper")
        for ri, row in enumerate(table.rows):
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            if not cells or not cells[0]:
                continue
            name_cell = cells[0]
            # Skip a row that is itself the table header — detected when one
            # of the non-first cells contains a header phrase (e.g. "95%
            # Confidence Interval"). This prevents the parser from extracting
            # a fake "Output / 95.0" row from the column header.
            if any(any(p in (c or "").lower() for p in header_phrases)
                   for c in cells):
                continue
            numeric_cells: list[dict] = []
            for c in cells[1:]:
                parsed = _parse_numeric_cell(c)
                if parsed is not None:
                    numeric_cells.append(parsed)
            if not numeric_cells:
                continue
            if _parse_numeric_cell(name_cell) is not None:
                continue
            primary = numeric_cells[0]
            ci_cell = next((c for c in numeric_cells if "ci_low" in c), None)
            row_data = {
                "kpi_name": name_cell,
                "normalized_name": _normalize_kpi_name(name_cell),
                "value": primary["value"],
                "unit": primary.get("unit") or "",
                "ci_low": (ci_cell or {}).get("ci_low"),
                "ci_high": (ci_cell or {}).get("ci_high"),
                "table_index": ti,
                "row_index": ri,
                "raw_cells": cells,
            }
            rows.append(row_data)
    return rows


def _dsl_aggregate_targets(dsl_doc: dict) -> list[dict]:
    """Return all aggregate_target-like declarations from the DSL."""
    out: list[dict] = []
    seen_ids: set[str] = set()
    for e in (dsl_doc or {}).get("dsl_elements", []) or []:
        if not isinstance(e, dict):
            continue
        if e.get("element_type") != "aggregate_target":
            continue
        eid = e.get("id", "")
        if not eid or eid in seen_ids:
            continue
        seen_ids.add(eid)
        out.append({
            "id": eid,
            "name": e.get("name", ""),
            "metric": e.get("metric", ""),
            "expected_value": e.get("expected_value"),
            "tolerance": e.get("tolerance"),
            "assumed_default": bool(e.get("assumed_default")),
            "normalized_names": [
                _normalize_kpi_name(e.get("name", "")),
                _normalize_kpi_name(e.get("metric", "")),
            ],
        })
    return out


_STOPWORDS = {"of", "the", "a", "an", "to", "and", "or", "for", "by", "in",
              "on", "rate", "mean", "average", "daily", "per", "value", "icu"}


def _score_target(name: str, target: dict) -> int:
    """Score a DSL aggregate_target against a description KPI name. Higher is
    a better match; 0 means no match."""
    tokens_name = {tok for tok in name.split("_")
                   if tok and tok not in _STOPWORDS}
    if not tokens_name:
        return 0
    best = 0
    for tname in target["normalized_names"]:
        if not tname:
            continue
        if name in tname or tname in name:
            return 20  # substring match wins outright
        tokens_t = {tok for tok in tname.split("_")
                    if tok and tok not in _STOPWORDS}
        shared = tokens_name & tokens_t
        if not shared:
            continue
        s = 0
        for tok in shared:
            if len(tok) >= 6:
                s += 5         # strong domain word (e.g. mortality, utilization)
            elif len(tok) >= 4:
                s += 2         # medium word (e.g. bed, los, beds)
            else:
                s += 1
        best = max(best, s)
    return best


def _match_description_to_dsl(
    description_row: dict, targets: list[dict]
) -> list[dict]:
    """Pick the best DSL aggregate_target for a description row, or none if
    the match is ambiguous (multiple targets tied at the highest score) or
    too weak (score below the matching threshold).

    Substring matches win outright. Otherwise the best-scoring target wins
    only when its score is both above the minimum threshold AND strictly
    above the runner-up — this prevents over-matching when several DSL
    targets share a single generic token (e.g. ``utilization``).
    """
    name = description_row["normalized_name"]
    if not name:
        return []
    scored = [(t, _score_target(name, t)) for t in targets]
    scored = [(t, s) for t, s in scored if s > 0]
    if not scored:
        return []
    scored.sort(key=lambda kv: kv[1], reverse=True)
    best, best_score = scored[0]
    MIN_SCORE = 5             # at least one strong domain word shared
    if best_score < MIN_SCORE:
        return []
    if len(scored) > 1 and scored[1][1] == best_score:
        # Ambiguous — multiple targets tied at the top. Return nothing so the
        # row is reported as missing rather than spuriously matched.
        return []
    return [best]


def _value_within_ci(value: float, ci_low: float | None,
                      ci_high: float | None, tolerance: float | None) -> bool:
    """Verify a DSL value falls inside the description CI (preferred) or, if
    absent, within ±tolerance of the description value."""
    if ci_low is not None and ci_high is not None:
        return ci_low - 1e-9 <= value <= ci_high + 1e-9
    return True  # no CI declared; tolerance check happens at the caller


# Unit canonicalization. The description and the DSL frequently express the
# same quantity in different units (% vs fraction, days vs hours). Normalize
# the description's value to the units the DSL element implies, using two
# signals: (a) the description cell's unit string, (b) the DSL metric name.
_FRACTION_HINTS = ("fraction", "share", "ratio", "utilization", "rate",
                   "probability", "occupancy")
_HOURS_HINTS = ("hours", "_hours", "_h_", "hour")
_DAYS_HINTS = ("days", "_days", "day")
_SECONDS_HINTS = ("seconds", "_seconds", "_s_", "sec")


def _looks_fractional(metric_name: str) -> bool:
    n = (metric_name or "").lower()
    return any(h in n for h in _FRACTION_HINTS)


def _normalize_value_to_dsl_units(description_value: float,
                                   description_unit: str,
                                   ci_low: float | None,
                                   ci_high: float | None,
                                   dsl_element: dict
                                   ) -> tuple[float, float | None, float | None]:
    """Return (value, ci_low, ci_high) converted to the units the DSL element
    implies. Best-effort: handles %↔fraction and days↔hours↔seconds.
    """
    val = description_value
    lo = ci_low
    hi = ci_high
    desc_u = (description_unit or "").lower().strip()
    metric_name = (dsl_element.get("metric") or dsl_element.get("name") or "").lower()

    # Percentages → fractions, when the DSL element looks fractional.
    if "%" in desc_u and _looks_fractional(metric_name):
        val = val / 100.0
        if lo is not None: lo = lo / 100.0
        if hi is not None: hi = hi / 100.0

    # Days ↔ hours, when the DSL element looks hourly.
    if (any(h in metric_name for h in _HOURS_HINTS)
            and any(h in desc_u for h in _DAYS_HINTS)):
        val = val * 24.0
        if lo is not None: lo = lo * 24.0
        if hi is not None: hi = hi * 24.0

    # Hours ↔ seconds, when the DSL element is in seconds.
    if (any(h in metric_name for h in _SECONDS_HINTS)
            and any(h in desc_u for h in _HOURS_HINTS)):
        val = val * 3600.0
        if lo is not None: lo = lo * 3600.0
        if hi is not None: hi = hi * 3600.0

    return val, lo, hi


# ─────────────────────────────────────────────────────────────────────────────
# Dynamics-mechanism keyword scan (v5.3)
# ─────────────────────────────────────────────────────────────────────────────
#
# Scan the description text for hazard-class / discrete-check / event-driven
# language and verify the DSL declares a corresponding `dynamics.type` on at
# least one transition-bearing element. Catches the most consequential class
# of dynamics-mechanism substitution: descriptions that use continuous-time
# language ("discharge rate", "hazard", "half-life", "exponential time to")
# whose DSL silently defaults to discrete per-hour checks.

# Sentence patterns implying continuous-time hazard mechanisms. Match is
# substring-based on lowercased description text.
_CONTINUOUS_HAZARD_PATTERNS = (
    "discharge rate", "mortality rate", "death rate", "departure rate",
    "transition rate", "rate of discharge", "rate of mortality",
    "hazard rate", "hazard function", "instantaneous risk",
    "exponential time to", "exponentially distributed time",
    "half-life", "half life", "median time to", "mean time to event",
    "continuous-time", "continuous time hazard", "poisson process",
    "memoryless", "constant hazard",
)

# Patterns implying discrete periodic checks.
_DISCRETE_CHECK_PATTERNS = (
    "every hour", "every 30 minutes", "every 15 minutes", "hourly check",
    "per-check probability", "at each check", "discrete check",
    "at each round", "at each visit", "scheduled check",
    "per-rounding probability", "per-visit probability",
)

# Patterns implying event-driven transitions.
_EVENT_DRIVEN_PATTERNS = (
    "triggered by", "upon arrival", "upon completion",
    "when ", "after ", "on completion of",
    "in response to",
)


def _extract_description_text(docx_path: str) -> str:
    """Return the docx's full body text (paragraphs only — no tables)
    concatenated to a single lowercased string. Tables are parsed separately
    by extract_reference_rows; here we want the prose that surrounds them
    for keyword scanning."""
    try:
        from docx import Document  # type: ignore
    except Exception:
        return ""
    try:
        doc = Document(docx_path)
    except Exception:
        return ""
    parts: list[str] = []
    for p in doc.paragraphs:
        t = (p.text or "").strip()
        if t:
            parts.append(t)
    return "\n".join(parts).lower()


def _scan_dynamics_keywords(text: str) -> dict:
    """Classify the description text by which dynamics-mechanism language
    it contains. Returns counts plus matched-phrase samples for each of the
    three categories. A description with no matches in any category is
    silent about temporal structure (the most common case for casual
    descriptions); the DSL's dynamics defaults then propagate as
    `unspecified` placeholders without a description-side gate trip."""
    matches: dict[str, list[str]] = {
        "continuous_hazard": [],
        "discrete_check": [],
        "event_driven": [],
    }
    for pat in _CONTINUOUS_HAZARD_PATTERNS:
        if pat in text:
            matches["continuous_hazard"].append(pat)
    for pat in _DISCRETE_CHECK_PATTERNS:
        if pat in text:
            matches["discrete_check"].append(pat)
    for pat in _EVENT_DRIVEN_PATTERNS:
        if pat in text:
            matches["event_driven"].append(pat)
    return matches


def _dsl_transition_elements(dsl_doc: dict) -> list[dict]:
    """Return DSL elements that bear (or should bear) a `dynamics` block —
    state_restrictions, terminal_outcomes, recurring_activities that
    implement probabilistic transitions, and explicit state_transition_rules.
    """
    out: list[dict] = []
    for el in (dsl_doc or {}).get("dsl_elements", []) or []:
        if not isinstance(el, dict):
            continue
        et = el.get("element_type", "")
        if et in ("state_restriction", "terminal_outcome",
                  "recurring_activity"):
            out.append(el)
    return out


def check_dynamics_coverage(docx_path: str, dsl_doc: dict) -> dict:
    """Cross-check description dynamics-language against DSL dynamics
    declarations. Returns a structured finding suitable for surfacing
    alongside the KPI coverage report at A4. The check is conservative:
    it surfaces a FAIL only when the description uses continuous-hazard
    language but no DSL transition element declares
    `dynamics.type='continuous_hazard'`. The other two patterns
    (discrete_check, event_driven) produce INFO-class evidence rather than
    FAIL because the DSL's default is already a discrete-event simulation
    and discrete-check semantics are the path-of-least-surprise."""
    text = _extract_description_text(docx_path)
    matches = _scan_dynamics_keywords(text)
    elements = _dsl_transition_elements(dsl_doc)
    # Collect declared dynamics types across all transition-bearing elements.
    declared_types: dict[str, list[str]] = defaultdict(list)
    unspecified_count = 0
    for el in elements:
        dyn = el.get("dynamics") or {}
        dt = (dyn.get("type") or "").lower().strip() if isinstance(dyn, dict) else ""
        if dt in ("continuous_hazard", "discrete_check", "event_driven"):
            declared_types[dt].append(el.get("id", "?"))
        elif dt == "unspecified" or not dt:
            unspecified_count += 1
    findings: list[dict] = []
    # FAIL: description uses continuous-hazard language but DSL declares
    # zero continuous_hazard transitions. This is the headline case the
    # check exists to surface.
    if matches["continuous_hazard"] and not declared_types["continuous_hazard"]:
        findings.append({
            "type": "continuous_hazard_missing",
            "severity": "FAIL",
            "description_phrases": matches["continuous_hazard"][:5],
            "n_transition_elements": len(elements),
            "n_unspecified_dynamics": unspecified_count,
            "message": (
                f"Description uses continuous-hazard language "
                f"({', '.join(repr(p) for p in matches['continuous_hazard'][:3])}"
                f"{'...' if len(matches['continuous_hazard']) > 3 else ''}) "
                f"but no DSL transition-bearing element declares "
                f"dynamics.type='continuous_hazard'. {unspecified_count} "
                f"of {len(elements)} transition elements have unspecified "
                f"dynamics. Either declare continuous_hazard with a "
                f"hazard_rate on the appropriate element(s) — e.g. "
                f"discharge, mortality, or state-recovery transitions — "
                f"or document a justification for substituting discrete-check "
                f"semantics."
            ),
        })
    # INFO: description uses discrete-check language and DSL declares
    # discrete_check — alignment is consistent, no finding.
    # INFO: description uses event-driven language but DSL declares no
    # event_driven transitions. Lower severity than the hazard case;
    # often these are correctly implemented as on-arrival processes
    # rather than as a typed event_driven dynamics block.
    if matches["event_driven"] and not declared_types["event_driven"]:
        findings.append({
            "type": "event_driven_not_declared",
            "severity": "INFO",
            "description_phrases": matches["event_driven"][:3],
            "message": (
                f"Description uses event-trigger language but no transition "
                f"element declares dynamics.type='event_driven'. This is "
                f"often correctly handled by the DSL's process structure; "
                f"surfaced as INFO for awareness."
            ),
        })
    return {
        "matches": matches,
        "declared_types": dict(declared_types),
        "n_transition_elements": len(elements),
        "n_unspecified_dynamics": unspecified_count,
        "findings": findings,
        "status": ("FAIL" if any(f["severity"] == "FAIL" for f in findings)
                   else "PASS"),
    }


def check_description_dsl_coverage(docx_path: str, dsl_doc: dict,
                                    *, tolerance: float = 0.10) -> dict:
    """Verify every numeric reference row in the description has a matching
    DSL aggregate_target with a value consistent with the description.

    ``tolerance`` is the fallback relative-error tolerance used when the
    description row has no confidence interval and the DSL element has no
    declared tolerance.
    """
    rows = extract_reference_rows(docx_path)
    targets = _dsl_aggregate_targets(dsl_doc)
    per_row: list[dict] = []
    missing: list[str] = []
    value_mismatch: list[str] = []
    placeholder_in_dsl: list[str] = []
    for row in rows:
        matches = _match_description_to_dsl(row, targets)
        if not matches:
            per_row.append({
                "kpi": row["kpi_name"], "value": row["value"],
                "verdict": "FAIL",
                "reason": "no matching aggregate_target in DSL",
            })
            missing.append(row["kpi_name"])
            continue
        # Use the first match for the value comparison; report multiple.
        target = matches[0]
        target_val = target.get("expected_value")
        target_tol = target.get("tolerance") or tolerance
        if target_val is None:
            per_row.append({
                "kpi": row["kpi_name"], "value": row["value"],
                "verdict": "FAIL",
                "reason": (f"matched DSL element {target['id']!r} has no "
                           "expected_value"),
            })
            value_mismatch.append(row["kpi_name"])
            continue
        # Normalize the description's value and CI to the DSL element's
        # natural units before comparing, so % vs fraction and days vs hours
        # don't read as numerical disagreements when they are actually unit
        # mismatches.
        norm_value, norm_lo, norm_hi = _normalize_value_to_dsl_units(
            row["value"], row.get("unit", ""),
            row.get("ci_low"), row.get("ci_high"), target,
        )
        ok_ci = _value_within_ci(target_val, norm_lo, norm_hi, target_tol)
        rel_err = (abs(target_val - norm_value) /
                   max(abs(norm_value), 1e-9))
        ok_tol = rel_err <= target_tol
        ok_overall = ok_ci and (norm_lo is not None or ok_tol)
        if target.get("assumed_default") and not (row.get("ci_low") is not None
                                                    and target_val == row["value"]):
            placeholder_in_dsl.append(target["id"])
        if ok_overall:
            per_row.append({
                "kpi": row["kpi_name"], "value": row["value"],
                "ci": (row.get("ci_low"), row.get("ci_high")),
                "dsl_id": target["id"], "dsl_value": target_val,
                "verdict": ("WARN" if target.get("assumed_default") else "PASS"),
                "reason": (
                    f"matched {target['id']}; DSL expected_value={target_val} "
                    f"within CI" if row.get("ci_low") is not None else
                    f"matched {target['id']}; rel_err={rel_err:.3f} ≤ tol={target_tol}"
                ) + (" (placeholder: assumed_default=true)" if target.get("assumed_default") else ""),
            })
        else:
            per_row.append({
                "kpi": row["kpi_name"], "value": row["value"],
                "ci": (row.get("ci_low"), row.get("ci_high")),
                "dsl_id": target["id"], "dsl_value": target_val,
                "verdict": "FAIL",
                "reason": (
                    f"DSL expected_value={target_val} outside description's "
                    f"value/CI (rel_err={rel_err:.3f}; tolerance={target_tol})"
                ),
            })
            value_mismatch.append(row["kpi_name"])

    n_total = len(per_row)
    n_pass = sum(1 for r in per_row if r["verdict"] == "PASS")
    n_warn = sum(1 for r in per_row if r["verdict"] == "WARN")
    n_fail = sum(1 for r in per_row if r["verdict"] == "FAIL")

    # Dynamics-mechanism cross-check (v5.3). Surfaces description-language
    # vs DSL dynamics-type mismatches. A FAIL here is an A4-blocking issue
    # in the same way KPI coverage failures are; an INFO is advisory.
    dynamics_report = check_dynamics_coverage(docx_path, dsl_doc)
    dynamics_fail = dynamics_report["status"] == "FAIL"

    overall_fail = n_fail > 0 or dynamics_fail
    status = "PASS" if not overall_fail else "FAIL"
    if status == "PASS":
        summary = (f"{n_total} reference rows: {n_pass} matched cleanly, "
                   f"{n_warn} matched against DSL placeholder (assumed_default). "
                   f"Dynamics coverage: PASS.")
    else:
        parts: list[str] = []
        if n_fail > 0:
            parts.append(
                f"{n_fail} of {n_total} reference rows failed coverage "
                f"({len(missing)} without a DSL aggregate_target, "
                f"{len(value_mismatch)} where DSL value disagrees with "
                "description)")
        if dynamics_fail:
            parts.append(
                f"{len(dynamics_report['findings'])} dynamics-coverage "
                f"finding(s) at FAIL severity")
        summary = " · ".join(parts) + "."
    return {
        "status": status, "summary": summary,
        "per_row": per_row,
        "missing_from_dsl": missing,
        "value_mismatch": value_mismatch,
        "placeholder_in_dsl": placeholder_in_dsl,
        "n_total": n_total, "n_pass": n_pass,
        "n_warn": n_warn, "n_fail": n_fail,
        "dynamics": dynamics_report,
    }


def render(result: dict) -> str:
    lines = ["=" * 72,
             "  Description→DSL reference-data coverage",
             "=" * 72,
             f"  Overall: {result['status']}",
             f"  {result['summary']}", ""]
    for r in result["per_row"]:
        v = r["verdict"]
        mark = {"PASS": "  ✓", "WARN": "  ⚠", "FAIL": "  ✗"}.get(v, "  ?")
        lines.append(f"{mark} {r['kpi']:35s} value={r['value']}  → {r['reason']}")
    if result["placeholder_in_dsl"]:
        lines.append("")
        lines.append(f"  DSL placeholders (assumed_default=true) — promote to real values: "
                     f"{', '.join(result['placeholder_in_dsl'])}")
    # Dynamics-mechanism scan
    dyn = result.get("dynamics") or {}
    findings = dyn.get("findings", [])
    if findings or dyn.get("matches"):
        lines.append("")
        lines.append("  Dynamics-mechanism scan:")
        m = dyn.get("matches", {})
        for cat in ("continuous_hazard", "discrete_check", "event_driven"):
            phrases = m.get(cat, [])
            decl = dyn.get("declared_types", {}).get(cat, [])
            phrase_str = (", ".join(repr(p) for p in phrases[:3])
                          + ("..." if len(phrases) > 3 else "")) if phrases else "(none)"
            decl_str = ", ".join(decl) if decl else "(none)"
            lines.append(f"    {cat}: description matches={phrase_str}; "
                         f"DSL declarations={decl_str}")
        for f in findings:
            mark = "  ✗" if f["severity"] == "FAIL" else "  ⚠"
            lines.append(f"{mark} {f['message']}")
    lines.append("=" * 72)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Description→DSL reference-data coverage gate: every "
                    "numeric KPI row in the description's reference tables "
                    "must be present as a DSL aggregate_target with a value "
                    "consistent with the description.")
    p.add_argument("--docx", required=True, help="path to description.docx")
    p.add_argument("--dsl", required=True, help="path to my_dsl.json")
    p.add_argument("--tolerance", type=float, default=0.10,
                   help="fallback relative tolerance when description row has "
                        "no CI and DSL element has no declared tolerance")
    args = p.parse_args(argv)

    try:
        with open(args.dsl) as f:
            dsl_doc = json.load(f)
    except OSError as exc:
        print(f"[desc-dsl] cannot read DSL: {exc}", file=sys.stderr); return 3
    result = check_description_dsl_coverage(args.docx, dsl_doc,
                                             tolerance=args.tolerance)
    print(render(result))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
