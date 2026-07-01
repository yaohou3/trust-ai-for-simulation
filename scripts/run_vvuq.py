"""
run_vvuq.py — Local Phase 0 runner for the Trustworthy-AI-Simulation framework.

Purpose
-------
Move the *deterministic* parts of the VVUQ pipeline off the LLM and onto your
own machine. This script replaces the "paste the code → get Phase 0 report"
round-trip (which costs thousands of tokens per run) with a local CLI call:

    python run_vvuq.py \\
        --sim    path/to/generated_sim.py \\
        --dsl    path/to/dsl.json \\
        --config path/to/config.json \\
        --out    path/to/report_dir

It does four things:

  1. Loads a DSL JSON the LLM emitted in step 1 (a list of dsl_elements),
     rebuilds DslElement objects, runs compliance_mapper →
     compliance_spec + CoverageReport.
  2. Runs coverage_validator (coverage / grounding / observability /
     vague-clause / routing-prob sums). Outputs NEEDS_REVISION / BLOCKED / PASS.
  3. Dynamically imports the user's generated simulation (must define
     run_simulation(config) → {trace, metrics, config}) and runs it.
  4. Calls phase0_vvuq.run_phase0(result) to execute the full Phase 0
     cascade — Layer A (A1–A8), Layer B (B01–B17), Process contracts
     (B18–B23), Semantic contracts (B24–B41), Layer C advisories.

Writes two files to --out:
  • phase0_report.json   — machine-readable: every check, severity, message, details.
  • phase0_summary.md    — compact markdown (<2 kB) suited for pasting back
                           to Claude for Phase 5 / Final Synthesis.

Only the Phase 5 plausibility review and final synthesis still need an LLM.

CLI
---
Required:
  --sim    PATH   path to generated .py file exposing run_simulation(config)
  --dsl    PATH   path to dsl_elements JSON

Optional:
  --config PATH   path to JSON config to pass to run_simulation. If omitted,
                  a minimal default is built (seed=42, run_length=24*3600).
  --out    PATH   output directory. Defaults to ./vvuq_out.
  --skip-sim      skip the sim run and read a pre-computed result.json from
                  --result instead (useful for replaying without re-running).
  --result PATH   pre-computed run_simulation() result (used with --skip-sim).
  --quiet         suppress the verbose console echo of every check.

Usage example (minimal):
    python run_vvuq.py --sim ./my_sim.py --dsl ./my_dsl.json

Usage example (with custom config + explicit output dir):
    python run_vvuq.py --sim ./my_sim.py --dsl ./my_dsl.json \\
                       --config ./my_config.json --out ./vvuq_report
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

# Framework imports — resolve regardless of cwd by inserting THIS_DIR on path.
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

from dsl_schema import DslElement                          # noqa: E402
from compliance_mapper import map_dsl_to_compliance        # noqa: E402
from coverage_validator import validate                    # noqa: E402
import phase0_vvuq                                         # noqa: E402
from phase0_vvuq import run_phase0                         # noqa: E402
from vvuq_utils import CheckResult                         # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# DSL JSON → list[DslElement]
# ─────────────────────────────────────────────────────────────────────────────

def _load_dsl(path: str) -> list[DslElement]:
    """Load a DSL JSON file and rebuild DslElement objects.

    The JSON shape can be either:
      • a list of element dicts (top-level array), or
      • an object with a "dsl_elements" key (as emitted by the LLM's DSL step).
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    raw_elements: list[dict]
    if isinstance(data, list):
        raw_elements = data
    elif isinstance(data, dict) and "dsl_elements" in data:
        raw_elements = data["dsl_elements"]
    else:
        raise ValueError(
            f"--dsl JSON at {path} must be a list or an object with "
            "'dsl_elements' key.")

    # DslElement has many optional fields; pass through only the keys that
    # correspond to dataclass fields so unexpected keys don't blow it up.
    valid_fields = set(DslElement.__dataclass_fields__.keys())
    elements: list[DslElement] = []
    for i, el in enumerate(raw_elements):
        if not isinstance(el, dict):
            raise ValueError(f"dsl_elements[{i}] is not a dict.")
        kwargs = {k: v for k, v in el.items() if k in valid_fields}
        missing = {"id", "element_type", "description"} - kwargs.keys()
        if missing:
            raise ValueError(
                f"dsl_elements[{i}] missing required field(s): {sorted(missing)}")
        elements.append(DslElement(**kwargs))
    return elements


def _load_config(path: str | None) -> dict | None:
    """Load a JSON config override, or None when none was supplied.

    Returning None (rather than a hard-coded {seed, run_length, warmup})
    signals 'no caller override' to the runner, which then uses the SIM'S
    OWN DEFAULT_CONFIG. A hard-coded fallback here silently overrode every
    sim's declared run_length/warmup with 24h/no-warmup."""
    if path is None:
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_sim(path: str):
    """Import the user's generated simulation module by file path.

    The module MUST expose a callable `run_simulation(config) -> dict`
    per the v5.2 Trace Contract.
    """
    mod_name = "_user_sim_" + str(abs(hash(os.path.abspath(path))))
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load sim module from {path}")
    mod = importlib.util.module_from_spec(spec)
    # Make sure the sim's own internal imports can find sibling files.
    sim_dir = os.path.dirname(os.path.abspath(path))
    if sim_dir not in sys.path:
        sys.path.insert(0, sim_dir)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "run_simulation"):
        raise AttributeError(
            f"{path} does not define run_simulation(config). "
            "The generated code must follow the v5.2 Trace Contract.")
    # Also surface the sim's declared defaults (constant DEFAULT_CONFIG or a
    # default_config() factory) so the runner can honor them when no --config
    # override is given.
    default_config = getattr(mod, "DEFAULT_CONFIG", None)
    if default_config is None:
        fn = getattr(mod, "default_config", None)
        default_config = fn() if callable(fn) else {}
    return mod.run_simulation, (default_config or {})


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────

