"""
self_heal_orchestrator.py — Loop-driver for Python-validator + LLM-repair.

Why this exists
===============
The trust framework has two execution modes for Phases 0-4:

  - LLM mode: Claude executes the phase prompt, runs checks in its head,
    and proposes patches inline (the legacy VVUQ_SELF_HEAL pattern).
  - Python mode: a deterministic validator (validation.py / phase0_vvuq.py)
    detects and localizes failures; an LLM proposes patches; the validator
    re-runs to confirm. The LLM cannot mark its own work as fixed —
    PASS verdicts can only come from the deterministic validator.

This script implements the loop driver for Python mode, designed for users
who do not have direct LLM API access but do use Claude Code. The
orchestrator runs the validator and, on FAIL/BLOCK, writes a structured
repair prompt to disk and exits with a non-zero status. The user (or
Claude Code, agentically) reads the prompt, edits the simulation file,
and re-invokes this orchestrator. The loop terminates when the validator
returns PASS or after `--max-attempts` (default 3), at which point an
escalation report is written.

State machine
=============
  attempt 0  →  validator                        PASS         → exit 0
                                                FAIL/BLOCK    → write self_heal_<phase>_attempt0.md, exit 1
  attempt 1  →  user/Claude Code edits sim, re-runs orchestrator
                validator                        PASS         → exit 0
                                                FAIL/BLOCK    → write attempt1.md, exit 1
  attempt 2  →  same loop
  attempt 3  →  if still failing, write escalation_<phase>.md, exit 2

State persists in <out_dir>/.self_heal_state.json. To restart from scratch,
delete that file (or pass --reset).

Designed to be invoked from Claude Code as a Bash subprocess. A typical
agentic prompt to Claude Code:

    "Run python self_heal_orchestrator.py --sim my_sim.py --phase 1 .
     If it exits non-zero, read self_heal_phase1_attempt0.md, diagnose,
     edit my_sim.py, and re-run. Loop until exit 0 or attempt 3."

Claude Code's Bash + Read + Edit tools handle this loop natively. No API
access required.

Usage
-----
    python self_heal_orchestrator.py \
        --sim   my_sim.py \
        --phase 1 \
        --out   ./vvuq_out \
        --max-attempts 3

    # for Phase 0:
    python self_heal_orchestrator.py \
        --sim my_sim.py --dsl my_dsl.json --phase 0 --out ./vvuq_out

Exit codes
----------
  0  validator PASSed (or PASSed with WARNs only) — pipeline can continue
  1  validator FAILed/BLOCKed; repair prompt written; needs LLM patch + re-run
  2  max attempts exhausted; escalation prompt written; needs human/2nd-LLM review
  3  internal orchestrator error
  4  ILLEGAL_PATCH: a patch changed a DSL-declared parameter; rejected. Revert
     the parameter change (or rebaseline with --reset after a real DSL revision)
  5  SUSPICIOUS_PATCH: a patch changed trace fidelity (a whole event class
     appeared or disappeared) — the common signature of "fix" by deleting real
     events or emitting synthetic ones. Revert, or if the change is genuinely
     correct, confirm with --accept-trace-change to rebaseline the profile.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime


def _sim_sha256(sim_path: str) -> str:
    """SHA-256 of the simulation file. A phase result is only valid against
    the sim hash it was produced under; any patch changes the hash and
    invalidates every recorded phase result (a patch can affect any phase,
    upstream or downstream, so we re-verify all of them)."""
    try:
        with open(sim_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


# Framework run-controls that self-heal IS allowed to change — they are not
# DSL declarations, just run knobs. Everything else in DEFAULT_CONFIG is a
# DSL-declared parameter that a SIM_CODE patch must leave untouched.
_NON_DSL_CONFIG_KEYS = {"seed", "run_length", "warmup_time", "warmup",
                        "rep_id", "first_arrival_at"}


def _param_fingerprint(sim_path: str) -> str:
    """Fingerprint of the DSL-declared behavioral parameters in the sim's
    DEFAULT_CONFIG (or default_config()).

    Self-heal SIM_CODE patches may only fix implementation — trace emission,
    event ordering, resource accounting, control flow. They must NOT alter a
    DSL-declared parameter (distribution, rate, capacity, routing probability,
    coordination structure, compliance_spec). A change to this fingerprint
    means the patch redefined the model rather than fixing the code, which is
    a DSL_SPEC issue (→ DEFER_TO_DSL), not a legal SIM_CODE patch. The
    orchestrator rejects such patches deterministically — no LLM review needed.

    Framework run-controls (seed/run_length/warmup_time/…) are excluded
    because they are not DSL declarations. compliance_spec IS a DSL artifact,
    so it is included in the fingerprint."""
    import importlib.util as iu
    try:
        spec = iu.spec_from_file_location("fp_sim", sim_path)
        mod = iu.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:
        return ""
    cfg = getattr(mod, "DEFAULT_CONFIG", None)
    if cfg is None:
        fn = getattr(mod, "default_config", None)
        cfg = fn() if callable(fn) else {}
    if not isinstance(cfg, dict):
        return ""
    declared = {k: v for k, v in cfg.items() if k not in _NON_DSL_CONFIG_KEYS}
    blob = json.dumps(declared, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


_FIDELITY_MIN_COUNT = 5   # an event class below this is too small to judge


def _trace_event_profile(sim_path: str) -> dict[str, int] | None:
    """Run the sim once under its own DEFAULT_CONFIG and return a histogram of
    {event_type: count} from the trace. Used to detect a patch that changes
    trace fidelity — the signature of "fixing" a check by deleting real events
    (e.g. suppressing rework queue_enters) or emitting synthetic ones (e.g.
    fake system_departures). Returns None if the sim can't be run."""
    import importlib.util as iu
    from collections import Counter
    try:
        spec = iu.spec_from_file_location("fp_sim_trace", sim_path)
        mod = iu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cfg = getattr(mod, "DEFAULT_CONFIG", None)
        if cfg is None:
            fn = getattr(mod, "default_config", None)
            cfg = fn() if callable(fn) else {}
        result = mod.run_simulation(dict(cfg))
        trace = result.get("trace", [])
        return dict(Counter(ev.get("event") for ev in trace if ev.get("event")))
    except Exception:
        return None


