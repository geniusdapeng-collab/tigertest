"""Original-byte fidelity for escaped public links; credential URLs still fail closed."""
from datetime import datetime, timezone
import json

import pytest

from test_intelligence_acquisition import web
from trading_system.intelligence.contracts import credential_free_url, safe_value
from trading_system.intelligence.planner import build_plan
from trading_system.intelligence.registry import SourceRegistry, SourceSpec
from trading_system.intelligence.replay import reconstruct_document
from trading_system.intelligence.service import IntelligenceService
from trading_system.intelligence.transport import HttpTransport


@pytest.mark.parametrize('url', ['https://public.example/insights', 'https://public.example/?topic=AI&lang=en'])
def test_public_link_in_escaped_html_attribute_keeps_original_bytes(url):
    text = json.dumps({'markup': '<a href="' + url + '">Public portal</a>'})
    assert safe_value(text) == text
    assert not credential_free_url(url + '\\')
    assert safe_value(url + '\\') == '[REDACTED_URL]'


@pytest.mark.parametrize('url', [
    'https://public.example/?access_token=CANARY_URL_CREDENTIAL',
    'https://CANARY_URL_CREDENTIAL@public.example/page',
    'https://public.example/?redirect=https%3A%2F%2Finner.example%2F%3Ftoken%3DCANARY_URL_CREDENTIAL',
    'https://public.example/path\\?token=CANARY_URL_CREDENTIAL',
])
def test_escaped_attribute_does_not_hide_credential_url(url):
    text = json.dumps({'markup': '<a href="' + url + '">Portal</a>'})
    sanitized = safe_value(text)
    assert '[REDACTED_URL]' in sanitized and 'CANARY_URL_CREDENTIAL' not in sanitized


@pytest.mark.parametrize('secret_url', [False, True])
def test_actual_registered_page_retains_public_wire_or_downgrades_sensitive_wire(tmp_path, web, secret_url):
    base, routes, _calls = web
    target = 'https://public.example/portal' + ('?token=CANARY_URL_CREDENTIAL' if secret_url else '?topic=AI')
    encoded = json.dumps({'navigation': '<a href="' + target + '">Portal</a>'})
    body = ('<html><title>TEST_FIXTURE original</title><article><h1>TEST_FIXTURE original</h1>'
        '<p>Original operating evidence with complete body.</p></article><script type="application/json">'
        + encoded + '</script></html>')
    routes['/original'] = 200, 'text/html', body
    source = SourceSpec('original_page', 'page', base + '/original', allowed_hosts=('127.0.0.1',),
        minimum_interval_seconds=0)
    registry = SourceRegistry([source])
    service = IntelligenceService(tmp_path, registry,
        registry.build_adapters(transport=HttpTransport(allow_local=True), environment={}))
    plan = build_plan({'run_id': 'TEST_FIXTURE-redaction', 'as_of': datetime.now(timezone.utc).isoformat(),
        'universe': {}, 'frontier_manifest_hash': '0' * 64, 'source_ids': [source.source_id],
        'required_cells': [{'target_id': 'topic:original', 'question': 'Original page', 'required': True}],
        'limits': {'requests': 10, 'fetches': 2, 'bytes': 100000, 'rounds': 1,
                   'concurrency': 1, 'deadline_seconds': 10}}, registry)
    job = service.create_job(plan, 'TEST_FIXTURE-redaction')
    acquired = service.collect(job['job_id'])
    assert len(acquired['documents']) == 1
    document = acquired['documents'][0]
    archived = service.archive.read_blob(document['raw_blob'])
    assert document['normalization']['raw_redacted'] is secret_url
    if secret_url:
        assert document['completeness'] == 'partial' and b'CANARY_URL_CREDENTIAL' not in archived
    else:
        assert document['completeness'] == 'complete' and archived == body.encode()
    checked = reconstruct_document(document, service.archive.read_blob, registry.public_manifest())
    assert checked['completeness'] == document['completeness']


@pytest.mark.parametrize('escaped', [
    r'https://public.example/portal?topic=AI\u0026lang=en',
    r'https://public.example/research.html\u0026nbsp;\u0026nbsp;\u003ca',
])
def test_json_unicode_delimiters_in_public_navigation_are_understood_without_rewriting(escaped):
    source = json.dumps({'html': '<a href="' + escaped + '">Portal</a>'})
    assert safe_value(source) == source


@pytest.mark.parametrize('escaped', [
    r'https://public.example/?topic=AI\u0026token=CANARY_URL_CREDENTIAL',
    r'https://public.example/?\u0074oken=CANARY_URL_CREDENTIAL',
    r'https://public.example/?redirect=https%3A%2F%2Finner.example%2F%3Ftoken%3DCANARY_URL_CREDENTIAL\u0026lang=en',
])
def test_json_unicode_escapes_cannot_hide_credential_query(escaped):
    source = json.dumps({'html': '<a href="' + escaped + '">Portal</a>'})
    sanitized = safe_value(source)
    assert '[REDACTED_URL]' in sanitized and 'CANARY_URL_CREDENTIAL' not in sanitized
