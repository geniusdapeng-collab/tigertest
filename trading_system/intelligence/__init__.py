"""Auditable intelligence acquisition, separate from model and trade authority."""
from .replay import verify_bundle, replay_observation, reconstruct_document
from .contracts import (FetchResult, IntelligenceError, SearchRequest, SearchResult,
                        SourceError, canonical, digest, source_time)
from .planner import build_plan
from .registry import SourceRegistry, SourceSpec, default_registry
from .service import IntelligenceService, project_to_rawdocuments

__all__ = ["IntelligenceService", "SourceRegistry", "SourceSpec", "default_registry",
           "build_plan", "project_to_rawdocuments", "verify_bundle", "IntelligenceError",
           "SourceError", "SearchRequest", "SearchResult", "FetchResult", "canonical",
           "digest", "source_time", "replay_observation", "reconstruct_document"]
