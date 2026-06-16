// Package execution turns an approved signal into a trade. Two executors share
// one interface:
//
//   - PaperExecutor (default): simulates fills using live Jupiter quotes when
//     available, applying configured slippage. It never touches a wallet.
//   - LiveExecutor: intentionally NOT implemented. It is a guarded placeholder.
//     Signing and submitting real swaps is the one step you should implement
//     and review yourself; this file refuses to fake it.
package execution

import (
	"context"
	"errors"
	"fmt"
	"math/rand"
	"time"

	"memecoin-bot/internal/config"
	"memecoin-bot/internal/models"
	"memecoin-bot/pkg/jupiter"
)

type Executor interface {
	Buy(ctx context.Context, s *models.Signal) (*models.TradeResult, error)
	Sell(ctx context.Context, p *models.Position) (*models.TradeResult, error)
	Mode() string
}

// ---------------------------------------------------------------------------
// PaperExecutor
// ---------------------------------------------------------------------------

type PaperExecutor struct {
	cfg       config.ExecutionConfig
	jup       *jupiter.Client
	buySizeSOL float64
}

func NewPaperExecutor(cfg config.ExecutionConfig, jup *jupiter.Client, buySizeSOL float64) *PaperExecutor {
	if buySizeSOL <= 0 {
		buySizeSOL = 0.1
	}
	return &PaperExecutor{cfg: cfg, jup: jup, buySizeSOL: buySizeSOL}
}

func (e *PaperExecutor) Mode() string { return "paper" }

func (e *PaperExecutor) Buy(ctx context.Context, s *models.Signal) (*models.TradeResult, error) {
	impact := e.priceImpact(ctx, s.TokenAddress, true)
	res := &models.TradeResult{
		TokenAddress: s.TokenAddress,
		Side:         "buy",
		Mode:         e.Mode(),
		TxHash:       simHash(),
		PriceUSD:     s.MarketCapUSD, // proxy; real impl derives price from quote
		AmountSOL:    e.buySizeSOL,
		SlippagePct:  e.cfg.SlippageBuyPercent + impact,
		Timestamp:    time.Now().UTC(),
	}
	return res, nil
}

func (e *PaperExecutor) Sell(ctx context.Context, p *models.Position) (*models.TradeResult, error) {
	impact := e.priceImpact(ctx, p.TokenAddress, false)
	// Simulated PnL: a random walk minus round-trip slippage. This is a
	// placeholder so the pipeline produces realistic-looking results in
	// dry-run; it is NOT a forecast of real performance.
	gross := (rand.Float64()*0.4 - 0.15) * p.AmountSOL // -15%..+25% of size
	roundTripSlip := (e.cfg.SlippageBuyPercent + e.cfg.SlippageSellPercent + 2*impact) / 100
	profit := gross - roundTripSlip*p.AmountSOL
	return &models.TradeResult{
		TokenAddress: p.TokenAddress,
		Side:         "sell",
		Mode:         e.Mode(),
		TxHash:       simHash(),
		AmountSOL:    p.AmountSOL,
		ProfitSOL:    profit,
		SlippagePct:  e.cfg.SlippageSellPercent + impact,
		Timestamp:    time.Now().UTC(),
	}, nil
}

// priceImpact best-effort fetches a real quote to estimate price impact. On
// any failure it returns 0 and the simulation falls back to configured slippage.
func (e *PaperExecutor) priceImpact(ctx context.Context, token string, buy bool) float64 {
	if e.jup == nil {
		return 0
	}
	in, out := jupiter.WSOLMint, token
	if !buy {
		in, out = token, jupiter.WSOLMint
	}
	q, err := e.jup.GetQuote(ctx, in, out, uint64(e.buySizeSOL*1e9), int(e.cfg.SlippageBuyPercent*100))
	if err != nil {
		return 0
	}
	return q.PriceImpact()
}

// ---------------------------------------------------------------------------
// LiveExecutor — deliberately unimplemented.
// ---------------------------------------------------------------------------

// ErrLiveNotImplemented is returned by every LiveExecutor method. Implementing
// real execution means: build a Jupiter swap transaction, sign it with your
// SOLANA_PRIVATE_KEY (e.g. via gagliardetto/solana-go), optionally bundle via
// Jito for MEV *protection*, submit through your RPC, and confirm finality.
// That code moves real money and must be written and tested by you.
var ErrLiveNotImplemented = errors.New(
	"live execution is not implemented in this scaffold: implement signing/submission yourself and review it carefully before risking funds")

type LiveExecutor struct {
	cfg config.ExecutionConfig
}

func NewLiveExecutor(cfg config.ExecutionConfig) *LiveExecutor { return &LiveExecutor{cfg: cfg} }

func (e *LiveExecutor) Mode() string { return "live" }

func (e *LiveExecutor) Buy(context.Context, *models.Signal) (*models.TradeResult, error) {
	return nil, fmt.Errorf("buy: %w", ErrLiveNotImplemented)
}

func (e *LiveExecutor) Sell(context.Context, *models.Position) (*models.TradeResult, error) {
	return nil, fmt.Errorf("sell: %w", ErrLiveNotImplemented)
}

func simHash() string {
	const hex = "0123456789abcdef"
	b := make([]byte, 16)
	for i := range b {
		b[i] = hex[rand.Intn(len(hex))]
	}
	return "SIM_" + string(b)
}
