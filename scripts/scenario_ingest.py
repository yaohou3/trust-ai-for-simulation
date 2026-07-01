"""
scenario_ingest.py — the connector from the LLM scenario generator to the harness.

WHY THIS EXISTS
---------------
Writing the generator PROMPT (the JSX "Scenarios" step / scenGenP) is not enough.
The generator emits typed candidate scenarios as text; something has to capture
that text and load it into the DSL's compliance_spec, or run_phase4_5 reads an
empty spec and returns PASS_NO_SCENARIOS no matter how good the candidates were.
This tool is that missing wire.

WHAT IT DOES
------------
1. Parses the generator's response for blocks fenced by
       ===SCENARIO_JSON_START===
       { ...typed scenario... }
       ===SCENARIO_JSON_END===
2. Normalizes each block under the same integrity rules the harness enforces:
     • provenance is forced to "auto_generated" unless the block is explicitly
       "sme_approved": true (a human vouched) — an LLM cannot self-promote;
     • auto_generated scenarios are direction-only — any magnitude/min_effect_size
       is stripped (nobody certified the size);
     • a scenario whose direction is not up/down/unchanged is REJECTED as
       NOT_A_CLAIM (a test that asserts nothing is not a test).
3. Routes each scenario to the right compliance_spec section by scenario_category.
4. Merges into the DSL's compliance_spec (de-duped by name) — or, by default,
   writes a sidecar `generated_scenarios.json` so you can review before merging.

USAGE
-----
    # review-only: write a sidecar, don't touch the DSL
    python scenario_ingest.py --from generator_response.txt --dsl my_dsl.json

    # merge the candidates straight into the DSL's compliance_spec
    python scenario_ingest.py --from generator_response.txt --dsl my_dsl.json --merge

After merging, run Phase 4.5 normally — the scenarios appear as the advisory
(auto_generated) tier. Promote any scenario to authoritative weight by editing
its block to "provenance": "sme_approved" after a human has approved the claim.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# Map a scenario_category to the compliance_spec section run_phase4_5 reads.
_CATEGORY_TO_SECTION = {
    "emergent_pattern":  "semantic_scenarios",
    "coupling":          "coupling_rules",
    "control_policy":    "control_policies",
    "policy_threshold":  "policy_thresholds",
    "escalation":        "escalations",
    "workload_response": "workload_responses",
}
_VALID_DIRECTIONS = {"up", "down", "unchanged"}

_BLOCK_RE = re.compile(
    r"===SCENARIO_JSON_START===\s*(\{.*?\})\s*===SCENARIO_JSON_END===",
    re.DOTALL,
)


def parse_scenarios(text: str) -> tuple[list[dict], list[str]]:
    """Extract and json-decode every fenced scenario block. Returns
    (scenarios, warnings)."""
    scenarios: list[dict] = []
    warnings: list[str] = []
    blocks = _BLOCK_RE.findall(text or "")
    if not blocks:
        warnings.append("No ===SCENARIO_JSON_START===/END=== blocks found in input.")
    for i, raw in enumerate(blocks, 1):
        try:
            scenarios.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            warnings.append(f"Block {i}: invalid JSON ({exc}); skipped.")
    return scenarios, warnings


def _directions(scn: dict) -> list[str]:
    """Every direction the scenario asserts (per-metric + claim-level)."""
    out: list[str] = []
    for m in scn.get("metrics", []) or []:
        if isinstance(m, dict) and m.get("direction"):
            out.append(str(m["direction"]).lower())
    exp = scn.get("expectation") or {}
    if exp.get("direction"):
        out.append(str(exp["direction"]).lower())
    return out


def normalize(scn: dict) -> tuple[dict | None, list[str]]:
    """Apply the harness integrity rules. Returns (normalized | None, warnings).
    None means the scenario was rejected."""
    warnings: list[str] = []
    name = scn.get("name")
    if not name or not isinstance(name, str):
        return None, ["Rejected: scenario has no name."]

    cat = scn.get("scenario_category") or "emergent_pattern"
    if cat not in _CATEGORY_TO_SECTION:
        warnings.append(f"{name}: unknown scenario_category '{cat}' → emergent_pattern.")
        cat = "emergent_pattern"
        scn = {**scn, "scenario_category": cat}

    dirs = _directions(scn)
    if not dirs:
        return None, [f"Rejected '{name}': no direction declared (NOT_A_CLAIM)."]
    bad = [d for d in dirs if d not in _VALID_DIRECTIONS]
    if bad:
        return None, [f"Rejected '{name}': non-falsifiable direction {bad} "
                      "(use up/down/unchanged; 'any' asserts nothing)."]

    # Provenance: an LLM-proposed scenario is advisory unless a human approved it.
    sme = scn.get("sme_approved") is True or (
        str(scn.get("provenance") or scn.get("source") or "").lower() == "sme_approved")
    scn = dict(scn)
    scn["provenance"] = "sme_approved" if sme else "auto_generated"

    # Magnitude licensing: auto_generated is direction-only.
    if scn["provenance"] == "auto_generated":
        dropped = False
        exp = scn.get("expectation")
        if isinstance(exp, dict) and exp.get("min_effect_size") is not None:
            exp = {k: v for k, v in exp.items() if k != "min_effect_size"}
            scn["expectation"] = exp
            dropped = True
        new_metrics = []
        for m in scn.get("metrics", []) or []:
            if isinstance(m, dict) and "min_effect_size" in m:
                m = {k: v for k, v in m.items() if k != "min_effect_size"}
                dropped = True
            new_metrics.append(m)
        if new_metrics:
            scn["metrics"] = new_metrics
        if "expected_magnitude" in scn:
            scn.pop("expected_magnitude")
            dropped = True
        if dropped:
            warnings.append(f"{name}: magnitude stripped (auto_generated is "
                            "direction-only).")
    return scn, warnings


def normalize_all(scenarios: list[dict]) -> tuple[list[dict], list[str]]:
    kept: list[dict] = []
    warnings: list[str] = []
    for scn in scenarios:
        norm, w = normalize(scn)
        warnings.extend(w)
        if norm is not None:
            kept.append(norm)
    return kept, warnings


def group_by_section(scenarios: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for scn in scenarios:
        section = _CATEGORY_TO_SECTION.get(
            scn.get("scenario_category", "emergent_pattern"), "semantic_scenarios")
        out.setdefault(section, []).append(scn)
    return out


def merge_into_compliance_spec(dsl: dict, grouped: dict[str, list[dict]]) -> tuple[int, int]:
    """Append scenarios into the DSL's compliance_spec, de-duped by name.
    Returns (added, skipped_duplicates)."""
    cs = dsl.setdefault("compliance_spec", {})
    added = skipped = 0
    for section, scns in grouped.items():
        existing = cs.setdefault(section, [])
        have = {s.get("name") for s in existing if isinstance(s, dict)}
        for scn in scns:
            if scn.get("name") in have:
                skipped += 1
                continue
            existing.append(scn)
            have.add(scn.get("name"))
            added += 1
    return added, skipped


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Ingest LLM-generated scenario blocks into the DSL / a sidecar.")
    p.add_argument("--from", dest="src", required=True,
                   help="file containing the generator's response text")
    p.add_argument("--dsl", required=True, help="path to the DSL JSON")
    p.add_argument("--merge", action="store_true",
                   help="merge into the DSL's compliance_spec (default: write a "
                        "sidecar generated_scenarios.json for review)")
    p.add_argument("--out", default="generated_scenarios.json",
                   help="sidecar path when not merging (default: generated_scenarios.json)")
    args = p.parse_args(argv)

    try:
        with open(args.src) as f:
            text = f.read()
    except OSError as exc:
        print(f"[ingest] cannot read {args.src}: {exc}", file=sys.stderr)
        return 2

    raw, parse_warn = parse_scenarios(text)
    kept, norm_warn = normalize_all(raw)
    for w in parse_warn + norm_warn:
        print(f"[ingest] {w}")

    if not kept:
        print("[ingest] no valid scenarios to ingest. Nothing written.")
        return 1

    grouped = group_by_section(kept)
    n_auto = sum(1 for s in kept if s.get("provenance") == "auto_generated")
    n_sme = len(kept) - n_auto
    print(f"[ingest] {len(kept)} valid scenario(s): {n_auto} auto_generated "
          f"(advisory), {n_sme} sme_approved (authoritative).")
    for section, scns in grouped.items():
        print(f"[ingest]   {section}: {len(scns)} → {[s['name'] for s in scns]}")

    if args.merge:
        with open(args.dsl) as f:
            dsl = json.load(f)
        added, skipped = merge_into_compliance_spec(dsl, grouped)
        with open(args.dsl, "w") as f:
            json.dump(dsl, f, indent=2)
        print(f"[ingest] merged into {args.dsl}: {added} added, {skipped} "
              "duplicate(s) skipped.")
        print("[ingest] Re-run Phase 4.5 — the scenarios now appear as the "
              "advisory tier (or authoritative if sme_approved).")
    else:
        with open(args.out, "w") as f:
            json.dump(grouped, f, indent=2)
        print(f"[ingest] wrote sidecar {args.out} (review, then re-run with "
              "--merge to apply).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
