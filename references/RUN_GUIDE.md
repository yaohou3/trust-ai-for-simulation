# Running the Trust Framework End-to-End — Plain-English Guide

This guide walks you through the **entire** pipeline, from describing your
system in plain words to getting a final trustworthiness verdict — with **no
coding required**. You mostly give Claude Code plain-English instructions and
let it do the work. Exact commands are included as a backup.

If you ever get stuck, paste any message or report back to Claude and ask
"what does this mean and what do I do next?"

---

## What this framework does, in one sentence

You describe a system in plain English (an emergency department, a factory
line, an ICU); the framework turns that into a runnable simulation **and** a
stack of automatic checks that prove the simulation is trustworthy before you
rely on it for decisions.

---

## The big picture: five stages

The pipeline has many steps, but they group into five stages. You move through
them in order. Each stage either happens by **chatting with Claude Code** (the
creative steps) or by **running a Python command** (the deterministic checks).

| Stage | What happens | How you run it |
|---|---|---|
| **A. Describe & structure** | Your plain-English description becomes a structured spec (the "DSL"), then a reviewer checks it | Chat with Claude Code |
| **B. Build the simulation** | The spec becomes runnable simulation code, then a reviewer checks it | Chat with Claude Code |
| **C. Credibility checks (Phase 0–4)** | Automatic checks: structure, math, validation, uncertainty, sensitivity | **Python command** (this is the deterministic core) |
| **D. Decision checks (Phase 4.5)** | "What-if" scenario tests for the specific decision you care about | Chat with Claude Code (calls a Python helper) |
| **E. Judgment & verdict (Phase 5 + Synthesis)** | An expert-style review of realism, then a final plain-English verdict | Chat with Claude Code |

Stages A, B, D, E are judgment work — they need an LLM, so Claude Code does
them. Stage C is the deterministic core — it runs in Python, costs nothing, and
gives the same answer every time. This guide spends the most time on Stage C
because that's the part you run yourself.

---

## What you need before you start

1. **A computer with Python installed.** To check, open a terminal (Mac:
   "Terminal" app; Windows: "Command Prompt" or "PowerShell") and type:

   ```
   python3 --version
   ```

   If you see something like `Python 3.11.4`, you're set. If not, ask Claude
   Code: *"Help me install Python 3 on my computer."*

2. **The `Trust framework` folder** — the one containing `verify_and_run.py`,
   `validation.py`, `run_validators.py`, `self_heal_orchestrator.py`,
   `dsl_code_coverage.py`, and `description_dsl_coverage.py`. You already
   have it.

3. **Claude Code**, open and pointed at that folder. No API key or extra
   subscription is needed beyond Claude Code itself.

4. **A description of the system you want to model**, in plain English. A few
   sentences to a few paragraphs. Example: *"An ICU with 24 beds, 1 consultant,
   6 residents, 24 nurses. Patients arrive about 6 per day…"*

> **Keep everything in one folder.** Throughout this guide, the two files you
> create (`my_dsl.json` and `my_sim.py`) are saved **inside the `Trust
> framework` folder**, right next to the framework code, and you run all
> commands from inside that folder. That keeps every path simple — just a file
> name, no `../`.

---

## Stage A — Describe your system and structure it

**What this produces:** a structured spec file (`my_dsl.json`) that captures
your description in a form the checks can read.

**Step A1.** Open Claude Code in the `Trust framework` folder.

**Step A2.** Paste an instruction like this, with your own description:

> Here is a description of the system I want to simulate: *[paste your plain-
> English description]*. Using the trust framework in this folder, generate the
> DSL (the structured spec) for it following the DSL Generation step, then run
> the DSL Alignment Review on it. If the review asks for changes, revise the
> DSL and review again until it is "ALIGNED". Then save the final DSL as
> `my_dsl.json` inside the `Trust framework` folder.

**Step A3.** Claude Code will produce the structured spec, check it against
your description (does it capture everything? is every piece traceable?), fix
any gaps, and save the result. When it says the DSL is **ALIGNED**, this stage
is done.

If Claude Code asks you a clarifying question about your system, answer it —
that makes the spec more accurate.

