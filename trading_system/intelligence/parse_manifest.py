"""Explicit original-to-text structure contract; never upgrades legacy evidence.

HTML locators address decoded source characters, not HTTP byte offsets. PDF
locators address page extraction text, not visual coordinates. Neither proves
financial correctness or that a linked attachment has already been fetched.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import hashlib
from importlib.metadata import PackageNotFoundError, version
import re

from .contracts import IntelligenceError, aware, digest, source_time
from .extract import _Page

CONTRACT = 'tiger.parse-manifest/v1'
PARSER = 'tiger.structure-text/v1'
_VOID = frozenset({'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'})
_BLOCKS = frozenset({'p', 'li', 'blockquote', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'td', 'th'})
MAX_STRUCTURE_NODES = 100000


def _sha(value):
    return hashlib.sha256(value).hexdigest()


class MappedPage(_Page):
    """Capture the exact fragments selected by the existing extraction rules."""
    def __init__(self, url):
        super().__init__(url)
        self.nodes, self.active_nodes, self.fragments = [], [], []
        self.structure_gaps = []
        self._source = ''
        self._line_offsets = [0]
        self._table = 0
        self._tables = []

    def feed(self, data):
        # Scrapling may feed multiple selected roots. Source offsets therefore
        # refer to this explicitly named selected-markup representation.
        self._source += data
        self._line_offsets = [0] + [m.end() for m in re.finditer('\n', self._source)]
        super().feed(data)

    def _offset(self):
        line, column = self.getpos()
        return self._line_offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if len(self.nodes) >= MAX_STRUCTURE_NODES:
            raise IntelligenceError('PARSER_STRUCTURE_LIMIT')
        marker = ' '.join(attributes.get(k, '') for k in ('role', 'id', 'class')).lower()
        kind = ('footnote' if 'footnote' in marker or re.search(r'\bfn\d+\b', marker)
                else 'footnote_reference' if tag == 'sup' or attributes.get('role') == 'doc-noteref'
                else 'table_cell' if tag in {'td', 'th'} else 'table' if tag == 'table'
                else 'heading' if tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
                else 'paragraph' if tag in _BLOCKS else 'container')
        if tag == 'table':
            self._table += 1
            self._tables.append({'table': self._table, 'row': 0, 'cell': 0})
        if tag == 'tr' and self._tables:
            self._tables[-1]['row'] += 1
            self._tables[-1]['cell'] = 0
        if tag in {'td', 'th'} and self._tables:
            self._tables[-1]['cell'] += 1
        node = {'id': len(self.nodes), 'tag': tag, 'kind': kind, 'source_start': self._offset(),
                'source_end': None, 'line': self.getpos()[0], 'column': self.getpos()[1],
                'parent': self.active_nodes[-1] if self.active_nodes else None}
        if tag in {'td', 'th'}:
            position = self._tables[-1] if self._tables else {'table': None, 'row': None, 'cell': None}
            if not self._tables:
                self.structure_gaps.append('HTML_CELL_WITHOUT_TABLE')
            node['table_position'] = {**position,
                'rowspan': attributes.get('rowspan', '1'), 'colspan': attributes.get('colspan', '1')}
        if tag == 'img' and self.main_depth and not self.skip and attributes.get('alt') != '':
            self.structure_gaps.append('HTML_IMAGE_CONTENT_UNVERIFIED')
        self.nodes.append(node)
        self.active_nodes.append(node['id'])
        super().handle_starttag(tag, attrs)
        if tag in _VOID:
            node['source_end'] = self._offset() + len(self.get_starttag_text())
            self.active_nodes.pop()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        index = next((i for i in range(len(self.active_nodes) - 1, -1, -1)
                      if self.nodes[self.active_nodes[i]]['tag'] == tag), None)
        if index is None:
            return
        if index != len(self.active_nodes) - 1:
            self.structure_gaps.append('HTML_UNCLOSED_STRUCTURE')
        end = self._source.find('>', self._offset())
        for item in self.active_nodes[index:]:
            self.nodes[item]['source_end'] = end + 1 if end >= 0 else len(self._source)
        closed_tables = sum(self.nodes[item]['tag'] == 'table' for item in self.active_nodes[index:])
        if closed_tables:
            del self._tables[-closed_tables:]
        del self.active_nodes[index:]

    def _add(self, text):
        if not self.skip:
            owners = self.active_nodes[:-1] if text == ' | ' else self.active_nodes
            self.fragments.append((text, tuple(owners), bool(self.main_depth)))
        super()._add(text)

    def projection(self, *, main_only=False):
        selected = [x for x in self.fragments if not main_only or x[2]]
        joined = ''.join(x[0] for x in selected)
        # One linear pass preserves adjacency across inline tags; it does not
        # insert spaces in e.g. Rev<strong>enue</strong>.
        bounds, parts, length, cursor, fragment_index = {}, [], 0, 0, 0
        intervals=[]
        for fragment, owners, _ in selected:
            intervals.append((cursor, cursor + len(fragment), owners)); cursor += len(fragment)
        previous = 0
        for token in re.finditer(r'\S+', joined):
            separator = joined[previous:token.start()]
            if parts and separator:
                gap = '\n' if '\n' in separator else ' '
                parts.append(gap); length += 1
            start = length
            parts.append(token.group()); length += len(token.group())
            while fragment_index < len(intervals) and intervals[fragment_index][1] <= token.start():
                fragment_index += 1
            index = fragment_index
            while index < len(intervals) and intervals[index][0] < token.end():
                for owner in intervals[index][2]:
                    local_start = start + max(0, intervals[index][0] - token.start())
                    local_end = start + min(token.end(), intervals[index][1]) - token.start()
                    bounds.setdefault(owner, [local_start, local_end])[1] = local_end
                index += 1
            previous = token.end()
        text = ''.join(parts)
        units=[]
        for node in self.nodes:
            if node['id'] not in bounds or node['kind'] == 'container':
                continue
            start, end = bounds[node['id']]
            unit = {'id': 'element-' + str(node['id']), 'kind': node['kind'], 'start': start, 'end': end,
                'text_sha256': _sha(text[start:end].encode()),
                'source_locator': {'kind': 'html_element', 'coordinate_system': 'decoded_html_characters',
                    'tag': node['tag'], 'start': node['source_start'],
                    'end': node['source_end'] if node['source_end'] is not None else len(self._source),
                    'line': node['line'], 'column': node['column']}}
            if 'table_position' in node:
                unit['table_position'] = node['table_position']
            units.append(unit)
        omitted = [{'id': 'element-' + str(node['id']), 'kind': node['kind'],
            'source_start': node['source_start'], 'source_end': node['source_end'],
            'reason': 'outside_selected_text_or_empty'} for node in self.nodes
            if node['kind'] != 'container' and node['id'] not in bounds]
        gaps = list(self.structure_gaps)
        if self.active_nodes:
            gaps.append('HTML_UNCLOSED_STRUCTURE')
        return text, {'units': units, 'omitted_units': omitted, 'gaps': sorted(set(gaps)),
                      'locator_source_sha256': _sha(self._source.encode()),
                      'locator_source_kind': 'decoded_original_html'}


def attach_manifest(result, policy):
    """Pure, deterministic projection over existing raw/text and observations."""
    if policy.manifest_version is None:
        return result
    metadata = dict(result.metadata)
    structure = metadata.pop('_parse_structure', None)
    units, gaps = [], []
    raw_hash, text_hash = _sha(result.raw_bytes), _sha(result.text.encode())
    parser = {'name': PARSER, 'engine': policy.html_engine, 'policy': policy.public()}
    locator_source = {'kind': 'raw_bytes', 'sha256': raw_hash}
    if structure is not None:
        units, gaps = structure['units'], structure['gaps']
        locator_source = {'kind': structure['locator_source_kind'], 'sha256': structure['locator_source_sha256']}
    elif result.media_type == 'application/pdf':
        try:
            dependency_version = version('pypdf')
        except PackageNotFoundError:
            dependency_version = None
            gaps.append('PDF_DEPENDENCY_UNAVAILABLE')
        parser['dependency'] = {'name': 'pypdf', 'version': dependency_version}
        locations = metadata.get('page_locations', [])
        for row in locations:
            units.append({'id': 'page-' + str(row['page']), 'kind': 'page', 'start': row['start'], 'end': row['end'],
                'text_sha256': _sha(result.text[row['start']:row['end']].encode()),
                'source_locator': {'kind': 'pdf_page', 'page': row['page'], 'coordinate_system': 'page_extraction_text'}})
        gaps.append('PDF_TABLE_FOOTNOTE_LAYOUT_UNVERIFIED')
        if any(not result.text[x['start']:x['end']].strip() for x in locations):
            gaps.append('PDF_EMPTY_PAGE_REQUIRES_REVIEW')
        if not locations:
            gaps.append('PDF_PAGE_COVERAGE_UNKNOWN')
    elif result.text:
        for number, match in enumerate(re.finditer(r'[^\n]+', result.text), 1):
            units.append({'id': 'line-' + str(number), 'kind': 'paragraph', 'start': match.start(), 'end': match.end(),
                'text_sha256': _sha(match.group().encode()),
                'source_locator': {'kind': 'decoded_text_line', 'line': number, 'coordinate_system': 'decoded_text_characters'}})
        if result.text_kind in {'html_main', 'html_text', 'data'}:
            gaps.append('STRUCTURE_UNVERIFIED')
    if metadata.get('extraction_error'):
        gaps.append(metadata['extraction_error'])
    if result.completeness != 'complete':
        gaps.append('SOURCE_CONTENT_INCOMPLETE')
    attachments = [{'url': row['url'], 'label': row.get('label', ''), 'status': 'pending_fetch',
                    'parent_raw_sha256': raw_hash} for row in result.attachments]
    manifest = {'schema': CONTRACT, 'parser': parser, 'raw_sha256': raw_hash, 'raw_bytes': len(result.raw_bytes),
        'text_sha256': text_hash, 'text_bytes': len(result.text.encode()), 'text_characters': len(result.text),
        'locator_source': locator_source, 'units': units, 'omitted_units': structure.get('omitted_units', []) if structure else [],
        'coverage_scope': 'selected_text_and_detected_structure', 'attachments': attachments,
        'attachments_complete': not attachments, 'gaps': sorted(set(gaps)),
        'completeness': 'complete' if not gaps else 'partial', 'review_required': bool(gaps),
        'times': {'published': result.published, 'revised': result.updated,
            'retrieved': source_time(metadata.get('retrieved_at'), evidence={'kind': 'transport_observation'}),
            'first_available': source_time(None, evidence={'kind': 'not_observed'}),
            'event': source_time(None, evidence={'kind': 'semantic_review_required'})}}
    manifest['sha256'] = digest(manifest)
    metadata.update(parse_manifest=manifest, extractor=PARSER,
        extractor_config_sha256=digest({'parser': parser, 'previous': metadata.get('extractor_config_sha256')}))
    return replace(result, metadata=metadata, completeness=manifest['completeness'])


def verify_manifest(result):
    """Reparse the original bytes and compare the complete structure projection.

    Transport authenticity still belongs to the enclosing archive proof.
    A self-consistent rehash of changed ranges alone never passes this check.
    """
    value = result.metadata.get('parse_manifest')
    try:
        if (value['schema'] != CONTRACT or value['raw_sha256'] != _sha(result.raw_bytes)
                or value['raw_bytes'] != len(result.raw_bytes)
                or value['text_sha256'] != _sha(result.text.encode())
                or value['text_bytes'] != len(result.text.encode())
                or value['text_characters'] != len(result.text)
                or value['sha256'] != digest({k:v for k,v in value.items() if k != 'sha256'})):
            raise ValueError
        seen=set()
        for unit in value['units']:
            if (unit['id'] in seen or type(unit['start']) is not int or type(unit['end']) is not int
                    or not 0 <= unit['start'] <= unit['end'] <= len(result.text)
                    or unit['text_sha256'] != _sha(result.text[unit['start']:unit['end']].encode())):
                raise ValueError
            seen.add(unit['id'])
    except (KeyError, TypeError, ValueError):
        raise IntelligenceError('HASH_MISMATCH', 'Parse manifest does not bind original and text') from None
    from .extract import extract_response
    from .transport import HttpResponse
    try:
        metadata = result.metadata
        replay = extract_response(HttpResponse(result.final_url, metadata["http_status"],
            metadata["http_headers"], result.raw_bytes, metadata["retrieved_at"],
            metadata["transport_sha256"], metadata["request_count"], metadata.get("parse_failure")),
            original_url=result.url, trusted_filing=metadata.get("trusted_filing_extraction", False),
            policy=value["parser"]["policy"])
        if (replay.text != result.text or replay.completeness != result.completeness
                or replay.metadata.get("parse_manifest") != value):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise IntelligenceError("HASH_MISMATCH", "Parse manifest source replay differs") from None
    return value


def as_of_eligibility(manifest, as_of):
    """Conservative no-lookahead admission; retrieval is not publication time."""
    cutoff = aware(as_of)
    reasons=[]
    for field in ('published', 'revised', 'retrieved'):
        stamp=manifest['times'][field]
        if stamp['precision'] == 'unknown':
            if field != 'revised':reasons.append(field.upper() + '_UNKNOWN')
            continue
        if stamp['precision'] == 'date':
            latest=aware(stamp['value'] + 'T00:00:00+00:00') + timedelta(days=1, hours=14)
        else:
            latest=aware(stamp['value'])
        if latest > cutoff:reasons.append(field.upper() + '_AFTER_AS_OF')
    return {'eligible': not reasons, 'reasons': reasons, 'as_of': cutoff.isoformat(), 'manifest_sha256': manifest['sha256']}
