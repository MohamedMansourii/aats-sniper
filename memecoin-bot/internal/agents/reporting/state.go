package reporting

import (
	"sync"
	"time"

	"memecoin-bot/internal/agents/risk"
	"memecoin-bot/internal/models"
)

// State is the shared, thread-safe view the dashboard reads and the
// orchestrator writes.
type State struct {
	mu sync.RWMutex

	mode        string
	dryRun      bool
	autoExecute bool
	startedAt   time.Time

	tokensFound    int
	tokensFiltered int
	tradesToday    int
	wins           int
	losses         int
	totalPnLSOL    float64

	candidates []models.Candidate
	lastBuy    *models.TradeResult
	lastSell   *models.TradeResult
	riskStatus risk.Status
}

func NewState(mode string, dryRun, autoExecute bool) *State {
	return &State{mode: mode, dryRun: dryRun, autoExecute: autoExecute, startedAt: time.Now().UTC()}
}

func (s *State) IncFound()    { s.mu.Lock(); s.tokensFound++; s.mu.Unlock() }
func (s *State) IncFiltered() { s.mu.Lock(); s.tokensFiltered++; s.mu.Unlock() }

func (s *State) AddCandidate(c models.Candidate) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.candidates = append([]models.Candidate{c}, s.candidates...)
	if len(s.candidates) > 50 {
		s.candidates = s.candidates[:50]
	}
}

func (s *State) RecordBuy(r *models.TradeResult) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.lastBuy = r
	s.tradesToday++
}

func (s *State) RecordSell(r *models.TradeResult) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.lastSell = r
	s.totalPnLSOL += r.ProfitSOL
	if r.ProfitSOL >= 0 {
		s.wins++
	} else {
		s.losses++
	}
}

func (s *State) SetRisk(st risk.Status) { s.mu.Lock(); s.riskStatus = st; s.mu.Unlock() }

// Snapshot is the JSON shape returned by /api/metrics and /api/status.
type Snapshot struct {
	Mode           string         `json:"mode"`
	DryRun         bool           `json:"dry_run"`
	AutoExecute    bool           `json:"auto_execute"`
	UptimeSeconds  int            `json:"uptime_seconds"`
	TokensFound    int            `json:"tokens_found"`
	TokensFiltered int            `json:"tokens_filtered"`
	Candidates     int            `json:"candidates"`
	TradesToday    int            `json:"trades_today"`
	Wins           int            `json:"wins"`
	Losses         int            `json:"losses"`
	WinRate        float64        `json:"win_rate"`
	TotalPnLSOL    float64        `json:"total_pnl_sol"`
	LastBuy        *models.TradeResult `json:"last_buy"`
	LastSell       *models.TradeResult `json:"last_sell"`
	Risk           risk.Status    `json:"risk"`
}

func (s *State) Snapshot() Snapshot {
	s.mu.RLock()
	defer s.mu.RUnlock()
	total := s.wins + s.losses
	wr := 0.0
	if total > 0 {
		wr = float64(s.wins) / float64(total)
	}
	return Snapshot{
		Mode:           s.mode,
		DryRun:         s.dryRun,
		AutoExecute:    s.autoExecute,
		UptimeSeconds:  int(time.Since(s.startedAt).Seconds()),
		TokensFound:    s.tokensFound,
		TokensFiltered: s.tokensFiltered,
		Candidates:     len(s.candidates),
		TradesToday:    s.tradesToday,
		Wins:           s.wins,
		Losses:         s.losses,
		WinRate:        wr,
		TotalPnLSOL:    s.totalPnLSOL,
		LastBuy:        s.lastBuy,
		LastSell:       s.lastSell,
		Risk:           s.riskStatus,
	}
}

func (s *State) Candidates() []models.Candidate {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]models.Candidate, len(s.candidates))
	copy(out, s.candidates)
	return out
}
