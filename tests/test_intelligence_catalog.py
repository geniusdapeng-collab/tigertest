"""Material and trusted source contracts, independent of source connectivity."""
from datetime import datetime, timezone
from copy import deepcopy

import pytest

from trading_system.intelligence.catalog import DIMENSIONS, SourceCatalog, material_policy
from trading_system.intelligence.contracts import IntelligenceError, digest
from trading_system.intelligence.registry import SourceRegistry, SourceSpec


def test_twelve_materials_are_distinct_from_registered_sources():
    catalog = SourceCatalog()
    assert len(DIMENSIONS) == 12
    assert len(catalog.sources) != len(DIMENSIONS) or set(catalog.sources) != set(DIMENSIONS)
    assert catalog.sources["sec_submissions"]["material_types"] == ["issuer_disclosure", "capital_positions"]
    assert catalog.entities["us:NVDA"]["identifiers"]["ticker"] == ["NVDA"]
    assert catalog.manifest()["sha256"] == catalog.manifest()["sha256"]


def test_requirement_policy_does_not_make_every_dimension_required():
    policy = material_policy(has_entities=True)
    assert policy["issuer_disclosure"]["status"] == policy["news_events"]["status"] == "required"
    assert policy["formal_research"]["status"] == "supplemental"
    with pytest.raises(IntelligenceError):
        material_policy({"formal_research": "not_applicable"})
    assert material_policy({"formal_research": {"status": "not_applicable", "reason": "Question concerns only the release wording"}})["formal_research"]["status"] == "not_applicable"


def test_entitlement_and_credentials_are_not_connection_proof():
    reg = SourceRegistry([SourceSpec("deepseek", "search", "https://example.com/search", credential_env="TEST_SOURCE_KEY")])
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    catalog = SourceCatalog(reg, {"sources": {"deepseek": {"entitlement": {
        "kind": "licensed", "reference": "subscription-01", "valid_until": "2026-11-05T00:00:00Z"}}}})
    health = catalog.connection("deepseek", environment={"TEST_SOURCE_KEY": "not-persisted"}, now=now)
    assert health["credential_ready"] and not health["entitlement_ready"] and not health["connected"]
    assert "not-persisted" not in str(health) and not catalog.eligible_sources(["formal_research"], now=now)


def test_catalog_cannot_register_urls_or_smuggle_unknown_authority():
    for config in ({"sources": {"unknown": {}}}, {"api_key": "not-allowed"},
            {"sources": {"fed_policy": {"allowed_uses": ["trade_action"]}}},
            {"sources": {"fed_policy": {"author_ids": ["person:invented"]}}}):
        with pytest.raises(IntelligenceError):
            SourceCatalog(config=config)


def test_person_and_institution_identity_has_role_validity_and_original_source_scope():
    catalog = SourceCatalog(config={"entities": {"institution:reviewed": {"type": "institution", "name": "Reviewed institution"},
        "person:reviewed": {"type": "person", "name": "Reviewed author", "role_history": [{
            "institution_id": "institution:reviewed", "role": "Research head",
            "valid_from": "2026-01-01T00:00:00Z", "valid_until": "2026-12-31T00:00:00Z"}]}},
        "sources": {"fed_policy": {"author_ids": ["person:reviewed"], "business_unit": "Policy communications"}}})
    assert catalog.validate_entities(["person:reviewed"]) == ["person:reviewed"]
    with pytest.raises(IntelligenceError):
        catalog.validate_entities(["person:not-reviewed"])


@pytest.mark.parametrize("entities", [["us:NVDA", "us:NVDA"], ["unknown:company"], [None]])
def test_topic_entity_boundary(entities):
    with pytest.raises(IntelligenceError):
        SourceCatalog().validate_entities(entities)


