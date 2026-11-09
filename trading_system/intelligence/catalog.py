"""Trusted material, publisher and identity contracts for topic intelligence.

These are acquisition/review policies, not claims that a source is connected or
that a document qualifies for a trading action. Values never come from a page.
"""
from __future__ import annotations

import os
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone

from .contracts import IntelligenceError, aware, digest, identifier, safe_value, utc_now
from .registry import SourceRegistry, default_registry


CATALOG_SCHEMA = "tiger.intelligence-catalog/v1"
_SPECS = (
    ("issuer_disclosure", "Company statutory disclosures", 300, 7, 760, "publication_or_amendment"),
    ("issuer_ir", "Company operations and investor relations", 900, 30, 760, "guidance_or_revision"),
    ("formal_research", "Entitled investment research", 3600, 30, 365, "report_version_or_withdrawal"),
    ("institutional_commentary", "Institutional strategy and public commentary", 900, 7, 365, "view_horizon_or_revision"),
    ("industry_data", "Industry supply, demand, price and inventory", 86400, 730, 730, "expected_next_release"),
    ("macro_policy", "Macroeconomic releases and policy", 900, 30, 730, "release_vintage_or_effective_date"),
    ("market_structure", "Price, volatility and market structure", 300, 7, 420, "native_market_session"),
    ("capital_positions", "Capital flows, disclosed holdings and expectations", 3600, 90, 730, "disclosure_period"),
    ("person_statement", "Identified people and expert original statements", 900, 7, 365, "original_quote_and_role_date"),
    ("competition_technology", "Competition, technology and alternative routes", 3600, 30, 730, "new_evidence_or_revision"),
    ("news_events", "Original news, geopolitical and sudden events", 300, 7, 30, "correction_or_counterevidence"),
    ("alternative_signals", "Authorized channel observations and sentiment", 3600, 7, 90, "sample_window_and_bias"),
)
DIMENSIONS = {key: {"id": key, "label": label, "cadence_seconds": cadence,
    "window_days": window, "baseline_days": baseline, "review_trigger": trigger,
    "counterevidence_required": True} for key, label, cadence, window, baseline, trigger in _SPECS}
_SOURCE_MATERIALS = {
    "sec_directory": (), "sec_current": ("issuer_disclosure",),
    "sec_submissions": ("issuer_disclosure", "capital_positions"),
    "fed_policy": ("macro_policy", "person_statement"), "bls_calendar": ("macro_policy",),
    "fred": ("macro_policy", "industry_data"), "cboe_vix": ("market_structure",),
}
_ENTITY_TYPES = {"security", "issuer", "person", "institution", "industry", "product", "region"}
_STATEMENT_BASES = {"original_statement", "original_transcript", "original_publication",
    "secondary_report", "unknown"}
_ORIGINAL_STATEMENT_BASES = _STATEMENT_BASES - {"secondary_report", "unknown"}
_SOURCE_FIELDS = {"material_types", "publisher", "business_unit", "author_ids", "entitlement",
    "allowed_uses", "cadence_seconds", "expected_release_seconds", "priority", "maximum_requests_per_day",
    "person_statement_basis"}


def dimensions():
    return deepcopy(DIMENSIONS)


def material_policy(policy=None, *, has_entities=False):
    if policy is not None and (not isinstance(policy, dict) or set(policy) - DIMENSIONS.keys()):
        raise IntelligenceError("CONTRACT_INVALID", "Unknown material dimension")
    result = {}
    for key in DIMENSIONS:
        default = "required" if key == "news_events" or has_entities and key == "issuer_disclosure" else "supplemental"
        value = (policy or {}).get(key, default)
        if isinstance(value, str):
            value = {"status": value, "reason": "Selected research requirement" if policy and key in policy else "Question-scope default"}
        if (not isinstance(value, dict) or set(value) - {"status", "reason"}
                or value.get("status") not in {"required", "supplemental", "not_applicable"}
                or not isinstance(value.get("reason"), str) or not value["reason"].strip()
                or len(value["reason"]) > 1000):
            raise IntelligenceError("CONTRACT_INVALID", "Material policy requires a status and reason")
        if value["status"] == "not_applicable" and isinstance((policy or {}).get(key), str):
            raise IntelligenceError("CONTRACT_INVALID", "Not-applicable dimensions require an explicit scope reason")
        result[key] = dict(value)
    if not any(row["status"] == "required" for row in result.values()):
        raise IntelligenceError("CONTRACT_INVALID", "A topic must have at least one required dimension")
    return result