def _trace_identity(sim_path: str) -> str | None:
    """A soft model identity used when no DSL is given: a hash of the SET of
    event classes the sim emits (not their counts). Two genuinely different
    models almost always emit different event-class sets, so this distinguishes
    a model swap (rebaseline) from a parameter tweak to the same model (guard).
    Returns None if the sim can't be run."""
    profile = _trace_event_profile(sim_path)
    if profile is None:
        return None
    classes = ",".join(sorted(profile.keys()))
    return hashlib.sha256(classes.encode()).hexdigest()


# Fractional-suppression threshold (audit M1): a class whose count drops
# below this fraction of a substantial baseline is flagged even though the
# class still exists. Whole-class checks alone let a patch suppress 90%+ of
# a real event class (keeping a token few) — exactly how count-based checks
# get gamed. Legitimate fixes that genuinely shift counts this far route
# through the existing --accept-trace-change rebaseline, same as whole-class
# changes, so the repair loop cannot deadlock.
_FIDELITY_SUPPRESSION_RATIO = 0.5
_FIDELITY_SUPPRESSION_MIN_BASE = 20


def _fidelity_deltas(baseline: dict, current: dict) -> list[str]:
    """Return human-readable descriptions of suspicious trace-profile changes
    between runs: whole-event-class appearances/disappearances (the
    low-false-positive signal — suppression/synthesis does exactly that),
    plus large fractional suppressions within a surviving class (audit M1:
    dropping a class from 1000 events to a token 4 previously passed because
    the class still 'existed'). Moderate count shifts remain unflagged since
    legitimate fixes can shift counts."""
    deltas: list[str] = []
    keys = set(baseline) | set(current)
    for k in sorted(keys):
        b = baseline.get(k, 0)
        c = current.get(k, 0)
        if b >= _FIDELITY_MIN_COUNT and c == 0:
            deltas.append(f"event class '{k}' DISAPPEARED ({b} → 0) — patch may have "
                          "suppressed real events")
        elif c >= _FIDELITY_MIN_COUNT and b == 0:
            deltas.append(f"event class '{k}' APPEARED (0 → {c}) — patch may have "
                          "synthesized events")
        elif (b >= _FIDELITY_SUPPRESSION_MIN_BASE and c > 0
              and c < b * _FIDELITY_SUPPRESSION_RATIO):
            deltas.append(
                f"event class '{k}' SUPPRESSED ({b} → {c}, "
                f"-{(1 - c / b):.0%}) — a drop this large in a surviving "
                f"class is the signature of partial event suppression; if "
                f"the change is a genuine consequence of a correct fix, "
                f"re-run with --accept-trace-change")
    return deltas


def _write_suspicious_patch_report(out_dir: str, sim_path: str,
                                   deltas: list[str],
                                   baseline: dict, current: dict) -> str:
    md_path = os.path.join(out_dir, "suspicious_patch.md")
    lines = [
        "# SUSPICIOUS PATCH — trace fidelity changed",
        "",
        "> **In plain words:** Caution — this change made a whole category of events",
        "> appear or disappear in the simulation's history. That is usually a sign of",
        "> hiding a problem rather than fixing it. **Do not trust any result** until",
        "> this is reverted or verified by hand. The safe move: tell Claude Code to",
        "> undo the change — the failing check is most likely a framework issue, not a",
        "> model bug.",
        "",
        f"_Generated: {datetime.now().isoformat(timespec='seconds')}_",
        "",
        f"Simulation file: `{sim_path}`",
        "",
        "## What happened",
        "",
        "A patch applied during self-healing changed the trace's event-class",
        "composition — a whole event type appeared or disappeared:",
        "",
    ]
    for d in deltas:
        lines.append(f"- {d}")
    lines += [
        "",
        f"Baseline profile: {baseline}",
        f"Current profile:  {current}",
        "",
        "## Why this is gated",
        "",
        "Making an entire event class vanish (or a synthetic one appear) is the",
        "signature of 'fixing' a check by distorting the trace rather than the",
        "model — e.g. suppressing the queue_enter/queue_exit events that a",
        "reworked part genuinely produces, or emitting fake system_departure /",
        "loss events to satisfy a terminal-outcome check. The trace must remain a",
        "faithful record of the system; a patch may fix HOW events are emitted,",
        "never delete real ones or invent absent ones.",
        "",
        "## How to proceed",
        "",
        "1. If the patch suppressed/synthesized events: REVERT it. The failing",
        "   check is almost certainly a VALIDATOR issue (a check that doesn't",
        "   anticipate a legitimate behavior); emit VALIDATOR_FIX and stop.",
        "2. If the trace change is genuinely correct (a real bug fix that",
        "   legitimately removes a spurious event class, or adds a real one the",
        "   sim was wrongly omitting), re-run with --accept-trace-change to",
        "   rebaseline the trace profile and continue.",
        "",
    ]
    with open(md_path, "w") as f:
        f.write("\n".join(lines))
    print(f"[self_heal] wrote suspicious-patch report: {md_path}")
    return md_path


