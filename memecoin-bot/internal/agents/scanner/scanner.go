// Package scanner is the ingestion layer. Real sources (Telegram via gotd/Bot
// API, and on-chain Geyser/WebSocket streams) are represented as interfaces and
// left as stubs — wiring live credentials is the operator's job. A built-in
// demo source emits synthetic signals so the full pipeline and dashboard are
// observable in dry-run without any external connection.
package scanner

import (
	"context"
	"math/rand"
	"time"

	"memecoin-bot/internal/config"
	"memecoin-bot/internal/models"
	"memecoin-bot/pkg/telegram"

	"github.com/sirupsen/logrus"
)

type Scanner struct {
	cfg       config.TelegramConfig
	extractor *telegram.Extractor
	log       *logrus.Logger
	out       chan<- models.Signal
	demo      bool
}

func New(cfg config.TelegramConfig, out chan<- models.Signal, log *logrus.Logger, demo bool) *Scanner {
	return &Scanner{
		cfg:       cfg,
		extractor: telegram.NewExtractor(cfg.RegexPattern),
		log:       log,
		out:       out,
		demo:      demo,
	}
}

// Run starts all enabled ingestion sources and blocks until ctx is cancelled.
func (s *Scanner) Run(ctx context.Context) {
	if s.demo {
		go s.runDemo(ctx)
	}
	// Real sources would be started here, e.g.:
	//   go s.runTelegram(ctx)
	//   go s.runOnchain(ctx)
	s.log.WithField("channels", len(s.cfg.MonitoredChannels)).
		Info("scanner started (telegram/on-chain sources are stubs; enable demo to see the pipeline run)")
	<-ctx.Done()
}

// HandleMessage parses one raw channel message and emits a Signal per detected
// contract address. This is the seam a real Telegram listener would call.
func (s *Scanner) HandleMessage(channel, kol, text string) {
	for _, ca := range s.extractor.Addresses(text) {
		sig := models.Signal{
			TokenAddress: ca,
			Chain:        models.ChainSolana,
			KOLName:      kol,
			Source:       "telegram",
			RawMessage:   text,
			DetectedAt:   time.Now().UTC(),
		}
		s.emit(sig)
	}
}

func (s *Scanner) emit(sig models.Signal) {
	select {
	case s.out <- sig:
	default:
		s.log.Warn("signal channel full; dropping signal")
	}
}

// runDemo fabricates believable signals on an interval. Clearly synthetic.
func (s *Scanner) runDemo(ctx context.Context) {
	kols := []string{"DemoWhaleKOL", "DemoAlphaCaller", "DemoRandomShiller"}
	t := time.NewTicker(8 * time.Second)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			sig := models.Signal{
				TokenAddress: randCA(),
				Chain:        models.ChainSolana,
				KOLName:      kols[rand.Intn(len(kols))],
				Source:       "telegram",
				RawMessage:   "[DEMO] synthetic call",
				DetectedAt:   time.Now().UTC(),
				MarketCapUSD: 500_000 + rand.Float64()*3_000_000,
				Volume5mUSD:  rand.Float64() * 150_000,
				Holders:      100 + rand.Intn(800),
			}
			s.emit(sig)
		}
	}
}

func randCA() string {
	const b58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
	b := make([]byte, 44)
	for i := range b {
		b[i] = b58[rand.Intn(len(b58))]
	}
	return string(b)
}
