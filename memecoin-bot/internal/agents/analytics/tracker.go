package analytics

import (
	"context"
	"time"

	"memecoin-bot/internal/models"
	"memecoin-bot/internal/store"

	"github.com/sirupsen/logrus"
)

// PriceProvider returns the current USD price for a token. The default
// implementation is a stub; wire it to Birdeye / a private node for real data.
type PriceProvider interface {
	PriceUSD(ctx context.Context, token string) (float64, error)
}

// Tracker records every KOL call and snapshots its price at fixed intervals,
// building the historical win-rate dataset the probability engine consumes.
type Tracker struct {
	store     store.Store
	prices    PriceProvider
	log       *logrus.Logger
	intervals []time.Duration
}

func NewTracker(s store.Store, p PriceProvider, intervals []time.Duration, log *logrus.Logger) *Tracker {
	return &Tracker{store: s, prices: p, log: log, intervals: intervals}
}

// Record persists a new call and schedules its snapshots. Snapshots run via
// time.AfterFunc; in a long-running process consider a durable scheduler so
// pending snapshots survive restarts.
func (t *Tracker) Record(s *models.Signal) {
	call := models.KolCall{
		KOLName:         s.KOLName,
		TokenAddress:    s.TokenAddress,
		CallTimestamp:   time.Now().UTC(),
		MarketCapAtCall: s.MarketCapUSD,
		WinLossFlag:     "open",
	}
	id, err := t.store.SaveKolCall(call)
	if err != nil {
		t.log.WithError(err).Warn("failed to persist KOL call")
		return
	}

	entry, err := t.priceNow(s.TokenAddress)
	if err != nil {
		t.log.WithError(err).Debug("no entry price for tracker")
		return
	}
	for _, d := range t.intervals {
		d := d
		time.AfterFunc(d, func() { t.snapshot(id, entry, d) })
	}
}

func (t *Tracker) snapshot(id uint, entry float64, after time.Duration) {
	call, ok, err := t.store.GetKolCall(id)
	if err != nil || !ok {
		return
	}
	price, err := t.priceNow(call.TokenAddress)
	if err != nil {
		return
	}
	ret := 0.0
	if entry > 0 {
		ret = (price - entry) / entry
	}
	switch after {
	case 2 * time.Minute:
		call.PriceAt2min, call.Return2min = price, ret
	case 6 * time.Minute:
		call.PriceAt6min, call.Return6min = price, ret
		if ret > 0 {
			call.WinLossFlag = "win"
		} else {
			call.WinLossFlag = "loss"
		}
	case 10 * time.Minute:
		call.PriceAt10min = price
	case time.Hour:
		call.PriceAt1hr = price
	case 4 * time.Hour:
		call.PriceAt4hr = price
	case 12 * time.Hour:
		call.PriceAt12hr = price
	}
	if ret > call.MaxGain {
		call.MaxGain = ret
	}
	if err := t.store.UpdateKolCall(call); err != nil {
		t.log.WithError(err).Debug("failed to update KOL call snapshot")
	}
}

func (t *Tracker) priceNow(token string) (float64, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	return t.prices.PriceUSD(ctx, token)
}

// StubPriceProvider returns 0 and is used when no price feed is configured.
type StubPriceProvider struct{}

func (StubPriceProvider) PriceUSD(context.Context, string) (float64, error) { return 0, nil }