**Step A4 (optional but recommended) — Description→DSL coverage check.** If
your description contains a reference data table with KPI values and confidence
intervals (the kind of "Length of Stay 3.01 days (2.90, 3.13)" rows you'd
quote from a paper), run the table-coverage gate to make sure every reference
KPI was promoted into the DSL:

```
python description_dsl_coverage.py --docx your_description.docx --dsl my_dsl.json
```

It reads any docx table that has confidence intervals, matches each KPI row
against a DSL `aggregate_target`, and FAILs if a row has no matching target or
the DSL value disagrees with the description (with %↔fraction and days↔hours
normalization). This is the cleanest way to catch reference data that
silently dropped on the way to the DSL.

For each declared transition in a `state_transition_rules` entry with a
`competing_risks` arc that can go in more than one direction (e.g. deterioration
vs recovery), add an `emission_contract` field to the arc with one of:
`silent_state_change` (only a state_change event), `resource_event` (clinical
work fires alongside), or `both`. This pins the per-direction event semantics
so the Stage C checks can verify them.

---

## Stage B — Build the simulation

**What this produces:** the simulation program (`my_sim.py`).

**Step B1.** Continue in the same Claude Code session and paste:

> Read `CODEGEN_BRIEF.md` and `my_dsl.json`. Generate the simulation following
> the brief — and do **not** read the checker files (`validation.py`,
> `phase0_vvuq.py`, `process_contracts.py`, `semantic_checks.py`,
> `vvuq_utils.py`, `coverage_validator.py`). Save the code as `my_sim.py`
> inside the `Trust framework` folder (next to `my_dsl.json`), then run the
> Code Alignment Review against `my_dsl.json`. If the review asks for changes,
> revise and review again until it is "ALIGNED".

**Why "don't read the checker files"?** If Claude Code studies the checks
before writing the simulation, it can quietly write code that's tailored to
pass the checks rather than to faithfully model your system — that's "teaching
to the test," and it would undermine the whole point of the verification. The
simulation should be written from your spec and the trace contract only. The
`CODEGEN_BRIEF.md` file contains exactly that (the contract plus the rules), so
Claude Code has everything it needs without opening the validators. This also
makes Stage B much faster — the brief is a few KB versus the 200+ KB of checker
files Claude Code would otherwise read.

**Step B2.** Claude Code writes the simulation, then a reviewer pass checks
that the code faithfully implements the spec (right rates, right capacities,
right structure) and emits the standard "trace" the checks need. When it says
the code is **ALIGNED**, you now have your two key files: `my_dsl.json` and
`my_sim.py`.

**Step B3 — declared-element implementation manifest.** Have Claude Code add
a `declared_element_implementation_status` block to the `MANIFEST` in
`my_sim.py`. For every element ID in `my_dsl.json` it records the
implementation status, with one of:

- `FULL` — implemented as declared, with `code_citation: "my_sim.py:<lines>"`
- `PARTIAL` — partly implemented, with a `gap_description`
- `NOT_IMPLEMENTED` — not implemented, with a `justification`
- `DEFERRED_TO_DSL` — needs a DSL revision, with a `justification`

This is a deterministic counterpart to the LLM Code Alignment Review:
`verify_and_run.py` reads this block at Stage C preflight and refuses to start
verification if any declared DSL element is missing or unjustified. The Code
Alignment Review can be persuaded by prose to ratify a silent coverage gap;
enumeration + manifest lookup cannot.

You can also run the coverage check standalone before kicking off Stage C:

```
python dsl_code_coverage.py --dsl my_dsl.json --sim my_sim.py
```

You're now ready for the deterministic core. The simulation has never seen the
checks — so when Stage C verifies it, that verification is genuinely
independent.

---

## Stage C — Credibility checks (Phase 0–4) — the Python core

This is the part you run with **one command**, `verify_and_run.py`. It is the
single front door to the whole credibility core: it checks structure (Phase 0),
math (Phase 1), validation (Phase 2), uncertainty (Phase 3), and sensitivity
(Phase 4); it blocks any fix that cheats; and **only if everything passes does
it print a green VERIFIED banner and write a "proof of verification" file.**
When a check fails, it helps fix the simulation and re-checks — automatically.

