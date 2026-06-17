"""M3 Controller — triple-loop (SNIPE / FAST / SLOW) + single-writer FSM.

T-340: triple loop + FSM + atomic snipe->fast handoff.

The architecture (BLUEPRINT §2, ADR-0002) specifies SNIPE + FAST as Rust hot-core
processes; SLOW as a Python process.  Because this task builds the PYTHON controller
running against SimulationVenue (DRY-RUN FIRST per task directive), all three loops
are implemented here as Python asyncio tasks that communicate through the injectable
StateStore abstraction (real = Redis; test = InMemoryStateStore).

Key invariants enforced here:
  - FAST loop never awaits an LLM, slow model, or unbounded RPC (BLUEPRINT §2.1)
  - FSM is single-writer: SNIPE writes IDLE→ENTERING (atomic CAS); FAST writes all other
    transitions (ADR-0007)
  - Atomic snipe→fast handoff via Lua-equivalent SETNX on per-mint lock (ADR-0007)
  - Write-ahead: FSM transition persisted BEFORE side-effect
  - DRY_RUN is the default; real capital disabled unless DRY_RUN_ENABLED=false
  - No win-rate field, no float money, event-time only (not wall-clock)

Public surface:
  from aats.controller.state       import InMemoryStateStore, RedisStateStore
  from aats.controller.fsm         import PositionFSM
  from aats.controller.snipe_loop  import SnipeLoop
  from aats.controller.fast_loop   import FastLoop
  from aats.controller.slow_loop   import SlowLoop
  from aats.controller.reconcile   import Reconciler
  from aats.controller.control_api import build_app
  from aats.controller.loops       import ControllerOrchestrator
"""