def _tally(results: list[CheckResult]) -> dict[str, int]:
    c = Counter(r.severity for r in results)
    return {k: c.get(k, 0) for k in ("PASS", "INFO", "WARN", "FAIL", "BLOCK")}


def _gate_decision(tally: dict[str, int],
                   layer_a_block: bool,
                   layer_b_fail: bool,
                   process_fail: bool,
                   semantic_fail: bool) -> str:
    """Translate the B24–B41 gate rules into a single verdict string."""
    if layer_a_block:
        return "BLOCKED (structural)"
    if layer_b_fail:
        return "BLOCKED (contract)"
    if process_fail:
        return "BLOCKED (process contract)"
    if semantic_fail:
        return "BLOCKED (semantic contract)"
    if tally["FAIL"] > 0:
        return "BLOCKED (other FAIL)"
    return "OPEN"


def _classify_check(check_name: str) -> str:
    """Return which layer a check belongs to for gate accounting."""
    name = check_name.upper()
    if name.startswith("A") and any(name.startswith(f"A{i}") for i in range(1, 9)):
        return "A"
    if name.startswith("B"):
        try:
            num = int("".join(ch for ch in name[1:4] if ch.isdigit()))
        except ValueError:
            return "other"
        if 1 <= num <= 17:
            return "B"
        if 18 <= num <= 23:
            return "BEXT_PROC"
        if 24 <= num <= 41:
            return "BEXT_SEM"
    return "other"


def _phase0_json(results: list[CheckResult]) -> dict:
    return {
        "results": [
            {
                "severity":   r.severity,
                "check_name": r.check_name,
                "message":    r.message,
                "details":    r.details or {},
            }
            for r in results
        ],
        "tally": _tally(results),
    }


def _validation_json(vr) -> dict:
    return {
        "status": vr.status,
        "coverage_fraction": vr.coverage_fraction,
        "errors": vr.errors,
        "warnings": vr.warnings,
        "observability_gaps": vr.observability_gaps,
        "grounding_errors": vr.grounding_errors,
    }


