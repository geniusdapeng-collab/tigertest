"""AM01/AM25: real loopback acquisition and topic transport, synthetic content.

Every source, identity and model response here is TEST_FIXTURE. The tests prove
binding, attribution gates and fixed-report mechanics, not production financial
facts, provider billing, or a real model's ability to identify a person.
"""
from copy import deepcopy
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest
import requests

from trading_system.intelligence.catalog import SourceCatalog
from trading_system.intelligence.contracts import IntelligenceError, digest, source_time, utc_now
from trading_system.intelligence.registry import SourceRegistry, SourceSpec
from trading_system.intelligence.replay import reconstruct_document
from trading_system.intelligence.service import IntelligenceService
from trading_system.intelligence.topic import TopicResearch
from trading_system.intelligence.transport import HttpTransport


ISSUER = "issuer:TEST_FIXTURE-company"
INSTITUTION = "institution:TEST_FIXTURE-bank"
PERSON = "person:TEST_FIXTURE-alex-bank"
OTHER_PERSON = "person:TEST_FIXTURE-alex-university"
ENTITY_IDS = [ISSUER, INSTITUTION, PERSON]
MATERIALS = ["issuer_disclosure", "formal_research", "industry_data",
    "institutional_commentary", "person_statement"]


def _payload(wire):
    prefix = wire["messages"][1]["content"].split("\nJSON schema:\n")[0]
    return json.loads(prefix.rsplit("\n", 1)[1])


def _analysis(payload):
    conclusions = []
    for document in payload["documents"]:
        material = document["material_types"][0]
        # The returned text is a controlled model-response fixture. In negative
        # cases it deliberately attributes a valid quote to the wrong person;
        # the production deterministic identity gate must reject that coverage.
        for entity in ENTITY_IDS:
            conclusions.append({"id": "TEST_FIXTURE-" + material + "-" + entity,
                "text": "TEST_FIXTURE conditional evidence about " + entity,
                "kind": "opinion" if material in {"person_statement", "institutional_commentary", "formal_research"} else "fact",
                "dimension_id": material, "entity_id": entity,
                "supports": [{"document_id": document["id"], "quote": document["text"]}],
                "refutes": [], "conditions": "TEST_FIXTURE only; no investment or present-employment claim."})
    return {"summary": "TEST_FIXTURE source-bound three-entity five-material report.",
        "conclusions": conclusions, "unknowns": ["TEST_FIXTURE has no production model-quality evidence."],
        "changes": [], "next_review": "TEST_FIXTURE review on changed originals.", "followup_questions": []}


