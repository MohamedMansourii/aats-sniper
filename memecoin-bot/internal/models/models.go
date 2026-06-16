// Package models holds the domain types that flow through the trading
// pipeline plus the persisted records. gorm struct tags are plain string
// tags, so this package has no third-party imports and can be imported by
// every agent without creating cycles.
package models

import "time"

type Chain string

const (
	ChainSolana Chain = "solana"
	ChainBase   Chain = "base"
)

// Signal is the unit of work that travels through the agent pipeline:
// scanner -> safety -> analytics -> decision -> execution. Each stage fills
// in more fields rather than allocating new structs.
type Signal struct {
	TokenAddress string    `json:"token_address"`
	Chain        Chain     `json:"chain"`
	Ticker       string    `json:"ticker"`
	KOLName      string    `json:"kol_name"`
	Source       string    `json:"source"` // "telegram" | "onchain" | "twitter"
	RawMessage   string    `json:"raw_message"`
	DetectedAt   time.Time `json:"detected_at"`

	// Market context (populated by the scanner / enrichment step).
	MarketCapUSD float64 `json:"market_cap_usd"`
	Volume5mUSD  float64 `json:"volume_5m_usd"`
	Holders      int     `json:"holders"`

	// Filled by the safety agent.
	SafetyScore   int      `json:"safety_score"` // 0..100
	SafetyReasons []string `json:"safety_reasons"`

	// Filled by the analytics agent.
	WinProbability float64 `json:"win_probability"` // 0..100

	// Filled by the decision agent.
	Approved       bool   `json:"approved"`
	DecisionReason string `json:"decision_reason"`
}

// Candidate is a Signal surfaced to the dashboard / candidate queue.
type Candidate struct {
	Signal
	Status    string    `json:"status"` // "pending" | "executed" | "skipped"
	CreatedAt time.Time `json:"created_at"`
}

// Position represents an open paper or live position awaiting its timed exit.
type Position struct {
	TokenAddress  string    `json:"token_address"`
	Chain         Chain     `json:"chain"`
	KOLName       string    `json:"kol_name"`
	EntryPriceUSD float64   `json:"entry_price_usd"`
	AmountSOL     float64   `json:"amount_sol"`
	OpenedAt      time.Time `json:"opened_at"`
	HoldSeconds   int       `json:"hold_seconds"`
}

// TradeResult is returned by an Executor for a single buy or sell leg.
type TradeResult struct {
	TokenAddress string    `json:"token_address"`
	Side         string    `json:"side"` // "buy" | "sell"
	Mode         string    `json:"mode"` // "paper" | "live" | "dry-run"
	TxHash       string    `json:"tx_hash"`
	PriceUSD     float64   `json:"price_usd"`
	AmountSOL    float64   `json:"amount_sol"`
	ProfitSOL    float64   `json:"profit_sol"`
	SlippagePct  float64   `json:"slippage_pct"`
	Timestamp    time.Time `json:"timestamp"`
	Err          string    `json:"err,omitempty"`
}

// ---------------------------------------------------------------------------
// Persisted records (the "ISAC" KOL performance database + trade ledger).
// ---------------------------------------------------------------------------

type Trade struct {
	ID            uint      `gorm:"primaryKey" json:"id"`
	TokenAddress  string    `gorm:"index" json:"token_address"`
	KOLName       string    `json:"kol_name"`
	Chain         string    `json:"chain"`
	EntryPriceUSD float64   `json:"entry_price_usd"`
	ExitPriceUSD  float64   `json:"exit_price_usd"`
	ProfitSOL     float64   `json:"profit_sol"`
	Mode          string    `json:"mode"`
	Timestamp     time.Time `json:"timestamp"`
}

// KolCall is one influencer call tracked across fixed snapshot intervals.
type KolCall struct {
	ID              uint      `gorm:"primaryKey" json:"id"`
	KOLName         string    `gorm:"index" json:"kol_name"`
	TokenAddress    string    `gorm:"index" json:"token_address"`
	CallTimestamp   time.Time `json:"call_timestamp"`
	MarketCapAtCall float64   `json:"market_cap_at_call"`
	PriceAt2min     float64   `json:"price_at_2min"`
	PriceAt6min     float64   `json:"price_at_6min"`
	PriceAt10min    float64   `json:"price_at_10min"`
	PriceAt1hr      float64   `json:"price_at_1hr"`
	PriceAt4hr      float64   `json:"price_at_4hr"`
	PriceAt12hr     float64   `json:"price_at_12hr"`
	MaxGain         float64   `json:"max_gain"`
	Return2min      float64   `json:"return_2min"`
	Return6min      float64   `json:"return_6min"`
	WinLossFlag     string    `json:"win_loss_flag"` // "win" | "loss" | "open"
}

// KolStats is the derived view the probability engine consumes.
type KolStats struct {
	KOLName          string  `json:"kol_name"`
	TotalCalls       int     `json:"total_calls"`
	Wins             int     `json:"wins"`
	WinRate          float64 `json:"win_rate"` // 0..1
	MedianReturn6min float64 `json:"median_return_6min"`
}
