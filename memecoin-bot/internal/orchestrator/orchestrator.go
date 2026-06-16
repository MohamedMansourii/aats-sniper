// Package orchestrator wires the agents into one pipeline:
//
//	scanner -> [signals chan] -> worker pool -> safety -> analytics ->
//	decision -> (paper/live) execution -> store + risk + dashboard state
//
// Every signal produces an auditable candidate regardless of outcome.
package orchestrator

import (
	"context"
	"sync"
	"time"

	"memecoin-bot/internal/agents/analytics"
	"memecoin-bot/internal/agents/decision"
	"memecoin-bot/internal/agents/execution"
	"memecoin-bot/internal/agents/reporting"
	"memecoin-bot/internal/agents/risk"
	"memecoin-bot/internal/agents/safety"
	"memecoin-bot/internal/agents/scanner"
	"memecoin-bot/internal/config"
	"memecoin-bot/internal/models"
	"memecoin-bot/internal/store"

	"github.com/sirupsen/logrus"
)

type Orchestrator struct {
	cfg     *config.Config
	log     *logrus.Logger
	signals chan models.Signal

	scanner  *scanner.Scanner
	safety   *safety.Checker
	decision *decision.Engine
	risk     *risk.Manager
	exec     execution.Executor
	store    store.Store
	tracker  *analytics.Tracker
	state    *reporting.State
	server   *reporting.Server

	demo    bool
	workers int
}

type Deps struct {
	Config   *config.Config
	Log      *logrus.Logger
	Safety   *safety.Checker
	Decision *decision.Engine
	Risk     *risk.Manager
	Exec     execution.Executor
	Store    store.Store
	Tracker  *analytics.Tracker
	State    *reporting.State
	Server   *reporting.Server
	Demo     bool
}

func New(d Deps) *Orchestrator {
	signals := make(chan models.Signal, 256)
	return &Orchestrator{
		cfg:      d.Config,
		log:      d.Log,
		signals:  signals,
		scanner:  scanner.New(d.Config.Telegram, signals, d.Log, d.Demo),
		safety:   d.Safety,
		decision: d.Decision,
		risk:     d.Risk,
		exec:     d.Exec,
		store:    d.Store,
		tracker:  d.Tracker,
		state:    d.State,
		server:   d.Server,
		demo:     d.Demo,
		workers:  4,
	}
}

func (o *Orchestrator) Run(ctx context.Context) {
	if o.demo {
		o.seedDemoKOL()
	}
	var wg sync.WaitGroup
	for i := 0; i < o.workers; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); o.worker(ctx) }()
	}
	go o.scanner.Run(ctx)

	o.log.WithFields(logrus.Fields{
		"dry_run": o.cfg.Bot.DryRun, "auto_execute": o.cfg.Bot.AutoExecute, "exec_mode": o.exec.Mode(),
	}).Info("orchestrator running")

	<-ctx.Done()
	wg.Wait()
}

func (o *Orchestrator) worker(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case sig := <-o.signals:
			o.handle(ctx, sig)
		}
	}
}

