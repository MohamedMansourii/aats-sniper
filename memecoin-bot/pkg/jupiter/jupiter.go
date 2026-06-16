// Package jupiter is a minimal, read-only client for the Jupiter aggregator
// quote API on Solana. It only fetches quotes (price discovery / slippage
// estimation). It deliberately does NOT build or submit swap transactions —
// that path moves real funds and is left to the operator.
package jupiter

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

const (
	// SOL mint, used as the default input when pricing a buy.
	WSOLMint = "So11111111111111111111111111111111111111112"
)

type Client struct {
	baseURL string
	http    *http.Client
}

func New(baseURL string) *Client {
	if baseURL == "" {
		baseURL = "https://quote-api.jup.ag/v6"
	}
	return &Client{
		baseURL: baseURL,
		http:    &http.Client{Timeout: 3 * time.Second},
	}
}

// Quote is the trimmed subset of the Jupiter /quote response we care about.
type Quote struct {
	InputMint     string `json:"inputMint"`
	OutputMint    string `json:"outputMint"`
	InAmount      string `json:"inAmount"`
	OutAmount     string `json:"outAmount"`
	PriceImpactPct string `json:"priceImpactPct"`
}

// PriceImpact parses PriceImpactPct as a float (percentage points).
func (q Quote) PriceImpact() float64 {
	f, _ := strconv.ParseFloat(q.PriceImpactPct, 64)
	return f * 100
}

// GetQuote fetches a quote for swapping `amount` (in the input mint's base
// units) of inputMint -> outputMint at the given slippage (in basis points).
func (c *Client) GetQuote(ctx context.Context, inputMint, outputMint string, amount uint64, slippageBps int) (*Quote, error) {
	q := url.Values{}
	q.Set("inputMint", inputMint)
	q.Set("outputMint", outputMint)
	q.Set("amount", strconv.FormatUint(amount, 10))
	q.Set("slippageBps", strconv.Itoa(slippageBps))

	endpoint := fmt.Sprintf("%s/quote?%s", c.baseURL, q.Encode())
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("jupiter quote: status %d", resp.StatusCode)
	}
	var out Quote
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, err
	}
	return &out, nil
}
