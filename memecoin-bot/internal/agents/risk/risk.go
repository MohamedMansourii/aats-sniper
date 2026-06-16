// Package risk implements the circuit breakers that pause trading before a bad
// run compounds: a daily realised-loss cap, a consecutive-loss cooldown, and a
// rolling slippage guard. All methods are safe for concurrent use.
package risk

import (
	"fmt"
	"sync"
	"time"
)

type Config struct {
	MaxDailyLossSOL      float64
	ConsecutiveLossLimit int
	ConsecutivePause     time.Duration // cooldown after too many losses in a row
	SlippageWindow       int           // number of recent trades to average
	SlippageThresholdPct float64       // pause if rolling avg slippage exceeds this
}

func DefaultConfig() Config {
	return Config{
		ConsecutivePause:     time.Hour,
		SlippageWindow:       5,
		SlippageThresholdPct: 10,
	}
}

type Manager struct {
	mu  sync.Mutex
	cfg Config
	now func() time.Time

	day            string
	dailyLossSOL   float64
	consecLosses   int
	pausedUntil    time.Time
	recentSlippage []float64
}

func NewManager(cfg Config) *Manager {
	m := &Manager{cfg: cfg, now: time.Now}
	m.day = m.now().UTC().Format("2006-01-02")
	return m
}

// Allow reports whether a new trade may proceed, with a reason when it may not.
func (m *Manager) Allow() (bool, string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.rollDayLocked()

	if now := m.now(); now.Before(m.pausedUntil) {
		return false, fmt.Sprintf("paused until %s", m.pausedUntil.UTC().Format(time.RFC3339))
	}
	if m.cfg.MaxDailyLossSOL > 0 && m.dailyLossSOL >= m.cfg.MaxDailyLossSOL {
		return false, fmt.Sprintf("daily loss limit hit: %.3f SOL", m.dailyLossSOL)
	}
	return true, ""
}

// RecordResult updates breaker state after a closed trade. profitSOL is
// negative for a loss; slippagePct is the realised slippage for the trade.
func (m *Manager) RecordResult(profitSOL, slippagePct float64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.rollDayLocked()

	if profitSOL < 0 {
		m.dailyLossSOL += -profitSOL
		m.consecLosses++
		if m.cfg.ConsecutiveLossLimit > 0 && m.consecLosses >= m.cfg.ConsecutiveLossLimit {
			m.pausedUntil = m.now().Add(m.cfg.ConsecutivePause)
			m.consecLosses = 0
		}
	} else {
		m.consecLosses = 0
	}

	m.recentSlippage = append(m.recentSlippage, slippagePct)
	if len(m.recentSlippage) > m.cfg.SlippageWindow {
		m.recentSlippage = m.recentSlippage[len(m.recentSlippage)-m.cfg.SlippageWindow:]
	}
	if m.cfg.SlippageWindow > 0 && len(m.recentSlippage) == m.cfg.SlippageWindow {
		if avg(m.recentSlippage) > m.cfg.SlippageThresholdPct {
			m.pausedUntil = m.now().Add(m.cfg.ConsecutivePause)
		}
	}
}

type Status struct {
	Paused         bool      `json:"paused"`
	PausedUntil    time.Time `json:"paused_until"`
	DailyLossSOL   float64   `json:"daily_loss_sol"`
	ConsecLosses   int       `json:"consecutive_losses"`
	AvgSlippagePct float64   `json:"avg_slippage_pct"`
}

func (m *Manager) Status() Status {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.rollDayLocked()
	return Status{
		Paused:         m.now().Before(m.pausedUntil),
		PausedUntil:    m.pausedUntil,
		DailyLossSOL:   m.dailyLossSOL,
		ConsecLosses:   m.consecLosses,
		AvgSlippagePct: avg(m.recentSlippage),
	}
}

func (m *Manager) rollDayLocked() {
	d := m.now().UTC().Format("2006-01-02")
	if d != m.day {
		m.day = d
		m.dailyLossSOL = 0
		m.consecLosses = 0
	}
}

func avg(v []float64) float64 {
	if len(v) == 0 {
		return 0
	}
	var s float64
	for _, x := range v {
		s += x
	}
	return s / float64(len(v))
}