func (o *Orchestrator) handle(ctx context.Context, sig models.Signal) {
	o.state.IncFound()

	// 1. Safety (fails closed on timeout/error).
	o.safety.Evaluate(ctx, &sig)

	// 2. Probability from this KOL's tracked history.
	stats, _ := o.store.KolStats(sig.KOLName)
	sig.WinProbability = analytics.CalcWinProbability(analytics.ProbabilityInput{
		KOLWinRate:        stats.WinRate,
		KOLTotalCalls:     stats.TotalCalls,
		KOLMedianReturn6m: stats.MedianReturn6min,
		MarketCapUSD:      sig.MarketCapUSD,
		SafetyScore:       sig.SafetyScore,
		Volume5mUSD:       sig.Volume5mUSD,
		Holders:           sig.Holders,
	})

	// 3. Track the call for future win-rate learning (regardless of verdict).
	if o.cfg.KOLDatabase.EnableTracking {
		o.tracker.Record(&sig)
	}

	// 4. Decision gate.
	approved := o.decision.Evaluate(&sig)

	cand := models.Candidate{Signal: sig, CreatedAt: time.Now().UTC()}
	if !approved {
		o.state.IncFiltered()
		cand.Status = "skipped"
		o.state.AddCandidate(cand)
		o.log.WithFields(logrus.Fields{"ca": sig.TokenAddress, "reason": sig.DecisionReason}).Debug("signal rejected")
		return
	}

	// 5. Execution policy.
	switch {
	case !o.cfg.Bot.DryRun && !o.cfg.Bot.AutoExecute:
		// Recommend only.
		cand.Status = "pending"
		o.state.AddCandidate(cand)
		o.log.WithField("ca", sig.TokenAddress).Info("candidate recommended (auto-execute off)")
		return
	case o.server != nil && o.server.Paused():
		cand.Status = "pending"
		o.state.AddCandidate(cand)
		o.log.WithField("ca", sig.TokenAddress).Warn("trading paused via dashboard; holding candidate")
		return
	}

	if ok, reason := o.risk.Allow(); !ok {
		cand.Status = "skipped"
		o.state.AddCandidate(cand)
		o.state.SetRisk(o.risk.Status())
		o.log.WithFields(logrus.Fields{"ca": sig.TokenAddress, "reason": reason}).Warn("circuit breaker blocked trade")
		return
	}

	// 6. Buy.
	buy, err := o.exec.Buy(ctx, &sig)
	if err != nil {
		cand.Status = "skipped"
		o.state.AddCandidate(cand)
		o.log.WithError(err).WithField("ca", sig.TokenAddress).Error("buy failed")
		return
	}
	cand.Status = "executed"
	o.state.AddCandidate(cand)
	o.state.RecordBuy(buy)
	o.log.WithFields(logrus.Fields{"ca": sig.TokenAddress, "mode": buy.Mode, "tx": buy.TxHash}).Info("BUY")

	// 7. Schedule timed exit.
	hold := o.cfg.Execution.DefaultHoldSeconds
	if o.demo && hold > 15 {
		hold = 8
	}
	pos := &models.Position{
		TokenAddress:  sig.TokenAddress,
		Chain:         sig.Chain,
		KOLName:       sig.KOLName,
		EntryPriceUSD: buy.PriceUSD,
		AmountSOL:     buy.AmountSOL,
		OpenedAt:      time.Now().UTC(),
		HoldSeconds:   hold,
	}
	time.AfterFunc(time.Duration(hold)*time.Second, func() { o.exit(context.Background(), pos) })
}

func (o *Orchestrator) exit(ctx context.Context, pos *models.Position) {
	sell, err := o.exec.Sell(ctx, pos)
	if err != nil {
		o.log.WithError(err).WithField("ca", pos.TokenAddress).Error("sell failed")
		return
	}
	o.risk.RecordResult(sell.ProfitSOL, sell.SlippagePct)
	o.state.RecordSell(sell)
	o.state.SetRisk(o.risk.Status())
	_ = o.store.SaveTrade(models.Trade{
		TokenAddress: pos.TokenAddress,
		KOLName:      pos.KOLName,
		Chain:        string(pos.Chain),
		EntryPriceUSD: pos.EntryPriceUSD,
		ProfitSOL:    sell.ProfitSOL,
		Mode:         sell.Mode,
		Timestamp:    time.Now().UTC(),
	})
	o.log.WithFields(logrus.Fields{
		"ca": pos.TokenAddress, "mode": sell.Mode, "profit_sol": sell.ProfitSOL,
	}).Info("SELL")
}

// seedDemoKOL inserts synthetic history so DemoWhaleKOL clears the probability
// threshold and the dry-run pipeline visibly executes paper trades. Clearly
// fake data; only runs in demo mode.
func (o *Orchestrator) seedDemoKOL() {
	for i := 0; i < 25; i++ {
		flag, ret := "win", 0.6
		if i%3 == 0 {
			flag, ret = "loss", -0.2
		}
		_, _ = o.store.SaveKolCall(models.KolCall{
			KOLName:      "DemoWhaleKOL",
			TokenAddress: "seed",
			WinLossFlag:  flag,
			Return6min:   ret,
		})
	}
	o.log.Info("seeded demo KOL history (DemoWhaleKOL)")
}
