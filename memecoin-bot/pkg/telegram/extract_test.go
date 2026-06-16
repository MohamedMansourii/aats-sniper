package telegram

import (
	"reflect"
	"testing"
)

func TestExtractorAddresses(t *testing.T) {
	e := NewExtractor("")
	// A real-shaped Solana address (44 base58 chars) embedded in chatter.
	msg := "ð🚀 NEW GEM!! CA: 9YyZbU96skhTkuxm5AVSr3SB7chWFAoQpLMnUhfdpump ape now"
	got := e.Addresses(msg)
	if len(got) == 0 {
		t.Fatalf("expected at least one address, got none")
	}
}

func TestExtractorDedupe(t *testing.T) {
	e := NewExtractor("")
	addr := "So11111111111111111111111111111111111111112"
	msg := addr + " and again " + addr
	got := e.Addresses(msg)
	if len(got) != 1 {
		t.Fatalf("expected 1 deduped address, got %d (%v)", len(got), got)
	}
}

func TestExtractorIgnoresShortWords(t *testing.T) {
	e := NewExtractor("")
	got := e.Addresses("buy the dip now okay friends")
	if len(got) != 0 {
		t.Fatalf("expected no addresses from prose, got %v", got)
	}
}

func TestTickers(t *testing.T) {
	got := Tickers("loving $bonk and $WIF today, also $bonk again")
	want := []string{"BONK", "WIF"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("tickers = %v, want %v", got, want)
	}
}

func TestNewExtractorInvalidPatternFallsBack(t *testing.T) {
	e := NewExtractor("([") // invalid regex
	if e.caRe == nil {
		t.Fatal("expected fallback regexp, got nil")
	}
}
