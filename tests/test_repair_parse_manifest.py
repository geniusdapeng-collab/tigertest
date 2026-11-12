"""Versioned original/structure preservation; all financial names are fixtures."""
from dataclasses import replace
from io import BytesIO
import hashlib
import time

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from trading_system.intelligence.contracts import IntelligenceError, digest
from trading_system.intelligence.extract import extract_response
from trading_system.intelligence.parse_limits import ParsePolicy
from test_intelligence_parse_limits import response, pdf


BODY = b'''<html><title>TEST_FIXTURE Alpha</title><meta property="article:published_time" content="2026-11-01T10:00:00Z"><article><h1>TEST_FIXTURE Alpha</h1><p>Revenue > 5 USD million in Q3 2026.</p><table><tr><th>Period</th><th>USD million</th></tr><tr><td>Q3 2026</td><td>10<sup>1</sup></td></tr></table><p role="doc-footnote" id="fn1">1 Includes 4 million non-recurring revenue.</p><a href="/supplement.pdf">Financial supplement</a></article></html>'''


def policy(**values):
    return ParsePolicy(manifest_version="v1", **values)


def test_explicit_manifest_preserves_cells_footnotes_and_source_coordinates():
    result = extract_response(response(BODY), policy=policy())
    manifest = result.metadata['parse_manifest']
    assert result.raw_bytes == BODY and result.completeness == 'complete'
    assert manifest['raw_sha256'] == hashlib.sha256(BODY).hexdigest()
    assert manifest['text_sha256'] == hashlib.sha256(result.text.encode()).hexdigest()
    assert manifest['sha256'] == digest({k:v for k,v in manifest.items() if k != 'sha256'})
    assert len([x for x in manifest['units'] if x['kind'] == 'table_cell']) == 4
    footnote = next(x for x in manifest['units'] if x['kind'] == 'footnote')
    assert 'non-recurring' in result.text[footnote['start']:footnote['end']]
    for unit in manifest['units']:
        assert 0 <= unit['start'] <= unit['end'] <= len(result.text)
        assert unit['source_locator']['kind'] == 'html_element'
        assert unit['text_sha256'] == hashlib.sha256(result.text[unit['start']:unit['end']].encode()).hexdigest()
    assert manifest['attachments'][0]['status'] == 'pending_fetch'
    assert manifest['attachments_complete'] is False
    assert manifest['times']['published']['precision'] == 'instant'
    assert manifest['times']['event']['precision'] == 'unknown'
    assert manifest['times']['first_available']['precision'] == 'unknown'
    assert manifest['times']['retrieved']['value'] != manifest['times']['published']['value']


def test_manifest_is_explicit_and_old_public_policy_unchanged():
    legacy = extract_response(response(BODY))
    assert 'parse_manifest' not in legacy.metadata
    assert 'manifest_version' not in ParsePolicy().public()
    with pytest.raises(IntelligenceError):
        ParsePolicy(manifest_version='v99')


def test_scanned_and_mixed_pdf_cannot_claim_complete():
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    stream = DecodedStreamObject(); stream.set_data(b'BT /F1 10 Tf 1 50 Td (Revenue 100) Tj ET')
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
    page[NameObject('/Contents')] = stream
    writer.add_blank_page(width=100, height=100)
    out=BytesIO();writer.write(out)
    for body in [pdf(1), out.getvalue()]:
        result = extract_response(response(body,'application/pdf'),policy=policy())
        assert result.completeness == 'partial'
        assert result.raw_bytes == body
        assert 'PDF_EMPTY_PAGE_REQUIRES_REVIEW' in result.metadata['parse_manifest']['gaps']
        assert result.metadata['parse_manifest']['review_required'] is True


def test_invalid_encoding_and_resource_failure_have_honest_empty_manifests():
    for raw, limits, error in [(b'\xff\xfe',{},'SOURCE_ENCODING_OR_FORMAT_UNSUPPORTED'), (BODY,{'max_input_bytes':10},'PARSER_INPUT_TOO_LARGE')]:
        result=extract_response(response(raw),policy=policy(**limits))
        assert result.raw_bytes == raw
        assert result.metadata['extraction_error'] == error
        manifest=result.metadata['parse_manifest']
        assert manifest['units'] == [] and manifest['review_required']
        assert manifest['completeness'] != 'complete'


def test_as_of_does_not_promote_fetch_time_to_publication():
    from trading_system.intelligence.parse_manifest import as_of_eligibility
    result=extract_response(response(BODY),policy=policy())
    m=result.metadata['parse_manifest']
    assert as_of_eligibility(m,'2026-10-31T00:00:00Z')['eligible'] is False
    assert as_of_eligibility(m,'2026-11-05T00:00:00Z')['eligible'] is False
    assert as_of_eligibility(m,'2026-11-07T00:00:00Z')['eligible'] is True
    missing=extract_response(response(b'<article><p>No publication time.</p></article>'),policy=policy())
    assert as_of_eligibility(missing.metadata['parse_manifest'],'2026-11-07T00:00:00Z')['eligible'] is False


def test_tampered_mapping_is_rejected():
    from trading_system.intelligence.parse_manifest import verify_manifest
    result=extract_response(response(BODY),policy=policy())
    verify_manifest(result)
    result.metadata['parse_manifest']['units'][0]['end'] += 1
    with pytest.raises(IntelligenceError):verify_manifest(result)