**Two preflight gates run before the phases begin.** First, the
*harness-provenance* gate refuses to start if any of the canonical checker
files are missing or substituted — a self-written grader cannot grade itself.
Second, the *DSL→code coverage* gate refuses to start unless your sim's
`MANIFEST` (the `declared_element_implementation_status` block from Step B3)
covers every element ID in `my_dsl.json` with a status and justification. If
either gate trips, the run never gets to Phase 0; the banner reads STOP and
the report tells you exactly which elements are missing.

**A few checks worth knowing the name of so the report reads cleanly.**
Phase 1 §1.4 verifies that recurring activities, scheduled windows, and
resource busy-times match what the DSL declared — catching the "free
parameter tuned to a target" pattern where, say, a 1h cadence is silently
implemented as 2.5h to hit a length-of-stay number. Phase 1 §1.5 verifies the
`emission_contract` you declared on each state-transition arc (Step A4) —
so if you said a recovery transition should be silent and the code emits a
hard-pool team event, the check FAILs the arc by name. Phase 4 §4.2.3
verifies that a declared `barrier_fraction` actually gates preempt timing
(derived from trace event timing, not from a sim-supplied progress field)
and flags `INERT_PARAMETER` when the barrier is declared but not enforced.
Phase 4 §4.2.6 verifies that every declared `preemption_rule` has positive
evidence in the trace, negative evidence (a `no_initiate` rule that fired),
or an explicit annotation; a rule with none of the three is reported as
`STRUCTURAL IMPOSSIBILITY` — the rule was declared but cannot fire under the
code's logic. You don't need to read these names to use the framework, but
they make the FAIL reports navigable.

> ## ⭐ The one rule that keeps you safe
>
> **Trust ONLY the green `VERIFIED` banner that `verify_and_run.py` prints on
> your screen.** A result is trustworthy *only* when you have seen that banner
> for the current code.
>
> Do **not** trust a result because Claude Code *says* "done", "fixed", or
> "verified" in chat. Claude Code is helpful but can be confidently wrong — it
> may propose a code change that looks right and isn't. You don't have to judge
> the code yourself; that's the whole point. The command judges it for you. **No
> green banner → the result is UNVERIFIED → don't use it.**
>
> This is safe because the command refuses to bless a fix that cheats — one that
> changes the model's declared numbers, or that makes whole categories of events
> appear or vanish. Those are blocked automatically, whether or not you'd have
> spotted them.

### The easy way: let Claude Code run it

**Step C1.** In Claude Code, paste (adjust file names to match yours):

> Run the trusted verification on my simulation:
> `python verify_and_run.py --sim my_sim.py --dsl my_dsl.json --out vvuq_out`
> If it stops with a STOP banner, read the report it names, fix **only** the
> simulation file as instructed — never the checker files, and never a declared
> number — then run the **same command** again. Keep looping until it prints the
> green "VERIFIED" banner. Then tell me each phase's status and whether a Step 4
> re-review is needed. Do not tell me the result is verified unless that green
> banner actually appeared.

**Step C2.** Claude Code runs the checks and:
- If everything passes → the command prints a green **VERIFIED** banner and
  writes `vvuq_out/verified_manifest.json` (your proof).
- If a check fails → it reads the repair instructions, fixes your simulation,
  and re-runs. Each re-run re-checks the guards, so a cheating fix can't slip
  through.
- It stops on its own after at most three repair attempts per phase, or
  immediately if a fix tried to cheat, and tells you what needs your attention.

**Step C3.** Confirm it for yourself: ask *"Did the green VERIFIED banner
actually appear? Show me the last lines of output and the contents of
verified_manifest.json."* Then: *"Summarize what passed and what needs
attention, in plain language."*

You can re-check the last verdict at any time without re-running everything:

```
python verify_and_run.py --sim my_sim.py --status
```

It will tell you whether the simulation is currently VERIFIED, or whether the
code changed since (which makes the old verdict STALE — re-run to re-verify).

### The manual way: copy-paste commands

If you'd rather run it yourself in a terminal:

1. **Go into the framework folder.** Type `cd ` (with a space), drag the
   `Trust framework` folder onto the terminal, press Enter.
2. **Install the one dependency** the simulations need:
   ```
   pip install simpy --break-system-packages
   ```
