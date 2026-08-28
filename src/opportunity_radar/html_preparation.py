from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, Tag


_WHITESPACE = re.compile(r"[\t\f\v ]+")


def _useful_href(href: str, base_url: str) -> str | None:
    absolute = urljoin(base_url, href.strip())
    return absolute if urlsplit(absolute).scheme in {"http", "https"} else None


def prepare_html(html: str, *, base_url: str) -> str:
    """Turn HTML into compact extraction text without inferring facts."""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(string=lambda value: isinstance(value, Comment)):
        node.extract()
    for tag in soup.find_all(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    for tag in soup.find_all(["nav", "footer", "aside"]):
        tag.decompose()
    for tag in soup.find_all(attrs={"role": ["navigation", "contentinfo", "banner"]}):
        tag.decompose()

    for anchor in soup.find_all("a", href=True):
        label = anchor.get_text(" ", strip=True)
        href = _useful_href(str(anchor["href"]), base_url)
        if href:
            replacement = f"{label} ({href})" if label and label != href else href
            anchor.replace_with(replacement)

    # Preserve semantic label/value relationships before flattening the DOM.
    # A single line is intentionally used so later date extraction cannot bind
    # a value from one timeline row to the label in another row.
    for row in soup.find_all("tr"):
        cells = [" ".join(cell.get_text(" ", strip=True).split()) for cell in row.find_all(["th", "td"])]
        cells = [cell for cell in cells if cell]
        if cells:
            row.replace_with(f"\n{' | '.join(cells)}\n")
    for term in list(soup.find_all("dt")):
        definition = term.find_next_sibling("dd")
        if definition is None:
            continue
        label = " ".join(term.get_text(" ", strip=True).split())
        value = " ".join(definition.get_text(" ", strip=True).split())
        term.replace_with(f"\n{label} | {value}\n")
        definition.decompose()

    for item in soup.find_all("li"):
        item.replace_with(f"\n- {item.get_text(' ', strip=True)}\n")
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        heading.insert_before("\n")
        heading.insert_after("\n")
    for block in soup.find_all(["p", "div", "section", "article", "main", "ul", "ol", "table", "tr"]):
        if isinstance(block, Tag):
            block.insert_after("\n")

    lines: list[str] = []
    previous_blank = True
    for raw_line in soup.get_text("\n").splitlines():
        line = _WHITESPACE.sub(" ", raw_line).strip()
        if line:
            lines.append(line)
            previous_blank = False
        elif not previous_blank:
            lines.append("")
            previous_blank = True
    return "\n".join(lines).strip()
