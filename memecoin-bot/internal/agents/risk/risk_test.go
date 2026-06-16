package risk

import (
	"testing"
	"time"
)

func newTestManager(cfg Config) (*Manager, *time.Time) {
	m := NewManager(cfg)
	clock := time.Date(2025, 1, 1, 12, 0, 0, 0, time.UTC)
	m.now = func() time.Time { return clock }
	return m, &clock
}

func TestConsecutiveLossPause(t *testing.T) {
	m, _ := newTestManager(Config{ConsecutiveLossLimit: 3, ConsecutivePause: time.Hour, SlippageWindow: 5, SlippageThresholdPct: 10})
	for i := 0; i < 3; i++ {
		m.RecordResult(-0.1, 1)
	}
	if ok, _ := m.Allow(); ok {
		t.Fatal("expected trading paused after 3 consecutive losses")
	}
}

func TestDailyLossLimit(t *testing.T) {
	m, _ := newTestManager(Config{MaxDailyLossSOL: 1.0, ConsecutiveLossLimit: 99, ConsecutivePause: time.Hour, SlippageWindow: 5, SlippageThresholdPct: 10})
	m.RecordResult(-0.6, 1)
	if ok, _ := m.Allow(); !ok {
		t.Fatal("should still allow before hitting daily limit")
	}
	m.RecordResult(-0.6, 1)
	if ok, _ := m.Allow(); ok {
		t.Fatal("should pause after exceeding daily loss limit")
	}
}

func TestPauseExpires(t *testing.T) {
	m, clock := newTestManager(Config{ConsecutiveLossLimit: 1, ConsecutivePause: time.Hour, SlippageWindow: 5, SlippageThresholdPct: 10})
	m.RecordResult(-0.1, 1)
	if ok, _ := m.Allow(); ok {
		t.Fatal("expected pause immediately after loss limit")
	}
	*clock = clock.Add(2 * time.Hour)
	if ok, reason := m.Allow(); !ok {
		t.Fatalf("expected trading resumed after pause expired, got: %s", reason)
	}
}

func TestSlippageGuard(t *testing.T) {
	m, _ := newTestManager(Config{ConsecutiveLossLimit: 99, ConsecutivePause: time.Hour, SlippageWindow: 5, SlippageThresholdPct: 10})
	for i := 0; i < 5; i++ {
		m.RecordResult(0.1, 15) // profitable but very high slippage
	}
	if ok, _ := m.Allow(); ok {
		t.Fatal("expected pause when rolling avg slippage exceeds threshold")
	}
}

func TestWinResetsConsecutiveLosses(t *testing.T) {
	m, _ := newTestManager(Config{ConsecutiveLossLimit: 3, ConsecutivePause: time.Hour, SlippageWindow: 5, SlippageThresholdPct: 100})
	m.RecordResult(-0.1, 1)
	m.RecordResult(-0.1, 1)
	m.RecordResult(0.2, 1) // win resets
	m.RecordResult(-0.1, 1)
	if ok, _ := m.Allow(); !ok {
		t.Fatal("a win should reset the consecutive-loss counter")
	}
}
