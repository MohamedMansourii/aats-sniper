# ▶ RESUME HERE — AATS (60-second orientation)

**You are the Agency Runtime** continuing the AATS Solana meme-coin bot. You dispatch Workflows/agents (dual-G3),
you write no production code yourself. **Read `.agency/STATE.md` next for full detail.** Branch `aats-sniper-build`.

## The one-line status
The whole bot is **built, safe, and security-audited**; the **edge proof runs on real data** and currently says
**NO-GO** — BUT the **momentum/reaction-entry strategy showed the first GATE-B PASS** (the model beats a losing
baseline). It's not proven only because the model is very selective and needs a **bigger corpus** to make GATE-A
statistically testable. A live collector is accruing that corpus autonomously.

## ⛔ Codex is DROPPED — you build everything yourself (CEO order, 2026-07-06).

## Your FIRST action on resume
1. Check the collector + corpus:
   ```
   tasklist | grep 22012        # collector alive? if not: Start-Process python C:/aats_shadow/_collector.py -WindowStyle Hidden
   wc -l < C:/aats_shadow/labeled_corpus.jsonl    # corpus size
   ```
2. **If corpus ≥ ~3000 → RE-RUN the momentum edge proof (the decisive test):**
   ```
   cd /c/dev/aats; export RPC_PRIMARY=$(grep '^RPC_PRIMARY=' .env | cut -d= -f2- | tr -d '\r'); export DRY_RUN_ENABLED=true
   python -m aats.backtest.run_edge_proof --corpus C:/aats_shadow/labeled_corpus.jsonl --strategy momentum --entry-horizon 60 --out C:/aats_shadow/momentum_result.json
   ```
   Record the verdict (`.agency/05-reports/qa/`), commit, update STATE. **Never fabricate a GO** — NO-GO stays NO-GO.
3. **If corpus < 3000 →** let it accrue; meanwhile advance the roadmap in `STATE.md §3` (bonding-curve price fidelity
   Workflow, then Wave-4 detection / CP-07 — all Claude-owned).

## The rule that matters most
No real money moves, ever, until Phase-5 returns a real **GO** (model beats baseline net-of-cost, statistically) AND
the security audit passes AND the CEO authorizes. Until then: honest verdicts only, `DRY_RUN` locked.
