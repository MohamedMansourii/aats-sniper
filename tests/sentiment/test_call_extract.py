"""Tests for EN4 — RawPost -> CallerCall extraction glue (call_extract.py).

Mandatory self-check items verified here (per the EN4 dispatch):
  CE-A: Base58 extraction correctness, INCLUDING false-positive rejection
        (0/O/I/l correctly break/exclude a run; oversized runs are refused).
  CE-B: Ambiguous (0 or >1 distinct CA) CA matches => DROPPED, never guessed.
  CE-C: Direction heuristic ("long" vs "avoid"), no/conflicting signal => None.
  CE-D: event_time honesty — CallerCall.call_event_time_ms is the post's
        platform event_time_ms, VERBATIM, never fetch_time_ms / wall-clock.
  CE-E: The only path out of this module (CallerCall) can only reach a
        scorer (CallerTrackRecordScorer) whose output is <= 1.0 selectivity /
        <= 0.0 mcs_delta_contribution — proven by an end-to-end hookup test.
  CE-F: No LLM / no network calls anywhere in this module (grep-verified).
"""

from __future__ import annotations

import inspect

import pytest

from aats.sentiment.call_extract import (
    BASE58_ALPHABET,
    MINT_MAX_LEN,
    MINT_MIN_LEN,
    CallExtractionResult,
    classify_direction,
    extract_caller_call,
    extract_caller_calls,
    extract_mint_candidates,
)
from aats.sentiment.caller_score import (
    DEFAULT_UNIVERSE_ACCURACY,
    CallerCall,
    CallerCallOutcome,
    CallerTrackRecordScorer,
    InMemoryCallerOutcomeStore,
    assert_caller_signal_cannot_raise_conviction,
)
from tests.sentiment.fixtures import make_post

# ---------------------------------------------------------------------------
# Fixture addresses
# ---------------------------------------------------------------------------

# Two real, well-known Solana mint addresses (43-44 chars, pure base58) used
# as "happy path" fixtures.  They are used here only as realistic-shaped
# strings — this module never validates on-chain existence.
_WSOL = "So11111111111111111111111111111111111111112"          # 43 chars
_BONK_LOOKALIKE = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"  # 44 chars

assert len(_WSOL) == 43
assert len(_BONK_LOOKALIKE) == 44
assert all(c in BASE58_ALPHABET for c in _WSOL)
assert all(c in BASE58_ALPHABET for c in _BONK_LOOKALIKE)


# ---------------------------------------------------------------------------
# CE-A: Base58 extraction correctness + false-positive rejection
# ---------------------------------------------------------------------------


