// Package decision is the gate between analysis and execution. It applies the
// configured hard filters and probability threshold and records a human
// readable reason for every accept/reject so decisions are auditable.
package decision

import (
	"fmt"

	"memecoin-bot/internal/config"
	"memecoin-bot/internal/models"
)

type Engine struct {
	f         config.FiltersConfig
	threshold float64 // 0..100
}

func NewEngine(f config.FiltersConfig) *Engine {
	return &Engine{f: f, threshold: f.WinProbabilityThreshold * 100}
}

// Evaluate mutates s.Approved and s.DecisionReason and returns the verdict.
func (e *Engine) Evaluate(s *models.Signal) bool {
	if s.WinProbability < e.threshold {
		return e.reject(s, fmt.Sprintf("win probability %.0f < threshold %.0f", s.WinProbability, e.threshold))
	}
	if e.f.MinMarketCapUSD > 0 && s.MarketCapUSD < e.f.MinMarketCapUSD {
		return e.reject(s, fmt.Sprintf("market cap %.0f below min %.0f", s.MarketCapUSD, e.f.MinMarketCapUSD))
	}
	if e.f.MaxMarketCapUSD > 0 && s.MarketCapUSD > e.f.MaxMarketCapUSD {
		return e.reject(s, fmt.Sprintf("market cap %.0f above max %.0f", s.MarketCapUSD, e.f.MaxMarketCapUSD))
	}
	if e.f.MinVolume5mUSD > 0 && s.Volume5mUSD < e.f.MinVolume5mUSD {
		return e.reject(s, fmt.Sprintf("5m volume %.0f below min %.0f", s.Volume5mUSD, e.f.MinVolume5mUSD))
	}
	if e.f.MinHolders > 0 && s.Holders < e.f.MinHolders {
		return e.reject(s, fmt.Sprintf("holders %d below min %d", s.Holders, e.f.MinHolders))
	}
	s.Approved = true
	s.DecisionReason = "passed all filters and probability threshold"
	return true
}

func (e *Engine) reject(s *models.Signal, reason string) bool {
	s.Approved = false
	s.DecisionReason = reason
	return false
}