def _write_illegal_patch_report(out_dir: str, sim_path: str,
                                baseline_fp: str, current_fp: str) -> str:
    md_path = os.path.join(out_dir, "illegal_patch.md")
    lines = [
        "# ILLEGAL PATCH — a self-heal edit changed a DSL-declared parameter",
        "",
        "> **In plain words:** Stop — this change altered a declared model number",
        "> (a rate, time, capacity, or probability) to make a check pass. That is not",
        "> allowed. **Do not use the result.** Tell Claude Code to revert it and fix",
        "> the code without touching any declared number. If the model genuinely needs",
        "> a different number, that is a specification change (revise Step 1), not a",
        "> code fix.",
        "",
        f"_Generated: {datetime.now().isoformat(timespec='seconds')}_",
        "",
        f"Simulation file: `{sim_path}`",
        f"Baseline parameter fingerprint: `{baseline_fp[:16]}`",
        f"Current parameter fingerprint:  `{current_fp[:16]}`",
        "",
        "## What happened",
        "",
        "A patch applied during self-healing changed at least one DSL-declared",
        "parameter — a distribution, rate, capacity, routing probability,",
        "coordination structure, or the compliance_spec itself. SIM_CODE patches",
        "are only permitted to fix the *implementation* (how the trace is",
        "emitted, event ordering, resource accounting, control flow), never to",
        "*redefine the model*.",
        "",
        "## Why this is rejected",
        "",
        "Changing a declared parameter to make a check pass is the canonical",
        "gaming failure mode the framework exists to prevent: it would make the",
        "code disagree with the DSL that Step 2 and Step 4 already approved, and",
        "would invalidate the credibility argument. The deterministic parameter",
        "diff catches it without needing an LLM review.",
        "",
        "## First: is this actually a different model? (stale state)",
        "",
        "If you have run a DIFFERENT model into this same output directory, the",
        "saved baseline belongs to the OLD model and this is a false alarm, not a",
        "parameter change in your code. (Tell-tale sign: the baseline's trace —",
        "arrival counts, event classes — doesn't match your current model at all.)",
        "When the DSL is provided (`--dsl`), the orchestrator now detects this and",
        "auto-rebaselines instead of reporting ILLEGAL_PATCH. If you still see this",
        "after switching models, just re-run with `--reset` (or use a fresh `--out`",
        "dir per model). Do NOT edit the simulation.",
        "",
        "## Otherwise (same model) — how to fix",
        "",
        "1. Revert the parameter change in the simulation file.",
        "2. If the failing check is a genuine implementation bug, fix the code",
        "   without touching the declared parameter, then re-run the orchestrator.",
        "3. If the fix genuinely requires a different declared parameter, that is",
        "   a DSL_SPEC issue, not a SIM_CODE patch: emit `DEFER_TO_DSL`, revise",
        "   the DSL at Step 1, regenerate the code, re-pass Step 2 + Step 4, then",
        "   re-run the orchestrator with `--reset` to rebaseline against the new",
        "   intended parameters.",
        "",
    ]
    with open(md_path, "w") as f:
        f.write("\n".join(lines))
    print(f"[self_heal] wrote illegal-patch report: {md_path}")
    return md_path


def _state_path(out_dir: str) -> str:
    return os.path.join(out_dir, ".self_heal_state.json")


def _load_state(out_dir: str) -> dict:
    path = _state_path(out_dir)
    if not os.path.isfile(path):
        return {"attempts_by_phase": {}}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {"attempts_by_phase": {}}


def _save_state(out_dir: str, state: dict) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(_state_path(out_dir), "w") as f:
        json.dump(state, f, indent=2)


def _record_phase(state: dict, phase: str, status: str, sim_hash: str) -> None:
    """Tag a phase's verdict with the sim hash it was produced against."""
    ledger = state.setdefault("phase_ledger", {})
    ledger[phase] = {"status": status, "sim_sha256": sim_hash}


def _phase_is_valid(state: dict, phase: str, sim_hash: str) -> bool:
    """A phase counts as already-validated only if it PASSed/WARNed against
    the CURRENT sim hash. If the sim has been patched since, the recorded
    verdict is stale and the phase must re-run."""
    rec = state.get("phase_ledger", {}).get(phase)
    return bool(rec and rec.get("status") in ("PASS", "WARN")
               and rec.get("sim_sha256") == sim_hash)


def _stale_phases(state: dict, sim_hash: str) -> list[str]:
    """Phases that previously passed but against a different (now patched)
    sim hash — i.e. results that must be invalidated and re-verified."""
    ledger = state.get("phase_ledger", {})
    return sorted(ph for ph, rec in ledger.items()
                  if rec.get("status") in ("PASS", "WARN")
                  and rec.get("sim_sha256") != sim_hash)


def _run_validator(sim: str, phase: str, dsl: str | None,
                   config: str | None, out_dir: str) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    cmd = [sys.executable, os.path.join(here, "run_validators.py"),
           "--sim", sim, "--phase", phase, "--out", out_dir]
    if dsl:
        cmd += ["--dsl", dsl]
    if config:
        cmd += ["--config", config]
    print(f"\n[self_heal] running: {' '.join(cmd)}\n")
    return subprocess.call(cmd)