class TestBase58Extraction:
    def test_alphabet_excludes_confusable_characters(self) -> None:
        """BASE58_ALPHABET must never contain 0, O, I, or l."""
        for forbidden in ("0", "O", "I", "l"):
            assert forbidden not in BASE58_ALPHABET, (
                f"BASE58_ALPHABET contains forbidden char {forbidden!r} — "
                "Solana base58 excludes 0/O/I/l."
            )
        assert len(BASE58_ALPHABET) == 58

    def test_extracts_single_valid_candidate_happy_path(self) -> None:
        text = f"New gem just launched, CA: {_WSOL} — LFG!"
        assert extract_mint_candidates(text) == [_WSOL]

    def test_extracts_44_char_candidate(self) -> None:
        text = f"contract address {_BONK_LOOKALIKE} check it out"
        assert extract_mint_candidates(text) == [_BONK_LOOKALIKE]

    def test_no_candidate_in_plain_english_text(self) -> None:
        """A generic sentence with no base58-length run yields zero candidates."""
        text = "This project looks promising, the team seems active and responsive."
        assert extract_mint_candidates(text) == []

    def test_no_candidate_in_short_alnum_tokens(self) -> None:
        """Short tokens like tickers/hashtags (< 32 chars) never match."""
        text = "$DOGE #Solana pump.fun degen play 123abc"
        assert extract_mint_candidates(text) == []

    @pytest.mark.parametrize("forbidden_char", ["0", "O", "I", "l"])
    def test_false_positive_rejection_forbidden_char_breaks_run(
        self, forbidden_char: str
    ) -> None:
        """Inserting a forbidden char mid-run must NOT yield the tampered
        44-char string as a candidate.

        Splitting a 44-char run with one forbidden character produces two
        fragments (20 + 23 chars) both BELOW MINT_MIN_LEN, so the correct,
        non-guessing behaviour is: extract NOTHING from that run at all.
        """
        tampered = _BONK_LOOKALIKE[:20] + forbidden_char + _BONK_LOOKALIKE[21:]
        assert len(tampered) == len(_BONK_LOOKALIKE)
        candidates = extract_mint_candidates(f"CA: {tampered}")
        assert _BONK_LOOKALIKE not in candidates
        assert tampered not in candidates
        assert candidates == []

    def test_false_positive_rejection_oversized_run_refused(self) -> None:
        """A run LONGER than MINT_MAX_LEN pure base58 chars yields NO match —
        the extractor refuses to guess a 32-44 char substring out of an
        oversized blob (e.g. a long hash, a base58-encoded blob, junk)."""
        oversized = "A" * (MINT_MAX_LEN + 1)  # 45 chars, all valid base58
        assert extract_mint_candidates(f"blob {oversized} end") == []

    def test_minimum_length_boundary(self) -> None:
        exactly_min = "A" * MINT_MIN_LEN
        too_short = "A" * (MINT_MIN_LEN - 1)
        assert extract_mint_candidates(f"x {exactly_min} y") == [exactly_min]
        assert extract_mint_candidates(f"x {too_short} y") == []

    def test_maximum_length_boundary(self) -> None:
        exactly_max = "A" * MINT_MAX_LEN
        too_long = "A" * (MINT_MAX_LEN + 1)
        assert extract_mint_candidates(f"x {exactly_max} y") == [exactly_max]
        assert extract_mint_candidates(f"x {too_long} y") == []

    def test_dedupes_identical_repeated_address(self) -> None:
        """The SAME address mentioned twice is ONE distinct candidate (not ambiguous)."""
        text = f"CA: {_WSOL}\nConfirming CA again: {_WSOL} — same token!"
        assert extract_mint_candidates(text) == [_WSOL]

    def test_ambiguous_two_distinct_addresses(self) -> None:
        """Two DIFFERENT valid-length addresses in one post => both returned;
        the CALLER (extract_caller_call) is responsible for treating this as
        ambiguous and dropping — this function itself just reports what it found."""
        text = f"Either {_WSOL} or {_BONK_LOOKALIKE}, not sure which"
        candidates = extract_mint_candidates(text)
        assert set(candidates) == {_WSOL, _BONK_LOOKALIKE}
        assert len(candidates) == 2

    def test_never_executes_or_evaluates_text(self) -> None:
        """Extraction must be pure string/regex processing — embedding a
        dangerous-looking payload alongside a valid CA must not affect
        Python's runtime state; the extractor must return the CA candidate
        unchanged and untouched as an opaque string."""
        payload = "__import__('os').system('echo pwned')"
        text = f"{payload} CA: {_WSOL} moon soon"
        candidates = extract_mint_candidates(text)
        assert candidates == [_WSOL]


# ---------------------------------------------------------------------------
# CE-C: NO-LLM direction classifier
# ---------------------------------------------------------------------------