3. **Run the trusted verification** (Phases 0–4, with automatic re-checking and
   the anti-cheating guards). Because your two files live inside this folder, the
   paths are just file names:
   ```
   python verify_and_run.py --sim my_sim.py --dsl my_dsl.json --out vvuq_out
   ```
   Replace the file names with yours. Drop `--dsl my_dsl.json` if you only want
   Phases 1–4 (skip the structural Phase 0).
4. **Look at the banner at the end** (see the decision table below). On success,
   the green VERIFIED banner names a `vvuq_out/verified_manifest.json` file —
   that's your proof. The detailed reports also land in `vvuq_out` as
   `phaseN_summary.md` (easy to read) and `phaseN_report.json` (full detail).

When this stage ends with the green **VERIFIED** banner, the simulation is
**credible** — internally consistent and statistically sound. The remaining
stages confirm it's also **applicable** to your decision and **realistic**.

### What the banner is telling you — decision table

| Banner you see | What it means | What to do |
|---|---|---|
| 🟢 **VERIFIED** | Passed every gate; produced from your approved spec; no cheating. A `verified_manifest.json` was written. | Safe to review. Continue to Stage D. |
| 🔴 **STOP — a check FAILED** | A real problem; a repair instruction was written. | Let Claude Code apply the fix to the simulation only, then re-run the **same** command. Loop until green. |
| 🔴 **STOP — changed numbers (ILLEGAL_PATCH)** | A fix tried to change a declared model number to pass. Blocked. | Tell Claude Code to **revert** it and fix the code without touching any declared number. If the model truly needs a different number, that's a Stage A change. |
| 🔴 **STOP — changed behavior (SUSPICIOUS_PATCH)** | A fix made a whole category of events appear or vanish — a sign of hiding a problem. Blocked. | Tell Claude Code to **revert** it. The failing check is most likely a framework issue, not a model bug. |
| 🔴 **STOP — repair gave up (escalation)** | A check failed three times; too deep for a small patch. | Ask Claude Code to read the escalation file and explain what kind of problem it is. |
| 🔴 **STOP — verifier couldn't run** | The simulation or its state failed to load. | Share the whole message with Claude Code and ask it to fix the load error. |
| 🔴 **STOP — DSL→code coverage** | The sim's `MANIFEST` is missing entries for one or more declared DSL elements, or an entry is marked `PARTIAL`/`NOT_IMPLEMENTED` without a justification. | Tell Claude Code to add the missing or unjustified entries to `MANIFEST["declared_element_implementation_status"]` in `my_sim.py` — see Step B3 — then re-run. |
| 🔴 **STOP — STRUCTURAL IMPOSSIBILITY (4.2.6)** | A declared `preemption_rule` produced zero positive evidence and no manifest annotation — the rule may be unreachable under the current code. | Either fix the code path that prevents the rule from firing, or add an explicit `not_applicable_under_workload` annotation in `MANIFEST["preemption_rule_annotations"]` if the rule is genuinely inert in nominal load. |
| 🔴 **STOP — INERT_PARAMETER (4.2.3)** | A declared `barrier_fraction` is read by the sim but the actual preempt timing ignores it (the diagnostic says "trivially satisfied" or similar). | Fix the sim to actually enforce the barrier (preempts must wait for the served fraction to reach `barrier_fraction × duration`) or remove the rule's `barrier_aware` policy if no enforcement is intended. **The 4.2.3 diagnostic now includes a residual-analysis block:** when the violations share a structural fingerprint (e.g. all same-tick races), the diagnostic emits a `STRUCTURAL CEILING REACHED` signal with `architectural_implication: MECHANISM_INTERCEPTION_REQUIRED` and lists incompatible patterns (e.g. `PreemptiveResource.request(preempt=True)` with external pre-wait) so the agent does not waste iterations parametrically tuning a mechanism that cannot satisfy the check. When the verdict is `PARAMETRIC_TUNING_PROMISING`, continued parametric repair is the right next step. |

**The unifying rule:** every red banner means **UNVERIFIED — do not use the
result.** Only the green one is your go-ahead.

---

## Stage D — Decision checks (Phase 4.5)

