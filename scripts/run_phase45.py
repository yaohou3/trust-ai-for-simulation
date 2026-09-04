#!/usr/bin/env python3
"""run_phase45.py — Stage D CLI: run the Phase 4.5 what-if scenario harness.

Usage (from anywhere; artifacts referenced by path):
    python3 run_phase45.py --sim my_sim.py [--config config.json]
        [--seeds 1,2,3,4,5,6,7,8] [--include-canonical]
        [--out vvuq_out]

The compliance_spec is read from the sim's DEFAULT_CONFIG (the codegen brief
requires it to be embedded there). Results are printed per-scenario and the
provenance-weighted aggregate verdict is written to
<out>/phase45_results.json — which the Stage E CLI (decision_validity.py)
can then read, keeping the pipeline free of hand-transcribed verdicts.
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import os
import sys


def _load_sim(path: str):
    spec = importlib.util.spec_from_file_location("phase45_sim", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 4.5 scenario harness CLI.")
    p.add_argument("--sim", required=True, help="path to my_sim.py")
    p.add_argument("--config", default=None,
                   help="optional JSON config; defaults to the sim's DEFAULT_CONFIG")
    p.add_argument("--seeds", default="1,2,3,4,5,6,7,8",
                   help="comma-separated seed list")
    p.add_argument("--include-canonical", action="store_true",
                   help="also synthesize the canonical claim library from the DSL")
    p.add_argument("--out", default="vvuq_out", help="output directory")
    args = p.parse_args(argv)

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import semantic_scenarios as ss

    sim = _load_sim(args.sim)
    run_simulation = getattr(sim, "run_simulation", None)
    if run_simulation is None:
        print("[phase45] sim exposes no run_simulation(config); cannot run.",
              file=sys.stderr)
        return 3
    if args.config:
        with open(args.config) as f:
            base_config = json.load(f)
    else:
        base_config = dict(getattr(sim, "DEFAULT_CONFIG", None)
                           or sim.default_config())
    compliance_spec = base_config.get("compliance_spec") or {}
    if not compliance_spec:
        print("[phase45] config carries no compliance_spec — nothing to test.",
              file=sys.stderr)
        return 3

    seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    results = ss.run_phase4_5(
        base_config=base_config,
        compliance_spec=compliance_spec,
        run_simulation=run_simulation,
        seeds=seeds,
        include_canonical=args.include_canonical,
    )
    aggregate = ss.aggregate_verdict(results)

    for r in results:
        tier = "advisory" if r.advisory else "authoritative"
        print(f"  [{r.verdict:12s}] {r.name} :: {r.metric} "
              f"({r.provenance}/{tier}) — {r.message}")
    print(f"\nPhase 4.5 aggregate verdict: {aggregate} "
          f"({len(results)} scenario results, seeds={seeds}, "
          f"canonical={'on' if args.include_canonical else 'off'})")

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, "phase45_results.json")
    with open(out_path, "w") as f:
        json.dump({
            "aggregate_verdict": aggregate,
            "seeds": seeds,
            "include_canonical": args.include_canonical,
            "results": [dataclasses.asdict(r) for r in results],
        }, f, indent=2, default=str)
    print(f"Results written: {out_path}")
    return 0 if aggregate in ("PASS", "PASS_ADVISORY_ONLY", "PASS_NO_SCENARIOS") else 1


if __name__ == "__main__":
    sys.exit(main())
