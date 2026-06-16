// Package telegram contains the message-parsing helpers used by the scanner.
// Network/listening code (gotd or Bot API) is intentionally left as a stubbed
// source in the scanner agent; this file holds the pure, well-tested parsing
// logic that turns raw channel text into candidate contract addresses.
package telegram

import "regexp"

// DefaultSolanaCAPattern matches a base58 string of plausible Solana address
// length. base58 excludes 0, O, I and l.
const DefaultSolanaCAPattern = `\b[1-9A-HJ-NP-Za-km-z]{32,44}\b`

var tickerRe = regexp.MustCompile(`\$[A-Za-z][A-Za-z0-9_]{1,9}`)

// Extractor compiles a CA pattern once and reuses it.
type Extractor struct {
	caRe *regexp.Regexp
}

// NewExtractor compiles the given pattern, falling back to the default if the
// supplied pattern is empty or invalid.
func NewExtractor(pattern string) *Extractor {
	if pattern == "" {
		pattern = DefaultSolanaCAPattern
	}
	re, err := regexp.Compile(pattern)
	if err != nil {
		re = regexp.MustCompile(DefaultSolanaCAPattern)
	}
	return &Extractor{caRe: re}
}

// Addresses returns de-duplicated contract addresses found in text, preserving
// first-seen order.
func (e *Extractor) Addresses(text string) []string {
	return dedupe(e.caRe.FindAllString(text, -1))
}

// Tickers returns de-duplicated $TICKER symbols (without the $), uppercased.
func Tickers(text string) []string {
	raw := tickerRe.FindAllString(text, -1)
	out := make([]string, 0, len(raw))
	for _, r := range raw {
		out = append(out, upper(r[1:]))
	}
	return dedupe(out)
}

func dedupe(in []string) []string {
	seen := make(map[string]struct{}, len(in))
	out := make([]string, 0, len(in))
	for _, s := range in {
		if _, ok := seen[s]; ok {
			continue
		}
		seen[s] = struct{}{}
		out = append(out, s)
	}
	return out
}

func upper(s string) string {
	b := []byte(s)
	for i := range b {
		if b[i] >= 'a' && b[i] <= 'z' {
			b[i] -= 'a' - 'A'
		}
	}
	return string(b)
}