@pytest.fixture
def identity_flow(tmp_path, monkeypatch):
    state = {"get": [], "model": [], "routes": {}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["get"].append(self.path)
            status, body = state["routes"].get(self.path, (404, b"TEST_FIXTURE missing source"))
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            wire = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["model"].append(wire)
            parsed = (_analysis(_payload(wire)) if wire.get("reasoning_effort") == "high" else
                {"accepted": True, "issues": [], "summary": "TEST_FIXTURE original quotes and honest unknown coverage."})
            body = json.dumps({"id": "TEST_FIXTURE-response-" + str(len(state["model"])),
                "model": "deepseek-flash", "object": "chat.completion",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant",
                    "content": json.dumps(parsed), "reasoning_content": "TEST_FIXTURE private reasoning"}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:" + str(server.server_port)

    def route_model(url, **kwargs):
        assert url == "https://api.deepseek.com/chat/completions"
        return requests.api.post(base + "/model", **kwargs)

    monkeypatch.setattr(requests, "post", route_model)
    for key, value in {"LLM_BACKEND": "api", "LLM_BASE_URL": "https://api.deepseek.com",
            "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": "TEST_FIXTURE-only-credential"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TIGER_INTEL_REGISTRY_FILE", raising=False)

    def build(case="complete", *, missing=None):
        published = utc_now().replace(microsecond=0) - timedelta(days=1)
        publication_text = published.isoformat()
        roles = [{"institution_id": INSTITUTION, "role": "TEST_FIXTURE analyst",
            "valid_from": "2020-01-01T00:00:00+00:00", "valid_until": "2030-01-01T00:00:00+00:00"}]
        author_ids = [PERSON]
        basis = "original_transcript"
        if case == "same_name":
            author_ids = [OTHER_PERSON]
        elif case == "departed":
            roles[0]["valid_until"] = (published - timedelta(days=1)).isoformat()
        elif case == "secondary_report":
            basis = "secondary_report"
        elif case == "ambiguous_same_name_authors":
            author_ids = [PERSON, OTHER_PERSON]
        elif case == "unknown_role":
            roles = []
        elif case in {"historical_episode", "wrong_episode_date"}:
            publication_text = "2026-09-01T00:00:00"
            roles = [{"institution_id": INSTITUTION, "role": "TEST_FIXTURE episode speaker",
                "observed_in_source_on": "2026-09-01" if case == "historical_episode" else "2026-09-02",
                "source_id": "TEST_FIXTURE-person_statement", "current_affiliation_verified": False}]
        entities = {
            ISSUER: {"type": "issuer", "name": "TEST_FIXTURE Corporation"},
            INSTITUTION: {"type": "institution", "name": "TEST_FIXTURE Research Bank"},
            "institution:TEST_FIXTURE-university": {"type": "institution", "name": "TEST_FIXTURE University"},
            PERSON: {"type": "person", "name": "Alex Morgan", "role_history": roles},
            OTHER_PERSON: {"type": "person", "name": "Alex Morgan", "role_history": [{
                "institution_id": "institution:TEST_FIXTURE-university", "role": "TEST_FIXTURE professor",
                "valid_from": "2020-01-01T00:00:00+00:00", "valid_until": "2030-01-01T00:00:00+00:00"}]}}
        specs = []
        state["routes"].clear()
        for material in MATERIALS:
            source_id = "TEST_FIXTURE-" + material
            text = ("TEST_FIXTURE Corporation, TEST_FIXTURE Research Bank and Alex Morgan are all named "
                "in this controlled " + material + " original. The source records a conditional power-demand scenario "
                "and its explicit uncertainty; it does not describe an actual company, current employment or a trade.")
            if material == "person_statement" and case == "secondary_report":
                text = "TEST_FIXTURE Reporter paraphrases a secondhand story about Alex Morgan, TEST_FIXTURE Research Bank and TEST_FIXTURE Corporation; the original speech was not obtained."
            time_meta = ("" if material == "person_statement" and case == "unknown_publication" else
                '<meta property="article:published_time" content="' + publication_text + '">')
            html = ("<!doctype html><html><head>" + time_meta + "<title>TEST_FIXTURE " + material + "</title></head>"
                "<body><article><h1>TEST_FIXTURE " + material + "</h1><p>" + text + "</p></article></body></html>").encode()
            state["routes"]["/" + material] = (404, b"TEST_FIXTURE missing required material") if missing == material else (200, html)
            options = {"material_types": [material], "publisher": "TEST_FIXTURE publisher",
                "allowed_uses": ["topic_analysis", "historical_baseline"]}
            if material == "person_statement":
                options.update(author_ids=author_ids, person_statement_basis=basis)
            if material == "issuer_disclosure":
                options["issuer_id"] = ISSUER
            specs.append(SourceSpec(source_id, "page", base + "/" + material,
                version="TEST_FIXTURE-v1", allowed_hosts=("127.0.0.1",), minimum_interval_seconds=0,
                max_retries=0, authority_scopes=("issuer_ir",) if material == "issuer_disclosure" else ("discovery",),
                options=options))
        registry = SourceRegistry(specs, policy_version="TEST_FIXTURE-identity-v1")
        config = {"version": "TEST_FIXTURE-catalog-v1", "entities": entities,
            "sources": {"TEST_FIXTURE-formal_research": {"entitlement": {
                "kind": "licensed", "reference": "TEST_FIXTURE local licensed-report contract"}}}}
        catalog_path = tmp_path / (case + "-catalog.json")
        catalog_path.write_text(json.dumps(config))
        monkeypatch.setenv("TIGER_INTEL_CATALOG_FILE", str(catalog_path))

        def source_service(root, selected_registry):
            return IntelligenceService(root, selected_registry, selected_registry.build_adapters(
                transport=HttpTransport(allow_local=True), environment={}))

        service = TopicResearch(tmp_path / (case + "-state"), tmp_path / (case + "-archive"),
            mode="research", registry=registry, service_factory=source_service)
        policy = {key: {"status": "required" if key in MATERIALS else "not_applicable",
            "reason": "TEST_FIXTURE five related materials for the same three-entity question" if key in MATERIALS
                else "TEST_FIXTURE question does not ask for this other material"} for key in service.catalog.dimensions()}
        topic = service.create({"question": "TEST_FIXTURE compare company, bank and named analyst power-demand scenario originals",
            "entity_ids": ENTITY_IDS, "material_policy": policy,
            "horizon": "baseline" if case in {"historical_episode", "wrong_episode_date"} else "current",
            "idempotency_key": "TEST_FIXTURE-topic"})
        job = service.submit({"topic_id": topic["topic_id"], "version": topic["version"],
            "idempotency_key": "TEST_FIXTURE-research"})
        return service, topic, job

    yield build, state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    assert not thread.is_alive()


def _person_cell(report):
    return next(cell for cell in report["coverage"] if cell["entity_id"] == PERSON
        and cell["dimension_id"] == "person_statement")


def _acceptance_receipt(service, report, observed, case):
    """Persist actual fixture observations alongside their immutable originals."""
    archive = service._service(readonly=True).archive
    snapshot = archive.snapshot(report["snapshot_ids"][0])
    documents = archive.snapshot_documents(snapshot["snapshot_id"])
    value = {"schema": "tiger.topic-identity-acceptance/v1",
        "evidence_kind": "TEST_FIXTURE_LOOPBACK_NOT_PRODUCTION_MODEL_QUALITY",
        "case": case, "job_id": report["job_id"], "state": report["state"],
        "report_receipt_sha256": report["receipt_sha256"],
        "source_fingerprint_sha256": report["source_fingerprint"]["sha256"],
        "snapshot_id": snapshot["snapshot_id"], "snapshot_manifest_sha256": snapshot["manifest_sha256"],
        "entity_types": {key: service.catalog.entities[key]["type"] for key in report["topic"]["entity_ids"]},
        "coverage": report["coverage"], "required_gaps": report["required_gaps"],
        "source_http_paths": list(observed["get"]),
        "model_http_requests": [{key: wire.get(key) for key in ("model", "thinking", "reasoning_effort", "max_tokens")}
            for wire in observed["model"]],
        "model_response_ids": [row["receipt"]["response_id"] for row in report["model_attempts"]],
        "documents": [{"id": doc["id"], "source_id": doc["source_id"],
            "material_types": doc["material_types"], "raw_blob": doc["raw_blob"], "text_blob": doc["text_blob"],
            "published": doc["times"]["published"], "origin_cluster": doc["origin_cluster"]} for doc in documents],
        "account_connected": False, "production_network_or_model_calls": 0,
        "limitations": "Controlled source text and controlled model answers over real HTTP; no claim of real model attribution quality or actual licensed research access."}
    (service.store.directory(report["job_id"]) / "TEST_FIXTURE-acceptance.json").write_text(json.dumps(value, indent=2))


def test_am01_three_entity_five_material_full_loopback_topic_and_fixed_snapshot(identity_flow):
    build, observed = identity_flow
    service, topic, job = build()
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    assert len(report["snapshot_ids"]) == 1 and len(observed["model"]) == 2
    assert {service.catalog.entities[key]["type"] for key in report["topic"]["entity_ids"]} == {"issuer", "institution", "person"}
    assert [wire["reasoning_effort"] for wire in observed["model"]] == ["high", "max"]
    assert len(observed["get"]) == 5 and set(observed["get"]) == {"/" + key for key in MATERIALS}
    required = [cell for cell in report["coverage"] if cell["required"]]
    assert len(required) == 15 and {cell["entity_id"] for cell in required} == set(ENTITY_IDS)
    assert all(cell["status"] == "covered" and cell["claim_ids"] for cell in required)
    snapshot_id = report["snapshot_ids"][0]
    archive = service._service(readonly=True).archive
    manifest = archive.snapshot(snapshot_id)
    documents = archive.snapshot_documents(snapshot_id)
    assert len(documents) == 5 and {d["material_types"][0] for d in documents} == set(MATERIALS)
    assert {d["id"] for d in documents} == {row["id"] for row in manifest["members"]}
    assert len({d["raw_blob"]["sha256"] for d in documents}) == 5
    assert {d["origin_cluster"] for d in documents} == {"127.0.0.1"}  # Five materials do not become five independent origins.
    assert all(not row["validated_subjects"] and set(row["requested_entity_ids"]) <= set(ENTITY_IDS)
        for row in manifest["members"])
    plan = json.loads((service.store.directory(job["job_id"]) / "acquisition-plan-0.json").read_text())
    assert {(cell["entity_id"], cell["material_type"]) for cell in plan["coverage_cells"]} == {
        (entity, material) for entity in ENTITY_IDS for material in MATERIALS}
    by_id = {d["id"]: d for d in documents}
    for claim in report["analysis"]["conclusions"]:
        citation = claim["supports"][0]
        assert citation["document_id"] in by_id and citation["quote"] in by_id[citation["document_id"]]["text"]
    for document in documents:
        reconstruct_document(document, archive.read_blob, service.registry.public_manifest())
    assert service.report(job["job_id"])["receipt_sha256"] == report["receipt_sha256"]
    assert len(observed["get"]) == 5 and len(observed["model"]) == 2
    person = next(d for d in documents if d["source_id"] == "TEST_FIXTURE-person_statement")
    identity = service.catalog.person_statement_evidence(PERSON, person)
    assert identity["qualified"] and identity["current_affiliation_verified"] is False
    _acceptance_receipt(service, report, observed, "AM01-three-entity-five-material")


def test_am01_missing_one_required_material_stays_partial_with_same_snapshot(identity_flow):
    build, observed = identity_flow
    service, _, job = build(missing="formal_research")
    report = service.run(job["job_id"])
    assert report["state"] == "partial", report
    assert len(report["snapshot_ids"]) == 1 and len(observed["model"]) == 2
    missing = [cell for cell in report["coverage"] if cell["dimension_id"] == "formal_research"]
    assert len(missing) == 3 and all(cell["required"] and cell["status"] == "unknown" for cell in missing)
    assert report["required_gaps"]
    assert len(service._service(readonly=True).archive.snapshot_documents(report["snapshot_ids"][0])) == 4
    assert service.report(job["job_id"])["receipt_sha256"] == report["receipt_sha256"]
    _acceptance_receipt(service, report, observed, "AM01-missing-required-material")


@pytest.mark.parametrize("case,reason", [
    ("same_name", "PERSON_IDENTITY_UNVERIFIED"),
    ("departed", "PERSON_ROLE_OUTSIDE_VALIDITY"),
    ("secondary_report", "PERSON_ORIGINAL_STATEMENT_UNVERIFIED"),
    ("ambiguous_same_name_authors", "PERSON_IDENTITY_AMBIGUOUS"),
    ("unknown_role", "PERSON_ROLE_TIME_UNVERIFIED"),
    ("unknown_publication", "PERSON_STATEMENT_TIME_UNVERIFIED"),
    ("wrong_episode_date", "PERSON_ROLE_TIME_UNVERIFIED")])
def test_am25_full_loopback_report_does_not_promote_name_departed_role_or_retelling(identity_flow, case, reason):
    build, observed = identity_flow
    service, _, job = build(case)
    report = service.run(job["job_id"])
    assert report["state"] == "partial", report
    cell = _person_cell(report)
    assert cell["status"] == "unknown" and cell["reason"] == reason
    assert cell["candidate_claim_ids"] and not cell["claim_ids"]
    documents = service._service(readonly=True).archive.snapshot_documents(report["snapshot_ids"][0])
    original = next(d for d in documents if d["source_id"] == "TEST_FIXTURE-person_statement")
    assert "Alex Morgan" in original["text"] and PERSON in report["topic"]["entity_ids"]
    # Even a requested entity and an exact quote plus a fixture model's
    # confident candidate cannot override the trusted identity/time contract.
    identity = service.catalog.person_statement_evidence(PERSON, original)
    assert identity["qualified"] is False and identity["reason"] == reason
    assert identity["current_affiliation_verified"] is False
    assert reason in report["readable_report"]
    assert service.report(job["job_id"])["receipt_sha256"] == report["receipt_sha256"]
    assert len(observed["get"]) == 5 and len(observed["model"]) == 2
    _acceptance_receipt(service, report, observed, "AM25-" + case)


def test_am25_historical_source_role_preserves_unknown_timezone_and_not_current_employment(identity_flow):
    build, observed = identity_flow
    service, _, job = build("historical_episode")
    report = service.run(job["job_id"])
    assert report["state"] == "completed", report
    documents = service._service(readonly=True).archive.snapshot_documents(report["snapshot_ids"][0])
    original = next(d for d in documents if d["source_id"] == "TEST_FIXTURE-person_statement")
    identity = service.catalog.person_statement_evidence(PERSON, original)
    assert original["times"]["published"]["value"] is None
    assert original["times"]["published"]["precision"] == "unknown"
    assert original["times"]["published"]["raw"] == "2026-09-01T00:00:00"
    assert identity["qualified"] and identity["reason"] == "PERSON_HISTORICAL_ROLE_SOURCE_ATTRIBUTED"
    assert identity["role_date_basis"] == "original_local_calendar_date_only"
    assert identity["current_affiliation_verified"] is False
    assert identity["publication"] == original["times"]["published"]
    assert service.report(job["job_id"])["receipt_sha256"] == report["receipt_sha256"]
    assert len(observed["model"]) == 2
    _acceptance_receipt(service, report, observed, "AM25-historical-source-role")


def test_am25_qualified_person_requires_exact_registered_source_binding(identity_flow):
    build, _ = identity_flow
    service, _, job = build()
    report = service.run(job["job_id"])
    original = next(d for d in service._service(readonly=True).archive.snapshot_documents(report["snapshot_ids"][0])
        if d["source_id"] == "TEST_FIXTURE-person_statement")
    for field, value in (("source_contract_sha256", "0" * 64), ("source_id", "other-source")):
        tampered = deepcopy(original)
        tampered["normalization"][field] = value
        check = service.catalog.person_statement_evidence(PERSON, tampered)
        assert not check["qualified"] and check["reason"] == "PERSON_SOURCE_BINDING_UNVERIFIED"
    tampered = deepcopy(original)
    tampered["source_version"] = "old-version"
    assert not service.catalog.person_statement_evidence(PERSON, tampered)["qualified"]


def test_am25_date_precision_cannot_invent_role_validity_at_departure_boundary(identity_flow):
    build, _ = identity_flow
    service, _, job = build()
    report = service.run(job["job_id"])
    original = next(d for d in service._service(readonly=True).archive.snapshot_documents(report["snapshot_ids"][0])
        if d["source_id"] == "TEST_FIXTURE-person_statement")
    original["times"]["published"] = source_time("2026-09-01")
    service.catalog.entities[PERSON]["role_history"][0]["valid_until"] = "2026-09-01T12:00:00+00:00"
    identity = service.catalog.person_statement_evidence(PERSON, original)
    assert not identity["qualified"] and identity["reason"] == "PERSON_ROLE_OUTSIDE_VALIDITY"
    assert original["times"]["published"]["precision"] == "date"
    assert original["times"]["published"]["timezone"] is None


@pytest.mark.parametrize("damage", ["unknown_institution", "mistyped_institution", "mistyped_current_flag",
    "bad_observed_day", "missing_observed_source", "observed_after_departure", "wrong_statement_basis",
    "duplicate_authors", "mistyped_author"])
def test_am25_catalog_rejects_unreviewed_role_and_original_statement_contract(damage):
    spec = SourceSpec("TEST_FIXTURE-person_statement", "page", "https://example.com/original",
        allowed_hosts=("example.com",), options={"material_types": ["person_statement"],
            "author_ids": [PERSON], "person_statement_basis": "original_transcript"})
    registry = SourceRegistry([spec])
    role = {"institution_id": INSTITUTION, "role": "TEST_FIXTURE analyst"}
    config = {"entities": {INSTITUTION: {"type": "institution", "name": "TEST_FIXTURE Bank"},
        PERSON: {"type": "person", "name": "Alex Morgan", "role_history": [role]}}, "sources": {}}
    if damage == "unknown_institution":
        role["institution_id"] = "institution:unreviewed"
    elif damage == "mistyped_institution":
        role["institution_id"] = {}
    elif damage == "mistyped_current_flag":
        role["current_affiliation_verified"] = "yes"
    elif damage == "bad_observed_day":
        role.update(observed_in_source_on="yesterday", source_id=spec.source_id)
    elif damage == "missing_observed_source":
        role["observed_in_source_on"] = "2026-09-01"
    elif damage == "observed_after_departure":
        role.update(observed_in_source_on="2026-09-01", source_id=spec.source_id,
            valid_until="2026-08-01T00:00:00+00:00")
    else:
        config["sources"][spec.source_id] = {"person_statement_basis": "invented"} if damage == "wrong_statement_basis" else {
            "author_ids": [PERSON, PERSON] if damage == "duplicate_authors" else [{}]}
    with pytest.raises(IntelligenceError):
        SourceCatalog(registry, config, reviewed_entities={})