def _read_validator_status(out_dir: str, phase: str) -> tuple[str, dict | None]:
    """Read the JSON report for the given phase. Returns (aggregate_status, report)."""
    if phase == "0":
        # Phase 0's report is at vvuq_out/phase0_report.json with a different shape.
        path = os.path.join(out_dir, "phase0_report.json")
        if not os.path.isfile(path):
            return "MISSING", None
        with open(path) as f:
            data = json.load(f)
        # Compute aggregate from tally if present, else infer from gate.
        tally = data.get("tally") or {}
        gate = data.get("gate", "")
        if "BLOCK" in gate:
            return "BLOCK", data
        if tally.get("FAIL", 0) > 0:
            return "FAIL", data
        if tally.get("WARN", 0) > 0:
            return "WARN", data
        return "PASS", data
    else:
        path = os.path.join(out_dir, f"phase{phase}_report.json")
        if not os.path.isfile(path):
            return "MISSING", None
        with open(path) as f:
            data = json.load(f)
        return data.get("status", "MISSING"), data


def _read_payload(out_dir: str, phase: str) -> dict | None:
    """Read the self-heal payload (only present when phase status is FAIL/BLOCK)."""
    path = os.path.join(out_dir, f"phase{phase}_self_heal_payload.json")
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return json.load(f)


def _write_repair_prompt(out_dir: str, phase: str, attempt: int,
                         max_attempts: int, sim_path: str, payload: dict | None,
                         report: dict | None) -> str:
    """Write a structured repair prompt the user (or Claude Code) reads next."""
    md_path = os.path.join(out_dir, f"self_heal_phase{phase}_attempt{attempt}.md")
    failures = (payload or {}).get("failures", [])
    lines = [
        f"# Self-heal repair prompt — phase {phase}, attempt {attempt + 1} of {max_attempts}",
        "",
        f"_Generated: {datetime.now().isoformat(timespec='seconds')}_",
        "",
        "The deterministic validator detected the failures listed below.",
        "Diagnose root causes, edit the simulation file, and re-run the orchestrator.",
        "Only the validator can declare a PASS; do not mark a check as fixed yourself.",
        "",
        f"## Simulation file under repair",
        f"`{sim_path}`",
        "",
        f"## Failing checks ({len(failures)})",
    ]
    if len(failures) >= 10:
        lines += [
            "",
            "> **Triage first — high failure count.** With this many failures, do NOT",
            "> patch them individually. Most groups of checks share a root cause (one",
            "> missing event class, one contract field, one routing mistake). Steps:",
            "> 1. CLUSTER the failures by suspected root cause (group `check_id`s that",
            ">    likely fail for the same reason).",
            "> 2. Pick the SINGLE cluster likeliest to clear the most checks.",
            "> 3. Fix only that cluster's root cause, re-run, and let progress-aware",
            ">    budgeting credit the gains (a run that strictly shrinks the failure",
            ">    set does NOT cost an attempt).",
            "> 4. Repeat per cluster. Do not try to fix 22 things in one patch.",
            "",
        ]
    if not failures:
        lines.append("(no structured failures available — see report below)")
    for i, f in enumerate(failures, 1):
        lines += [
            "",
            f"### {i}. `{f.get('check_id')}` — **{f.get('status')}**",
            "",
            f"**Diagnostic:** {f.get('diagnostic')}",
            "",
            f"**Suspected location:** `{f.get('suspected_location') or 'unknown — locate it yourself'}`",
            "",
            "**Evidence:**",
            "```json",
            json.dumps(f.get("evidence", {}), indent=2),
            "```",
        ]
    lines += [
        "",
        "## Repair protocol",
        "",
        "0. CLASSIFICATION GATE (do this before touching anything). For each",
        "   failing check, FIRST write one sentence stating why the check's",
        "   expectation is correct *for this system*. If you cannot justify the",
        "   expectation — if the failure is caused by a legitimate behaviour the",
        "   check did not anticipate — it is a VALIDATOR issue, not a sim bug.",
        "   Legitimate behaviours that commonly trip naive checks include:",
        "     • re-entry / rework loops (an entity visits a queue more than once)",
        "     • branching terminal outcomes (some entities depart, others are lost)",
        "     • a config shape the sim legitimately declared (int vs dict resources)",
        "     • Monte-Carlo sampling noise within the expected error at this n",
        "     • boundary/in-flight entities at the run horizon",
        "   If any of these is the cause → emit `VALIDATOR_FIX: <check_id>` and STOP.",
        "   Do NOT patch the simulation to satisfy a check that is itself wrong.",
        "1. Otherwise, identify the root cause from the diagnostic, the evidence,",
        "   and the simulation source.",
        "2. Classify the cause as one of:",
        "     - SIM_CODE   — the simulation implementation is wrong / incomplete.",
        "     - DSL_SPEC   — the DSL or compliance_spec mis-declares the element.",
        "     - VALIDATOR  — the check's expectation does not apply in this regime.",
        "3. Act:",
        "     - SIM_CODE   → edit the simulation file directly. Do NOT modify the validator.",
        "     - DSL_SPEC   → record `DEFER_TO_DSL: <check_id>` in this file and stop. The",
        "                    framework's DSL step (Step 1) needs to be revised.",
        "     - VALIDATOR  → record `VALIDATOR_FIX: <check_id>` with the proposed correction.",
        "                    The validator change is out of scope for this loop — escalate.",
        "4. Re-run the orchestrator. It re-runs the deterministic validator and only",
        "   accepts a PASS verdict from the validator itself. If the same `check_id`",
        "   fails three times in a row, the orchestrator escalates instead of looping.",
        "",
        "## Hard rules",
        "",
        "- Do NOT edit any checker or harness file — `validation.py`, `phase0_vvuq.py`,",
        "  `process_contracts.py`, `semantic_checks.py`, `semantic_scenarios.py`,",
        "  `coverage_validator.py`, `compliance_mapper.py`, `vvuq_utils.py`. Those are",
        "  the truth source / the graders. Edit the simulation file only.",
        "- Do NOT author or run your OWN copy of a checker or harness. If a required",
        "  grader is missing from the folder, STOP and report it — never re-create the",
        "  thing that judges you. A self-written grader cannot grade itself; the trusted",
        "  runner records each grader's hash and refuses to run when one is absent.",
        "- Do NOT loosen tolerances or narrow scope to make a FAIL pass.",
        "- Do NOT relax a scenario/counterfactual's `expected_direction` or",
        "  `expected_magnitude` to clear a failure. Changing the prediction to fit the",
        "  result is gaming the oracle: a claim relaxed to `any`/no-direction asserts",
        "  nothing and is scored NOT_A_CLAIM (never PASS). If a counterfactual's expected",
        "  direction does not hold in this model, that is a finding (report it, or mark",
        "  the scenario not-applicable) — not something to edit away.",
        "- A guessed/defaulted expectation is advisory only. Never present an expectation",
        "  you invented (rather than one the DSL declared or an SME approved) as ground",
        "  truth; auto-generated scenarios are direction-only and cannot raise the verdict.",
        "- Make the smallest change consistent with the diagnostic. The validator will",
        "  re-run all checks for this phase; over-broad edits risk breaking unrelated checks.",
        "- A SIM_CODE patch may ONLY change implementation: trace emission, event",
        "  ordering, resource grant/release accounting, control flow, and internal",
        "  stochastic plumbing (e.g. separating RNG streams). It must NOT change any",
        "  DSL-declared parameter — distribution, rate, capacity, routing probability,",
        "  coordination structure, or compliance_spec. The orchestrator fingerprints",
        "  the declared parameters and will REJECT any such change (ILLEGAL_PATCH).",
        "- A SIM_CODE patch must NEVER distort the trace to pass a check. Specifically,",
        "  these are forbidden and will be rejected (SUSPICIOUS_PATCH) or are simply",
        "  wrong:",
        "    • do NOT suppress or remove real events (e.g. omitting the queue_enter a",
        "      reworked part genuinely produces) to satisfy a conservation/FIFO check;",
        "    • do NOT emit synthetic events the system does not really produce (e.g. a",
        "      fake system_departure for a scrapped part, or a fake loss for a good one)",
        "      to satisfy a terminal-outcome check;",
        "    • do NOT 'close out' genuinely-incomplete in-flight entities at the horizon;",
        "    • do NOT add code that inspects or special-cases the validator's config",
        "      shapes or sentinel values — if a config shape crashes the sim, that is a",
        "      VALIDATOR issue (the validator must preserve the sim's declared shape).",
        "  The trace must remain a faithful record of the system. A patch may fix HOW",
        "  events are emitted; it may not delete real ones or invent absent ones.",
        "- If the fix genuinely requires a different declared parameter, that is a",
        "  DSL_SPEC issue: emit `DEFER_TO_DSL` and stop.",
        "",
    ]
    if report:
        lines += [
            "## Full validator report (for context)",
            "```json",
            json.dumps(report, indent=2)[:6000] + ("…[truncated]" if len(json.dumps(report)) > 6000 else ""),
            "```",
        ]
    with open(md_path, "w") as f:
        f.write("\n".join(lines))
    print(f"[self_heal] wrote repair prompt: {md_path}")
    return md_path


