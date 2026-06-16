// Command trading is the entry point for the memecoin trading bot.
//
// SAFETY: the bot starts in dry-run/paper mode by default. Real fund-moving
// execution is intentionally not implemented in this scaffold (see
// internal/agents/execution). Read the README before changing any of this.
package main

import (
	"bufio"
	"context"
	"flag"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"memecoin-bot/internal/agents/analytics"
	"memecoin-bot/internal/agents/decision"
	"memecoin-bot/internal/agents/execution"
	"memecoin-bot/internal/agents/reporting"
	"memecoin-bot/internal/agents/risk"
	"memecoin-bot/internal/agents/safety"
	"memecoin-bot/internal/config"
	"memecoin-bot/internal/orchestrator"
	"memecoin-bot/internal/store"
	"memecoin-bot/pkg/jupiter"

	"github.com/sirupsen/logrus"
)

func main() {
	var (
		cfgPath = flag.String("config", "config.yaml", "path to config.yaml")
		envPath = flag.String("env", ".env", "path to .env file (optional)")
		demo    = flag.Bool("demo", false, "emit synthetic signals so the pipeline/dashboard are observable without live sources")
	)
	flag.Parse()

	log := logrus.New()
	log.SetFormatter(&logrus.JSONFormatter{})
	log.SetLevel(logrus.InfoLevel)

	loadDotEnv(*envPath, log)

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		log.WithError(err).Fatal("failed to load config")
	}

	// Persistence: PostgreSQL if configured & reachable, else in-memory.
	st := selectStore(cfg, log)

	// Read-only quote client (used by the paper executor for slippage est.).
	jup := jupiter.New(cfg.Secrets.JupiterAPI)

	// Executor selection. Live execution is a guarded stub; even when armed it
	// will refuse to move funds until you implement it yourself.
	var exec execution.Executor = execution.NewPaperExecutor(cfg.Execution, jup, 0.1)
	mode := "paper"
	if cfg.LiveTradingArmed() {
		exec = execution.NewLiveExecutor(cfg.Execution)
		mode = "live"
		log.Warn("LIVE trading armed (dry_run=false, auto_execute=true, key present) — execution is a stub and will error until implemented")
	} else {
		log.Info("running in DRY-RUN / paper mode — no real funds at risk")
	}

	// Risk circuit breakers.
	rcfg := risk.DefaultConfig()
	rcfg.MaxDailyLossSOL = cfg.Bot.MaxDailyLossSOL
	rcfg.ConsecutiveLossLimit = cfg.Bot.ConsecutiveLossLimit
	rmgr := risk.NewManager(rcfg)

	// KOL tracker.
	tracker := analytics.NewTracker(st, analytics.StubPriceProvider{}, parseIntervals(cfg.KOLDatabase.SnapshotIntervals, log), log)

	// Dashboard state + server.
	state := reporting.NewState(mode, cfg.Bot.DryRun, cfg.Bot.AutoExecute)
	server := reporting.NewServer(cfg.Dashboard, state, log)

	orch := orchestrator.New(orchestrator.Deps{
		Config:   cfg,
		Log:      log,
		Safety:   safety.NewChecker(safety.HeuristicProvider{}),
		Decision: decision.NewEngine(cfg.Filters),
		Risk:     rmgr,
		Exec:     exec,
		Store:    st,
		Tracker:  tracker,
		State:    state,
		Server:   server,
		Demo:     *demo,
	})

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	go func() {
		if err := server.ListenAndServe("frontend"); err != nil {
			log.WithError(err).Error("dashboard server stopped")
		}
	}()

	orch.Run(ctx)
	log.Info("shutdown complete")
}

func selectStore(cfg *config.Config, log *logrus.Logger) store.Store {
	if cfg.Database.Host == "" {
		log.Info("no DB_HOST configured; using in-memory store (state lost on restart)")
		return store.NewMemoryStore()
	}
	gs, err := store.NewGormStore(store.PostgresConfig{
		Host: cfg.Database.Host, Port: cfg.Database.Port, User: cfg.Database.User,
		Password: cfg.Database.Password, DBName: cfg.Database.DBName,
	})
	if err != nil {
		log.WithError(err).Warn("postgres unavailable; falling back to in-memory store")
		return store.NewMemoryStore()
	}
	log.Info("connected to postgres store")
	return gs
}

func parseIntervals(in []string, log *logrus.Logger) []time.Duration {
	var out []time.Duration
	for _, s := range in {
		d, err := time.ParseDuration(s)
		if err != nil {
			log.WithField("interval", s).Warn("skipping invalid snapshot interval")
			continue
		}
		out = append(out, d)
	}
	if len(out) == 0 {
		out = []time.Duration{2 * time.Minute, 6 * time.Minute, 10 * time.Minute, time.Hour, 4 * time.Hour, 12 * time.Hour}
	}
	return out
}

// loadDotEnv is a tiny KEY=VALUE loader so secrets can live in a .env file. It
// never overrides variables already set in the environment.
func loadDotEnv(path string, log *logrus.Logger) {
	f, err := os.Open(path)
	if err != nil {
		return // .env is optional
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		k = strings.TrimSpace(k)
		v = strings.Trim(strings.TrimSpace(v), `"'`)
		if _, exists := os.LookupEnv(k); !exists {
			_ = os.Setenv(k, v)
		}
	}
	log.WithField("path", path).Debug("loaded .env")
}
