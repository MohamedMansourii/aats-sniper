package analytics

import "testing"

func TestSafetyHardCap(t *testing.T) {
	in := ProbabilityInput{
		KOLWinRate: 0.9, KOLTotalCalls: 100, KOLMedianReturn6m: 0.5,
		MarketCapUSD: 1_000_000, SafetyScore: 40, Volume5mUSD: 100_000, Holders: 500,
	}
	if got := CalcWinProbability(in); got > 20 {
		t.Fatalf("low safety must cap probability at 20, got %.1f", got)
	}
}

func TestStrongSignalScoresHigh(t *testing.T) {
	in := ProbabilityInput{
		KOLWinRate: 0.8, KOLTotalCalls: 60, KOLMedianReturn6m: 0.4,
		MarketCapUSD: 1_500_000, SafetyScore: 90, Volume5mUSD: 80_000, Holders: 400,
	}
	got := CalcWinProbability(in)
	if got < 70 {
		t.Fatalf("strong signal should score >=70, got %.1f", got)
	}
}

func TestLowSampleReducesScore(t *testing.T) {
	base := ProbabilityInput{KOLWinRate: 1.0, MarketCapUSD: 1_000_000, SafetyScore: 90}
	few := base
	few.KOLTotalCalls = 3
	many := base
	many.KOLTotalCalls = 30
	if CalcWinProbability(few) >= CalcWinProbability(many) {
		t.Fatal("fewer calls should yield lower confidence and lower score")
	}
}

func TestClampUpperBound(t *testing.T) {
	in := ProbabilityInput{
		KOLWinRate: 1.0, KOLTotalCalls: 1000, KOLMedianReturn6m: 5,
		MarketCapUSD: 1_000_000, SafetyScore: 100, Volume5mUSD: 1e9, Holders: 1e6,
	}
	if got := CalcWinProbability(in); got > 100 {
		t.Fatalf("probability must not exceed 100, got %.1f", got)
	}
}