class TestDirectionClassifier:
    @pytest.mark.parametrize(
        "text",
        [
            "Ape in now, this is going to moon!",
            "Bullish af, loading up on this one",
            "Strong buy signal, get in early",
            "This is the next 100x gem alert",
            "Going long on this, great entry here",
        ],
    )
    def test_long_phrases_classify_as_long(self, text: str) -> None:
        assert classify_direction(text) == "long"

    @pytest.mark.parametrize(
        "text",
        [
            "Avoid this one, looks like a rug pull",
            "Honeypot detected, stay away",
            "This is a scam, red flag everywhere",
            "Sell signal firing, dev is dumping",
            "Warning: liquidity pulled, exit scam confirmed",
        ],
    )
    def test_avoid_phrases_classify_as_avoid(self, text: str) -> None:
        assert classify_direction(text) == "avoid"

    def test_negated_buy_phrase_is_avoid_not_long(self) -> None:
        """'don't buy' must NOT be misread as a bullish 'buy' signal."""
        assert classify_direction("Don't buy this, it's rugged") == "avoid"
        assert classify_direction("Do not buy, not a buy at all") == "avoid"

    def test_no_signal_at_all_is_ambiguous_none(self) -> None:
        """A bare CA drop with no sentiment words at all is NOT a directional call."""
        assert classify_direction("Just sharing the CA, no comment") is None
        assert classify_direction(_WSOL) is None

    def test_conflicting_signal_is_ambiguous_none(self) -> None:
        """Both a long AND an avoid phrase in the same post => conflicting => None."""
        assert classify_direction("Bullish but also feels like a scam, be careful") is None
        assert classify_direction("Ape in now, or maybe avoid, honestly a rug pull vibe") is None

    def test_classifier_is_case_insensitive(self) -> None:
        assert classify_direction("APE IN NOW, BULLISH AF") == "long"
        assert classify_direction("AVOID, THIS IS A SCAM") == "avoid"


class TestBullishFamilyNegationGuard:
    """PROGRAM-REVIEW-2026-07-03.md finding #6 (MED, dormant):
    `classify_direction()` misclassified negated bullish phrases as "long".

    Reproduced defect (must now be fixed, never re-regress):
        classify_direction(
            "Not bullish on this one at all, would not touch it"
        ) used to return "long" — directly contradicting the docstring's own
        "never default to long" rule.  A negated bullish-family keyword must
        drop the post (return None), exactly like the pre-existing
        buy-family negation handling for "don't buy" / "not a buy".
    """

    def test_reproduced_negation_case_is_no_longer_long(self) -> None:
        """The exact reproduced string from the program review."""
        result = classify_direction(
            "Not bullish on this one at all, would not touch it"
        )
        assert result != "long", (
            "regression: negated bullish phrase misclassified as 'long'"
        )
        assert result is None

    @pytest.mark.parametrize(
        "text",
        [
            "Not bullish on this project, would not touch it",
            "This is definitely not going to moon",
            "This will never 100x, don't get your hopes up",
            "Not going long on this one, feels like a trap",
            "No way this is mooning, not buying the hype",
            "I am not bullish at all on this chart",
        ],
    )
    def test_negated_bullish_family_phrase_is_never_long(self, text: str) -> None:
        assert classify_direction(text) != "long"

    def test_negated_bullish_with_no_other_signal_is_dropped_none(self) -> None:
        """A negated bullish keyword with nothing else present is NO SIGNAL
        (None) — the guard must discard the contribution, never guess
        'avoid' as a replacement label."""
        assert classify_direction("Not bullish on this one, would not touch it") is None
        assert classify_direction("This will never 100x honestly") is None
        assert classify_direction("Not going long on this one") is None

    def test_negated_bullish_plus_explicit_avoid_phrase_is_avoid(self) -> None:
        """When an explicit avoid phrase is ALSO present, that phrase (not a
        guess) drives the 'avoid' classification; the negated bullish token
        contributes nothing to either side."""
        assert classify_direction("Not bullish at all, this is a scam") == "avoid"

    def test_unnegated_bullish_family_phrase_still_classifies_long(self) -> None:
        """Sanity check: the guard must not over-fire on plain (unnegated)
        bullish-family phrases — those must still classify as 'long'."""
        assert classify_direction("Extremely bullish on this one") == "long"
        assert classify_direction("This is going to moon soon") == "long"
        assert classify_direction("Going long on this, LFG") == "long"
        assert classify_direction("This is the next 100x for sure") == "long"

    def test_negation_scope_does_not_leak_across_a_clause_boundary(self) -> None:
        """A negation cue in an EARLIER, unrelated clause (separated by a
        full stop) must NOT suppress a later, distinct bullish mention —
        the guard is local to the clause governing the keyword, not a
        blanket 'any negation anywhere in the post' rule."""
        assert (
            classify_direction("No thanks, I'm out. Bullish on this one though!")
            == "long"
        )