**What this does:** tests the specific "what-if" claims you care about — e.g.,
"if we add a nurse, length-of-stay drops" or "if response is delayed, harm
rises." It runs the simulation twice (with and without the change) and checks
the result moved the way you expect.

**Step D1.** In Claude Code, paste:

> Run Phase 4.5 (the counterfactual scenario harness) on `my_sim.py` using the
> scenarios declared in `my_dsl.json`. Use the PROVIDED `semantic_scenarios.py`
> in this folder — do NOT write your own harness. If it is missing, stop and tell
> me rather than re-creating it. Report each scenario's verdict and the overall
> result.

> **Use the provided harness — never a self-written one.** The harness is the
> grader. If Claude Code can't find `semantic_scenarios.py` and offers to "implement
> one to spec," say no: a grader written by the same assistant that wrote the
> simulation will be more lenient and will pass code the real grader fails. Restore
> the file from the Trust framework folder instead.

**Step D2.** Claude Code runs the paired comparisons and reports which
decision-claims hold. A few things to understand in the result:

These findings are **advisory** — they inform your decision but cannot overturn
the credibility result from Stage C.

Each scenario carries a **tier**. *Declared* (you wrote it) and *SME-approved* (an
expert vouched for the expected outcome) scenarios are authoritative. *Canonical*
scenarios are the framework's fixed theory checks. *Auto-generated* scenarios — ones
an LLM proposed but no expert approved — are advisory only: they're direction-only
(no magnitude), they can flag a problem, but they can't by themselves earn a clean
pass. If the whole result is `PASS_ADVISORY_ONLY`, that means *only* auto-generated
scenarios ran — get an expert to approve the key ones before trusting it for a
decision.

A scenario can come back **INCONCLUSIVE** — the effect couldn't be distinguished
from noise (the confidence interval straddles zero). That is *not* a pass and *not*
a failure; it usually means too few seeds. Re-run with more seeds (the harness now
defaults to 12; bump higher for small effects) before reading anything into it.

And the integrity rule that applies here too: a scenario's expected direction must
never be relaxed to "any" to make it pass. A test that can't fail proves nothing —
the harness scores it `NOT_A_CLAIM`, never PASS.

### If Phase 4.5 says `PASS_NO_SCENARIOS` — you need to feed it scenarios

`PASS_NO_SCENARIOS` means the harness ran but your DSL declared no "what-if" claims
to test, so it had nothing to falsify. That's honest, but it's *no evidence*. There
are three ways to give it something to run:

**Fastest — no DSL edit:** re-run with `include_canonical=True`. The framework
synthesizes 7 standard scenarios from your DSL (load-up on the bottleneck,
capacity-up, etc.). Good directional smoke test, zero setup.

**Let an LLM propose case-specific scenarios (generate → ingest → run):** this is
the path for complex systems where you can't enumerate the edge cases yourself.

> 1. **Generate.** In Claude Code: *"Act as the scenario generator (scenGenP):
>    working ONLY from my_dsl.json — not the simulation code — propose typed
>    counterfactual scenarios as `===SCENARIO_JSON_START===` blocks. Save your full
>    response to `gen_response.txt`."*
> 2. **Ingest.** Run:
>    `python scenario_ingest.py --from gen_response.txt --dsl my_dsl.json --merge`
>    This parses the blocks, forces them to the advisory (`auto_generated`,
>    direction-only) tier, rejects any non-falsifiable ones, and writes them into
>    your DSL's `compliance_spec`. (Leave off `--merge` to write a review sidecar
>    first.)
> 3. **Run.** Re-run Phase 4.5 — the scenarios now appear as the advisory tier.

