// Package store provides persistence for trades and the KOL performance
// database. The default MemoryStore has zero external dependencies so the
// bot runs out of the box in dry-run; GormStore wires the same interface to
// PostgreSQL when a database is configured and reachable.
package store

import (
	"sort"
	"sync"

	"memecoin-bot/internal/models"
)

type Store interface {
	SaveTrade(t models.Trade) error
	ListTrades() ([]models.Trade, error)

	SaveKolCall(c models.KolCall) (uint, error)
	UpdateKolCall(c models.KolCall) error
	GetKolCall(id uint) (models.KolCall, bool, error)
	KolStats(name string) (models.KolStats, error)
}

// MemoryStore is an in-process, thread-safe Store. State is lost on restart.
type MemoryStore struct {
	mu     sync.RWMutex
	trades []models.Trade
	calls  map[uint]models.KolCall
	nextID uint
}

func NewMemoryStore() *MemoryStore {
	return &MemoryStore{calls: make(map[uint]models.KolCall), nextID: 1}
}

func (s *MemoryStore) SaveTrade(t models.Trade) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	t.ID = uint(len(s.trades) + 1)
	s.trades = append(s.trades, t)
	return nil
}

func (s *MemoryStore) ListTrades() ([]models.Trade, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]models.Trade, len(s.trades))
	copy(out, s.trades)
	return out, nil
}

func (s *MemoryStore) SaveKolCall(c models.KolCall) (uint, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	c.ID = s.nextID
	s.nextID++
	s.calls[c.ID] = c
	return c.ID, nil
}

func (s *MemoryStore) UpdateKolCall(c models.KolCall) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.calls[c.ID] = c
	return nil
}

func (s *MemoryStore) GetKolCall(id uint) (models.KolCall, bool, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	c, ok := s.calls[id]
	return c, ok, nil
}

func (s *MemoryStore) KolStats(name string) (models.KolStats, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var (
		total    int
		wins     int
		returns6 []float64
	)
	for _, c := range s.calls {
		if c.KOLName != name {
			continue
		}
		total++
		if c.WinLossFlag == "win" {
			wins++
		}
		if c.WinLossFlag != "open" {
			returns6 = append(returns6, c.Return6min)
		}
	}
	st := models.KolStats{KOLName: name, TotalCalls: total, Wins: wins, MedianReturn6min: median(returns6)}
	if total > 0 {
		st.WinRate = float64(wins) / float64(total)
	}
	return st, nil
}

func median(v []float64) float64 {
	if len(v) == 0 {
		return 0
	}
	sort.Float64s(v)
	n := len(v)
	if n%2 == 1 {
		return v[n/2]
	}
	return (v[n/2-1] + v[n/2]) / 2
}
