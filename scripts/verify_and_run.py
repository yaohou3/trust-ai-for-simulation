"""
verify_and_run.py — Trusted Execution Layer (the single front door).

WHY THIS EXISTS
---------------
The framework's anti-gaming guards are deterministic, but they only protect
you if every simulation change is actually run through them before any result
is trusted. A non-technical user can't be expected to judge whether a code
change Claude Code proposes is a legitimate fix or a disguised cheat. This
wrapper removes that burden: it makes verification MANDATORY and makes
unverified output unusable.

THE ONE RULE FOR THE USER
-------------------------
    Trust ONLY the GREEN "VERIFIED" banner this command prints to your screen.
    Never accept a result because Claude Code typed "done" or "verified" in
    chat. No GREEN banner -> the result is UNVERIFIED -> do not use it.

WHAT IT DOES (gate + self-heal)
-------------------------------
It delegates to self_heal_orchestrator.py in --pipeline mode, which already:
  * runs Phase 0 (if --dsl given) -> Phases 1,2,3,4 in order,
  * enforces the parameter-fingerprint guard (rejects changed declared numbers),
  * enforces the trace-fidelity guard (rejects deleted/invented event classes),
  * on a check FAIL, writes a repair prompt and halts the pipeline so the fix
    can be applied (by Claude Code) and the SAME command re-run.

Only when the orchestrator reports a clean pipeline (exit 0) does this wrapper
write a provenance-bound manifest and print the GREEN banner. Otherwise it
writes NO manifest, marks any prior manifest STALE, and prints a plain-language
STOP banner telling the user exactly what to do.

PROVENANCE BINDING (no manifest, no trust)
------------------------------------------
The manifest binds the verdict to recomputable hashes:
  * dsl_hash            — hash of the approved DSL/spec file
  * code_hash           — sha256 of the simulation file as verified
  * parameter_fingerprint — declared-parameter fingerprint (must equal baseline)
  * trace_profile_hash  — hash of the trace's event-class profile
  * per-phase verdicts  — read from the orchestrator's own state ledger
If the simulation file changes after stamping, code_hash no longer matches and
the manifest is STALE by construction — re-running this command is required.

THREAT MODEL (state this honestly)
----------------------------------
This layer defends against a *persuasive-but-wrong* fix: an LLM that genuinely
believes its patch is correct and explains it convincingly. It is NOT a defense
against an adversary that hand-forges a manifest file. The procedural backstop
for that is simple and human-followable: trust only the banner THIS program
prints, not a status written elsewhere.

EXIT CODES (passed through from the orchestrator, plus 0 on VERIFIED)
--------------------------------------------------------------------
  0  VERIFIED — manifest written, safe to review
  1  STOP — a check failed; repair prompt written (apply fix, re-run)
  2  STOP — self-heal budget exhausted; escalation report written
  3  STOP — internal error (couldn't load sim/state)
  4  STOP — ILLEGAL_PATCH (a declared parameter was changed)
  5  STOP — SUSPICIOUS_PATCH (trace fidelity changed: event class appeared/vanished)

USAGE
-----
    python verify_and_run.py --sim my_sim.py --dsl my_dsl.json
    python verify_and_run.py --sim my_sim.py --dsl my_dsl.json --config cfg.json
    python verify_and_run.py --sim my_sim.py --dsl my_dsl.json --status   # just show last verdict
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime

# Reuse the orchestrator's hashing + state helpers and its pipeline driver so
# there is exactly ONE implementation of each guard (no parallel stack).
import self_heal_orchestrator as orch

MANIFEST_NAME = "verified_manifest.json"
STALE_NOTICE_NAME = "UNVERIFIED_RESULT.txt"

# Framework checker/harness files. These are the *graders* — they must be the
# canonical, provided artifacts, never authored or substituted by the sim/
# self-heal LLM. We record their hashes in every manifest so provenance is
# auditable, and we REFUSE TO RUN if a required grader is missing (the failure
# mode where a missing harness tempts the LLM to write — and grade itself with
# — a weaker substitute).
_REQUIRED_CHECKERS = [
    "validation.py",
    "phase0_vvuq.py",
    "semantic_scenarios.py",
    "run_validators.py",
    "self_heal_orchestrator.py",
]
_OPTIONAL_CHECKERS = [
    "compliance_mapper.py", "process_contracts.py", "semantic_checks.py",
    "coverage_validator.py", "vvuq_utils.py", "dsl_schema.py",
    "scenario_ingest.py", "decision_validity.py",
]

_PHASE_LABELS = {
    "0": "Phase 0 (structure + contracts)",
    "1": "Phase 1 (math identities)",
    "2": "Phase 2 (validation gates)",
    "3": "Phase 3 (uncertainty)",
    "4": "Phase 4 (sensitivity)",
}


# ── hashing helpers ───────────────────────────────────────────────────────
def _file_sha256(path: str | None) -> str | None:
    if not path or not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _trace_profile_hash(profile: dict | None) -> str | None:
    if profile is None:
        return None
    blob = json.dumps(profile, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _framework_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _checker_hashes() -> dict:
    """Hash every canonical checker/harness file that ships with the framework.
    Recorded in the manifest so the exact graders used are auditable, and a
    self-authored substitute placed elsewhere on the path is detectable."""
    fw = _framework_dir()
    out: dict[str, str | None] = {}
    for name in _REQUIRED_CHECKERS + _OPTIONAL_CHECKERS:
        out[name] = _file_sha256(os.path.join(fw, name))
    return out


def _missing_required_checkers() -> list[str]:
    fw = _framework_dir()
    return [n for n in _REQUIRED_CHECKERS
            if not os.path.isfile(os.path.join(fw, n))]


# ── banners ───────────────────────────────────────────────────────────────
def _rule(width: int = 72) -> str:
    return "=" * width


def _green_banner(manifest_path: str, step4_current: bool) -> None:
    print()
    print(_rule())
    print("  VERIFIED RESULT — safe to review.")
    print("  This run was produced from the approved specification, changed no")
    print("  declared model parameters, kept the trace structure verifiable, and")
    print("  passed every required validation gate.")
    if not step4_current:
        print()
        print("  NOTE: the code was patched after its last Step 4 (intent) review.")
        print("  Declared numbers are unchanged, but route the final code through")
        print("  one Step 4 re-review before using results for a real decision.")
    print()
    print(f"  Proof of verification: {manifest_path}")
    print(_rule())


def _stop_banner(rc: int, out_dir: str, sim_path: str) -> None:
    """Plain-language STOP message keyed to the orchestrator's exit code."""
    print()
    print(_rule())
    headers = {
        1: "STOP — a verification check FAILED. Do not use this result.",
        2: "STOP — automatic repair gave up. Do not use this result.",
        3: "STOP — the verifier could not run. Do not use this result.",
        4: "STOP — the code changed numbers from the approved model. Do not use this result.",
        5: "STOP — the code may have changed how the model behaves. Do not use this result.",
    }
    actions = {
        1: ("A repair instruction was written. Ask Claude Code to apply the fix\n"
            "  WITHOUT changing any model numbers, then run THIS SAME command again.\n"
            "  Keep re-running until you see the GREEN 'VERIFIED' banner.\n"
            f"  Repair instruction: {os.path.join(out_dir, 'repair_prompt_phase*.md')}"),
        2: ("The automatic fixer hit its attempt limit. This usually means the\n"
            "  problem is deeper than a small code patch (it may need the model\n"
            "  specification revised, or the framework itself checked).\n"
            f"  Details: {os.path.join(out_dir, 'escalation_phase*.md')}"),
        3: ("The simulation or its state could not be loaded. Share this whole\n"
            "  message with Claude Code and ask it to diagnose the load error."),
        4: ("Claude Code changed a declared number (a rate, time, capacity, or\n"
            "  probability) to make a check pass. That is not allowed. Tell Claude\n"
            "  Code to REVERT that change and fix the code without touching any\n"
            "  declared numbers. If the model genuinely needs a different number,\n"
            "  that is a specification change — revise Step 1, not the code.\n"
            f"  Details: {os.path.join(out_dir, 'illegal_patch.md')}"),
        5: ("A change made a whole category of events appear or disappear in the\n"
            "  simulation's history. That is the signature of hiding a problem\n"
            "  rather than fixing it. Tell Claude Code to REVERT it; the failing\n"
            "  check is most likely a framework issue, not a model bug.\n"
            f"  Details: {os.path.join(out_dir, 'suspicious_patch.md')}"),
    }
    print(f"  {headers.get(rc, 'STOP — verification did not pass. Do not use this result.')}")
    print()
    print(f"  {actions.get(rc, 'Re-run after addressing the issue above.')}")
    print()
    print("  Trust only a GREEN 'VERIFIED' banner from this command — never a")
    print("  status Claude Code types in chat.")
    print(_rule())


