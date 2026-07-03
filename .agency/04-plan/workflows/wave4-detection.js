// AATS Wave 4 — detection completeness (buildable-now items).
// Run:  Workflow({scriptPath: "C:\\dev\\aats\\.agency\\04-plan\\workflows\\wave4-detection.js"})
// NOTE: E-M1-01 (live-validate Geyser multi-venue) needs a REAL paid Geyser endpoint (CEO-provided) and is
// NOT in this script — do it once RPC_PRIMARY/GEYSER_ENDPOINT point at a real gRPC endpoint; until then, do
// NOT claim multi-venue live coverage. This script covers the code/provenance/honesty fixes that need no key.
export const meta = {
  name: 'aats-wave4-detection',
  description: 'Wave 4: fingerprint fix + real decoder fixtures + dev-wallet lineage ingestion + shredstream-param honesty + bundler_cluster_id ADR. Sequential, dual-G3, non-destructive review + no-git.',
  phases: [ { title: 'Build' }, { title: 'Review' }, { title: 'Fix' } ],
}
const VERDICT_SCHEMA={type:'object',additionalProperties:false,required:['verdict','summary','blocking_findings','handoff'],properties:{verdict:{type:'string',enum:['PASS','FAIL']},summary:{type:'string'},blocking_findings:{type:'array',items:{type:'object',additionalProperties:false,required:['id','location','issue','required_fix'],properties:{id:{type:'string'},location:{type:'string'},issue:{type:'string'},required_fix:{type:'string'}}}},handoff:{type:'string'}}}
const BUILD_SCHEMA={type:'object',additionalProperties:false,required:['status','summary','files_changed','self_check','handoff'],properties:{status:{type:'string',enum:['COMPLETE','BLOCKED','FAILED']},summary:{type:'string'},files_changed:{type:'array',items:{type:'string'}},self_check:{type:'string'},handoff:{type:'string'}}}
const GUARDS = `PROCESS GUARDS (mandatory): (1) DO NOT run any git command — version control is the Runtime's job; only edit files. (2) Smallest correct change; no unrelated refactors.`
const HR = `HARD RULES (violation = FAIL): raw ingestion produces DATA only — no scoring, no trade decision, no win-rate. Any downstream use is de-risk/exclude only. STRICT point-in-time (T-300a): event-time from on-chain slot/block_time only, never wall-clock; a decoder that cannot determine on-chain time holds the event PENDING (None), never substitutes. Money int lamports/Decimal. No secrets in code/logs (keys from env, never logged). Frozen contracts not edited without a solana-systems-architect ADR (E-M1-03 IS an ADR — write it, do not unilaterally change the field).`
const ITEMS = [
  { id:'E-M1-02', agent:'data-ingestion-engineer', title:'Fix deploy_template_fingerprint (template-invariant, not mint)',
    brief:`deploy_template_fingerprint is currently sha256(creator_wallet + mint[:8]) — it changes every launch even for a repeat rug-factory creator, so it cannot express the "same template, new wallet" pattern. Fix: decode the pump.fun/PumpSwap create's inner mplTokenMetadata URI (referenced by account index but not parsed today) and fingerprint on the URI domain+path-SHAPE (not the full URI), which stays stable across a template's reuse by different funding wallets. Pure decode-time fix, same str|None field, no contract shape change. VERIFY: pytest tests/ingestion -p no:randomly -q — two different mints from the same creator+template produce the SAME fingerprint; different templates differ; malformed metadata -> None (refuse). Mutation-meaningful. ruff clean.` },
  { id:'E-M1-05', agent:'data-ingestion-engineer', title:'Real captured-mainnet-signature decoder fixtures (decode-and-assert)',
    brief:`Replace the synthetic hand-built decoder fixtures (fabricated signatures) with at least ONE real captured mainnet transaction per venue (pump.fun create/buy/sell/migrate, Raydium v4 initialize2, CPMM init+swap, PumpSwap create_pool/swap): fetch via getTransaction, commit the raw tx as a fixture, and assert the decoder produces the exact typed LaunchEvent. This is the M1 charter's own mandated self-check, currently unmet — most material for the Raydium v4/CPMM paths that are unproven live. VERIFY: pytest tests/ingestion -p no:randomly -q — each new fixture decodes to the asserted LaunchEvent; offsets/discriminators hold on REAL bytes. ruff clean. (Fetching real txs needs RPC_PRIMARY; if unavailable, capture what you can and mark the rest pending — do NOT fabricate.)` },
  { id:'E-M1-06', agent:'data-ingestion-engineer', title:'Raw dev-wallet history + funding-lineage ingestion (feeds E15/E16)',
    brief:`Ingest, per creator_wallet: (a) prior LaunchEvents from that wallet already on our own bus (cheap, no new dependency), and (b) the wallet's first N SOL-funding inbound transfers (getSignaturesForAddress + getTransaction) to surface the funding-source address, staleness-tagged. M1 STOPS at publishing the raw {creator_wallet, prior_launch_count, funding_source_wallet, funding_source_seen_before: bool} tuple — NO scoring here (that is risk-guardrails' E15/E16). Rate-limited RPC stays OFF the hot snipe path (enrichment tier). VERIFY: pytest tests/ingestion -p no:randomly -q — the tuple is produced point-in-time (only funding with slot <= deploy event-time); undecodable -> refuse/None; no scoring/win-rate field. Mutation-meaningful. ruff clean.` },
  { id:'E-M1-07', agent:'data-ingestion-engineer', title:'Remove/guard the dead shredstream_endpoint param (fail loud)',
    brief:`GeyserTransport accepts a shredstream_endpoint constructor arg that is stored and NEVER read — the exact "silent gap" the charter warns against. Given deploy/colocation-rpc-plan.md frames ShredStream as an operator-triggered infra upgrade behind INFRA_TIER, make a no-op-safe stub transport that raises NotImplementedError with a clear message when INFRA_TIER=colo_shred is set but no client exists — rather than silently ignoring a configured endpoint. VERIFY: pytest tests/ingestion -p no:randomly -q — a configured shredstream endpoint no longer silently no-ops (raises or is explicitly logged as unimplemented). ruff clean.` },
  { id:'E-M1-03', agent:'solana-systems-architect', title:'ADR: bundler_cluster_id dead field (populate vs deprecate)',
    brief:`LaunchEvent.bundler_cluster_id is REQUIRED by data-models.md for C-10 group-aware purge but is unconditionally None everywhere; the equivalent capability now lives in feature-quant's bundling_detected/sniper_cluster_score. Write an ADR under .agency/02-architecture/adr/ deciding EITHER (a) M1 implements a decode-time co-signer/co-slot clustering heuristic to honestly populate it, OR (b) the field is formally deprecated since its role is covered elsewhere. Do NOT unilaterally change the frozen field — the ADR is the deliverable; if (a), also spec the heuristic for a follow-up build mission. VERIFY: the ADR is written, references the evidence, and states the decision + migration. No code contract change without the ADR's authorization.` },
]
function reviewBrief(role, lens, item, build){return `You are ${role}. Half of dual-G3 for Wave-4 item ${item.id} — "${item.title}". ${lens}
NON-DESTRUCTIVE REVIEW: do NOT modify ANY file during review and do NOT run git; mutation-test on a COPY; leave the tree byte-stable.
${HR}
Engineer reported:
${JSON.stringify(build,null,2)}
Independently VERIFY (read changed files + RUN tests read-only). Confirm raw-data-only (no scoring/win-rate); STRICT point-in-time (no wall-clock, no lookahead); refuse-by-default on undecodable; no frozen-contract drift except a properly-written ADR (E-M1-03); no secret logged; fixtures are REAL captures not fabricated (E-M1-05); tests mutation-meaningful and actually pass. PASS only if all hold. Return VERDICT_SCHEMA.`}
const results = []
for (const item of ITEMS) {
  phase('Build')
  let build = await agent(`TASK ${item.id} (Wave 4 — detection completeness).\n${GUARDS}\n${HR}\n\n${item.brief}\n\nReturn BUILD_SCHEMA (exact files_changed, commands+counts). End with === HANDOFF ===.`, { agentType: item.agent, label: `build:${item.id}`, phase: 'Build', schema: BUILD_SCHEMA })
  if (!build) { results.push({ id: item.id, gate: 'BLOCKED' }); continue }
  let passed = false
  for (let strike = 1; strike <= 3; strike++) {
    phase('Review')
    const [cr, qa] = await parallel([
      () => agent(reviewBrief('the code-reviewer', 'Focus: decode correctness, real-fixture authenticity, ADR quality, no-secrets.', item, build), { agentType: 'code-reviewer', label: `review:${item.id}:code#${strike}`, phase: 'Review', schema: VERDICT_SCHEMA }),
      () => agent(reviewBrief('the backtest-qa-engineer', 'Focus: point-in-time honesty, raw-data-only, fixtures are REAL not fabricated, mutation-meaningful tests that RUN green.', item, build), { agentType: 'backtest-qa-engineer', label: `review:${item.id}:qa#${strike}`, phase: 'Review', schema: VERDICT_SCHEMA }),
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
return { wave: 'Wave 4 — detection completeness', results }
