"""Scrapling selectors over already-acquired bytes; this module never fetches."""
from __future__ import annotations

from dataclasses import replace
from importlib.metadata import version
import re

from .contracts import digest


_EXCLUDED_TAGS = frozenset({"nav", "aside", "footer", "header", "form", "button"})
_EXCLUDED_MARKERS = frozenset({"navigation", "advertisement", "advert", "advertising", "sidebar",
    "aside", "banner", "related", "relatednews", "relatedposts", "recommendation", "recommendations", "recommended"})
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr"})
_PLACEHOLDERS = frozenset({"loading", "loading content", "loading article", "please enable javascript",
    "enable javascript", "javascript is required", "content is loading", "加载中", "正在加载"})


def _excluded_markup(tag, attributes, *, allow_header=False):
    if tag in _EXCLUDED_TAGS and not (allow_header and tag == "header"):
        return True
    marker = " ".join(str(attributes.get(key, "")) for key in
        ("id", "class", "role", "aria-label", "data-component", "data-testid")).casefold()
    return bool(_EXCLUDED_MARKERS.intersection(re.findall(r"[a-z0-9]+", marker)))


def _excluded(node, *, allow_header=False):
    while node is not None:
        if _excluded_markup(node.tag, node.attrib, allow_header=allow_header):
            return True
        node = node.parent
    return False


def _words(value):
    return " ".join(str(value).split()).casefold()


def apply_scrapling(result, policy):
    from scrapling import Selector
    from .extract import _Page, _clean

    page = Selector(result.raw_bytes, url=result.final_url, adaptive=False, huge_tree=False)
    selected = page.css(policy.selector)
    roots = []
    for node in selected:
        # A recommendation can use a genuine <article> tag. Check its enclosing
        # region as well as the selected node before calling it original text.
        if node.tag not in {"article", "main", "section", "div"} or _excluded(node):
            continue
        parent = node.parent
        nested = False
        while parent is not None:
            if any(parent.html_content == root.html_content for root in roots):
                nested = True
                break
            parent = parent.parent
        if not nested:
            roots.append(node)
    class SelectedPage(_Page):
        def __init__(self, url):
            super().__init__(url)
            self.filtered_stack, self.filtered_depth = [], 0

        def handle_starttag(self, tag, attrs):
            excluded = bool(self.filtered_depth or _excluded_markup(tag, dict(attrs)))
            if tag not in _VOID_TAGS:
                self.filtered_stack.append(excluded)
                self.filtered_depth += int(excluded)
            if not excluded:
                super().handle_starttag(tag, attrs)

        def handle_endtag(self, tag):
            if tag in _VOID_TAGS:
                return
            excluded = self.filtered_stack.pop() if self.filtered_stack else False
            if excluded:
                self.filtered_depth -= 1
            else:
                super().handle_endtag(tag)

        def handle_data(self, data):
            if not self.filtered_depth:
                super().handle_data(data)

    content = SelectedPage(result.final_url)
    for node in roots:
        content.feed(node.html_content)
    content.close()
    text = _clean("".join(content.all))
    headline = _words(result.title)
    headline_inside = bool(headline) and any(_words(heading.get_all_text()) == headline
        for root in roots for heading in root.css("h1") if not _excluded(heading, allow_header=True))
    substantive = any(_words(block.get_all_text()).strip(".!… ") not in {"", headline, *_PLACEHOLDERS}
        for root in roots for block in root.css("p, td, th, li, blockquote") if not _excluded(block))
    default_selector = re.sub(r"\s+", "", policy.selector).casefold() in {"article,main", "main,article"}
    # A site-specific body selector may intentionally exclude an external h1.
    # Require one structural body and explicit terms found in that body, rather
    # than accepting a matching page title plus unrelated recommendation cards.
    explicit_body = (not default_selector and len(roots) == 1 and bool(policy.expected_terms)
        and all(term.casefold() in text.casefold() for term in policy.expected_terms))
    valid = bool(roots and text.strip() and headline and substantive and (headline_inside or explicit_body))
    combined = (result.title + "\n" + text).casefold()
    if policy.expected_terms:
        valid = valid and all(term.casefold() in combined for term in policy.expected_terms)
    config = {"engine": "scrapling", "version": version("scrapling"),
              "selector": policy.selector, "expected_terms": list(policy.expected_terms), "adaptive": False}
    if result.metadata.get("redaction_markup_policy"):
        config["redaction_markup_policy"] = result.metadata["redaction_markup_policy"]
    metadata = {**result.metadata, "extractor": "tiger-scrapling/v1", "parser": config,
        "extractor_config_sha256": digest(config), "parse_policy": policy.public(),
        "selector_validation": {"validated": valid, "selected_roots": len(roots),
            "has_title": bool(result.title.strip()), "expected_terms_checked": len(policy.expected_terms)}}
    if not valid:
        metadata["extraction_error"] = "PARSER_SELECTOR_UNVERIFIED"
    # A correct selector cannot remove an explicit paywall or create complete
    # authority/subject evidence. Those gates remain separate from extraction.
    complete = valid and not metadata.get("explicit_paywall")
    return replace(result, text=text if text else result.text, text_kind="html_main" if roots else "html_text",
                   completeness="complete" if complete else "partial", metadata=metadata)