# ---------------------------------------------------------------------------
# CE-B / CE-D: extract_caller_call — RawPost -> CallerCall (or DROP)
# ---------------------------------------------------------------------------


class TestExtractCallerCall:
    def test_happy_path_produces_callercall(self) -> None:
        post = make_post(
            f"Ape in now! CA: {_WSOL} — moon soon",
            source="telegram",
            author_id="caller_alpha",
        )
        call = extract_caller_call(post)
        assert call is not None
        assert isinstance(call, CallerCall)
        assert call.caller_id == "caller_alpha"
        assert call.asset == _WSOL
        assert call.direction == "long"
        assert call.source == "telegram"

    def test_zero_ca_matches_is_dropped(self) -> None:
        post = make_post("Ape in now, bullish af, no CA mentioned here", source="telegram")
        assert extract_caller_call(post) is None

    def test_ambiguous_ca_matches_is_dropped_never_defaults(self) -> None:
        """Two distinct CA candidates => DROP.  Must NOT default to the
        first-seen candidate (that would be exactly the forbidden
        'default to a trusted asset' behaviour)."""
        post = make_post(
            f"Ape in now! Either {_WSOL} or {_BONK_LOOKALIKE}",
            source="telegram",
        )
        assert extract_caller_call(post) is None

    def test_no_direction_signal_is_dropped(self) -> None:
        post = make_post(f"CA: {_WSOL}", source="discord")
        assert extract_caller_call(post) is None

    def test_conflicting_direction_signal_is_dropped(self) -> None:
        post = make_post(
            f"CA: {_WSOL} — bullish but also smells like a scam, be careful",
            source="discord",
        )
        assert extract_caller_call(post) is None

    @pytest.mark.parametrize("unsupported_source", ["reddit", "news"])
    def test_unsupported_source_is_dropped(self, unsupported_source: str) -> None:
        post = make_post(
            f"Ape in now! CA: {_WSOL}",
            source=unsupported_source,
        )
        assert extract_caller_call(post) is None

    @pytest.mark.parametrize("supported_source", ["telegram", "discord", "x"])
    def test_supported_call_channel_sources_all_pass(self, supported_source: str) -> None:
        post = make_post(
            f"Ape in now! CA: {_WSOL}",
            source=supported_source,
        )
        call = extract_caller_call(post)
        assert call is not None
        assert call.source == supported_source

    def test_never_executes_embedded_payload(self) -> None:
        """The extracted asset must be the opaque CA string, never a payload
        that got 'executed' anywhere in the mapping path."""
        payload = "'; DROP TABLE calls; --"
        post = make_post(f"Ape in! {payload} CA: {_WSOL}", source="x")
        call = extract_caller_call(post)
        assert call is not None
        assert call.asset == _WSOL


# ---------------------------------------------------------------------------
# CE-D: event_time honesty (T-300a) — NEVER fetch_time_ms / wall-clock
# ---------------------------------------------------------------------------