**Promote to authoritative:** an auto-generated scenario is advisory and
direction-only. If a domain expert reviews one and vouches for its expected
outcome, edit that scenario in the DSL to `"provenance": "sme_approved"` (and add a
`min_effect_size` if they'll commit to a magnitude). It then carries full weight.

One dependency for the generated/declared scenarios: each scenario's metric name
and override key (e.g. `arrival_rate_scale`, `capacity`, `routing_probability`)
must match what your simulation actually exposes and reports — otherwise it runs but
the change doesn't bite and comes back INCONCLUSIVE. If unsure, ask Claude Code to
list your sim's `config_overrides_supported` and `kpis_implemented` from its
manifest and reconcile the scenario keys against it.

---

## Stage E — Judgment and final verdict (Phase 5 + Synthesis)

**What this does:** an expert-style review of things automatic checks can't
see (does the model behave like the real world? are any results suspiciously
clean?), followed by a trust verdict reported in **three separate dimensions**.

**Step E1.** In Claude Code, paste:

> Run Phase 5 (the plausibility review) over the Phase 4 and Phase 4.5 results
> for my simulation. Emit typed, evidence-citing flags (Phase 5 is advisory — it
> can raise concerns but cannot fail the model on its own). Then run
> `decision_validity.py` and produce the Final Synthesis as three separate
> dimensions with reasons.

**Step E2 — the three-dimension verdict.** Instead of one collapsed PASS (which
invites "it passed, so trust it blindly"), you get three verdicts that mean
different things:

*Credibility* — is the model built correctly? (Phases 0–4.) *Causal Validity* —
does it respond correctly when you intervene? (Phase 4.5.) *Decision Validity* —
is it safe to use for **the specific decision** you care about?

These are gated, computed deterministically by `decision_validity.py` so the
verdict can't be quietly merged:

The dimensions are **monotonic** — Decision Validity can never be better than the
worse of the other two. A model that isn't credible, or whose causal response is
unverified, cannot be decision-valid no matter how good the prose sounds.

Decision Validity needs **authoritative** coverage of your decision: the
decision-relevant what-if must be a *declared* or *SME-approved* Phase 4.5 PASS.
If your only Phase 4.5 evidence is auto-generated (advisory) or you ran no
scenarios, Decision Validity is capped at **WARN** — this is the
`PASS_ADVISORY_ONLY` / `PASS_NO_SCENARIOS` situation, enforced.

Phase 5 is **advisory**: a high-severity realism or decision-risk flag pulls
Decision Validity down to WARN, but Phase 5 can never FAIL the model by itself —
only the deterministic lower layers can.

So a very common — and honest — result is **Credibility: PASS / Causal Validity:
PASS / Decision Validity: WARN**, meaning "the model is sound and responds
correctly, but it hasn't yet been expert-verified for *this* decision." The
Synthesis spells out exactly what would move Decision Validity to PASS (usually:
get an SME to approve the decision-relevant scenario).

---

## What the results mean

Each individual check ends in one of five words:

- **PASS** — satisfied. Good.
- **INFO** — not applicable here, or just a note. Nothing to do.
- **WARN** — a soft concern worth knowing, not a defect. The pipeline continues.
- **FAIL** — a real problem the simulation must fix. The pipeline stops and
  writes a repair prompt.
- **BLOCK** — a fundamental/structural problem (often a crash). Like FAIL, but
  more serious.

Each phase ends with a tally like
`{PASS: 20, WARN: 0, FAIL: 0, BLOCK: 0, INFO: 2}`. A phase is "good to continue"
when FAIL and BLOCK are both 0.

The phases in plain terms:
- **Phase 0** — does the simulation match the description it was built from?
- **Phase 1** — is the math internally consistent (nothing appears or vanishes)?
- **Phase 2** — does it behave correctly in easy and extreme situations?
- **Phase 3** — are the numbers statistically trustworthy (averaging, warm-up,
  repeatability)?
- **Phase 4** — does it respond sensibly when inputs change (more demand →
  longer waits)?
- **Phase 4.5** — does it correctly answer your specific what-if decision?
- **Phase 5** — would a domain expert find it realistic and believable?

---

## When things need a human

Most failures fix themselves in the repair loop. A few messages mean the loop
deliberately stopped and wants you. In every one of these cases the simulation
is **UNVERIFIED** until you've resolved it and seen the green banner.

**"ILLEGAL_PATCH"** — a fix tried to change a *declared number* in the model (a
service rate, a capacity) instead of fixing a code bug. The framework blocks
this on purpose, because changing the model's declared numbers to make a check
pass would be cheating. Ask Claude Code: *"Read illegal_patch.md and either fix
the code without changing declared parameters, or tell me this needs a change
to my original description."* If it genuinely needs a different number, that's a
change back at Stage A, not a quick fix.

> **Stale-state false alarm:** if you ran a *different* model into the same
> `vvuq_out` folder, the saved baseline belongs to the old model and this report
> is spurious — not a change in your code. When you pass `--dsl`, the orchestrator
> now detects the model switch and auto-rebaselines instead of crying
> ILLEGAL_PATCH. If you still hit it after switching models, re-run with `--reset`
> (or, cleaner, use a separate `--out` folder per model). Either way, **do not
> touch the simulation.**

**"SUSPICIOUS_PATCH"** — a fix made a whole *category of events* appear or vanish
in the simulation's history (for example, quietly dropping the events that a
reworked item really produces, or inventing "departure" events to satisfy a
check). This is the signature of hiding a problem rather than fixing it, so the
framework blocks it. Ask Claude Code: *"Read suspicious_patch.md, revert that
change, and treat the failing check as a possible framework issue rather than a
model bug."* The detailed report (`suspicious_patch.md`) opens with a plain-
language explanation of exactly what changed.

