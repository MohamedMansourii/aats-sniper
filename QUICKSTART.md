# Quick Start — Running Your AI Coding Agency

You are the CEO. This guide gets you from zero to a running build with one instruction.

## What's in the box

```
your-project/
├── CLAUDE.md                      # The agency charter (roles, rules, gates, protocol)
├── QUICKSTART.md                  # This file
├── docs/
│   └── WORKFLOW.md                # End-to-end delivery pipeline
└── .claude/
    └── agents/                    # The 12 agents
        ├── orchestrator.md          ├── devops-engineer.md
        ├── solutions-architect.md   ├── qa-engineer.md
        ├── product-analyst.md       ├── security-engineer.md
        ├── uiux-designer.md         ├── code-reviewer.md
        ├── frontend-engineer.md     ├── mobile-engineer.md
        ├── backend-engineer.md      └── docs-delivery.md
```

The `.agency/` workspace (specs, blueprints, task board, reports) is created
automatically during your first project — you never touch it.

## Step 1 — Drop it into a project

**This folder is already set up.** For any NEW project:
1. Create an empty folder for the project.
2. Copy `CLAUDE.md`, `docs/WORKFLOW.md`, and the whole `.claude/` folder into it.
3. Open a terminal in that folder and run `claude` (or open it in the Claude Code app).

## Step 2 — Verify the agency is loaded

In Claude Code, type:
```
/agents
```
You should see all 12 agents listed as project agents. (Optional: in `/agents` you can
pin stronger models to `orchestrator` and `solutions-architect`, e.g. Opus, and a faster
model to high-volume agents — defaults work fine.)

## Step 3 — Kick off with a single instruction

Just describe what you want built. That's it — the charter does the rest. Examples:

**Small:**
```
New project brief: a one-page landing page for "Atlas Coffee", a specialty coffee
subscription. Warm premium feel, email signup, mobile-first. Deploy-ready.
```

**Large:**
```
New project brief: a two-sided marketplace platform — web app + iOS/Android apps —
where homeowners book vetted contractors. Payments, reviews, real-time chat,
admin dashboard. Think "Airbnb for home renovation".
```

## Step 4 — What happens next (your only touchpoints)

1. **Plan** — the Orchestrator sends you a short delivery plan. *(Read it.)*
2. **Questions** — if the brief is ambiguous, you get ONE batched list of questions,
   each with a recommended default. *(Reply "all defaults" or pick answers.)*
3. **G0** — approve the scope/spec summary.
4. **G1** — approve the architecture briefing (stack, shape, trade-offs). **No code
   is written before you say yes here.**
5. **G2** — approve the design direction.
6. *(Silence while the agency builds — every task passes code review + QA, then
   integration QA + a security audit. You get status updates, not homework.)*
7. **Secrets** — if deploying, you'll be asked for credentials by name (API keys,
   signing keys). You provide values at deploy time; they never go into code.
8. **G6** — you receive the delivery package: what was built, how to run it, quality
   evidence, known limitations. Approve, or request changes (which re-enter the
   pipeline as a spec delta).

## CEO phrasebook

| You say | What happens |
|---|---|
| `New project brief: …` | Full pipeline kicks off from Stage 0 |
| `Status?` | Orchestrator reports from the task board |
| `Approved.` (at a gate) | Pipeline proceeds to the next stage |
| `Change request: …` | Routed to analyst → architect → board (never hacked in) |
| `Show me the open questions / risks` | Orchestrator briefs you |
| `Pause the project` / `Resume` | Board frozen / resumed where it left off |

## House rules (already enforced, just so you know)

- **You never execute.** If you're ever asked to write code or specs, that's a bug — say
  "route it to an agent."
- **Architecture before code, always** — even for a landing page (it's just a 2-page blueprint).
- **Nothing skips the gates.** Review + QA per task, security before release.
- A long-running build may span sessions — just say `Status?` or `Continue the project`
  in a new session; everything lives in `.agency/` and the codebase, so no context is lost.
