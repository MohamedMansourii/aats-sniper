"""E-M1-02 — deploy_template_fingerprint is template-invariant, not per-mint.

Today's bug (pre-fix): fingerprint = sha256(creator_wallet + mint[:8]) changes
on every single launch, even for a repeat rug-factory creator reusing the same
off-chain metadata template — so it can never express "same template, new
wallet."

The fix: decode the pump.fun `create` instruction's inner mpl-token-metadata
CPI (the metadata account is referenced BY INDEX in the outer create ix's
account_keys, never a hardcoded program-ID literal — see decoders.py
PUMP_CREATE_ACCOUNT_IDX_MPL_METADATA_PROGRAM) and fingerprint on the metadata
URI's SHAPE (scheme + host-identity + normalized path pattern with the
per-mint hash/CID collapsed to `*`) rather than its exact bytes.

These tests are mutation-meaningful: each one fails against the OLD
`sha256(creator+mint[:8])` implementation (different mint/creator always
differs) and passes against the fixed, URI-shape-based implementation.
"""

from __future__ import annotations

from aats.contracts.events import DetectionTransport, LaunchSource
from aats.ingestion.decoders import (
    PumpFunDecoder,
    _deploy_template_fingerprint_from_uri,
    _find_mpl_metadata_uri,
    _parse_mpl_create_metadata_uri,
    _uri_shape,
)
from tests.ingestion.fixtures import (
    MPL_TOKEN_METADATA_PROGRAM_ID,
    make_mpl_create_metadata_ix,
    make_pumpfun_create_tx,
    make_registry,
)


def _decode_create(**kwargs):
    registry = make_registry()
    decoder = PumpFunDecoder(registry)
    tx = make_pumpfun_create_tx(**kwargs)
    result = decoder.decode(tx, DetectionTransport.GEYSER)
    assert result is not None, "expected a decoded LaunchEvent for this create fixture"
    ev, _kind = result
    return ev


# ---------------------------------------------------------------------------
# End-to-end: same template, different wallet/mint -> SAME fingerprint
# ---------------------------------------------------------------------------


class TestSameTemplateDifferentWallet:
    def test_same_domain_and_path_shape_same_creator_different_mint(self):
        ev1 = _decode_create(
            creator="WalletRugFactory11111111111111111111111111111",
            mint="MintLaunchOne111111111111111111111111111111111",
            metadata_uri="https://ipfs.io/ipfs/QmHashOneAAAAAAAAAAAAAAAAAAAAAAAAAA.json",
        )
        ev2 = _decode_create(
            creator="WalletRugFactory11111111111111111111111111111",
            mint="MintLaunchTwo111111111111111111111111111111111",
            metadata_uri="https://ipfs.io/ipfs/QmHashTwoBBBBBBBBBBBBBBBBBBBBBBBBBB.json",
        )
        assert ev1.mint != ev2.mint
        assert ev1.deploy_template_fingerprint is not None
        assert ev1.deploy_template_fingerprint == ev2.deploy_template_fingerprint

    def test_same_template_different_creator_wallet_different_mint(self):
        """The core bug-fix case: a REPEAT TEMPLATE from a NEW wallet on a NEW
        mint must fingerprint identically — this is what lets the risk gates
        express 'same rug factory, new wallet.'"""
        ev1 = _decode_create(
            creator="WalletAAA111111111111111111111111111111111111",
            mint="MintAAA1111111111111111111111111111111111111111",
            metadata_uri="https://pump.mypinata.cloud/ipfs/QmABCDEFGHIJKLMNOPQRSTUVWXYZ012345.json",
        )
        ev2 = _decode_create(
            creator="WalletZZZ111111111111111111111111111111111111",
            mint="MintZZZ1111111111111111111111111111111111111111",
            metadata_uri="https://pump.mypinata.cloud/ipfs/Qm999888777666555444333222111000.json",
        )
        assert ev1.creator_wallet != ev2.creator_wallet
        assert ev1.mint != ev2.mint
        assert ev1.deploy_template_fingerprint is not None
        assert ev1.deploy_template_fingerprint == ev2.deploy_template_fingerprint

    def test_fingerprint_unaffected_by_name_and_symbol(self):
        """The fingerprint is derived from the URI shape only — different
        display name/symbol (also on the same metadata CPI) must not change
        it, otherwise 'same template' launches with cosmetic renames would
        wrongly diverge."""
        ev1 = _decode_create(
            metadata_uri="https://ipfs.io/ipfs/QmSameShapeHashAAAAAAAAAAAAAAAAAAAA.json",
            metadata_name="Doge Coin",
            metadata_symbol="DOGE",
            mint="MintNameTestA11111111111111111111111111111111",
        )
        ev2 = _decode_create(
            metadata_uri="https://ipfs.io/ipfs/QmSameShapeHashBBBBBBBBBBBBBBBBBBBB.json",
            metadata_name="Cat Coin",
            metadata_symbol="CAT",
            mint="MintNameTestB11111111111111111111111111111111",
        )
        assert ev1.deploy_template_fingerprint == ev2.deploy_template_fingerprint


