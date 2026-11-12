"""Search and bind point-in-time SEC filings for numerically approved candidates."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ..redline.execution import RequiredModelFailed
from ..redline.model_diagnostics import RequiredModelUnavailable, validate_model_diagnostic
from .deepseek_required import retryable_search_failure

COMPANY_EVIDENCE_CONTRACT = "sec-cover-search/v1"
MAX_CANDIDATES = 7
SEARCH_COUNT = 4
LOOKBACK_DAYS = 90
MARKET_ZONE = ZoneInfo("America/New_York")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,14}")
_FILING_PATH = re.compile(r"/Archives/edgar/data/[0-9]{1,10}/[0-9]{18}/[A-Za-z0-9][A-Za-z0-9._-]{0,179}\.html?")
_FORM = re.compile(r"^form\s+(?:8-k|10-q|10-k|6-k|20-f)(?:/a)?$", re.IGNORECASE)
_EXCHANGE = re.compile(
    r"(?:(?:the )?new york stock exchange(?: llc)?|nyse(?: american| arca)?"
    r"|(?:the )?nasdaq(?: stock market(?: llc)?| global(?: select)? market| capital market)?)",
    re.IGNORECASE,
)


def candidate_searches(symbols: list[str], run_date: date) -> dict[str, str]:
    if (len(symbols) > MAX_CANDIDATES
            or any(not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None for symbol in symbols)
            or len(symbols) != len(set(symbols))):
        raise RequiredModelFailed("Company evidence candidates do not match the approved ticker contract")
    earliest = run_date - timedelta(days=LOOKBACK_DAYS + 1)
    latest = run_date + timedelta(days=1)
    return {symbol: f'site:sec.gov "{symbol}" "Trading Symbol" after:{earliest} before:{latest}'
            for symbol in symbols}


def unavailable_status(diagnostic: dict) -> str:
    """Only controlled retrieval failures can yield an explicit source gap."""
    value = validate_model_diagnostic(diagnostic)
    if value["code"] == "NO_USABLE_SEARCH_RESULTS":
        return "no_eligible_filing"
    if (value["code"] in {"NETWORK_TIMEOUT", "RESPONSE_DEADLINE", "TRANSPORT_FAILURE"}
            or retryable_search_failure(value)):
        return "search_unavailable"
    raise RequiredModelFailed("Mandatory company evidence search failed its provider contract")


def _published_in_window(value: str | None, run_date: date, admitted_at: datetime | None) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 80:
        return False
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if published.utcoffset() is None:
        return False
    # Date-only values represented as midnight have no reliable intraday
    # precision. Require the stated date to have already passed in New York.
    if published.time() == time.min:
        observed_date = published.date()
        before_cutoff = observed_date < run_date
    else:
        observed_date = published.astimezone(MARKET_ZONE).date()
        before_cutoff = (published <= admitted_at if admitted_at is not None
                         else observed_date < run_date)
    return before_cutoff and run_date - timedelta(days=LOOKBACK_DAYS) <= observed_date <= run_date


def _issuer_cover(content: str, symbol: str) -> bool:
    # Search/Fetched Markdown must show the SEC registration table on its
    # cover. A ticker mention or a similarly named issuer is insufficient.
    cover = [" ".join(line.strip().strip("#* ").split())
             for line in content.splitlines() if line.strip()][:90]
    if (not any(line.casefold() in {"securities and exchange commission",
                                    "united states securities and exchange commission"} for line in cover[:25])
            or not any(_FORM.fullmatch(line) for line in cover[:35])):
        return False

    def cells(line):
        return [cell.strip().strip("*_` ").strip() for cell in line.split("|") if cell.strip()]

    for index, line in enumerate(cover):
        header = [cell.casefold() for cell in cells(line)]
        if header not in (["title of each class", "trading symbol(s)", "name of each exchange on which registered"],
                          ["title of each class", "trading symbol", "name of each exchange on which registered"]):
            continue
        for row in cover[index + 1:]:
            if "|" not in row:
                break
            values = cells(row)
            if (len(values) == 3 and values[1] == symbol
                    and _EXCHANGE.fullmatch(values[2])):
                return True
    return False


def eligible_filings(symbol: str, rows: list[dict], run_date: date,
                     admitted_at: datetime | None) -> list[dict]:
    """Check issuer, filing origin and reported publication time from search evidence."""
    eligible = []
    seen_urls = set()
    for row in rows:
        url = urlsplit(row["url"])
        if (url.scheme != "https" or url.netloc not in {"sec.gov", "www.sec.gov"}
                or not _FILING_PATH.fullmatch(url.path) or url.query or url.fragment
                or url.path in seen_urls
                or not _published_in_window(row["published"], run_date, admitted_at)
                or not _issuer_cover(row["content"], symbol)):
            continue
        seen_urls.add(url.path)
        eligible.append(row)
    return eligible


def candidate_evidence(symbol: str, rows: list[dict], run_date: date,
                       admitted_at: datetime | None, *, failure: dict | None = None) -> tuple[dict, list[dict]]:
    if failure is not None:
        status, filings = unavailable_status(failure), []
    else:
        filings = eligible_filings(symbol, rows, run_date, admitted_at)
        status = "filing_verified" if filings else "no_eligible_filing" if rows else "no_results"
    documents = [{"id": row["id"], "title": row["title"], "url": row["url"],
                  "published_at": row["published"], "content": row["content"],
                  "native_content_sha256": row["content_hash"]} for row in filings]
    return {"status": status, "source_ids": [item["id"] for item in documents]}, documents


def collect_candidate_evidence(llm, approved: list[str]) -> tuple[dict, dict]:
    run_date = llm.execution.run_date
    admitted_at = getattr(llm.execution, "admitted_at", None)
    outcomes, documents = {}, {}
    for symbol, query in candidate_searches(approved, run_date).items():
        try:
            rows = llm.search(query, count=SEARCH_COUNT, need_content=True)
            outcome, filings = candidate_evidence(symbol, rows, run_date, admitted_at)
        except RequiredModelUnavailable as error:
            outcome, filings = candidate_evidence(symbol, [], run_date, admitted_at, failure=error.diagnostic)
        outcomes[symbol] = outcome
        documents.update((row["id"], row) for row in filings)
    return outcomes, documents