**"escalation" (escalation_phaseN.md)** — a check failed three times and
couldn't be auto-fixed. The problem is deeper than a small code bug. Ask Claude
Code to read the escalation file and explain what kind of problem it is.

**"STEP4_REREVIEW_REQUIRED"** — the simulation was edited during Stage C, so its
code should be re-checked against your description once before you fully trust
it. The declared numbers are guaranteed unchanged (the framework verified that);
this is just a final intent review. Ask Claude Code: *"Re-run the Code Alignment
Review on the patched simulation and confirm it still matches my description,"*
then it's safe to continue.

---

## Practical tips

- **You can re-run anytime.** Re-running the Stage C command is safe — if
  nothing changed, it skips finished work and completes instantly.
- **Re-running without editing the sim doesn't burn an attempt.** The self-heal
  budget is *progress-aware*: an investigation re-run (same sim) is free, and a
  re-run that strictly shrinks the failure set (some checks newly PASS, none
  newly fail) is also free. Only a no-effect patch or a regression consumes an
  attempt. So read the reports first, *then* patch.
- **Many failures? Cluster by root cause.** A0–A8 / B-series checks usually
  share root causes (one missing event class can FAIL 5 checks at once). Ask
  Claude Code to triage the failures by suspected root cause and fix one
  cluster at a time rather than 20 individual patches. The repair prompt now
  surfaces this hint automatically when there are 10+ failures. For very large
  batches, pass `--max-attempts 10` instead of the default 3.
- **Check the last verdict without re-running** with
  `python verify_and_run.py --sim my_sim.py --status`. It tells you whether the
  simulation is currently VERIFIED, or STALE because the code changed since.
- **Start fresh** by adding `--reset` to the Stage C command (use this after you
  intentionally changed your description and regenerated the code).
- **Run a subset of phases** with `--phases 1,2,3,4` (include `0` only if you
  also pass `--dsl`).
- **Everything is logged.** The `vvuq_out` folder keeps every report, repair
  attempt, and the `verified_manifest.json` proof, so you (or a reviewer) can
  see exactly what happened.
- **When in doubt, ask Claude Code.** Paste any message you don't understand and
  ask what it means and what to do next.

---

## The shortest possible version

Open Claude Code in the `Trust framework` folder and work through five chats:

1. *"Here's my system description: [paste]. Generate the DSL, run the DSL review
   until ALIGNED, save it as my_dsl.json inside the Trust framework folder."*
2. *"Read CODEGEN_BRIEF.md and my_dsl.json. Generate the simulation following the
   brief — do NOT read the checker files. Run the code review until ALIGNED, save
   it as my_sim.py inside the Trust framework folder."*
3. *"Run `python verify_and_run.py --sim my_sim.py --dsl my_dsl.json --out
   vvuq_out`. If it stops with a STOP banner, read the report it names, fix only
   my_sim.py (never a declared number), and re-run the same command — loop until
   the green VERIFIED banner appears. Only then tell me it's verified, and
   summarize the results."*
4. *"Run Phase 4.5 on my_sim.py using semantic_scenarios.py; report the scenario
   verdicts."*
5. *"Run Phase 5 and the Final Synthesis; give me the overall trust verdict in
   plain language."*

That's the whole framework, end to end.
