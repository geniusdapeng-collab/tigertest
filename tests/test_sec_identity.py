from datetime import datetime, timezone

import pytest

from trading_system.llm.sec_identity import filing_request, confirm_filings, sec_body, sec_verification_input
from test_model_required_company_evidence import URL, _filing

CUTOFF = datetime(2026, 10, 20, 14, 45, tzinfo=timezone.utc)
ACCESSION = "0000000123-26-000001"


def search_row():
    row = _filing()
    return {**row, "id": "ds_" + "a" * 24, "published": row.pop("date"), "content_hash": "a" * 64}


def frozen_responses(*, issuer="AAA", accepted="2026-10-20T11:00:00Z", original="issuer-8k.htm"):
    import json

    submissions = {"cik": "0000000123", "filings": {"recent": {
        "accessionNumber": [ACCESSION], "filingDate": ["2026-10-20"],
        "acceptanceDateTime": [accepted], "reportDate": ["2026-10-20"],
        "form": ["8-K"], "primaryDocument": [original],
    }}}
    html = f"""<!doctype html><html><body><p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p>
    <p>FORM 8-K</p><table><tr><th>Title of each class</th><th>Trading Symbol(s)</th>
    <th>Name of each exchange on which registered</th></tr>
    <tr><td>Class A</td><td>{issuer}</td><td>New York Stock Exchange</td></tr>
    </table><p>Official document content</p></body></html>"""
    request = filing_request(URL)
    return {"filings": [{
        "id": "ds_" + "a" * 24,
        "submissions": sec_body(request["submissions"], json.dumps(submissions).encode()),
        "original": sec_body(request["original"], html.encode()),
    }]}


def test_requires_official_body_and_returns_frozen_issuer_period_and_digest():
    rows = [search_row()]
    request = sec_verification_input("AAA", rows, CUTOFF.date(), CUTOFF)
    outcome, documents = confirm_filings(request, rows, frozen_responses(), CUTOFF.date(), CUTOFF)
    assert outcome["status"] == "filing_verified"
    assert documents[0]["sec_filing"]["accession"] == ACCESSION
    assert documents[0]["sec_filing"]["cik"] == "0000000123"
    assert documents[0]["sec_filing"]["report_period_end"] == "2026-10-20"
    assert "Official document content" in documents[0]["content"]


@pytest.mark.parametrize("changes", [{"issuer": "BBB"}, {"accepted": "2026-10-20T16:00:00Z"},
                                     {"original": "different-file.htm"}])
def test_provider_excerpt_cannot_override_official_issuer_filing_or_date(changes):
    rows = [search_row()]
    outcome, documents = confirm_filings(sec_verification_input("AAA", rows, CUTOFF.date(), CUTOFF),
                                          rows, frozen_responses(**changes), CUTOFF.date(), CUTOFF)
    assert outcome["status"] == "no_eligible_filing" and documents == []


def test_missing_contact_is_distinct_and_snapshot_digest_is_rechecked():
    rows = [search_row()]
    request = sec_verification_input("AAA", rows, CUTOFF.date(), CUTOFF)
    unavailable = {"filings": [{"id": rows[0]["id"],
                                "submissions": {"url": request["filings"][0]["submissions"],
                                                "status": "unavailable", "reason": "contact_missing"},
                                "original": None}]}
    outcome, documents = confirm_filings(request, rows, unavailable, CUTOFF.date(), CUTOFF)
    assert outcome["status"] == "verification_unavailable" and documents == []
    drift = frozen_responses()
    drift["filings"][0]["original"]["sha256"] = "f" * 64
    with pytest.raises(ValueError, match="digest"):
        confirm_filings(request, rows, drift, CUTOFF.date(), CUTOFF)
