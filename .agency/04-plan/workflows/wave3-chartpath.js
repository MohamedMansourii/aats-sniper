// AATS Wave 3 — chart-path / regime model ARCHITECTURE (buildable-now items).
// Run:  Workflow({scriptPath: "C:\\dev\\aats\\.agency\\04-plan\\workflows\\wave3-chartpath.js"})
// NOTE: M2-CP-03 (train the regime model) and M2-CP-06 (survivor temporal) are DATA-BLOCKED until >=3,000
// recorded launches — they are NOT in this script. Build the tensor/label/contract/feature/card now; train later.
export const meta = {
  name: 'aats-wave3-chartpath',
  description: 'Wave 3: price-path tensor + regime-label spec + RegimeSignal contract + dev-distribution feature + model-card harness. Sequential, dual-G3, non-destructive review + no-git.',
  phases: [ { title: 'Build' }, { title: 'Review' }, { title: 'Fix' } ],
}
const VERDICT_SCHEMA={type:'object',additionalProperties:false,required:['verdict','summary','blocking_findings','handoff'],properties:{verdict:{type:'string',enum:['PASS','FAIL']},summary:{type:'string'},blocking_findings:{type:'array',items:{type:'object',additionalProperties:false,required:['id','location','issue','required_fix'],properties:{id:{type:'string'},location:{type:'string'},issue:{type:'string'},required_fix:{type:'string'}}}},handoff:{type:'string'}}}
const BUILD_SCHEMA={type:'object',additionalProperties:false,required:['status','summary','files_changed','self_check','handoff'],properties:{status:{type:'string',enum:['COMPLETE','BLOCKED','FAILED']},summary:{type:'string'},files_changed:{type:'array',items:{type:'string'}},self_check:{type:'string'},handoff:{type:'string'}}}
const GUARDS = `PROCESS GUARDS (mandatory): (1) DO NOT run any git command — version control is the Runtime's job; only edit files. (2) Smallest correct change; no unrelated refactors.`
const HR = `HARD RULES (violation = FAIL): the chart-path/regime model is SLOW-loop ONLY (assert_slow_loop_only guard, no ONNX/Rust shim, never on FAST/SNIPE). Its output may ONLY de-risk (distribution/rug-in-progress -> exit/reduce/veto); the accumulation/bullish class is provably INERT (can never delay an exit, relax a stop, or size up). NO win-rate — regime is a multiclass STATE + calibrated prob + uncertainty, never a success rate or price. STRICT point-in-time (T-300a): bars/labels gated at close_slot/outcome_slot vs event-time, no wall-clock, no forward-fill from future bars; leak audit reuses assert_event_time_leq_decision + assert_no_label_taint. Money int lamports/Decimal (prices are ratios, float ok per data-models §0). Frozen contracts not edited without a solana-systems-architect ADR (CP-04 IS that ADR/contract addition — do it properly). is_bootstrap_not_real=True until the R1 corpus exists; DRY_RUN untouched.`
const ITEMS = [
  { id:'M2-CP-01', agent:'feature-quant-engineer', title:'Point-in-time post-migration price-path tensor (CENSORED-aware)',
    brief:`Extend the OHLCV bar assembler (aats/features/ta.py) to emit a normalized, fixed-length price-path TENSOR (log-return sequence + drawdown-from-peak path + holder/volume-delta channels) with a presence/CENSORED mask, anchored strictly to bars with close_slot <= event_time.slot. Reuse the existing proven bar assembly; no new price source. This is the honest INPUT for the regime model. VERIFY: pytest -p no:randomly -q — tensor shape/normalization correct; a future bar (close_slot > event_time.slot) is EXCLUDED (leak test); short-history -> masked, never fabricated. Mutation-meaningful. ruff clean.` },
  { id:'M2-CP-02', agent:'ml-prediction-engineer', title:'Regime-label spec + remains-sellable gate (leak-verified)',
    brief:`Define the leak-free, event-time-joined, VERSIONED regime label taxonomy (accumulation / distribution / rug-in-progress / neutral) over the survivor/exit horizon, with a "remains-sellable at decision-relevant size" gate. Labels live in a SEPARATE dataset joined by event_time ONLY; resolution_event_time strictly > decision; reuse assert_event_time_leq_decision + assert_no_label_taint. Co-verify leak-freedom with backtest-qa (it is a reviewer here). VERIFY: pytest — a label built from a future outcome is rejected by the leak audit; taxonomy is exhaustive+mutually-exclusive; no win_rate field. ruff clean.` },
  { id:'M2-CP-04', agent:'solana-systems-architect', title:'RegimeSignal contract + StateStore de-risk wiring (+ wire orphaned survivor)',
    brief:`Add a frozen RegimeSignal contract (calibrated regime probs + uncertainty + model_version; NO price/size/win-rate) via a proper ADR under .agency/02-architecture/adr/. Pre-stage it in the SLOW loop as a scalar de-risk flag that exit_engine/rule_engine already read (narrative_failure/veto mechanism). Also finally wire the currently-orphaned survivor model into the control plane the same de-risk way. VERIFY: the wiring maps regime output ONLY to de-risk flags (FORCE_EXIT/REDUCE_SIZE/VETO-equiv) — structurally cannot size up; SNIPE reads a pre-computed scalar, never the model; contract carries no price/win-rate. pytest green. ruff clean. Write the ADR file.` },
  { id:'M2-CP-07', agent:'data-ingestion-engineer', title:'Creator/dev-wallet distribution-velocity feature (monotone de-risk)',
    brief:`Add a point-in-time creator-wallet post-migration OUTFLOW-velocity feature (the model-side analogue of Pepe Boost's dev-sell trigger) fed to the survivor + regime models. Event-time only (creator transfers stamped at on-chain slot, no wall-clock). Pin MONOTONE NON-INCREASING (creator selling => probability DOWN) and verify via the sign test, so it can only de-risk. VERIFY: pytest -p no:randomly -q — the feature rises with creator outflow and the monotone-decreasing constraint holds; event-time honesty; refuse-by-default on undecoded outflow. Mutation-meaningful. ruff clean.` },
  { id:'M2-CP-08', agent:'ml-prediction-engineer', title:'Regime model card + reproducible pinned training harness (scaffold)',
    brief:`Ship MODEL_CARD_regime.md (features/label/training-window/calibration/baseline-gap/known-failure-regimes/disable-thresholds) and a pinned, fixed-seed, DETERMINISTIC retraining SCRIPT scaffold (deep-model dep declared but lazily imported; the script must run end-to-end on the synthetic bootstrap corpus and be ready to point at the real corpus later). The card states explicitly NO capital license + NO win-rate + is_bootstrap_not_real until R1. VERIFY: the harness runs deterministically twice to the same numbers on the bootstrap corpus; pytest green for any wiring; no secrets. ruff clean.` },
]
function reviewBrief(role, lens, item, build){return `You are ${role}. Half of dual-G3 for Wave-3 item ${item.id} — "${item.title}". ${lens}
NON-DESTRUCTIVE REVIEW: do NOT modify ANY file during review and do NOT run git; mutation-test on a COPY or by reasoning; leave the tree byte-stable.
${HR}
Engineer reported:
${JSON.stringify(build,null,2)}
Independently VERIFY (read changed files + RUN tests read-only). Confirm SLOW-loop-only + accumulation-class-inert + de-risk-only; STRICT point-in-time leak-freedom (a future bar/outcome must be provably excluded); no win-rate; no frozen-contract drift except a properly-written ADR (CP-04); tests mutation-meaningful and actually pass. PASS only if all hold. Return VERDICT_SCHEMA.`}
const results = []
for (const item of ITEMS) {
  phase('Build')
  let build = await agent(`TASK ${item.id} (Wave 3 — chart-path architecture).\n${GUARDS}\n${HR}\n\n${item.brief}\n\nReturn BUILD_SCHEMA (exact files_changed, commands+counts). End with === HANDOFF ===.`, { agentType: item.agent, label: `build:${item.id}`, phase: 'Build', schema: BUILD_SCHEMA })
  if (!build) { results.push({ id: item.id, gate: 'BLOCKED' }); continue }
  let passed = false
  for (let strike = 1; strike <= 3; strike++) {
    phase('Review')
    const [cr, qa] = await parallel([
      () => agent(reviewBrief('the code-reviewer', 'Focus: contract/ADR correctness, SLOW-only enforcement, no-secrets.', item, build), { agentType: 'code-reviewer', label: `review:${item.id}:code#${strike}`, phase: 'Review', schema: VERDICT_SCHEMA }),
      () => agent(reviewBrief('the backtest-qa-engineer', 'Focus: point-in-time leak-freedom, accumulation-inert + de-risk-only, mutation-meaningful tests that RUN green.', item, build), { agentType: 'backtest-qa-engineer', label: `review:${item.id}:qa#${strike}`, phase: 'Review', schema: VERDICT_SCHEMA }),
    ])
    if (cr && cr.verdict==='PASS' && qa && qa.verdict==='PASS') { passed = true; break }
    if (strike === 3) break
    const findings = [...(cr?cr.blocking_findings.map(f=>({...f,from:'code'})):[]), ...(qa?qa.blocking_findings.map(f=>({...f,from:'qa'})):[])]
    if (!findings.length) continue
    phase('Fix')
    build = await agent(`FIX STRIKE ${strike} for ${item.id}. Both reviewers must PASS.\n${GUARDS}\n${HR}\nFINDINGS:\n${JSON.stringify(findings,null,2)}\nReturn BUILD_SCHEMA.`, { agentType: item.agent, label: `fix:${item.id}#${strike}`, phase: 'Fix', schema: BUILD_SCHEMA })
    if (!build) break
  }
  results.push({ id: item.id, gate: passed ? 'G3-PASS' : 'G3-FAIL', files: build ? build.files_changed : [] })
}
return { wave: 'Wave 3 — chart-path architecture', results }