class TestEventTimeHonesty:
    def test_call_event_time_equals_post_event_time_verbatim(self) -> None:
        post = make_post(
            f"Ape in! CA: {_WSOL}",
            source="telegram",
            event_time_ms=1_700_000_000_000,
        )
        call = extract_caller_call(post)
        assert call is not None
        assert call.call_event_time_ms == post.event_time_ms
        assert call.call_event_time_ms == 1_700_000_000_000

    def test_fetch_time_is_never_used_as_call_event_time(self) -> None:
        """Construct a post where fetch_time_ms is WILDLY different from
        event_time_ms (simulating ingestion lag) and prove the emitted
        CallerCall uses event_time_ms, not fetch_time_ms."""
        from aats.sentiment.models import RawPost

        post = RawPost(
            post_id="p_lag",
            source="telegram",
            author_id="caller_lag",
            text=f"Ape in! CA: {_WSOL}",
            event_time_ms=1_700_000_000_000,
            fetch_time_ms=1_700_000_000_000 + 999_999_999,  # huge ingestion lag
            account_age_days=100.0,
            follower_count=500,
            following_count=100,
            like_count=10,
            reply_count=2,
            repost_count=1,
            is_verified=False,
            has_default_avatar=False,
            posting_cadence_per_day=5.0,
        )
        call = extract_caller_call(post)
        assert call is not None
        assert call.call_event_time_ms == post.event_time_ms
        assert call.call_event_time_ms != post.fetch_time_ms

    def test_module_never_reads_wall_clock(self) -> None:
        """Source-grep: call_extract.py must not call time.time()/datetime.now()
        anywhere — it must only forward the already-stamped event_time_ms."""
        import aats.sentiment.call_extract as mod

        source = inspect.getsource(mod)
        assert "time.time(" not in source
        assert "datetime.now(" not in source
        assert "utcnow(" not in source


# ---------------------------------------------------------------------------
# Batch extraction + no-LLM / no-network self-check
# ---------------------------------------------------------------------------


class TestExtractCallerCallsBatch:
    def test_batch_counts_and_emitted_calls(self) -> None:
        posts = [
            make_post(f"Ape in! CA: {_WSOL}", source="telegram", author_id="c1"),  # emitted
            make_post("No CA here, bullish though", source="telegram"),  # dropped: no CA
            make_post(f"Either {_WSOL} or {_BONK_LOOKALIKE}", source="discord"),  # ambiguous CA
            make_post(f"CA: {_BONK_LOOKALIKE}", source="x"),  # dropped: no direction
            make_post(f"Avoid! Rug pull. CA: {_BONK_LOOKALIKE}", source="reddit"),  # unsupported source
        ]
        result = extract_caller_calls(posts)
        assert isinstance(result, CallExtractionResult)
        assert result.total_posts == 5
        assert len(result.calls) == 1
        assert result.calls[0].caller_id == "c1"
        assert result.dropped_unsupported_source == 1
        assert result.dropped_no_or_ambiguous_ca == 2
        assert result.dropped_no_or_ambiguous_direction == 1

    def test_empty_input_returns_empty_result(self) -> None:
        result = extract_caller_calls([])
        assert result.calls == []
        assert result.total_posts == 0
        assert result.dropped_unsupported_source == 0
        assert result.dropped_no_or_ambiguous_ca == 0
        assert result.dropped_no_or_ambiguous_direction == 0

    def test_no_llm_or_network_imports_in_module_source(self) -> None:
        """No-LLM-per-item rule (Tier-A) — this module must be pure regex/string
        processing.  Grep the source for any LLM/HTTP/network client usage."""
        import aats.sentiment.call_extract as mod

        source = inspect.getsource(mod)
        forbidden_tokens = [
            "openai",
            "instructor",
            "httpx",
            "requests.",
            "aiohttp",
            "socket.",
            "urllib",
        ]
        for token in forbidden_tokens:
            assert token not in source, f"Forbidden token {token!r} found in call_extract.py"

    def test_no_eval_exec_or_shell_in_module_source(self) -> None:
        """The extracted CA text is an untrusted lookup key — never executed."""
        import aats.sentiment.call_extract as mod

        source = inspect.getsource(mod)
        for token in ("eval(", "exec(", "os.system(", "subprocess."):
            assert token not in source, f"Forbidden token {token!r} found in call_extract.py"


