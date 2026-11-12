"""Trusted source capabilities; a web page cannot register itself as authoritative."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
import re
from urllib.parse import urlsplit

from .contracts import IntelligenceError, canonical, credential_free_url, digest, identifier, safe_value


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    kind: str
    endpoint: str
    version: str = "1"
    enabled: bool = True
    allowed_hosts: tuple[str, ...] = ()
    authority_scopes: tuple[str, ...] = ("discovery",)
    capabilities: tuple[str, ...] = ("search", "fetch")
    date_semantics: str = "provider_estimate"
    credential_env: str | None = None
    minimum_interval_seconds: float = 0.25
    max_retries: int = 1
    options: dict = field(default_factory=dict)

    def __post_init__(self):
        identifier(self.source_id, "source id")
        identifier(self.version, "source version")
        if (self.kind not in {"search", "web_fetch", "page", "feed", "issuer_rss", "sec_directory", "sec_submissions", "fred", "cboe_csv", "bls_calendar"}
                or type(self.enabled) is not bool or self.minimum_interval_seconds < 0
                or type(self.max_retries) is not int or not 0 <= self.max_retries <= 5):
            raise IntelligenceError("CONTRACT_INVALID", "Invalid source policy")
        if self.endpoint and not credential_free_url(self.endpoint, endpoint=True):
            raise IntelligenceError("CONTRACT_INVALID", "Invalid registered source endpoint")
        if self.credential_env is not None and (not isinstance(self.credential_env, str)
                or re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", self.credential_env) is None):
            raise IntelligenceError("CONTRACT_INVALID", "Credential references must be environment variable names")
        if safe_value(self.options) != self.options:
            raise IntelligenceError("POLICY_DENIED", "Secrets must be resolved from trusted environment")
        if any(x not in {"discovery", "sec_filing", "issuer_ir", "official_statistics", "official_policy"} for x in self.authority_scopes):
            raise IntelligenceError("CONTRACT_INVALID", "Unknown authority scope")
        canonical(asdict(self))

    def public(self) -> dict:
        value = asdict(self)
        for key in ("allowed_hosts", "authority_scopes", "capabilities"):
            value[key] = list(value[key])
        return value


class SourceRegistry:
    def __init__(self, sources, *, policy_version: str = "intelligence/v1"):
        if isinstance(sources, dict):
            sources = [SourceSpec(source_id=key, **{k: v for k, v in item.items() if k != "source_id"})
                       if isinstance(item, dict) else item for key, item in sources.items()]
        values = list(sources)
        if not values or any(not isinstance(x, SourceSpec) for x in values) or len({x.source_id for x in values}) != len(values):
            raise IntelligenceError("CONTRACT_INVALID", "Source ids must be unique registered contracts")
        self.sources = {x.source_id: x for x in values}
        self.policy_version = identifier(policy_version, "source policy version")

    def get(self, source_id: str, *, require_enabled=True) -> SourceSpec:
        spec = self.sources.get(source_id)
        if spec is None or require_enabled and not spec.enabled:
            raise IntelligenceError("POLICY_DENIED", "Source is not enabled by trusted policy")
        return spec

    def source_ids(self, *, enabled_only=True):
        return [key for key, spec in self.sources.items() if not enabled_only or spec.enabled]

    def public_manifest(self):
        result = {"schema": "tiger.source-registry/v1", "policy_version": self.policy_version,
                  "sources": {key: spec.public() for key, spec in sorted(self.sources.items())}}
        return {**result, "sha256": digest(result)}

    @classmethod
    def from_manifest(cls, manifest):
        if not isinstance(manifest, dict) or manifest.get("schema") != "tiger.source-registry/v1":
            raise IntelligenceError("HASH_MISMATCH", "Invalid frozen source registry")
        registry = cls(manifest.get("sources", {}), policy_version=manifest.get("policy_version", ""))
        if registry.public_manifest() != manifest:
            raise IntelligenceError("HASH_MISMATCH", "Frozen source registry differs from its canonical hash")
        return registry

    @classmethod
    def verify_manifest(cls, manifest):
        return cls.from_manifest(manifest).public_manifest()

    def build_adapters(self, *, deepseek_client=None, transport=None, environment=None, parser_policy=None):
        from .adapters import BraveAdapter, DeepSeekAdapter, DeepSeekBridge, ExaAdapter, TavilyAdapter, WebFetch
        from .direct import (BlsCalendarSource, CboeSource, FeedSource, FredSource,
                             RegisteredPageSource, SecDirectorySource, SecSubmissionsSource)
        from .transport import HttpTransport

        transport = transport or HttpTransport()
        env = os.environ if environment is None else environment
        adapters = {}
        for source_id, spec in self.sources.items():
            if not spec.enabled:
                continue
            credential = env.get(spec.credential_env, "") if spec.credential_env else None
            if spec.credential_env and spec.kind not in {"search", "page", "feed", "issuer_rss", "fred"}:
                raise IntelligenceError("POLICY_DENIED", "This source adapter has no credential transport contract")
            if spec.kind == "search":
                if source_id == "deepseek":
                    adapter = DeepSeekBridge(deepseek_client) if deepseek_client is not None else DeepSeekAdapter(transport, credential, endpoint=spec.endpoint)
                else:
                    cls = {"brave": BraveAdapter, "tavily": TavilyAdapter, "exa": ExaAdapter}.get(source_id)
                    if cls is None:
                        raise IntelligenceError("POLICY_DENIED", "Unknown search adapter")
                    adapter = cls(transport, credential, endpoint=spec.endpoint)
            elif spec.kind == "web_fetch":
                adapter = WebFetch(transport)
            elif spec.kind == "page":
                adapter = RegisteredPageSource(spec, transport, credential=credential,
                    user_agent=env.get("TIGER_INTEL_SEC_USER_AGENT"))
            elif spec.kind == "fred":
                adapter = FredSource(spec, transport, credential=credential)
            elif spec.kind == "sec_submissions":
                adapter = SecSubmissionsSource(spec, transport, user_agent=env.get("TIGER_INTEL_SEC_USER_AGENT"))
            elif spec.kind == "sec_directory":
                adapter = SecDirectorySource(spec, transport, user_agent=env.get("TIGER_INTEL_SEC_USER_AGENT"))
            elif spec.kind == "cboe_csv":
                adapter = CboeSource(spec, transport)
            elif spec.kind == "bls_calendar":
                adapter = BlsCalendarSource(spec, transport)
            else:
                adapter = FeedSource(spec, transport, credential=credential,
                    user_agent=env.get("TIGER_INTEL_SEC_USER_AGENT") if source_id.startswith("sec_") else None)
            chosen_parser = spec.options.get("extractor", parser_policy)
            if chosen_parser is not None and spec.kind in {"web_fetch", "page", "feed", "issuer_rss", "sec_submissions", "sec_directory"}:
                from .parse_limits import ParsePolicy
                adapter.parse_policy = ParsePolicy.from_value(chosen_parser)
            adapters[source_id] = adapter
        return adapters


def acquisition_mode(config: dict | None = None) -> str:
    """A trusted startup choice; it does not alter source permissions."""
    if config is None:
        return "live"
    if not isinstance(config, dict):
        raise IntelligenceError("CONTRACT_INVALID", "Trusted acquisition configuration must be an object")
    mode = config.get("acquisition_mode", "live")
    if not isinstance(mode, str) or mode not in {"live", "snapshot_only"}:
        raise IntelligenceError("CONTRACT_INVALID", "Unknown trusted acquisition mode")
    return mode


def default_registry(config: dict | None = None) -> SourceRegistry:
    """Only trusted startup config can enable providers, endpoints or IR identities."""
    acquisition_mode(config)
    config = config or {}
    definitions = {
        "web_page": dict(kind="web_fetch", endpoint="", capabilities=("fetch", "attachments"),
                         date_semantics="observed_page_metadata"),
        "deepseek": dict(kind="search", endpoint="https://api.deepseek.com/v1/tools/search",
                         credential_env="LLM_API_KEY", allowed_hosts=("api.deepseek.com",)),
        "brave": dict(kind="search", endpoint="https://api.search.brave.com/res/v1/news/search", enabled=False,
                      credential_env="TIGER_INTEL_BRAVE_API_KEY", allowed_hosts=("api.search.brave.com",)),
        "tavily": dict(kind="search", endpoint="https://api.tavily.com/search", enabled=False,
                       credential_env="TIGER_INTEL_TAVILY_API_KEY", allowed_hosts=("api.tavily.com",)),
        "exa": dict(kind="search", endpoint="https://api.exa.ai/search", enabled=False,
                    credential_env="TIGER_INTEL_EXA_API_KEY", allowed_hosts=("api.exa.ai",)),
        "sec_directory": dict(kind="sec_directory", endpoint="https://www.sec.gov/files/company_tickers.json",
                              allowed_hosts=("www.sec.gov", "sec.gov"), capabilities=("discover", "identity"),
                              date_semantics="observed_directory"),
        "sec_current": dict(kind="feed", endpoint="https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&owner=exclude&count=100&output=atom",
                            allowed_hosts=("www.sec.gov", "sec.gov"), authority_scopes=("sec_filing",),
                            capabilities=("discover", "fetch", "attachments"), date_semantics="filing_feed"),
        "sec_submissions": dict(kind="sec_submissions", endpoint="https://data.sec.gov/submissions/",
                                allowed_hosts=("data.sec.gov", "www.sec.gov", "sec.gov"), authority_scopes=("sec_filing",),
                                capabilities=("discover", "fetch", "attachments"), date_semantics="accepted_at"),
        "fed_policy": dict(kind="feed", endpoint="https://www.federalreserve.gov/feeds/press_monetary.xml",
                           allowed_hosts=("www.federalreserve.gov", "federalreserve.gov"),
                           authority_scopes=("official_policy",), capabilities=("discover", "fetch", "attachments"), date_semantics="official_feed"),
        "bls_calendar": dict(kind="bls_calendar", endpoint="https://www.bls.gov/schedule/news_release/bls.ics",
                             allowed_hosts=("www.bls.gov", "bls.gov"), authority_scopes=("official_policy",),
                             capabilities=("discover", "calendar"), date_semantics="announced_release_time"),
        "fred": dict(kind="fred", endpoint="https://api.stlouisfed.org/fred/series/observations",
                     allowed_hosts=("api.stlouisfed.org", "fred.stlouisfed.org"), authority_scopes=("official_statistics",),
                     credential_env="TIGER_INTEL_FRED_API_KEY", capabilities=("discover", "data", "vintage"),
                     date_semantics="day_vintage", options={"series_id": "FEDFUNDS"}),
        "cboe_vix": dict(kind="cboe_csv", endpoint="https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
                         allowed_hosts=("cdn.cboe.com", "www.cboe.com", "cboe.com"), authority_scopes=("official_statistics",),
                         capabilities=("discover", "data"), date_semantics="daily_observation", options={"series_id": "VIX"}),
    }
    overrides = config.get("sources", {})
    if not isinstance(overrides, dict) or set(overrides) - set(definitions):
        raise IntelligenceError("POLICY_DENIED", "Unknown source override")
    specs = [SourceSpec(source_id=key, **{**value, **overrides.get(key, {})}) for key, value in definitions.items()]
    for ir in config.get("ir_feeds", []):
        if not isinstance(ir, dict) or not ir.get("issuer_id") or not ir.get("url"):
            raise IntelligenceError("CONTRACT_INVALID", "IR feed needs registered issuer and URL")
        host = urlsplit(ir["url"]).hostname
        specs.append(SourceSpec(source_id=ir.get("source_id", "ir_" + ir["issuer_id"].replace(":", "_")),
            kind="issuer_rss", endpoint=ir["url"], allowed_hosts=tuple(ir.get("allowed_hosts", [host])),
            authority_scopes=("issuer_ir",), capabilities=("discover", "fetch", "attachments"),
            date_semantics="issuer_feed", options={**ir.get("options", {}), "issuer_id": ir["issuer_id"]}))
    registered = config.get("registered_sources", [])
    if not isinstance(registered, list):
        raise IntelligenceError("CONTRACT_INVALID", "Trusted registered sources must be a list")
    for value in registered:
        if not isinstance(value, dict) or value.get("kind") == "search":
            raise IntelligenceError("POLICY_DENIED", "Additional sources cannot replace a model/search adapter")
        spec = SourceSpec(**value)
        if not spec.endpoint or not spec.allowed_hosts:
            raise IntelligenceError("POLICY_DENIED", "Additional sources need an explicit origin allowlist")
        specs.append(spec)
    return SourceRegistry(specs, policy_version=config.get("policy_version", "intelligence/v1"))
