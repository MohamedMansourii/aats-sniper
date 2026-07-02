"""EN4 — RawPost -> CallerCall extraction glue (native Python; NO-LLM).

This module is the missing glue between raw social ingestion (`RawPost`,
`aats.sentiment.models`) and the E9 alpha-caller track-record scorer
(`CallerCall`, `aats.sentiment.caller_score`).  It performs the "Telethon
CA-extraction step" the elite bots run, reimplemented NATIVELY — the
directive forbids porting `memecoin-bot/pkg/telegram/extract.go`, so this
file was written from scratch against the spec, not translated from Go.

SLOW-LOOP ONLY.  This module MUST NEVER be imported by or called from the
SNIPE loop or FAST loop.  A social "call" is exactly the kind of pre-launch
narrative signal that must never gate the millisecond entry decision.

WHAT THIS MODULE DOES (three things, in order):
  1. `extract_mint_candidates()` — a Solana base58 mint-address extractor.
     Matches the base58 alphabet ONLY (digits 1-9, A-Z minus {O, I}, a-z
     minus {l} — '0'/'O'/'I'/'l' are visually-confusable and NEVER part of a
     match), length 32-44 inclusive (the encoded range of a 32-byte pubkey).
  2. `classify_direction()` — a NO-LLM keyword/heuristic classifier that maps
     free text to "long", "avoid", or None (no signal / conflicting signal).
     This is a Tier-A-style CPU heuristic: zero LLM calls, zero network calls,
     zero per-post model inference.
  3. `extract_caller_call()` / `extract_caller_calls()` — map a RawPost (or a
     batch) into a `CallerCall` for the E9 scorer, or DROP it.

HARD RULE (non-waivable, per the adversarial-sentiment charter):
  A post with ZERO or AMBIGUOUS CA matches is DROPPED — never defaulted to a
  "most likely" or "trusted" asset.  Likewise a post with ZERO or CONFLICTING
  direction signal is DROPPED — never defaulted to "long" (the direction that
  would look like a buy signal) nor to "avoid".  Guessing under uncertainty is
  exactly the failure mode this module exists to refuse.

UNTRUSTED DATA:
  `RawPost.text` is raw social content.  The extracted mint string is an
  UNTRUSTED LOOKUP KEY ONLY — this module never executes, evaluates, `eval`s,
  or shells out on any extracted or matched text.  It is pure regex/string
  processing.  The extracted key is later used ONLY to look up on-chain state
  by a downstream module (out of scope here) — it is never itself treated as
  code, an RPC method name, or a trusted instruction.

POINT-IN-TIME (T-300a, load-bearing):
  `CallerCall.call_event_time_ms` is copied VERBATIM from
  `RawPost.event_time_ms` — the platform-stamped publish time.  This module
  NEVER reads `RawPost.fetch_time_ms` (wall-clock at fetch) into the call
  record, and never substitutes the current wall-clock time.  There is no
  code path in this module that computes a timestamp; it only forwards the
  one already stamped by the ingestion adapter.

COST / NO-LLM-PER-POST:
  Zero LLM calls.  Zero network calls.  Pure in-process regex + string
  operations, safe to run over the full un-batched post stream before Tier-A
  and Tier-B ever see anything (this is upstream of both tiers).

DOWNSTREAM BOUNDARY (asymmetric trust, unchanged):
  A `CallerCall` produced here carries NO score and NO conviction by itself.
  It only becomes a signal once resolved outcomes exist and
  `CallerTrackRecordScorer.score_for_asset()` is run (E9, deferred wiring to
  T-401) — and that scorer's `CallerSignal.mcs_delta_contribution` is
  structurally <= 0.0 (selectivity-weighted, de-risk direction only; see
  `aats/sentiment/caller_score.py`).  This module cannot bypass that
  invariant because it does not touch scoring at all — it only builds the
  input record.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from aats.sentiment.caller_score import CallerCall
from aats.sentiment.models import RawPost

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Base58 mint-address extraction
# ---------------------------------------------------------------------------

# Solana (and Bitcoin) base58 alphabet: digits 1-9, upper A-Z minus {O, I},
# lower a-z minus {l}.  '0' (zero), 'O' (capital o), 'I' (capital i), and
# 'l' (lowercase L) are EXCLUDED — they are visually confusable with '1'/'o'
# and are never valid characters in a base58-encoded Solana pubkey.
BASE58_ALPHABET: str = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_CHAR_CLASS = "[1-9A-HJ-NP-Za-km-z]"

# A base58-encoded 32-byte Solana pubkey is 32-44 characters long.
MINT_MIN_LEN: int = 32
MINT_MAX_LEN: int = 44

# Matches a MAXIMAL run of base58-alphabet characters, using the base58
# alphabet itself (not generic \w) as the boundary condition.  This means:
#   - '0'/'O'/'I'/'l' immediately adjacent to a run correctly terminate it
#     (they are NOT base58 characters, so they act as a boundary even though
#     they are ordinary alphanumeric characters).
#   - A run LONGER than 44 pure base58 characters yields NO match at all —
#     this function refuses to guess a 32-44 char substring out of an
#     oversized blob rather than silently chopping it.
_MINT_CANDIDATE_RE = re.compile(
    rf"(?<!{_BASE58_CHAR_CLASS})"
    rf"{_BASE58_CHAR_CLASS}{{{MINT_MIN_LEN},{MINT_MAX_LEN}}}"
    rf"(?!{_BASE58_CHAR_CLASS})"
)


def extract_mint_candidates(text: str) -> list[str]:
    """Extract candidate Solana base58 mint-address strings from raw text.

    UNTRUSTED DATA: `text` is raw, untrusted social content.  This function
    performs pure regex extraction — it never evaluates, executes, or
    interprets the text as instructions.  The returned strings are UNTRUSTED
    LOOKUP KEYS, not validated on-chain addresses (on-chain existence checks
    are a separate, out-of-scope concern for a different module).

    Args:
        text: Raw post/message text (QUOTED UNTRUSTED DATA).

    Returns:
        Distinct candidate strings, in order of first appearance.  The SAME
        string repeated multiple times is collapsed to one entry; DIFFERENT
        candidate strings are each retained (this function does NOT resolve
        ambiguity — that is the caller's responsibility, and the caller MUST
        treat 0 or >1 distinct candidates as "drop", never guess).
    """
    seen: dict[str, None] = {}
    for match in _MINT_CANDIDATE_RE.finditer(text):
        candidate = match.group(0)
        if candidate not in seen:
            seen[candidate] = None
    return list(seen.keys())


# ---------------------------------------------------------------------------
# NO-LLM direction classifier ("long" vs "avoid")
# ---------------------------------------------------------------------------

DirectionLabel = Literal["long", "avoid"]

# Bullish / entry-call phrases.  Matching ANY of these (with no AVOID phrase
# present) classifies the post as "long".  Intentionally does NOT include a
# bare "buy" keyword — that single word is too easily negated ("don't buy",
# "not a buy") to be a reliable standalone bullish signal; multi-word bullish
# phrases below are chosen to be hard to accidentally negate.
_LONG_PATTERNS: tuple[str, ...] = (
    r"\bape(?:s|d|ing)?\s*in\b",
    r"\bsend(?:ing)?\s+it\b",
    r"\bload(?:ing)?\s+up\b",
    r"\bloading\s+bags\b",
    r"\bfilling\s+bags\b",
    r"\baccumulat\w*\b",
    r"\bbuy\s+the\s+dip\b",
    r"\bbuy\s+now\b",
    r"\bbuy\s+signal\b",
    r"\bstrong\s+buy\b",
    r"\bget\s+in\s+(?:now|early)\b",
    r"\bgem\s+alert\b",
    r"\bearly\s+gem\b",
    r"\bundervalued\s+gem\b",
    r"\b100x\b",
    r"\bnext\s+100x\b",
    r"\bmoon(?:ing)?\b",
    r"\bbullish\b",
    r"\bgoing\s+long\b",
    r"\blong\s+this\b",
    r"\bgreat\s+entry\b",
    r"\bentry\s+here\b",
)

# De-risk / warning phrases.  Matching ANY of these (with no LONG phrase
# present) classifies the post as "avoid".  Includes explicit negated-buy
# phrasings so that "don't buy this" is correctly read as bearish rather than
# accidentally scored via a bare "buy" token.
_AVOID_PATTERNS: tuple[str, ...] = (
    r"\bavoid\b",
    r"\brug\s*pull\w*\b",
    r"\brugged\b",
    r"\bhoneypot\b",
    r"\bscam\b",
    r"\bponzi\b",
    r"\bexit\s*scam\b",
    r"\bstay\s+away\b",
    r"\bred\s+flag\b",
    r"\bdon'?t\s+buy\b",
    r"\bdo\s+not\s+buy\b",
    r"\bnot\s+a\s+buy\b",
    r"\bno\s+buy\b",
    r"\bsell\s+now\b",
    r"\bsell\s+signal\b",
    r"\bdump(?:ing)?\s+incoming\b",
    r"\bdev\s+(?:is\s+)?dump(?:ing)?\b",
    r"\binsider\s+dump\w*\b",
    r"\bliquidity\s+pulled\b",
    r"\bwarning\b",
    r"\bcaution\b",
    r"\btoo\s+risky\b",
    r"\brisky\s+af\b",
)


def _any_pattern_hits(text_lower: str, patterns: Sequence[str]) -> bool:
    return any(re.search(p, text_lower) for p in patterns)


def classify_direction(text: str) -> DirectionLabel | None:
    """NO-LLM keyword/heuristic direction classifier: "long" vs "avoid".

    UNTRUSTED DATA: `text` is raw social content, treated as data only.

    Returns:
        "long"  — a bullish/entry phrase matched AND no avoid phrase matched.
        "avoid" — a de-risk/warning phrase matched AND no long phrase matched.
        None    — AMBIGUOUS: either (a) NEITHER phrase set matched (no
                  directional signal at all — a bare CA mention is not a
                  "call"), or (b) BOTH phrase sets matched (conflicting
                  signal within the same post).  Callers MUST treat None as
                  "drop the post" — never default to "long" or "avoid".

    This is a pure CPU keyword heuristic (Tier-A style): zero LLM calls,
    zero network calls, safe to run on every post.
    """
    text_lower = text.lower()
    long_hit = _any_pattern_hits(text_lower, _LONG_PATTERNS)
    avoid_hit = _any_pattern_hits(text_lower, _AVOID_PATTERNS)

    if long_hit and not avoid_hit:
        return "long"
    if avoid_hit and not long_hit:
        return "avoid"
    return None


# ---------------------------------------------------------------------------
# RawPost -> CallerCall mapping
# ---------------------------------------------------------------------------

# The E9 caller-call universe for this glue: call-channel sources only.
# "reddit" and "news" posts are NOT call-channel content for E9 purposes
# (per the EN4 task scope: "map a matched RawPost (source telegram/discord/x)").
_CALL_CHANNEL_SOURCES: frozenset[str] = frozenset({"telegram", "discord", "x"})


@dataclass(frozen=True)
class CallExtractionResult:
    """Batch result of `extract_caller_calls()` — audit-friendly drop counts.

    Nothing here is a score or a trigger; it is purely an accounting of how
    many posts were dropped and why, so the pipeline's behaviour is honestly
    observable (no silent guessing anywhere in the drop paths).

    calls:                              Emitted CallerCall records.
    total_posts:                        Number of RawPost inputs considered.
    dropped_unsupported_source:         Dropped: source not telegram/discord/x.
    dropped_no_or_ambiguous_ca:         Dropped: zero or >1 distinct CA match.
    dropped_no_or_ambiguous_direction:  Dropped: no or conflicting direction signal.
    """

    calls: list[CallerCall]
    total_posts: int
    dropped_unsupported_source: int
    dropped_no_or_ambiguous_ca: int
    dropped_no_or_ambiguous_direction: int


def extract_caller_call(post: RawPost) -> CallerCall | None:
    """Map a single RawPost into a CallerCall, or return None (DROP).

    DROP conditions (checked in order; each is a hard drop, never a guess):
      1. `post.source` is not one of {"telegram", "discord", "x"}.
      2. `extract_mint_candidates(post.text)` yields != 1 distinct candidate
         (zero => no CA found; >1 => ambiguous which asset the call is about).
      3. `classify_direction(post.text)` returns None (no or conflicting
         directional signal).

    POINT-IN-TIME: `call_event_time_ms` is copied verbatim from
    `post.event_time_ms` (the platform publish time) — `post.fetch_time_ms`
    is never read by this function.

    Args:
        post: A single ingested RawPost (any adapter).

    Returns:
        A `CallerCall` ready to feed `aats.sentiment.caller_score`, or None
        if the post must be dropped.
    """
    if post.source not in _CALL_CHANNEL_SOURCES:
        logger.debug(
            "call_extract: dropped post_id=%s reason=unsupported_source source=%s",
            post.post_id,
            post.source,
        )
        return None

    candidates = extract_mint_candidates(post.text)
    if len(candidates) != 1:
        logger.debug(
            "call_extract: dropped post_id=%s reason=no_or_ambiguous_ca match_count=%d",
            post.post_id,
            len(candidates),
        )
        return None

    direction = classify_direction(post.text)
    if direction is None:
        logger.debug(
            "call_extract: dropped post_id=%s reason=no_or_ambiguous_direction",
            post.post_id,
        )
        return None

    return CallerCall(
        caller_id=post.author_id,
        call_event_time_ms=post.event_time_ms,  # verbatim platform time (T-300a)
        asset=candidates[0],
        direction=direction,
        source=post.source,
    )


def extract_caller_calls(posts: Sequence[RawPost]) -> CallExtractionResult:
    """Batch-map RawPost objects into CallerCall records with drop accounting.

    Pure in-process function: no LLM calls, no network calls.  Safe to run on
    an unbounded post stream before Tier-A/Tier-B ever see anything.

    Args:
        posts: RawPost objects from any adapter(s).

    Returns:
        CallExtractionResult with the emitted calls and honest drop counts.
    """
    calls: list[CallerCall] = []
    dropped_unsupported_source = 0
    dropped_no_or_ambiguous_ca = 0
    dropped_no_or_ambiguous_direction = 0

    for post in posts:
        if post.source not in _CALL_CHANNEL_SOURCES:
            dropped_unsupported_source += 1
            continue

        candidates = extract_mint_candidates(post.text)
        if len(candidates) != 1:
            dropped_no_or_ambiguous_ca += 1
            continue

        direction = classify_direction(post.text)
        if direction is None:
            dropped_no_or_ambiguous_direction += 1
            continue

        calls.append(
            CallerCall(
                caller_id=post.author_id,
                call_event_time_ms=post.event_time_ms,
                asset=candidates[0],
                direction=direction,
                source=post.source,
            )
        )

    logger.info(
        "call_extract: total=%d emitted=%d dropped_source=%d dropped_ca=%d dropped_direction=%d",
        len(posts),
        len(calls),
        dropped_unsupported_source,
        dropped_no_or_ambiguous_ca,
        dropped_no_or_ambiguous_direction,
    )

    return CallExtractionResult(
        calls=calls,
        total_posts=len(posts),
        dropped_unsupported_source=dropped_unsupported_source,
        dropped_no_or_ambiguous_ca=dropped_no_or_ambiguous_ca,
        dropped_no_or_ambiguous_direction=dropped_no_or_ambiguous_direction,
    )