def test_disk_failure_is_classified_without_spawning(monkeypatch):
    from trading_system.intelligence import parse_limits
    def full(*a,**k):raise OSError(28,'TEST_FIXTURE disk full')
    monkeypatch.setattr(parse_limits.tempfile,'TemporaryFile',full)
    r=extract_response(response(BODY),policy=policy())
    assert r.metadata['extraction_error'] == 'PARSER_DISK_EXHAUSTED'
    assert r.raw_bytes == BODY and r.text == ''


def test_resource_pool_reserves_combined_peak_and_releases_on_error():
    from trading_system.intelligence.parse_limits import ParserResourcePool
    pool=ParserResourcePool(max_workers=1, memory_bytes=10, temporary_bytes=12)
    with pool.reserve(memory_bytes=10, temporary_bytes=12):
        assert pool.snapshot()['active_workers'] == 1
        with pytest.raises(IntelligenceError) as error:
            with pool.reserve(memory_bytes=1, temporary_bytes=1):
                pytest.fail('Overcommit must not execute')
        assert error.value.code == 'PARSER_CAPACITY_UNAVAILABLE'
    assert pool.snapshot()['active_workers'] == 0
    with pytest.raises(RuntimeError):
        with pool.reserve(memory_bytes=1,temporary_bytes=1):raise RuntimeError('fixture')
    assert pool.snapshot()['reserved_memory_bytes'] == 0


def test_parser_pool_blocks_before_child_and_preserves_raw():
    from trading_system.intelligence.parse_limits import ParserResourcePool
    pool=ParserResourcePool(max_workers=1,memory_bytes=1,temporary_bytes=1)
    result=extract_response(response(BODY),policy=policy(),resource_pool=pool)
    assert result.metadata['extraction_error'] == 'PARSER_CAPACITY_UNAVAILABLE'
    assert result.raw_bytes == BODY and result.text == ''
    assert result._parser_runtime['worker_returncode'] is None


def test_concurrent_pool_never_exceeds_capacity():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from trading_system.intelligence.parse_limits import ParserResourcePool
    pool=ParserResourcePool(max_workers=2,memory_bytes=100,temporary_bytes=100)
    barrier=Barrier(4)
    def run(_):
        barrier.wait()
        try:
            with pool.reserve(memory_bytes=50,temporary_bytes=50):
                observed=pool.snapshot()
                time.sleep(.1)
                return observed['active_workers']
        except IntelligenceError as error:
            assert error.code == 'PARSER_CAPACITY_UNAVAILABLE'
            return 0
    with ThreadPoolExecutor(max_workers=4) as executor:
        values=list(executor.map(run,range(4)))
    assert sorted(values) == [0,0,1,2]
    assert pool.snapshot()['active_workers'] == 0


@pytest.mark.parametrize('engine',['builtin','scrapling'])
def test_versioned_parser_is_deterministic_and_retains_inline_adjacency(engine):
    raw=b'<html><title>TEST_FIXTURE Words</title><article><h1>TEST_FIXTURE Words</h1><p>Rev<strong>enue</strong> > 5; A&nbsp;B.</p></article></html>'
    first=extract_response(response(raw),policy=policy(html_engine=engine))
    second=extract_response(response(raw),policy=policy(html_engine=engine))
    assert first.text == 'TEST_FIXTURE Words\nRevenue > 5; A B.'
    assert first.metadata == second.metadata
    assert first.completeness == 'complete'
    assert first.metadata['parse_manifest']['locator_source']['kind'] == ('decoded_original_html' if engine == 'builtin' else 'scrapling_selected_markup')


def test_image_content_is_an_explicit_review_gap():
    r=extract_response(response(b'<article><p>Financial chart:</p><img src="/chart.png" alt="Quarterly cash flow"></article>'),policy=policy())
    assert r.completeness == 'partial'
    assert 'HTML_IMAGE_CONTENT_UNVERIFIED' in r.metadata['parse_manifest']['gaps']


def test_v1_failed_operation_replays_without_promoting_to_success():
    from trading_system.intelligence.parse_limits import replay_partial
    observed=response(BODY)
    original=extract_response(observed,policy=policy(max_input_bytes=10))
    replay=replay_partial(observed,policy=policy(max_input_bytes=10),receipt=original.metadata['parse_failure'])
    assert original.metadata == replay.metadata
    assert replay.text == '' and replay.completeness == 'partial'


def test_inline_footnote_and_nested_table_positions_remain_exact():
    result=extract_response(response(BODY),policy=policy())
    reference=next(x for x in result.metadata['parse_manifest']['units'] if x['kind']=='footnote_reference')
    assert result.text[reference['start']:reference['end']] == '1'
    raw=b'<article><table><tr><td>Outer A<table><tr><td>Inner</td></tr></table></td><td>Outer B</td></tr></table></article>'
    result=extract_response(response(raw),policy=policy())
    cells=[x for x in result.metadata['parse_manifest']['units'] if x['kind']=='table_cell']
    assert [(x['table_position']['table'],x['table_position']['cell']) for x in cells]==[(1,1),(2,1),(1,2)]
    assert result.text[cells[-1]['start']:cells[-1]['end']] == 'Outer B'


def test_rehashed_false_positions_cannot_pass_source_replay():
    from trading_system.intelligence.parse_manifest import verify_manifest
    result=extract_response(response(BODY),policy=policy())
    manifest=result.metadata['parse_manifest']
    unit=manifest['units'][0]
    unit['end']-=1
    unit['text_sha256']=hashlib.sha256(result.text[unit['start']:unit['end']].encode()).hexdigest()
    manifest['sha256']=digest({k:v for k,v in manifest.items() if k!='sha256'})
    with pytest.raises(IntelligenceError) as failure:
        verify_manifest(result)
    assert failure.value.code == 'HASH_MISMATCH'