class SourceCatalog:
    """A trusted-startup overlay; source IDs still resolve through SourceRegistry."""

    def __init__(self, registry=None, config=None, *, reviewed_entities=None):
        self.registry = registry or default_registry()
        config = {} if config is None else config
        if (not isinstance(config, dict) or set(config) - {"schema", "version", "sources", "entities"}
                or safe_value(config) != config):
            raise IntelligenceError("POLICY_DENIED", "Catalog must be a credential-free trusted configuration")
        if config.get("schema", CATALOG_SCHEMA) != CATALOG_SCHEMA:
            raise IntelligenceError("CONTRACT_INVALID", "Unknown source catalog schema")
        self.version = identifier(config.get("version", "2026-11-06.1"), "catalog version")
        if reviewed_entities is None:
            from ..universe import EXTENDED_UNIVERSE
            reviewed_entities = {"us:" + symbol: {"type": "security", "name": symbol,
                "identifiers": {"ticker": [symbol]}, "review_basis": "versioned_local_research_roster"}
                for symbol in EXTENDED_UNIVERSE}
        if not isinstance(reviewed_entities, dict) or not isinstance(config.get("entities", {}), dict):
            raise IntelligenceError("CONTRACT_INVALID", "Reviewed entities must be a mapping")
        self.entities = deepcopy(reviewed_entities)
        self.entities.update(deepcopy(config.get("entities", {})))
        for key, row in self.entities.items():
            identifier(key, "reviewed entity")
            if (not isinstance(row, dict) or row.get("type") not in _ENTITY_TYPES
                    or not isinstance(row.get("name"), str) or not row["name"].strip()
                    or safe_value(row) != row):
                raise IntelligenceError("CONTRACT_INVALID", "Invalid reviewed entity identity")
            if row["type"] == "person" and not isinstance(row.get("role_history", []), list):
                raise IntelligenceError("CONTRACT_INVALID", "Person roles must retain their validity intervals")
        for row in self.entities.values():
            for role in row.get("role_history", []):
                if (not isinstance(role, dict) or not isinstance(role.get("institution_id"), str)
                        or role["institution_id"] not in self.entities
                        or self.entities[role["institution_id"]]["type"] != "institution"
                        or not isinstance(role.get("role"), str) or not role["role"].strip()):
                    raise IntelligenceError("CONTRACT_INVALID", "Person role needs an institution and role")
                for field in ("valid_from", "valid_until"):
                    if role.get(field) is not None:
                        aware(role[field])
                if role.get("valid_from") and role.get("valid_until") and aware(role["valid_from"]) >= aware(role["valid_until"]):
                    raise IntelligenceError("CONTRACT_INVALID", "Person role has an inverted validity interval")
                if "current_affiliation_verified" in role and type(role["current_affiliation_verified"]) is not bool:
                    raise IntelligenceError("CONTRACT_INVALID", "Current affiliation verification must be explicit")
                if role.get("observed_in_source_on") is not None:
                    try:
                        observed = date.fromisoformat(role["observed_in_source_on"])
                        if observed.isoformat() != role["observed_in_source_on"] or not role.get("source_id"):
                            raise ValueError
                        self.registry.get(role["source_id"], require_enabled=False)
                        if (role.get("valid_from") and observed < aware(role["valid_from"]).date()
                                or role.get("valid_until") and observed >= aware(role["valid_until"]).date()):
                            raise ValueError
                    except (TypeError, ValueError):
                        raise IntelligenceError("CONTRACT_INVALID", "Historical role needs an exact source and calendar date") from None
        overrides = config.get("sources", {})
        if not isinstance(overrides, dict) or set(overrides) - set(self.registry.source_ids(enabled_only=False)):
            raise IntelligenceError("POLICY_DENIED", "Catalog cannot register unknown network sources")
        self.sources = {}
        fields = _SOURCE_FIELDS
        for source_id in self.registry.source_ids(enabled_only=False):
            spec = self.registry.get(source_id, require_enabled=False)
            source_materials = _SOURCE_MATERIALS.get(source_id,
                ("issuer_ir",) if spec.kind == "issuer_rss" else tuple(DIMENSIONS) if spec.kind == "search" else ())
            source_materials = spec.options.get("material_types", source_materials)
            row = {"source_id": source_id, "version": spec.version, "material_types": list(source_materials),
                "publisher": None, "business_unit": None, "author_ids": [],
                "entitlement": {"kind": "public"}, "allowed_uses": ["discovery", "topic_analysis"],
                "cadence_seconds": min((DIMENSIONS[key]["cadence_seconds"] for key in source_materials), default=3600),
                "priority": 20 if spec.kind == "search" else 10, "maximum_requests_per_day": 5000,
                "expected_release_seconds": None, "person_statement_basis": "unknown"}
            for name in fields.intersection(spec.options):
                row[name] = deepcopy(spec.options[name])
            override = overrides.get(source_id, {})
            if not isinstance(override, dict) or set(override) - fields:
                raise IntelligenceError("CONTRACT_INVALID", "Unknown source catalog field")
            row.update(deepcopy(override))
            if (not isinstance(row["material_types"], list) or any(not isinstance(value, str) for value in row["material_types"])
                    or set(row["material_types"]) - DIMENSIONS.keys()
                    or len(set(row["material_types"])) != len(row["material_types"])
                    or type(row["cadence_seconds"]) is not int or not 60 <= row["cadence_seconds"] <= 604800
                    or type(row["priority"]) is not int or not 0 <= row["priority"] <= 100
                    or type(row["maximum_requests_per_day"]) is not int or row["maximum_requests_per_day"] < 1
                    or not isinstance(row["author_ids"], list)
                    or any(not isinstance(author, str) or author not in self.entities or self.entities[author]["type"] != "person" for author in row["author_ids"])
                    or len(set(row["author_ids"])) != len(row["author_ids"])
                    or not isinstance(row["person_statement_basis"], str)
                    or row["person_statement_basis"] not in _STATEMENT_BASES
                    or row["person_statement_basis"] in _ORIGINAL_STATEMENT_BASES and spec.kind not in {"page", "feed", "issuer_rss"}
                    or not isinstance(row["allowed_uses"], list)
                    or any(not isinstance(value, str) for value in row["allowed_uses"])
                    or set(row["allowed_uses"]) - {"discovery", "topic_analysis", "historical_baseline"}):
                raise IntelligenceError("CONTRACT_INVALID", "Invalid material/cadence/author contract")
            rights = row["entitlement"]
            if (not isinstance(rights, dict) or set(rights) - {"kind", "reference", "valid_until"}
                    or rights.get("kind") not in {"public", "licensed", "unavailable"}
                    or rights["kind"] == "licensed" and not rights.get("reference")):
                raise IntelligenceError("CONTRACT_INVALID", "Invalid source entitlement reference")
            if rights.get("valid_until"):
                aware(rights["valid_until"])
            self.sources[source_id] = row

    def dimensions(self):
        return dimensions()

    def manifest(self):
        value = {"schema": CATALOG_SCHEMA, "version": self.version, "dimensions": dimensions(),
            "sources": deepcopy(self.sources), "entities": deepcopy(self.entities),
            "source_registry_sha256": self.registry.public_manifest()["sha256"]}
        return {**value, "sha256": digest(value)}

    @classmethod
    def from_manifest(cls, manifest, registry_manifest):
        """Rebuild an admitted catalog from exact credential-free data only.

        Neither the current roster nor startup overrides may supplement this
        historical contract. Regeneration validates every field and both hashes;
        a version/field/schema that this release cannot reproduce fails closed.
        This factory does not build adapters, inspect credentials or perform I/O.
        """
        try:
            fields = {"schema", "version", "dimensions", "sources", "entities",
                "source_registry_sha256", "sha256"}
            if (not isinstance(manifest, dict) or set(manifest) != fields
                    or manifest["schema"] != CATALOG_SCHEMA
                    or not isinstance(manifest["sources"], dict)
                    or not isinstance(manifest["entities"], dict)
                    or safe_value(manifest) != manifest
                    or digest({key: value for key, value in manifest.items() if key != "sha256"}) != manifest["sha256"]):
                raise ValueError
            registry = SourceRegistry.from_manifest(registry_manifest)
            if (manifest["source_registry_sha256"] != registry.public_manifest()["sha256"]
                    or set(manifest["sources"]) != set(registry.source_ids(enabled_only=False))):
                raise ValueError
            overrides = {}
            for source_id, row in manifest["sources"].items():
                if (not isinstance(row, dict) or set(row) != _SOURCE_FIELDS | {"source_id", "version"}
                        or row["source_id"] != source_id or row["version"] != registry.get(source_id, require_enabled=False).version):
                    raise ValueError
                overrides[source_id] = {key: row[key] for key in _SOURCE_FIELDS}
            catalog = cls(registry, {"schema": manifest["schema"], "version": manifest["version"],
                "sources": overrides}, reviewed_entities=manifest["entities"])
            if catalog.manifest() != manifest:
                raise ValueError
            return catalog
        except (IntelligenceError, KeyError, TypeError, ValueError, AttributeError, OverflowError):
            raise IntelligenceError("HASH_MISMATCH", "Frozen source catalog or registry is invalid") from None

    def validate_entities(self, entity_ids):
        if (not isinstance(entity_ids, (tuple, list)) or len(entity_ids) > 50
                or any(not isinstance(value, str) for value in entity_ids)
                or len(set(entity_ids)) != len(entity_ids)):
            raise IntelligenceError("CONTRACT_INVALID", "Topic entities must be a unique bounded list")
        if set(entity_ids) - self.entities.keys():
            raise IntelligenceError("POLICY_DENIED", "Topic entity is outside the trusted reviewed catalog")
        return list(entity_ids)

    def eligible_sources(self, materials, *, now=None):
        requested = set(materials)
        if requested - DIMENSIONS.keys():
            raise IntelligenceError("CONTRACT_INVALID", "Unknown material dimension")
        return [key for key, row in self.sources.items()
            if requested.intersection(row["material_types"]) and self.connection(key, now=now)["enabled"]
            and self.connection(key, now=now)["entitlement_ready"] and self.connection(key, now=now)["allowed_uses_ready"]]

    def person_statement_evidence(self, entity_id, document):
        """Qualify an original speaker identity and its role at publication.

        A same-name mention, requested subject or saved semantic label cannot
        establish this binding. Historical episode attribution stays historical;
        it never asserts the person's current employment or verifies meaning.
        """
        def result(qualified, reason, **extra):
            return {"qualified": qualified, "reason": reason,
                "current_affiliation_verified": False, **extra}

        person = self.entities.get(entity_id, {})
        if person.get("type") != "person" or not isinstance(document, dict):
            return result(False, "PERSON_IDENTITY_UNVERIFIED")
        source_id = document.get("source_id")
        source = self.sources.get(source_id, {})
        if entity_id not in source.get("author_ids", []):
            return result(False, "PERSON_IDENTITY_UNVERIFIED")
        name = " ".join(person["name"].casefold().split())
        same_name_authors = [author for author in source["author_ids"]
            if " ".join(self.entities[author]["name"].casefold().split()) == name]
        if len(same_name_authors) != 1:
            return result(False, "PERSON_IDENTITY_AMBIGUOUS")
        text = document.get("text")
        if not isinstance(text, str) or name not in " ".join(text.casefold().split()):
            return result(False, "PERSON_IDENTITY_UNVERIFIED")
        if (source.get("person_statement_basis") not in _ORIGINAL_STATEMENT_BASES
                or "person_statement" not in source.get("material_types", ())):
            return result(False, "PERSON_ORIGINAL_STATEMENT_UNVERIFIED")
        spec = self.registry.get(source_id, require_enabled=False)
        recipe = document.get("normalization", {})
        if (document.get("source_version") != spec.version or not isinstance(recipe, dict)
                or recipe.get("source_id") != source_id
                or recipe.get("source_contract_sha256") != digest(spec.public())):
            return result(False, "PERSON_SOURCE_BINDING_UNVERIFIED")
        times = document.get("times", {})
        published = times.get("published", {}) if isinstance(times, dict) else {}
        if not isinstance(published, dict):
            return result(False, "PERSON_STATEMENT_TIME_UNVERIFIED")
        instant = lower = upper = calendar_day = None
        calendar_basis = None
        try:
            if published.get("precision") == "instant" and published.get("value"):
                instant = aware(published["value"])
                lower = upper = instant
                calendar_day = instant.date()
                calendar_basis = "published_instant_utc_date"
            elif published.get("precision") == "date" and published.get("value"):
                calendar_day = date.fromisoformat(published["value"])
                if calendar_day.isoformat() != published["value"]:
                    raise ValueError
                day = datetime.combine(calendar_day, time(), timezone.utc)
                # Date-only publication has no invented timezone. This wide
                # envelope must fit the complete role interval at a boundary.
                lower, upper = day - timedelta(days=1), day + timedelta(days=2)
                calendar_basis = "published_calendar_date_unknown_timezone"
            elif published.get("precision") == "unknown" and isinstance(published.get("raw"), str):
                raw = datetime.fromisoformat(published["raw"])
                if raw.utcoffset() is None:
                    calendar_day = raw.date()
                    calendar_basis = "original_local_calendar_date_only"
        except (TypeError, ValueError, OverflowError, IntelligenceError):
            return result(False, "PERSON_STATEMENT_TIME_UNVERIFIED")
        if calendar_day is None:
            return result(False, "PERSON_STATEMENT_TIME_UNVERIFIED")
        reasons = []
        for role in person.get("role_history", []):
            # A source-scoped role observation qualifies only that episode's
            # original local date, without changing the saved publication time.
            if (role.get("source_id") == source_id
                    and role.get("observed_in_source_on") == calendar_day.isoformat()):
                return result(True, "PERSON_HISTORICAL_ROLE_SOURCE_ATTRIBUTED", role=deepcopy(role),
                    publication=deepcopy(published), role_date_basis=calendar_basis)
            if lower is None or upper is None or not role.get("valid_from") or not role.get("valid_until"):
                reasons.append("PERSON_ROLE_TIME_UNVERIFIED")
                continue
            starts, ends = aware(role["valid_from"]), aware(role["valid_until"])
            if starts <= lower and (upper < ends if instant is not None else upper <= ends):
                return result(True, "PERSON_ROLE_AT_PUBLICATION_QUALIFIED", role=deepcopy(role),
                    publication=deepcopy(published), role_date_basis=calendar_basis)
            reasons.append("PERSON_ROLE_OUTSIDE_VALIDITY")
        return result(False, "PERSON_ROLE_OUTSIDE_VALIDITY" if "PERSON_ROLE_OUTSIDE_VALIDITY" in reasons
            else "PERSON_ROLE_TIME_UNVERIFIED", publication=deepcopy(published), role_date_basis=calendar_basis)

    def connection(self, source_id, *, environment=None, now=None):
        spec = self.registry.get(source_id, require_enabled=False)
        row = self.sources[source_id]
        env = os.environ if environment is None else environment
        credential = bool(env.get(spec.credential_env)) if spec.credential_env else True
        if any(host in {"sec.gov", "www.sec.gov", "data.sec.gov"} for host in spec.allowed_hosts):
            credential = credential and bool(env.get("TIGER_INTEL_SEC_USER_AGENT"))
        rights = row["entitlement"]
        entitlement = rights["kind"] != "unavailable"
        if rights.get("valid_until"):
            entitlement = entitlement and aware(rights["valid_until"]) > aware(now or utc_now())
        allowed = bool(set(row["allowed_uses"]).intersection({"discovery", "topic_analysis", "historical_baseline"}))
        return {"source_id": source_id, "implemented": True, "registered": True,
            "enabled": spec.enabled, "credential_ready": credential, "entitlement_ready": entitlement,
            "allowed_uses_ready": allowed,
            "connected": False, "last_success": None,
            "reason": "SOURCE_DISABLED" if not spec.enabled else "ENTITLEMENT_REQUIRED" if not entitlement
                else "SOURCE_AUTH_REQUIRED" if not credential else "SOURCE_USE_DENIED" if not allowed else "NOT_YET_OBSERVED"}
