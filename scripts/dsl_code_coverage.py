"""dsl_code_coverage.py — DSL→code coverage manifest preflight.

WHY THIS EXISTS
---------------
The Code Alignment Review at the end of Stage B is an LLM judgement step.
Empirical evidence from the ICU case study showed that LLM reviewers can be
persuaded by defensible-sounding rationalizations to ratify coverage gaps —
behaviours declared in the DSL but quietly omitted from the simulation
("preemption is implicitly honoured by priority queueing"). The reviewer is the
same LLM family that wrote the code; reviewer-generator collusion is a
documented failure mode.

This module is the deterministic counterpart to that review. It enumerates
every declared DSL element and requires the simulation's MANIFEST to enumerate
it back with an explicit implementation status. Missing elements, or elements
marked NOT_IMPLEMENTED without justification, BLOCK the pipeline at Stage C
preflight. Because the check is enumeration + dictionary lookup, it cannot
collude with the generator.

MANIFEST CONTRACT
-----------------
The simulation's MANIFEST (read via ``sim_module.manifest()`` or
``sim_module.MANIFEST``) must include::

    "declared_element_implementation_status": {
        "<element_id>": {
            "status": "FULL" | "PARTIAL" | "NOT_IMPLEMENTED" | "DEFERRED_TO_DSL",
            "code_citation": "my_sim.py:<line range>"  # required for FULL/PARTIAL
            "gap_description": "..."                   # required for PARTIAL
            "justification": "..."                     # required for NOT_IMPLEMENTED / DEFERRED_TO_DSL
        },
        ...
    }

USAGE
-----
    from dsl_code_coverage import check_dsl_code_coverage
    result = check_dsl_code_coverage(dsl_doc, sim_module)
    if result["status"] != "PASS":
        # BLOCK Stage C
        ...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

# Status values the manifest may declare for any declared element.
_FULL = "FULL"
_PARTIAL = "PARTIAL"
_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
_DEFERRED = "DEFERRED_TO_DSL"
_VALID_STATUSES = {_FULL, _PARTIAL, _NOT_IMPLEMENTED, _DEFERRED}


def _enumerate_dsl_elements(dsl_doc: dict) -> list[dict]:
    """Return a uniform list of declared DSL elements: one dict per element
    with at least ``id`` and ``element_type``.

    Reads both the ``dsl_elements`` list (canonical) and any compliance_spec
    sections that may carry stand-alone declarations the dsl_elements list
    does not duplicate.
    """
    elements: list[dict] = []
    seen_ids: set[str] = set()
    for e in (dsl_doc or {}).get("dsl_elements", []) or []:
        if not isinstance(e, dict):
            continue
        eid = e.get("id")
        if not eid:
            continue
        elements.append({
            "id": eid,
            "element_type": e.get("element_type", "unknown"),
            "name": e.get("name", ""),
        })
        seen_ids.add(eid)
    cs = (dsl_doc or {}).get("compliance_spec", {}) or {}
    # Compliance-spec sections frequently re-house element data with source_ids.
    # Pick up any source_id we did not already see in dsl_elements.
    for section_name, section in cs.items():
        if not isinstance(section, list):
            continue
        for entry in section:
            if not isinstance(entry, dict):
                continue
            for sid in entry.get("source_ids", []) or []:
                if sid in seen_ids:
                    continue
                elements.append({
                    "id": sid,
                    "element_type": section_name,
                    "name": entry.get("name", ""),
                })
                seen_ids.add(sid)
    return elements


def _read_manifest(sim_module) -> dict:
    """Pull the manifest dict from ``sim_module``. Accepts:

    1. A ``manifest()`` callable on the module that returns a dict.
    2. A ``MANIFEST`` attribute that IS a dict.

    Returns an empty dict if neither is present (which the caller will report
    as MISSING manifest)."""
    fn = getattr(sim_module, "manifest", None)
    if callable(fn):
        try:
            m = fn()
            if isinstance(m, dict):
                return m
        except Exception:
            pass
    m = getattr(sim_module, "MANIFEST", None)
    if isinstance(m, dict):
        return m
    return {}


def check_dsl_code_coverage(dsl_doc: dict, sim_module) -> dict:
    """Compare declared DSL elements against the simulation's manifest.

    Returns a result dict with:
        "status": "PASS" | "FAIL"
        "summary": one-line summary
        "per_element": [{id, element_type, manifest_status, verdict, reason}, ...]
        "missing_elements": [ids not in manifest at all]
        "unjustified_not_implemented": [ids with status NOT_IMPLEMENTED but no justification]
        "unjustified_partial": [ids with status PARTIAL but no gap_description]
        "unknown_status": [ids with a status outside the allowed set]
        "missing_manifest_section": True iff the manifest has no
            ``declared_element_implementation_status`` section at all.
    """
    elements = _enumerate_dsl_elements(dsl_doc)
    manifest = _read_manifest(sim_module)
    coverage = manifest.get("declared_element_implementation_status", {}) or {}

    missing_section = not isinstance(coverage, dict) or not coverage
    per_element: list[dict] = []
    missing_elements: list[str] = []
    unjustified_ni: list[str] = []
    unjustified_partial: list[str] = []
    unknown_status: list[str] = []

    for el in elements:
        eid = el["id"]
        entry = coverage.get(eid) if isinstance(coverage, dict) else None
        if entry is None or not isinstance(entry, dict):
            per_element.append({
                "id": eid, "element_type": el["element_type"],
                "manifest_status": "MISSING", "verdict": "FAIL",
                "reason": "no entry in manifest.declared_element_implementation_status",
            })
            missing_elements.append(eid)
            continue
        status = str(entry.get("status", "")).upper().strip()
        if status not in _VALID_STATUSES:
            per_element.append({
                "id": eid, "element_type": el["element_type"],
                "manifest_status": status or "MISSING", "verdict": "FAIL",
                "reason": (f"status {status!r} not in "
                           f"{{FULL, PARTIAL, NOT_IMPLEMENTED, DEFERRED_TO_DSL}}"),
            })
            unknown_status.append(eid)
            continue
        if status == _FULL:
            if not entry.get("code_citation"):
                per_element.append({
                    "id": eid, "element_type": el["element_type"],
                    "manifest_status": status, "verdict": "FAIL",
                    "reason": "FULL requires a code_citation (line range)",
                })
                continue
            per_element.append({
                "id": eid, "element_type": el["element_type"],
                "manifest_status": status, "verdict": "PASS", "reason": "",
            })
        elif status == _PARTIAL:
            gap = entry.get("gap_description") or ""
            if not gap.strip():
                per_element.append({
                    "id": eid, "element_type": el["element_type"],
                    "manifest_status": status, "verdict": "FAIL",
                    "reason": "PARTIAL requires a non-empty gap_description",
                })
                unjustified_partial.append(eid)
                continue
            per_element.append({
                "id": eid, "element_type": el["element_type"],
                "manifest_status": status, "verdict": "WARN",
                "reason": f"PARTIAL: {gap[:120]}",
            })
        elif status in (_NOT_IMPLEMENTED, _DEFERRED):
            justif = entry.get("justification") or ""
            if not justif.strip():
                per_element.append({
                    "id": eid, "element_type": el["element_type"],
                    "manifest_status": status, "verdict": "FAIL",
                    "reason": f"{status} requires a justification",
                })
                unjustified_ni.append(eid)
                continue
            per_element.append({
                "id": eid, "element_type": el["element_type"],
                "manifest_status": status, "verdict": "INFO",
                "reason": f"{status}: {justif[:120]}",
            })

    n_total = len(elements)
    n_pass = sum(1 for p in per_element if p["verdict"] == "PASS")
    n_warn = sum(1 for p in per_element if p["verdict"] == "WARN")
    n_info = sum(1 for p in per_element if p["verdict"] == "INFO")
    n_fail = sum(1 for p in per_element if p["verdict"] == "FAIL")

    overall = "PASS" if n_fail == 0 and not missing_section else "FAIL"
    if missing_section:
        summary = ("MANIFEST has no declared_element_implementation_status "
                   "section — required for Stage C entry.")
    elif overall == "PASS":
        summary = (f"{n_total} elements: {n_pass} FULL, {n_warn} PARTIAL, "
                   f"{n_info} NOT_IMPLEMENTED/DEFERRED (justified).")
    else:
        summary = (f"{n_fail} of {n_total} elements failed coverage: "
                   f"{len(missing_elements)} missing from manifest, "
                   f"{len(unjustified_ni)} NOT_IMPLEMENTED unjustified, "
                   f"{len(unjustified_partial)} PARTIAL with no gap_description, "
                   f"{len(unknown_status)} with unrecognised status.")
    return {
        "status": overall,
        "summary": summary,
        "per_element": per_element,
        "missing_elements": missing_elements,
        "unjustified_not_implemented": unjustified_ni,
        "unjustified_partial": unjustified_partial,
        "unknown_status": unknown_status,
        "missing_manifest_section": missing_section,
        "n_total": n_total,
        "n_pass": n_pass, "n_warn": n_warn,
        "n_info": n_info, "n_fail": n_fail,
    }


def render(result: dict) -> str:
    """Plain-text rendering for the user, suitable for the trust-layer banner."""
    lines = [
        "=" * 72,
        "  DSL→Code coverage preflight",
        "=" * 72,
        f"  Overall: {result['status']}",
        f"  {result['summary']}",
    ]
    if result["missing_elements"]:
        lines.append("")
        lines.append(f"  Missing from manifest ({len(result['missing_elements'])}):")
        for eid in result["missing_elements"][:20]:
            lines.append(f"    - {eid}")
        if len(result["missing_elements"]) > 20:
            lines.append(f"    … {len(result['missing_elements']) - 20} more")
    if result["unjustified_not_implemented"]:
        lines.append("")
        lines.append("  NOT_IMPLEMENTED without justification:")
        for eid in result["unjustified_not_implemented"]:
            lines.append(f"    - {eid}")
    if result["unjustified_partial"]:
        lines.append("")
        lines.append("  PARTIAL with no gap_description:")
        for eid in result["unjustified_partial"]:
            lines.append(f"    - {eid}")
    if result["unknown_status"]:
        lines.append("")
        lines.append("  Unrecognised status values:")
        for eid in result["unknown_status"]:
            lines.append(f"    - {eid}")
    lines.append("=" * 72)
    return "\n".join(lines)


def _load_sim_module(sim_path: str):
    import importlib.util as iu
    spec = iu.spec_from_file_location("dsl_code_coverage_sim", sim_path)
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="DSL→code coverage preflight: enumerate DSL elements, "
                    "require sim MANIFEST to declare implementation status "
                    "for each, BLOCK if any element is missing or unjustified.")
    p.add_argument("--dsl", required=True, help="path to my_dsl.json")
    p.add_argument("--sim", required=True, help="path to my_sim.py")
    args = p.parse_args(argv)

    try:
        with open(args.dsl) as f:
            dsl_doc = json.load(f)
    except OSError as exc:
        print(f"[coverage] cannot read DSL: {exc}", file=sys.stderr)
        return 3
    try:
        sim_module = _load_sim_module(args.sim)
    except Exception as exc:
        print(f"[coverage] cannot load sim: {exc}", file=sys.stderr)
        return 3
    result = check_dsl_code_coverage(dsl_doc, sim_module)
    print(render(result))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