# ---------------------------------------------------------------------------
# Different templates -> DIFFERENT fingerprint
# ---------------------------------------------------------------------------


class TestDifferentTemplatesDiffer:
    def test_different_hosting_domain_differs(self):
        ev1 = _decode_create(
            mint="MintDomainA111111111111111111111111111111111111",
            metadata_uri="https://ipfs.io/ipfs/QmSameShapeAAAAAAAAAAAAAAAAAAAAAAAA.json",
        )
        ev2 = _decode_create(
            mint="MintDomainB111111111111111111111111111111111111",
            metadata_uri="https://arweave.net/SameShapeAAAAAAAAAAAAAAAAAAAAAAAA",
        )
        assert ev1.deploy_template_fingerprint is not None
        assert ev2.deploy_template_fingerprint is not None
        assert ev1.deploy_template_fingerprint != ev2.deploy_template_fingerprint

    def test_different_path_shape_same_domain_differs(self):
        ev1 = _decode_create(
            mint="MintPathA1111111111111111111111111111111111111",
            metadata_uri="https://ipfs.io/ipfs/QmPathShapeAAAAAAAAAAAAAAAAAAAAAAAA.json",
        )
        ev2 = _decode_create(
            mint="MintPathB1111111111111111111111111111111111111",
            metadata_uri="https://ipfs.io/api/v2/metadata/QmPathShapeBBBBBBBBBBBBBBBBBBBBB.json",
        )
        assert ev1.deploy_template_fingerprint != ev2.deploy_template_fingerprint

    def test_different_file_extension_differs(self):
        ev1 = _decode_create(
            mint="MintExtA11111111111111111111111111111111111111",
            metadata_uri="https://ipfs.io/ipfs/QmExtensionAAAAAAAAAAAAAAAAAAAAAA.json",
        )
        ev2 = _decode_create(
            mint="MintExtB11111111111111111111111111111111111111",
            metadata_uri="https://ipfs.io/ipfs/QmExtensionBBBBBBBBBBBBBBBBBBBBBB.png",
        )
        assert ev1.deploy_template_fingerprint != ev2.deploy_template_fingerprint


# ---------------------------------------------------------------------------
# Malformed / absent metadata -> None (refuse-by-default, never fabricate)
# ---------------------------------------------------------------------------


class TestMalformedMetadataYieldsNone:
    def test_no_metadata_cpi_present(self):
        ev = _decode_create(metadata_uri=None)
        assert ev.deploy_template_fingerprint is None

    def test_metadata_cpi_payload_truncated(self):
        ev = _decode_create(malformed_metadata_ix=True)
        assert ev.deploy_template_fingerprint is None

    def test_empty_uri_string(self):
        ev = _decode_create(metadata_uri="")
        assert ev.deploy_template_fingerprint is None

    def test_non_url_uri(self):
        ev = _decode_create(metadata_uri="this-is-not-a-url")
        assert ev.deploy_template_fingerprint is None

    def test_unsupported_scheme(self):
        ev = _decode_create(metadata_uri="ftp://example.com/metadata.json")
        assert ev.deploy_template_fingerprint is None

    def test_mint_still_decodes_when_metadata_absent(self):
        """A missing/malformed metadata CPI must not block the rest of the
        create event — only deploy_template_fingerprint degrades to None."""
        ev = _decode_create(metadata_uri=None, mint="MintStillDecodes11111111111111111111111111")
        assert ev.mint == "MintStillDecodes11111111111111111111111111"
        assert ev.deploy_template_fingerprint is None


