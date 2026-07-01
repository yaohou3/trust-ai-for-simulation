"""
run_validators.py — Single CLI entry point for Phase 1-4 validators.

Companion to run_vvuq.py (which runs Phase 0). This script imports
validation.py, dispatches to the requested phase(s), and writes
phase{N}_report.json + phase{N}_summary.md to disk.

Exit codes
----------
  0  All requested phases ended in PASS or WARN
  1  At least one phase ended in FAIL or BLOCK (self-heal candidate)
  2  Internal error (couldn't load sim, malformed args, etc.)

The non-zero exit on FAIL/BLOCK is the contract that
self_heal_orchestrator.py uses to detect a repair candidate.

Usage
-----
    python run_validators.py \
        --sim   path/to/generated_sim.py \
        --config path/to/config.json \      # optional; uses sim.DEFAULT_CONFIG otherwise
        --phase 1                            # one of: 0 1 2 3 4 all
        --out   ./vvuq_out

    python run_validators.py --sim X.py --phase all
        # runs phases 1,2,3,4 in sequence; writes four pairs of files

Phase 0 is delegated to run_vvuq.py because Phase 0 needs the DSL JSON and
the existing run_vvuq.py already implements the right pre-code +
phase0_vvuq.py + process_contracts.py + semantic_checks.py composition.
"""

from __future__ import annotations

import argparse
import importlib.util as iu
import json
import os
import subprocess
import sys
import time

from validation import (
    validate_phase1, validate_phase2, validate_phase3, validate_phase4,
    report_to_json, report_to_markdown, report_to_self_heal_payload,
    PhaseReport,
)


PHASES = {
    "1": validate_phase1,
    "2": validate_phase2,
    "3": validate_phase3,
    "4": validate_phase4,
}


def _load_sim(path: str):
    if not os.path.isfile(path):
        raise FileNotFoundError(f"sim file not found: {path}")
    spec = iu.spec_from_file_location("generated_sim", path)
    module = iu.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "run_simulation"):
        raise AttributeError(f"{path} does not expose run_simulation(config)")
    return module


def _load_config(path: str | None) -> dict | None:
    if not path:
        return None
    with open(path) as f:
        return json.load(f)


def _delegate_phase0(sim_path: str, dsl_path: str, config_path: str | None,
                     out_dir: str) -> int:
    """Delegate Phase 0 to the existing run_vvuq.py."""
    here = os.path.dirname(os.path.abspath(__file__))
    cmd = [sys.executable, os.path.join(here, "run_vvuq.py"),
           "--sim", sim_path, "--dsl", dsl_path,
           "--out", out_dir]
    if config_path:
        cmd += ["--config", config_path]
    print(f"[run_validators] delegating Phase 0 to run_vvuq.py: {' '.join(cmd)}")
    # NOTE: this returns run_vvuq.py's own exit code, which is NOT the
    # validation.py 0/1/2/3 contract. Phase 0's verdict for the pipeline is
    # read from vvuq_out/phase0_report.json (by self_heal_orchestrator), not
    # from this exit code, so the passthrough is informational only. Map the
    # common case (nonzero → something failed) for callers that branch on it.
    rc = subprocess.call(cmd)
    return 1 if rc not in (0,) else 0


def _write_phase_outputs(report: PhaseReport, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, f"{report.phase_id}_report.json")
    md_path = os.path.join(out_dir, f"{report.phase_id}_summary.md")
    payload_path = os.path.join(out_dir, f"{report.phase_id}_self_heal_payload.json")
    with open(json_path, "w") as f:
        json.dump(report_to_json(report), f, indent=2)
    with open(md_path, "w") as f:
        f.write(report_to_markdown(report))
    if report.status in ("FAIL", "BLOCK"):
        with open(payload_path, "w") as f:
            json.dump(report_to_self_heal_payload(report), f, indent=2)
    print(f"[run_validators] wrote {json_path}")
    print(f"[run_validators] wrote {md_path}")
    if report.status in ("FAIL", "BLOCK"):
        print(f"[run_validators] wrote {payload_path}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Local Phase 1-4 validator runner. Phase 0 is delegated to run_vvuq.py.")
    p.add_argument("--sim", required=True, help="path to generated simulation .py file")
    p.add_argument("--phase", required=True,
                   choices=("0", "1", "2", "3", "4", "all"),
                   help="which phase to run (or 'all' for 1-4)")
    p.add_argument("--config", default=None, help="path to JSON config (optional)")
    p.add_argument("--dsl", default=None, help="path to DSL JSON (required for --phase 0)")
    p.add_argument("--out", default="./vvuq_out", help="output directory")
    args = p.parse_args(argv)

    if args.phase == "0":
        if not args.dsl:
            print("[run_validators] --phase 0 requires --dsl", file=sys.stderr)
            return 2
        return _delegate_phase0(args.sim, args.dsl, args.config, args.out)

    try:
        sim_module = _load_sim(args.sim)
    except Exception as exc:  # noqa: BLE001
        print(f"[run_validators] could not load sim: {exc}", file=sys.stderr)
        return 2

    config = _load_config(args.config)
    phases_to_run = ["1", "2", "3", "4"] if args.phase == "all" else [args.phase]

    overall_failed = False
    t0 = time.time()
    for ph in phases_to_run:
        validator = PHASES[ph]
        print(f"\n[run_validators] === phase {ph} ===")
        report = validator(sim_module, config)
        _write_phase_outputs(report, args.out)
        print(f"[run_validators] phase {ph} → {report.status} "
              f"({report.tally})")
        if report.status in ("FAIL", "BLOCK"):
            overall_failed = True

    print(f"\n[run_validators] total elapsed {time.time() - t0:.1f}s")
    return 1 if overall_failed else 0


if __name__ == "__main__":
    sys.exit(main())