# ── manifest lifecycle ────────────────────────────────────────────────────
def _write_manifest(out_dir: str, sim_path: str, dsl_path: str | None,
                    config_path: str | None, state: dict,
                    phases: list[str], step4_current: bool) -> str:
    code_hash = orch._sim_sha256(sim_path)
    profile = orch._trace_event_profile(sim_path)
    ledger = state.get("phase_ledger", {})
    phase_verdicts = {}
    for ph in phases:
        rec = ledger.get(ph)
        phase_verdicts[_PHASE_LABELS.get(ph, f"Phase {ph}")] = (
            rec.get("status") if rec else "NOT_RUN")

    manifest = {
        "status": "VERIFIED",
        "generated": datetime.now().isoformat(timespec="seconds"),
        "simulation_file": os.path.abspath(sim_path),
        "dsl_file": os.path.abspath(dsl_path) if dsl_path else None,
        "config_file": os.path.abspath(config_path) if config_path else None,
        "dsl_hash": _file_sha256(dsl_path),
        "code_hash": code_hash,
        "parameter_fingerprint": state.get("param_fingerprint_baseline"),
        "trace_profile_hash": _trace_profile_hash(profile),
        "trace_event_profile": profile,
        "phase_verdicts": phase_verdicts,
        "step4_review_current": step4_current,
        "checker_hashes": _checker_hashes(),
        "claim": (
            "This result was produced from the approved specification; the code "
            "did not change any declared parameter; the trace structure remained "
            "verifiable; and every required validation gate passed. This is NOT a "
            "claim that the model is true — only that it is provenance-bound and "
            "verifier-approved."
        ),
    }
    path = os.path.join(out_dir, MANIFEST_NAME)
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)
    # A fresh manifest supersedes any prior stale notice.
    stale = os.path.join(out_dir, STALE_NOTICE_NAME)
    if os.path.isfile(stale):
        os.remove(stale)
    return path