def _compact_markdown(
    validation_json: dict,
    phase0_json: dict,
    gate: str,
    dsl_element_count: int,
    sim_path: str,
    elapsed_s: float,
) -> str:
    p = phase0_json["tally"]
    v = validation_json
    # Keep the FAIL/WARN list short — paste-back economy.
    fails = [r for r in phase0_json["results"] if r["severity"] in ("FAIL", "BLOCK")]
    warns = [r for r in phase0_json["results"] if r["severity"] == "WARN"]

    lines: list[str] = []
    lines.append("# VVUQ Phase 0 — local report")
    lines.append("")
    lines.append(f"**Sim:** `{os.path.basename(sim_path)}` · "
                 f"**DSL elements:** {dsl_element_count} · "
                 f"**Wall-clock:** {elapsed_s:.1f}s")
    lines.append("")
    lines.append("## Pre-code validator")
    lines.append(f"- Status: **{v['status']}** · "
                 f"Coverage: **{v['coverage_fraction']:.0%}**")
    if v["errors"]:
        lines.append(f"- Errors ({len(v['errors'])}): " +
                     "; ".join(v["errors"][:3]) +
                     ("…" if len(v["errors"]) > 3 else ""))
    if v["grounding_errors"]:
        lines.append(f"- Grounding errors: {len(v['grounding_errors'])}")
    if v["observability_gaps"]:
        lines.append(f"- Observability gaps: {len(v['observability_gaps'])}")
    if v["warnings"]:
        lines.append(f"- Warnings: {len(v['warnings'])}")
    lines.append("")
    lines.append("## Phase 0 tally")
    lines.append(f"- **Gate:** {gate}")
    lines.append(f"- PASS {p['PASS']} · INFO {p['INFO']} · WARN {p['WARN']} · "
                 f"FAIL {p['FAIL']} · BLOCK {p['BLOCK']}")
    lines.append("")
    if fails:
        lines.append(f"## FAIL / BLOCK ({len(fails)})")
        for r in fails[:15]:
            msg = r["message"]
            if len(msg) > 220:
                msg = msg[:220] + "…"
            lines.append(f"- **[{r['severity']}] {r['check_name']}** — {msg}")
        if len(fails) > 15:
            lines.append(f"- … ({len(fails) - 15} more — see phase0_report.json)")
        lines.append("")
    if warns:
        lines.append(f"## WARN ({len(warns)})")
        for r in warns[:10]:
            msg = r["message"]
            if len(msg) > 180:
                msg = msg[:180] + "…"
            lines.append(f"- **{r['check_name']}** — {msg}")
        if len(warns) > 10:
            lines.append(f"- … ({len(warns) - 10} more — see phase0_report.json)")
        lines.append("")
    lines.append("## Next step")
    if gate == "OPEN":
        lines.append("Gate OPEN. Safe to hand off to Phase 1–3 (math/validation/UQ) "
                     "and Phase 5 (plausibility — LLM).")
    else:
        lines.append(f"Gate **{gate}**. Fix the FAIL/BLOCK items above before "
                     "running downstream phases.")
    lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Local VVUQ Phase 0 runner (deterministic; no LLM tokens).")
    p.add_argument("--sim", help="path to generated .py defining run_simulation(config)")
    p.add_argument("--dsl", required=True, help="path to DSL JSON")
    p.add_argument("--config", default=None, help="path to JSON config (optional)")
    p.add_argument("--out", default="./vvuq_out", help="output directory")
    p.add_argument("--skip-sim", action="store_true",
                   help="skip sim run; read pre-computed result from --result")
    p.add_argument("--result", default=None,
                   help="pre-computed run_simulation() result JSON (with --skip-sim)")
    p.add_argument("--quiet", action="store_true", help="suppress per-check console echo")
    args = p.parse_args(argv)

    if args.skip_sim:
        if not args.result:
            print("ERROR: --skip-sim requires --result PATH", file=sys.stderr)
            return 2
    else:
        if not args.sim:
            print("ERROR: --sim PATH is required unless --skip-sim is used",
                  file=sys.stderr)
            return 2

    t0 = time.time()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Load DSL + build compliance_spec ─────────────────────────────────
    elements = _load_dsl(args.dsl)
    spec, coverage = map_dsl_to_compliance(elements)
    # ── 2. Pre-code validator ───────────────────────────────────────────────
    vr = validate(elements, spec, coverage)
    if not args.quiet:
        print(f"[validator] {vr.status}  coverage={vr.coverage_fraction:.0%}  "
              f"errors={len(vr.errors)}  warnings={len(vr.warnings)}  "
              f"obs_gaps={len(vr.observability_gaps)}")

    # ── 3. Run the sim (or load a cached result) ────────────────────────────
    if args.skip_sim:
        config = _load_config(args.config) or {}
        config.setdefault("compliance_spec", spec)
        with open(args.result, "r", encoding="utf-8") as f:
            result = json.load(f)
    else:
        run_simulation, sim_default_config = _load_sim(args.sim)
        # Config precedence: an explicit --config wins; otherwise use the SIM'S
        # OWN DEFAULT_CONFIG (the sim author's intended run_length, warmup,
        # resources, etc.). Never override the sim's defaults with a hard-coded
        # placeholder — doing so silently ran every sim at 24h/no-warmup
        # regardless of its declared defaults.
        override = _load_config(args.config)
        config = dict(override) if override is not None else dict(sim_default_config or {})
        config.setdefault("compliance_spec", spec)
        if not args.quiet:
            src = "--config" if override is not None else "sim.DEFAULT_CONFIG"
            print(f"[sim] running {args.sim} … (config from {src})")
        result = run_simulation(config)
        if not isinstance(result, dict) or "trace" not in result:
            print("ERROR: run_simulation() did not return a dict with 'trace'.",
                  file=sys.stderr)
            return 3
        # Inject the user's sim so phase0_vvuq's A7 determinism check
        # can re-run it (otherwise A7 self-skips to INFO).
        phase0_vvuq.VVUQ_RUN_SIMULATION = run_simulation

    # Ensure the compliance_spec is in config so Phase 0 can read it.
    result.setdefault("config", {})
    result["config"].setdefault("compliance_spec", spec)

    # ── 4. Phase 0 ──────────────────────────────────────────────────────────
    p0_results = run_phase0(result)
    if not args.quiet:
        for r in p0_results:
            print(f"  {r}")

    # ── 5. Gate ─────────────────────────────────────────────────────────────
    layer_a_block = any(
        r.severity == "BLOCK" and _classify_check(r.check_name) == "A"
        for r in p0_results)
    layer_b_fail = any(
        r.severity == "FAIL" and _classify_check(r.check_name) == "B"
        for r in p0_results)
    process_fail = any(
        r.severity == "FAIL" and _classify_check(r.check_name) == "BEXT_PROC"
        for r in p0_results)
    semantic_fail = any(
        r.severity == "FAIL" and _classify_check(r.check_name) == "BEXT_SEM"
        for r in p0_results)
    gate = _gate_decision(_tally(p0_results),
                          layer_a_block, layer_b_fail, process_fail, semantic_fail)

    # ── 6. Emit report files ────────────────────────────────────────────────
    elapsed = time.time() - t0
    p0_json = _phase0_json(p0_results)
    vr_json = _validation_json(vr)
    full = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_s": elapsed,
        "sim": os.path.abspath(args.sim) if args.sim else None,
        "dsl": os.path.abspath(args.dsl),
        "dsl_element_count": len(elements),
        "compliance_spec_section_counts": {k: len(v) for k, v in spec.items() if v},
        "gate": gate,
        "pre_code_validator": vr_json,
        "phase0": p0_json,
    }
    report_path = out_dir / "phase0_report.json"
    summary_path = out_dir / "phase0_summary.md"
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(full, f, indent=2, default=str)

    md = _compact_markdown(vr_json, p0_json, gate,
                           dsl_element_count=len(elements),
                           sim_path=args.sim or "(cached result)",
                           elapsed_s=elapsed)
    with summary_path.open("w", encoding="utf-8") as f:
        f.write(md)

    print()
    print(f"Report: {report_path}")
    print(f"Summary: {summary_path}")
    print(f"GATE: {gate}")
    print(f"Tally: {p0_json['tally']}")
    return 0 if gate == "OPEN" else 1


if __name__ == "__main__":
    sys.exit(main())