def _frozen_catalog():
    registry = SourceRegistry([SourceSpec("TEST_FIXTURE-original", "page", "https://example.test/original",
        allowed_hosts=("example.test",), options={"material_types": ["person_statement"],
            "allowed_uses": ["topic_analysis", "historical_baseline"]})])
    catalog = SourceCatalog(registry, {"sources": {"TEST_FIXTURE-original": {
        "publisher": "TEST_FIXTURE Publisher", "business_unit": "TEST_FIXTURE Research",
        "author_ids": ["person:fixture"], "person_statement_basis": "original_statement",
        "entitlement": {"kind": "licensed", "reference": "TEST_FIXTURE-contract",
            "valid_until": "2020-01-01T00:00:00Z"}}}}, reviewed_entities={
        "institution:fixture": {"type": "institution", "name": "TEST_FIXTURE Institution"},
        "person:fixture": {"type": "person", "name": "Ada Specialist", "aliases": ["A. Specialist"],
            "role_history": [{"institution_id": "institution:fixture", "role": "TEST_FIXTURE Analyst",
                "valid_from": "2010-01-01T00:00:00Z", "valid_until": "2020-01-01T00:00:00Z"}]}})
    return catalog.manifest(), registry.public_manifest()


def test_frozen_catalog_roundtrip_preserves_historical_contract_without_current_roster_or_io(monkeypatch):
    catalog, registry = _frozen_catalog()
    from pathlib import Path
    import requests
    import trading_system.universe as universe
    def denied(*_args, **_kwargs):
        raise AssertionError("Frozen contract reconstruction attempted network, directories or adapters")
    monkeypatch.setattr(requests.sessions.Session, "request", denied)
    monkeypatch.setattr(Path, "mkdir", denied)
    monkeypatch.setattr(SourceRegistry, "build_adapters", denied)
    monkeypatch.setattr(universe, "EXTENDED_UNIVERSE", {"TEST_FIXTURE_CURRENT_ROSTER": {}})
    restored = SourceCatalog.from_manifest(catalog, registry)
    assert restored.manifest() == catalog
    assert restored.registry.public_manifest() == registry
    assert "us:TEST_FIXTURE_CURRENT_ROSTER" not in restored.entities
    assert restored.sources["TEST_FIXTURE-original"]["entitlement"]["valid_until"] == "2020-01-01T00:00:00Z"
    assert restored.entities["person:fixture"]["role_history"][0]["role"] == "TEST_FIXTURE Analyst"


@pytest.mark.parametrize("fault", ["catalog_hash", "registry_hash", "catalog_schema", "registry_schema",
    "catalog_field", "registry_field", "source_field", "source_id", "source_version", "dimensions",
    "registry_binding", "allowed_uses", "inverted_role"])
def test_frozen_catalog_reconstruction_rejects_invalid_hashes_fields_and_contracts(fault):
    catalog, registry = map(deepcopy, _frozen_catalog())
    if fault == "catalog_hash":
        catalog["sha256"] = "0" * 64
    elif fault == "registry_hash":
        registry["sha256"] = "0" * 64
    elif fault == "catalog_schema":
        catalog["schema"] = "TEST_FIXTURE-unknown-catalog/v99"
    elif fault == "registry_schema":
        registry["schema"] = "TEST_FIXTURE-unknown-registry/v99"
    elif fault == "catalog_field":
        catalog["TEST_FIXTURE_unknown_field"] = True
    elif fault == "registry_field":
        registry["TEST_FIXTURE_unknown_field"] = True
    elif fault == "source_field":
        catalog["sources"]["TEST_FIXTURE-original"]["TEST_FIXTURE_unknown_field"] = True
    elif fault == "source_id":
        catalog["sources"]["TEST_FIXTURE-original"]["source_id"] = "TEST_FIXTURE-other"
    elif fault == "source_version":
        catalog["sources"]["TEST_FIXTURE-original"]["version"] = "999"
    elif fault == "dimensions":
        catalog["dimensions"]["news_events"]["window_days"] = 999
    elif fault == "registry_binding":
        catalog["source_registry_sha256"] = "0" * 64
    elif fault == "allowed_uses":
        catalog["sources"]["TEST_FIXTURE-original"]["allowed_uses"] = ["trade"]
    else:
        catalog["entities"]["person:fixture"]["role_history"][0]["valid_until"] = "2000-01-01T00:00:00Z"
    # Coherent hash changes still cannot invent fields, relax fixed dimensions,
    # mismatch registries or bypass the normal source/identity validators.
    if fault != "catalog_hash":
        catalog["sha256"] = digest({key: value for key, value in catalog.items() if key != "sha256"})
    with pytest.raises(IntelligenceError) as error:
        SourceCatalog.from_manifest(catalog, registry)
    assert error.value.code == "HASH_MISMATCH"