# ---------------------------------------------------------------------------
# Whitebox unit tests on the pure helper functions
# ---------------------------------------------------------------------------


class TestUriShape:
    def test_domain_kept_verbatim_lowercased(self):
        shape = _uri_shape("https://IPFS.io/ipfs/QmSomeHashAAAAAAAAAAAAAAAAAAAAAAAA.json")
        assert shape is not None
        assert shape.startswith("https://ipfs.io/")

    def test_long_path_segment_collapsed_short_segment_kept(self):
        shape = _uri_shape("https://ipfs.io/ipfs/QmSomeVeryLongHashValueXYZ123456789.json")
        assert shape == "https://ipfs.io/ipfs/*.json"

    def test_query_keys_kept_values_stripped(self):
        shape1 = _uri_shape("https://example.com/meta.json?v=1111111111111111111")
        shape2 = _uri_shape("https://example.com/meta.json?v=2222222222222222222")
        assert shape1 == shape2
        assert shape1 is not None and "v" in shape1

    def test_ipfs_scheme_netloc_is_the_hash_and_collapses(self):
        shape1 = _uri_shape("ipfs://QmAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/metadata.json")
        shape2 = _uri_shape("ipfs://QmBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB/metadata.json")
        assert shape1 == shape2 == "ipfs://*/metadata.json"

    def test_empty_string_is_none(self):
        assert _uri_shape("") is None

    def test_missing_netloc_is_none(self):
        assert _uri_shape("https:///no/host/here.json") is None


class TestDeployTemplateFingerprintFromUri:
    def test_none_uri_is_none(self):
        assert _deploy_template_fingerprint_from_uri(None) is None

    def test_valid_uri_is_16_hex_chars(self):
        fp = _deploy_template_fingerprint_from_uri(
            "https://ipfs.io/ipfs/QmSomeHashAAAAAAAAAAAAAAAAAAAAAAAA.json"
        )
        assert fp is not None
        assert len(fp) == 16
        int(fp, 16)  # must be valid hex


class TestParseMplCreateMetadataUri:
    def test_round_trips_through_fixture_builder(self):
        ix = make_mpl_create_metadata_ix(uri="https://example.com/a/b.json")
        import base64

        data = base64.b64decode(ix.data_b64)
        assert _parse_mpl_create_metadata_uri(data) == "https://example.com/a/b.json"

    def test_truncated_payload_is_none(self):
        assert _parse_mpl_create_metadata_uri(bytes([33])) is None

    def test_empty_payload_is_none(self):
        assert _parse_mpl_create_metadata_uri(b"") is None


class TestFindMplMetadataUri:
    def test_no_program_id_given_is_none(self):
        ix = make_mpl_create_metadata_ix()
        assert _find_mpl_metadata_uri([ix], None) is None

    def test_no_matching_inner_instruction_is_none(self):
        ix = make_mpl_create_metadata_ix(program_id="SomeOtherProgram1111111111111111111111111111")
        assert _find_mpl_metadata_uri([ix], MPL_TOKEN_METADATA_PROGRAM_ID) is None

    def test_matching_inner_instruction_returns_uri(self):
        ix = make_mpl_create_metadata_ix(uri="https://example.com/x.json")
        found = _find_mpl_metadata_uri([ix], MPL_TOKEN_METADATA_PROGRAM_ID)
        assert found == "https://example.com/x.json"


# ---------------------------------------------------------------------------
# Field/type conformance (data-models.md §2: deploy_template_fingerprint: str | None)
# ---------------------------------------------------------------------------


class TestFieldConformance:
    def test_field_is_str_or_none_never_other_type(self):
        ev_with = _decode_create()
        ev_without = _decode_create(metadata_uri=None)
        assert isinstance(ev_with.deploy_template_fingerprint, str)
        assert ev_without.deploy_template_fingerprint is None

    def test_source_is_still_pumpfun(self):
        """The fix must not change unrelated LaunchEvent fields."""
        ev = _decode_create()
        assert ev.source == LaunchSource.PUMPFUN