def _invalidate_manifest(out_dir: str, reason: str) -> None:
    """On any gate trip, an existing manifest must no longer be trusted."""
    path = os.path.join(out_dir, MANIFEST_NAME)
    if os.path.isfile(path):
        try:
            with open(path) as f:
                m = json.load(f)
        except Exception:
            m = {}
        m["status"] = "STALE"
        m["stale_reason"] = reason
        m["stale_at"] = datetime.now().isoformat(timespec="seconds")
        with open(path, "w") as f:
            json.dump(m, f, indent=2)
    with open(os.path.join(out_dir, STALE_NOTICE_NAME), "w") as f:
        f.write("UNVERIFIED RESULT — DO NOT USE\n\n"
                f"{reason}\n\n"
                "Re-run verify_and_run.py and wait for the GREEN 'VERIFIED' "
                "banner before trusting any result from this simulation.\n")


def _check_manifest_freshness(out_dir: str, sim_path: str) -> None:
    """Print the last verdict and whether it still matches the current code."""
    path = os.path.join(out_dir, MANIFEST_NAME)
    if not os.path.isfile(path):
        print("[verify] No manifest found. This simulation has not been verified. "
              "Run without --status to verify it.")
        return
    with open(path) as f:
        m = json.load(f)
    current = orch._sim_sha256(sim_path)
    print(f"[verify] Last manifest status: {m.get('status')}  "
          f"(generated {m.get('generated')})")
    if m.get("status") != "VERIFIED":
        print("[verify] -> NOT a clean verification. Do not use prior results.")
        return
    if m.get("code_hash") != current:
        print("[verify] -> STALE: the simulation file changed since it was verified. "
              "Re-run verification before using any result.")
    else:
        print("[verify] -> Current: the code is unchanged since verification. "
              "The VERIFIED result still stands.")


