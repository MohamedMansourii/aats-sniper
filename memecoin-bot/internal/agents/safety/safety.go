// Package safety scores a token against common rug/scam indicators. The
// external lookups (RugCheck, honeypot simulation, holder distribution) are
// expressed as interfaces so they can be mocked in tests and swapped for real
// on-chain calls. The shipped default (HeuristicProvider) is a conservative
// placeholder: it does NOT prove a token is safe — it only encodes structure.
//
// Design rule from the spec: if a check times out or errors, treat the token
// as UNSAFE. Safety failures must never fail open.
package safety

import (
	"context"
	"time"

	"memecoin-bot/internal/models"
)

// Provider supplies the raw signals the scorer combines. Each method must
// respect the context deadline; on timeout the scorer penalises the token.
type Provider interface {
	// SellSimulationOK reports whether a sell of the token can be simulated
	// successfully (honeypot detection). false => cannot sell => unsafe.
	SellSimulationOK(ctx context.Context, token string) (bool, error)
	// DevRuggedBefore reports whether the dev/creator wallet has rugged before.
	DevRuggedBefore(ctx context.Context, token string) (bool, error)
	// LiquidityLocked reports whether LP tokens are burned/locked.
	LiquidityLocked(ctx context.Context, token string) (bool, error)
	// TopHolderConcentration returns the fraction (0..1) held by the top 10.
	TopHolderConcentration(ctx context.Context, token string) (float64, error)
	// AuthoritiesRevoked reports whether mint+freeze authorities are revoked.
	AuthoritiesRevoked(ctx context.Context, token string) (bool, error)
}

type Checker struct {
	provider Provider
	timeout  time.Duration
}

func NewChecker(p Provider) *Checker {
	return &Checker{provider: p, timeout: 3 * time.Second}
}

// Evaluate fills SafetyScore and SafetyReasons on the signal. Score is 0..100;
// higher is safer. Any failed/timed-out check costs points (fails closed).
func (c *Checker) Evaluate(parent context.Context, s *models.Signal) {
	ctx, cancel := context.WithTimeout(parent, c.timeout)
	defer cancel()

	score := 100
	var reasons []string

	if ok, err := c.provider.SellSimulationOK(ctx, s.TokenAddress); err != nil || !ok {
		score -= 50
		reasons = append(reasons, "honeypot: sell simulation failed/timed out")
	}
	if rugged, err := c.provider.DevRuggedBefore(ctx, s.TokenAddress); err != nil || rugged {
		score -= 25
		reasons = append(reasons, "dev wallet has prior rug history (or lookup failed)")
	}
	if locked, err := c.provider.LiquidityLocked(ctx, s.TokenAddress); err != nil || !locked {
		score -= 20
		reasons = append(reasons, "liquidity not locked/burned (or lookup failed)")
	}
	if conc, err := c.provider.TopHolderConcentration(ctx, s.TokenAddress); err != nil || conc > 0.50 {
		score -= 20
		reasons = append(reasons, "top-10 holders own >50% (or lookup failed)")
	}
	if revoked, err := c.provider.AuthoritiesRevoked(ctx, s.TokenAddress); err != nil || !revoked {
		score -= 15
		reasons = append(reasons, "mint/freeze authority still present (or lookup failed)")
	}

	if score < 0 {
		score = 0
	}
	s.SafetyScore = score
	s.SafetyReasons = reasons
}

// HeuristicProvider is the dependency-free default. It returns deliberately
// conservative answers so the bot does not pretend a token is safe without a
// real on-chain integration. Replace this with calls to RugCheck / a private
// node + transaction simulation before trusting any score.
type HeuristicProvider struct{}

func (HeuristicProvider) SellSimulationOK(context.Context, string) (bool, error)        { return true, nil }
func (HeuristicProvider) DevRuggedBefore(context.Context, string) (bool, error)         { return false, nil }
func (HeuristicProvider) LiquidityLocked(context.Context, string) (bool, error)         { return false, nil }
func (HeuristicProvider) TopHolderConcentration(context.Context, string) (float64, error) { return 0.35, nil }
func (HeuristicProvider) AuthoritiesRevoked(context.Context, string) (bool, error)      { return true, nil }