# ---------------------------------------------------------------------------
# CE-E: downstream boundary — output can only reach a non-positive scorer
# ---------------------------------------------------------------------------


class TestDownstreamScorerBoundary:
    """Prove the CallerCall records this module emits, once resolved into
    outcomes and scored by the REAL E9 scorer, can never raise conviction —
    mcs_delta_contribution stays <= 0.0 and caller_selectivity_modifier stays
    <= 1.0 regardless of the extracted call's direction."""

    DECISION_TIME_MS = 1_718_500_000_000
    ONE_HOUR_MS = 3_600_000
    ONE_WEEK_MS = 7 * 24 * 3_600_000

    def _extract_one(self, text: str, author_id: str) -> CallerCall:
        post = make_post(text, source="telegram", author_id=author_id)
        call = extract_caller_call(post)
        assert call is not None, f"expected a CallerCall to be extracted from: {text!r}"
        return call

    def test_extracted_negative_caller_yields_non_positive_signal(self) -> None:
        call = self._extract_one(f"Ape in! CA: {_WSOL}", author_id="caller_always_wrong")
        assert call.direction == "long"

        # Build a store where this caller is historically always wrong.
        outcomes = [
            CallerCallOutcome(
                caller_id=call.caller_id,
                call_event_time_ms=self.DECISION_TIME_MS - self.ONE_WEEK_MS + i * self.ONE_HOUR_MS,
                outcome_event_time_ms=self.DECISION_TIME_MS
                - self.ONE_WEEK_MS
                + i * self.ONE_HOUR_MS
                + self.ONE_HOUR_MS,
                asset=call.asset,
                outcome_correct=False,
            )
            for i in range(10)
        ]
        store = InMemoryCallerOutcomeStore(outcomes=outcomes)
        # Override the universe prior (DEFAULT_UNIVERSE_ACCURACY) rather than
        # letting it self-derive from this single always-wrong caller's own
        # outcomes (which would make accuracy_delta trivially 0.0) — this
        # mirrors the fixture pattern in test_caller_score.py.
        scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        signal = scorer.score_for_asset(call.asset, [call.caller_id], self.DECISION_TIME_MS)

        assert signal.caller_selectivity_modifier <= 1.0
        assert signal.mcs_delta_contribution <= 0.0
        assert signal.negative_caller_count == 1

        # And the asymmetric-trust boundary itself must hold: applying this
        # signal can only lower or maintain a base conviction, never raise it.
        base_conviction = 0.7
        adjusted = assert_caller_signal_cannot_raise_conviction(signal, base_conviction)
        assert adjusted <= base_conviction

    def test_extracted_avoid_call_also_yields_non_positive_signal(self) -> None:
        """Even an 'avoid'-direction extracted call feeds the SAME scorer,
        which is agnostic to direction and is non-positive by construction —
        proving direction cannot smuggle a size-up path through this glue."""
        call = self._extract_one(
            f"Avoid this, rug pull incoming. CA: {_BONK_LOOKALIKE}",
            author_id="caller_thin_data",
        )
        assert call.direction == "avoid"

        # Thin data (fewer than MIN_CALLS_FOR_SCORE outcomes) -> weight 0.0.
        store = InMemoryCallerOutcomeStore(outcomes=[])
        scorer = CallerTrackRecordScorer(store)
        signal = scorer.score_for_asset(call.asset, [call.caller_id], self.DECISION_TIME_MS)

        assert signal.caller_selectivity_modifier <= 1.0
        assert signal.mcs_delta_contribution <= 0.0
        assert signal.thin_data_caller_count == 1

        base_conviction = 0.5
        adjusted = assert_caller_signal_cannot_raise_conviction(signal, base_conviction)
        assert adjusted <= base_conviction