# ── main ──────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Trusted Execution Layer: the single front door. Verifies a "
                    "simulation through the full guarded pipeline and stamps a "
                    "provenance manifest only on success.")
    p.add_argument("--sim", required=True, help="path to the simulation .py file")
    p.add_argument("--dsl", default=None,
                   help="path to the approved DSL/spec JSON (enables Phase 0 and "
                        "binds the manifest to the approved specification)")
    p.add_argument("--config", default=None, help="path to config JSON (optional)")
    p.add_argument("--out", default="./vvuq_out", help="output directory")
    p.add_argument("--phases", default=None,
                   help="comma-separated phases (default: 0 if --dsl given, then 1,2,3,4)")
    p.add_argument("--max-attempts", type=int, default=3,
                   help="self-heal attempts per phase before escalation (default 3)")
    p.add_argument("--reset", action="store_true",
                   help="reset self-heal state and rebaseline (use after a legitimate "
                        "DSL revision regenerates the code)")
    p.add_argument("--accept-trace-change", action="store_true",
                   help="confirm a SUSPICIOUS_PATCH trace change is genuinely correct "
                        "and rebaseline (use only after verifying it by hand)")
    p.add_argument("--status", action="store_true",
                   help="just print the last verdict and whether it still matches the "
                        "current code; do not run verification")
    args = p.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)

    if args.status:
        _check_manifest_freshness(args.out, args.sim)
        return 0

    # ── Harness-provenance preflight ───────────────────────────────────────
    # The graders must be the canonical provided files. If a required checker/
    # harness is missing, REFUSE to run — never let the verdict be produced by a
    # grader the author could have written. (This closes the hole where a
    # missing harness invites a self-authored, weaker substitute.)
    missing = _missing_required_checkers()
    if missing:
        print()
        print(_rule())
        print("  STOP — required verification files are missing. Do not use any result.")
        print()
        print("  These canonical checker/harness files were not found next to")
        print(f"  verify_and_run.py ({_framework_dir()}):")
        for n in missing:
            print(f"    - {n}")
        print()
        print("  These are the graders. They must be the provided files — never")
        print("  re-created or substituted. Restore the full Trust framework folder")
        print("  and re-run. Do NOT let any tool write its own copy of a checker or")
        print("  harness; a self-authored grader cannot be trusted to grade itself.")
        print(_rule())
        return 3

    # ── DSL→code coverage preflight ────────────────────────────────────────
    # Enumerate every declared DSL element and require the sim's MANIFEST to
    # declare its implementation status (FULL / PARTIAL / NOT_IMPLEMENTED /
    # DEFERRED_TO_DSL). Missing entries or unjustified gaps BLOCK Stage C. This
    # is the deterministic counterpart to Stage B's Code Alignment Review — an
    # LLM reviewer can be persuaded to ratify silent coverage gaps; enumeration
    # against the manifest cannot. Skipped when no --dsl is provided.
    if args.dsl and os.path.isfile(args.dsl):
        try:
            import json as _json
            with open(args.dsl) as _f:
                _dsl_doc = _json.load(_f)
            import importlib.util as _iu
            _spec = _iu.spec_from_file_location("dsl_code_coverage_sim", args.sim)
            _mod = _iu.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            import dsl_code_coverage as _cov
            _cov_result = _cov.check_dsl_code_coverage(_dsl_doc, _mod)
            if _cov_result["status"] != "PASS":
                print()
                print(_cov.render(_cov_result))
                print()
                print(_rule())
                print("  STOP — DSL→code coverage preflight failed.")
                print()
                print("  Every declared DSL element must appear in the sim's MANIFEST")
                print("  under 'declared_element_implementation_status' with a status")
                print("  of FULL, PARTIAL (with gap_description), NOT_IMPLEMENTED")
                print("  (with justification), or DEFERRED_TO_DSL (with justification).")
                print("  This is the deterministic counterpart to the Code Alignment")
                print("  Review — it cannot be persuaded by prose to ratify a coverage")
                print("  gap. Fix the manifest entries above and re-run.")
                print(_rule())
                return 3
        except Exception as _exc:
            print(f"[verify] DSL→code coverage preflight skipped ({_exc}); "
                  "proceeding to Stage C. Provide a readable --dsl and a sim "
                  "with a manifest() / MANIFEST attribute to enable it.")

    # Determine the phase list the same way the orchestrator does, so the
    # manifest reports verdicts for exactly the phases that ran.
    if args.phases:
        phases = [s.strip() for s in args.phases.split(",") if s.strip()]
    else:
        phases = (["0"] if args.dsl else []) + ["1", "2", "3", "4"]

    # Build the orchestrator argv and hand off the full guarded pipeline.
    orch_argv = ["--sim", args.sim, "--pipeline", "--out", args.out,
                 "--phases", ",".join(phases),
                 "--max-attempts", str(args.max_attempts)]
    if args.dsl:
        orch_argv += ["--dsl", args.dsl]
    if args.config:
        orch_argv += ["--config", args.config]
    if args.reset:
        orch_argv += ["--reset"]
    if args.accept_trace_change:
        orch_argv += ["--accept-trace-change"]

    print("[verify] Running the guarded verification pipeline. A result becomes")
    print("[verify] trustworthy ONLY if this ends with a GREEN 'VERIFIED' banner.")
    print()

    rc = orch.main(orch_argv)

    if rc == 0:
        state = orch._load_state(args.out)
        sim_hash = orch._sim_sha256(args.sim)
        step4_hash = state.get("step4_approved_sim_hash")
        step4_current = step4_hash is not None and step4_hash == sim_hash
        manifest_path = _write_manifest(args.out, args.sim, args.dsl, args.config,
                                        state, phases, step4_current)
        _green_banner(manifest_path, step4_current)
        return 0

    # Any non-zero exit: invalidate prior trust and explain in plain language.
    reasons = {
        1: "A verification check failed and was not yet repaired.",
        2: "Automatic repair was exhausted without passing verification.",
        3: "The verifier could not run to completion.",
        4: "A patch changed a declared model parameter (ILLEGAL_PATCH).",
        5: "A patch changed the trace's event structure (SUSPICIOUS_PATCH).",
    }
    _invalidate_manifest(args.out, reasons.get(rc, "Verification did not pass."))
    _stop_banner(rc, args.out, args.sim)
    return rc


if __name__ == "__main__":
    sys.exit(main())
