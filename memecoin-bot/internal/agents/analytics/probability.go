// Package analytics computes a win probability for a signal and maintains the
// KOL ("ISAC") performance database. The probability function is pure so it
// can be unit-tested and tuned in isolation.
package analytics

// ProbabilityInput is the full set of features the engine scores. Keeping it
// flat and explicit makes the weighting auditable.
type ProbabilityInput struct {
	KOLWinRate        float64 // 0..1
	KOLTotalCalls     int
	KOLMedianReturn6m float64 // fraction, e.g. 0.4 == +40%
	MarketCapUSD      float64
	SafetyScore       int // 0..100
	Volume5mUSD       float64
	Holders           int
}

// Sweet spot for entry market cap, per the spec.
const (
	sweetSpotLow  = 600_000.0
	sweetSpotHigh = 2_700_000.0
)

// CalcWinProbability returns a 0..100 score. It is intentionally conservative:
// a low safety score hard-caps the result, because no historical edge survives
// buying an outright scam.
func CalcWinProbability(in ProbabilityInput) float64 {
	// Confidence in the KOL's win rate grows with sample size, saturating at
	// ~30 calls. With few calls we trust the win rate less.
	confidence := float64(in.KOLTotalCalls) / 30.0
	if confidence > 1 {
		confidence = 1
	}
	score := in.KOLWinRate * 60.0 * confidence // up to 60 pts

	// Market-cap sweet spot: full bonus inside the band, tapering outside.
	switch {
	case in.MarketCapUSD >= sweetSpotLow && in.MarketCapUSD <= sweetSpotHigh:
		score += 20
	case in.MarketCapUSD > 0:
		score += 10 // in some range but outside the ideal band
	}

	// Positive median 6-minute return is a (weak) momentum signal.
	if in.KOLMedianReturn6m > 0 {
		score += 10
	}

	// Liquidity / distribution bonuses.
	if in.Volume5mUSD >= 50_000 {
		score += 5
	}
	if in.Holders >= 300 {
		score += 5
	}

	// Safety hard-cap: per spec, safety < 70 caps probability at 20.
	if in.SafetyScore < 70 && score > 20 {
		score = 20
	}

	return clamp(score, 0, 100)
}

func clamp(v, lo, hi float64) float64 {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}