def _write_escalation(out_dir: str, phase: str, attempts: int,
                      payload_history: list[dict]) -> str:
    md_path = os.path.join(out_dir, f"escalation_phase{phase}.md")
    lines = [
        f"# Escalation — phase {phase} exhausted {attempts} self-heal attempts",
        "",
        f"_Generated: {datetime.now().isoformat(timespec='seconds')}_",
        "",
        "The deterministic validator continued to FAIL/BLOCK after the maximum",
        "number of self-heal attempts. The repair loop is suspended pending",
        "human review or escalation to a second-opinion LLM (different model",
        "family from the one that performed the repair attempts).",
        "",
        "## Attempt history",
        "",
    ]
    for i, p in enumerate(payload_history, 1):
        failures = p.get("failures", []) if p else []
        lines += [f"### Attempt {i}", "",
                  f"Failing checks: {len(failures)}",
                  ""]
        for f in failures[:5]:
            lines.append(f"- `{f.get('check_id')}` ({f.get('status')}) — {f.get('diagnostic')}")
        if len(failures) > 5:
            lines.append(f"- … {len(failures) - 5} more")
        lines.append("")
    lines += [
        "## Recommended next steps",
        "",
        "1. Open the most recent `phase{phase}_summary.md` and `phase{phase}_report.json`.",
        "2. Decide whether the failure is truly a SIM_CODE defect (in which case the",
        "   simulation needs a structural rewrite, not another local patch) or a",
        "   DSL_SPEC defect (in which case Step 1 of the trust pipeline needs revision)",
        "   or a VALIDATOR defect (in which case the framework itself needs an update).",
        "3. If you delete `.self_heal_state.json`, the orchestrator will start a fresh",
        "   3-attempt budget on the next invocation.",
        "",
    ]
    with open(md_path, "w") as f:
        f.write("\n".join(lines))
    print(f"[self_heal] wrote escalation report: {md_path}")
    return md_path


def _heal_one_phase(args, state: dict, phase: str, sim_hash: str) -> int:
    """Run + (on failure) write a repair prompt for a single phase.
    Records the verdict tagged with sim_hash. Returns the exit code:
    0 = validated, 1 = FAIL/BLOCK (repair prompt written), 2 = escalated,
    3 = validator crashed."""
    attempts_by_phase = state.setdefault("attempts_by_phase", {})
    escalated = state.setdefault("escalated_phases", [])
    attempt = attempts_by_phase.get(phase, 0)

    if attempt >= args.max_attempts:
        # Already exhausted. The escalation report was written when the budget
        # was first hit; don't rewrite it on every subsequent invocation.
        if phase not in escalated:
            history = state.get(f"payload_history_phase{phase}", [])
            _write_escalation(args.out, phase, attempt, history)
            escalated.append(phase); _save_state(args.out, state)
        print(f"[self_heal] phase {phase}: at max attempts "
              f"({attempt}/{args.max_attempts}); already escalated. "
              "Fix the sim and re-run with --reset, or address the escalation report.")
        return 2

    _run_validator(args.sim, phase, args.dsl, args.config, args.out)
    status, report = _read_validator_status(args.out, phase)
    print(f"[self_heal] phase {phase} attempt {attempt + 1}/{args.max_attempts} → {status}")

    if status in ("PASS", "WARN"):
        print(f"[self_heal] phase {phase} VALIDATED against sim {sim_hash[:12]}.")
        attempts_by_phase[phase] = 0           # reset budget on success
        _record_phase(state, phase, status, sim_hash)
        _save_state(args.out, state)
        return 0

    if status == "MISSING":
        print("[self_heal] no report found at expected path. Validator may have crashed.",
              file=sys.stderr)
        return 3

    # FAIL or BLOCK — record, write repair prompt, decide whether to consume an
    # attempt based on PROGRESS rather than just on "did the run end FAILing?".
    _record_phase(state, phase, status, sim_hash)
    payload = _read_payload(args.out, phase)
    history = state.setdefault(f"payload_history_phase{phase}", [])
    history.append(payload or {})

    # Progress-aware budget: compare against the LAST attempt's snapshot.
    current_failures = {(f.get("check_id") or "") for f in (payload or {}).get("failures", [])
                        if f.get("check_id")}
    prev = state.get(f"last_attempt_phase{phase}")
    prev_hash = prev.get("sim_hash") if prev else None
    prev_failures = set(prev.get("failures", [])) if prev else set()
    progress_note = "(first attempt)"
    consume = True

    if prev_hash is not None and sim_hash == prev_hash:
        # Same sim as last invocation — investigation re-run, no patch was made.
        # Do not burn budget; nothing has changed and nothing could have changed.
        consume = False
        progress_note = "(no code change since last attempt — investigation re-run)"
        print("[self_heal] sim unchanged since last attempt — no attempt consumed. "
              "Edit the sim and re-run.")
    elif prev_hash is not None:
        new_fail = current_failures - prev_failures
        newly_passing = prev_failures - current_failures
        if newly_passing and not new_fail:
            # Strict progress: some checks newly PASS, none newly broke.
            consume = False
            progress_note = (f"(progress: {len(newly_passing)} newly PASS, "
                             f"{len(current_failures)} still failing — attempt NOT consumed)")
            print(f"[self_heal] progress: {len(newly_passing)} check(s) newly PASS, "
                  f"{len(current_failures)} still failing. Attempt NOT consumed.")
        elif not newly_passing and not new_fail:
            # Same failure set after a sim edit — the patch didn't help.
            consume = True
            progress_note = "(patch had no effect on failures — attempt consumed)"
            print("[self_heal] patch changed the sim but did not change the failure "
                  "set — attempt consumed.")
        else:
            # Regression or mixed change.
            consume = True
            progress_note = (f"(regression: {len(new_fail)} newly failed — "
                             "attempt consumed)")
            print(f"[self_heal] regression: {len(new_fail)} check(s) newly failed "
                  f"({sorted(new_fail)[:3]}{'…' if len(new_fail)>3 else ''}); "
                  f"{len(newly_passing)} newly passed. Attempt consumed.")

    # Record this attempt's snapshot for the next comparison.
    state[f"last_attempt_phase{phase}"] = {
        "sim_hash": sim_hash, "failures": sorted(current_failures)}

    next_attempt = attempt + 1 if consume else attempt
    attempts_by_phase[phase] = next_attempt
    _save_state(args.out, state)

    if next_attempt >= args.max_attempts:
        _write_escalation(args.out, phase, next_attempt, history)
        if phase not in escalated:
            escalated.append(phase)
        _save_state(args.out, state)
        return 2

    md_path = _write_repair_prompt(args.out, phase, attempt, args.max_attempts,
                                   args.sim, payload, report)
    print()
    print(f"[self_heal] FAIL/BLOCK detected. Repair prompt: {md_path}")
    print(f"[self_heal] Attempts so far: {next_attempt}/{args.max_attempts}  {progress_note}")
    print(f"[self_heal] To repair: read the prompt, edit {args.sim}, then re-run me.")
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Self-heal orchestrator for Python-mode validators.")
    p.add_argument("--sim", required=True, help="path to generated simulation .py")
    p.add_argument("--phase", default=None,
                   choices=("0", "1", "2", "3", "4"),
                   help="single phase to repair (mutually exclusive with --pipeline)")
    p.add_argument("--pipeline", action="store_true",
                   help="run phases in order with sim-hash gating; re-verifies every "
                        "phase that was validated against an older (now patched) sim. "
                        "This is the regression-safe mode.")
    p.add_argument("--phases", default=None,
                   help="comma-separated phase list for --pipeline (default: 1,2,3,4; "
                        "include 0 only if --dsl is given)")
    p.add_argument("--dsl", default=None, help="path to DSL JSON (required for phase 0)")
    p.add_argument("--config", default=None, help="path to config JSON (optional)")
    p.add_argument("--out", default="./vvuq_out", help="output directory")
    p.add_argument("--max-attempts", type=int, default=3,
                   help="maximum self-heal attempts per phase before escalation (default 3)")
    p.add_argument("--reset", action="store_true",
                   help="delete prior self-heal state and rebaseline (use after a "
                        "legitimate DSL revision regenerates the code)")
    p.add_argument("--step4-approved", action="store_true",
                   help="record the current sim hash as having passed Step 4 (Code "
                        "Alignment Review). Call this once after Step 4 passes, before "
                        "the self-heal loop, so the orchestrator can flag when later "
                        "patches require a Step 4 re-review.")
    p.add_argument("--accept-trace-change", action="store_true",
                   help="confirm a SUSPICIOUS_PATCH trace-fidelity change is legitimate "
                        "and rebaseline the trace profile (use only after verifying a "
                        "patch's appearing/disappearing event class is genuinely correct).")
    args = p.parse_args(argv)

    if not args.pipeline and not args.phase and not args.step4_approved:
        print("[self_heal] specify --phase N, --pipeline, or --step4-approved", file=sys.stderr)
        return 3

    os.makedirs(args.out, exist_ok=True)
    if args.reset and os.path.isfile(_state_path(args.out)):
        os.remove(_state_path(args.out))
        print(f"[self_heal] state reset: {_state_path(args.out)}")

    state = _load_state(args.out)
    sim_hash = _sim_sha256(args.sim)
    if not sim_hash:
        print(f"[self_heal] could not hash sim file: {args.sim}", file=sys.stderr)
        return 3

    # ── Model-identity guard ───────────────────────────────────────────────
    # The baselines (param fingerprint, trace profile, phase ledger) are keyed
    # to the output dir. If the model identity (DSL hash, or trace-identity
    # when no DSL is given) changes between runs, those baselines belong to a
    # different model.
    #
    # SECURITY NOTE (audit C1): this guard must NOT auto-rebaseline. The
    # param-fingerprint and trace-fidelity guards below run against the stored
    # baselines; wiping state here would let them silently adopt fresh
    # baselines, so an edit to my_dsl.json (changing its hash) would disarm
    # all three guards at once — a change to declared parameters plus a DSL
    # bump would pass clean and still print the VERIFIED banner. Instead we
    # STOP with a distinct exit code and require the user to decide:
    #   - stale state from a genuinely different model → re-run with --reset
    #     (or use a fresh --out dir per model);
    #   - a DSL revision of the same model → this is the documented
    #     DEFER_TO_DSL flow, which already prescribes re-running with --reset
    #     after the revision is reviewed at Stage A.
    # Either way, the decision to discard baselines is the USER's, made
    # explicitly — never an automatic side effect of an artefact edit.
    model_id = _sim_sha256(args.dsl) if args.dsl else _trace_identity(args.sim)
    stored_model_id = state.get("model_id")
    if (not args.reset and stored_model_id and model_id
            and stored_model_id != model_id):
        print("[self_heal] MODEL_CHANGED: the model identity differs from the one "
              "these baselines belong to.")
        print(f"[self_heal]   stored model_id {str(stored_model_id)[:12]} ≠ current "
              f"{str(model_id)[:12]}")
        print("[self_heal] Refusing to proceed: continuing would silently discard "
              "the parameter-fingerprint")
        print("[self_heal] and trace-fidelity baselines, disarming the integrity "
              "guards.")
        print("[self_heal] If this is a REVIEWED model/DSL revision (or a different "
              "model in a reused out dir),")
        print("[self_heal] re-run with --reset to explicitly start a new credibility "
              "chain. If you did NOT")
        print("[self_heal] intend the model to change, inspect what edited the DSL "
              "before trusting anything.")
        return 6
    elif model_id and not stored_model_id:
        state["model_id"] = model_id
        _save_state(args.out, state)

    # ── Step 4 approval bookmark ───────────────────────────────────────────
    if args.step4_approved:
        state["step4_approved_sim_hash"] = sim_hash
        _save_state(args.out, state)
        print(f"[self_heal] recorded Step 4 approval for sim {sim_hash[:12]}.")
        if not args.pipeline and not args.phase:
            return 0

    # ── Parameter-diff guard (deterministic, per-invocation) ───────────────
    # A SIM_CODE patch may change the file (hash) but must NOT change any
    # DSL-declared parameter. Baseline the declared-parameter fingerprint on
    # first run; reject any later invocation whose fingerprint drifted.
    param_fp = _param_fingerprint(args.sim)
    baseline_fp = state.get("param_fingerprint_baseline")
    if baseline_fp is None:
        state["param_fingerprint_baseline"] = param_fp
        _save_state(args.out, state)
    elif param_fp and param_fp != baseline_fp:
        _write_illegal_patch_report(args.out, args.sim, baseline_fp, param_fp)
        print("[self_heal] ILLEGAL_PATCH: a self-heal edit changed a DSL-declared "
              "parameter.")
        print("[self_heal] SIM_CODE patches may only fix implementation / trace "
              "emission, not declared")
        print("[self_heal] distributions, rates, capacities, routing, or "
              "compliance_spec. If the fix")
        print("[self_heal] genuinely needs a parameter change, that's a DSL_SPEC "
              "issue: revert it, emit")
        print("[self_heal] DEFER_TO_DSL, revise Step 1, and re-run with --reset to "
              "rebaseline.")
        return 4

    # ── Trace-fidelity guard (deterministic, per-invocation) ───────────────
    # A SIM_CODE patch may fix HOW events are emitted, but must not "fix" a
    # check by deleting real events or synthesizing absent ones. Baseline the
    # trace's event-class profile on first run; on later runs, if a whole event
    # class appeared or disappeared, gate acceptance (SUSPICIOUS_PATCH) unless
    # the user explicitly confirms the change with --accept-trace-change.
    trace_profile = _trace_event_profile(args.sim)
    if trace_profile is not None:
        baseline_profile = state.get("trace_event_profile_baseline")
        if args.accept_trace_change or baseline_profile is None:
            state["trace_event_profile_baseline"] = trace_profile
            _save_state(args.out, state)
            if args.accept_trace_change and baseline_profile is not None:
                print(f"[self_heal] trace profile rebaselined (--accept-trace-change).")
        else:
            deltas = _fidelity_deltas(baseline_profile, trace_profile)
            if deltas:
                _write_suspicious_patch_report(args.out, args.sim, deltas,
                                               baseline_profile, trace_profile)
                print("[self_heal] SUSPICIOUS_PATCH: a patch changed trace fidelity:")
                for d in deltas:
                    print(f"[self_heal]   - {d}")
                print("[self_heal] A fix must not delete real events or synthesize "
                      "absent ones. Revert the")
                print("[self_heal] patch (the failing check is likely a VALIDATOR "
                      "issue), or if the trace")
                print("[self_heal] change is genuinely correct, re-run with "
                      "--accept-trace-change.")
                return 5

    # ── Single-phase mode ──────────────────────────────────────────────────
    if not args.pipeline:
        # If the sim has changed since this phase last passed, that's fine —
        # we're about to (re)run it. But warn the user about OTHER phases that
        # are now stale, so they know the single-phase run isn't a full verdict.
        stale = _stale_phases(state, sim_hash)
        if stale:
            print(f"[self_heal] NOTE: phases {stale} previously passed against a different "
                  f"sim hash and are now STALE. Run --pipeline to re-verify them.")
        return _heal_one_phase(args, state, args.phase, sim_hash)

    # ── Pipeline mode (regression-safe) ────────────────────────────────────
    if args.phases:
        phases = [s.strip() for s in args.phases.split(",") if s.strip()]
    else:
        phases = (["0"] if args.dsl else []) + ["1", "2", "3", "4"]

    stale = _stale_phases(state, sim_hash)
    if stale:
        print(f"[self_heal] sim changed (hash {sim_hash[:12]}); re-verifying stale phases {stale}.")

    for phase in phases:
        if _phase_is_valid(state, phase, sim_hash):
            print(f"[self_heal] phase {phase}: already valid against current sim — skipping.")
            continue
        rc = _heal_one_phase(args, state, phase, sim_hash)
        if rc != 0:
            # FAIL/BLOCK/escalation: stop here and hand control back. After the
            # repair edit, the next invocation re-hashes the sim; because the
            # hash changed, EVERY prior phase becomes stale and is re-verified
            # from the top — that's the no-regression guarantee.
            print(f"[self_heal] pipeline halted at phase {phase} (rc={rc}). "
                  f"Repair the sim and re-run --pipeline.")
            return rc

    print(f"[self_heal] PIPELINE COMPLETE — all of {phases} PASS/WARN against sim {sim_hash[:12]}.")

    # ── Step 4 re-review gate ──────────────────────────────────────────────
    # The deterministic parameter guard above already guarantees declared
    # parameters are unchanged. What remains for Step 4 is the non-mechanical
    # residue: code↔user-intent and manifest accuracy. If the code was patched
    # since Step 4 last approved it, flag that a single Step 4 re-review is
    # needed before the trust verdict is accepted.
    step4_hash = state.get("step4_approved_sim_hash")
    if step4_hash is None:
        print("[self_heal] NOTE: no Step 4 approval recorded. Run --step4-approved "
              "after Step 4 passes so re-review needs can be tracked.")
    elif step4_hash != sim_hash:
        print("[self_heal] STEP4_REREVIEW_REQUIRED: the sim was patched after Step 4 "
              f"approval ({step4_hash[:12]} → {sim_hash[:12]}).")
        print("[self_heal]   Declared parameters are unchanged (parameter guard passed),")
        print("[self_heal]   so this is an intent/manifest re-review only. Route the")
        print("[self_heal]   converged code through Step 4 (Code Alignment Review) once")
        print("[self_heal]   before accepting the trust verdict, then re-run "
              "--step4-approved.")
    else:
        print(f"[self_heal] Step 4 approval current ({step4_hash[:12]}) — no re-review needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
