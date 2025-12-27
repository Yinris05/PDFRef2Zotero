import argparse
import json
import urllib.parse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable, List, Optional

import requests

try:
    import fitz  # PyMuPDF
except Exception as exc:  # pragma: no cover - optional import
    fitz = None


DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
ARXIV_ID_RE = re.compile(r"\b(?:arxiv:)?(\d{4}\.\d{4,5})(v\d+)?\b", re.IGNORECASE)


@dataclass
class Reference:
    index: Optional[int]
    title: str
    raw: Optional[str] = None
    year: Optional[int] = None
    doi: Optional[str] = None
    authors: Optional[List[str]] = None
    sources: List[str] = field(default_factory=list)


def extract_text_pymupdf(pdf_path: str) -> str:
    if fitz is None:
        raise RuntimeError("PyMuPDF not available. Install with: pip install pymupdf")
    doc = fitz.open(pdf_path)
    chunks = []
    for page in doc:
        chunks.append(page.get_text("text"))
    return "\n".join(chunks)


def normalize_text(text: str) -> str:
    replacements = {
        "\ufb00": "ff",
        "\ufb01": "fi",
        "\ufb02": "fl",
        "\ufb03": "ffi",
        "\ufb04": "ffl",
        "\ufb05": "ft",
        "\ufb06": "st",
        "\u201c": "\"",
        "\u201d": "\"",
        "\u2018": "'",
        "\u2019": "'",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00a0": " ",
        "\u00b4": "'",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text


def find_references_block(text: str) -> str:
    # Basic heuristic: capture from "References" or "Bibliography" to end.
    lowered = text.lower()
    for marker in ["references", "bibliography", "参考文献"]:
        idx = lowered.find(marker)
        if idx != -1:
            return text[idx:]
    return text


def split_reference_lines(block: str) -> List[str]:
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    refs = []
    buff = []
    started = False
    for ln in lines:
        # A line starting with [1] or 1. likely begins a new reference.
        if re.match(r"^(\[\d+\]|\d+\.)\s+", ln):
            if buff and started:
                refs.append(" ".join(buff))
            started = True
            buff = [ln]
            continue
        if not started:
            continue
        buff.append(ln)
    if buff:
        refs.append(" ".join(buff))
    return refs


def parse_reference(ref_text: str) -> Reference:
    text = normalize_text(ref_text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(\w)-\s+(\w)", r"\1\2", text)
    idx_match = re.match(r"^\[(\d+)\]\s*", text)
    index = int(idx_match.group(1)) if idx_match else None
    if idx_match:
        text = text[idx_match.end():].strip()
    doi_match = DOI_RE.search(text)
    doi = doi_match.group(0).rstrip(").,;") if doi_match else None

    title = ""
    if "“" in text and "”" in text:
        first = text.find("“")
        last = text.rfind("”")
        if last > first:
            title = text[first + 1:last].strip()
    if not title:
        for pattern in (r"“([^”]+)”", r"\"([^\"]+)\"", r"'([^']+)'"):
            match = re.search(pattern, text)
            if match:
                title = match.group(1).strip()
                break

    year = None
    year_match = re.search(r"\b(19|20)\d{2}\b", text)
    if year_match:
        year = int(year_match.group(0))
    if not title:
        if year_match:
            after_year = text[year_match.end():]
            parts = [p.strip() for p in re.split(r"\.\s+|\.$", after_year) if p.strip()]
            for part in parts:
                lowered = part.lower()
                if re.match(r"^(pp|p)\.?($|\s)", lowered):
                    continue
                if len(part) < 6:
                    continue
                title = part
                break
            if not title:
                before_year = text[:year_match.start()]
                parts = [p.strip() for p in before_year.split(".") if p.strip()]
                for part in reversed(parts):
                    if part.startswith("["):
                        part = part.split("]", 1)[-1].strip()
                    lowered = part.lower()
                    if re.search(
                        r"\b(press|department|proceedings|journal|conference|transactions|letters|arxiv|preprint|publisher|society|acm|ieee|pmlr)\b",
                        lowered,
                    ):
                        continue
                    if len(part) < 6:
                        continue
                    title = part
                    break

    if not title:
        cleaned = DOI_RE.sub("", text)
        parts = [p.strip() for p in cleaned.split(".") if p.strip()]
        title = parts[0] if parts else ""
    title = title.strip(" ,.;:")
    return Reference(index=index, title=title, raw=text, year=year, doi=doi, authors=None)


def extract_references_from_pdf(pdf_path: str) -> List[Reference]:
    text = extract_text_pymupdf(pdf_path)
    block = find_references_block(text)
    refs_raw = split_reference_lines(block)
    return [parse_reference(r) for r in refs_raw]


def _normalize_title(title: str) -> str:
    lowered = title.lower()
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered


def _title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, _normalize_title(a), _normalize_title(b)).ratio()


def fill_arxiv_doi(refs: List[Reference]) -> None:
    total = len(refs)
    eligible = sum(
        1
        for ref in refs
        if not ref.doi
        and ref.raw
        and "arxiv" in ref.raw.lower()
        and "arxiv" not in ref.sources
    )
    hits = 0
    processed = 0
    print(f"[arXiv] start ({eligible}/{total} eligible)")
    if eligible == 0:
        print(f"[arXiv] done (0/{total} processed, hits 0)")
        return
    for ref in refs:
        if ref.doi or not ref.raw or "arxiv" in ref.sources:
            continue
        if "arxiv" not in ref.raw.lower():
            continue
        match = ARXIV_ID_RE.search(ref.raw)
        if match:
            arxiv_id = match.group(1)
            ref.doi = f"10.48550/arXiv.{arxiv_id}"
            hits += 1
            if "arxiv" not in ref.sources:
                ref.sources.append("arxiv")
        processed += 1
        if processed % 20 == 0:
            print(f"[arXiv] processed {processed}/{eligible} (hits {hits})")
    print(f"[arXiv] done ({processed}/{eligible} processed, hits {hits})")


def enrich_with_crossref(refs: List[Reference], rows: int = 5, threshold: float = 0.8) -> None:
    total = len(refs)
    eligible = sum(1 for ref in refs if not ref.doi and ref.title and "crossref" not in ref.sources)
    hits = 0
    processed = 0
    print(f"[Crossref] start ({eligible}/{total} eligible)")
    if eligible == 0:
        print(f"[Crossref] done (0/{total} processed, hits 0)")
        return
    for ref in refs:
        if ref.doi or not ref.title or "crossref" in ref.sources:
            continue
        items = []
        queries = [ref.raw, ref.title]
        for query in queries:
            if not query or len(query) < 10:
                continue
            params = {"query.bibliographic": query, "rows": rows}
            resp = requests.get("https://api.crossref.org/works", params=params, timeout=10)
            resp.raise_for_status()
            items.extend(resp.json().get("message", {}).get("items", []))
        if not items:
            continue
        best = None
        best_score = 0.0
        for item in items:
            item_title = (item.get("title") or [""])[0]
            score = _title_similarity(ref.title, item_title)
            if ref.year:
                issued = item.get("issued", {}).get("date-parts", [[None]])[0][0]
                if issued and abs(int(issued) - ref.year) > 1:
                    score *= 0.6
            if score > best_score:
                best_score = score
                best = item
        if best and best_score >= threshold:
            ref.doi = best.get("DOI")
            if ref.doi:
                hits += 1
                if "crossref" not in ref.sources:
                    ref.sources.append("crossref")
        processed += 1
        if processed % 10 == 0:
            print(f"[Crossref] processed {processed}/{eligible} (hits {hits})")
    print(f"[Crossref] done ({processed}/{eligible} processed, hits {hits})")


def enrich_with_semantic_scholar(
    refs: List[Reference], rows: int = 5, threshold: float = 0.8, api_key: Optional[str] = None
) -> None:
    base_url = "https://api.semanticscholar.org/graph/v1/paper/search"
    headers = {}
    if api_key:
        headers["x-api-key"] = api_key
    total = len(refs)
    eligible = sum(
        1
        for ref in refs
        if not ref.doi and ref.title and "semantic_scholar" not in ref.sources
    )
    hits = 0
    processed = 0
    rate_limited = False
    print(f"[Semantic Scholar] start ({eligible}/{total} eligible)")
    if eligible == 0:
        print(f"[Semantic Scholar] done (0/{total} processed, hits 0)")
        return
    for ref in refs:
        if ref.doi or not ref.title or "semantic_scholar" in ref.sources:
            continue
        query = ref.raw or ref.title
        if len(query) < 10:
            continue
        params = {
            "query": query,
            "limit": rows,
            "fields": "title,year,doi,externalIds",
        }
        resp = requests.get(base_url, params=params, headers=headers, timeout=10)
        if resp.status_code == 429:
            # Rate limit; skip remaining to avoid hammering.
            rate_limited = True
            break
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            continue
        best = None
        best_score = 0.0
        for item in data:
            item_title = item.get("title") or ""
            score = _title_similarity(ref.title, item_title)
            if ref.year and item.get("year"):
                if abs(int(item["year"]) - ref.year) > 1:
                    score *= 0.6
            if score > best_score:
                best_score = score
                best = item
        if best and best_score >= threshold:
            ref.doi = best.get("doi")
            if not ref.doi:
                ref.doi = (best.get("externalIds") or {}).get("DOI")
            if ref.doi:
                hits += 1
                if "semantic_scholar" not in ref.sources:
                    ref.sources.append("semantic_scholar")
        processed += 1
        if processed % 10 == 0:
            print(f"[Semantic Scholar] processed {processed}/{eligible} (hits {hits})")
    suffix = " (rate limited)" if rate_limited else ""
    print(f"[Semantic Scholar] done ({processed}/{eligible} processed, hits {hits}){suffix}")


def enrich_with_dblp(refs: List[Reference], rows: int = 10, threshold: float = 0.8) -> None:
    base_url = "https://dblp.org/search/publ/api"
    hits_count = 0
    total = len(refs)
    eligible = sum(1 for ref in refs if not ref.doi and ref.title and "dblp" not in ref.sources)
    processed = 0
    rate_limited = False
    print(f"[DBLP] start ({eligible}/{total} eligible)")
    if eligible == 0:
        print(f"[DBLP] done (0/{total} processed, hits 0)")
        return
    for ref in refs:
        if ref.doi or not ref.title or "dblp" in ref.sources:
            continue
        query = ref.raw or ref.title
        if len(query) < 10:
            continue
        params = {"q": query, "format": "json", "h": rows}
        resp = requests.get(base_url, params=params, timeout=10)
        if resp.status_code == 429:
            rate_limited = True
            break
        resp.raise_for_status()
        result = resp.json().get("result", {})
        hits = result.get("hits", {}).get("hit", [])
        if isinstance(hits, dict):
            hits = [hits]
        if not hits:
            continue
        best = None
        best_score = 0.0
        for hit in hits:
            info = hit.get("info", {})
            item_title = info.get("title") or ""
            score = _title_similarity(ref.title, item_title)
            if ref.year and info.get("year"):
                if abs(int(info["year"]) - ref.year) > 1:
                    score *= 0.6
            if score > best_score:
                best_score = score
                best = info
        if best and best_score >= threshold:
            ref.doi = best.get("doi")
            if ref.doi:
                hits_count += 1
                if "dblp" not in ref.sources:
                    ref.sources.append("dblp")
        processed += 1
        if processed % 10 == 0:
            print(f"[DBLP] processed {processed}/{eligible} (hits {hits_count})")
    suffix = " (rate limited)" if rate_limited else ""
    print(f"[DBLP] done ({processed}/{eligible} processed, hits {hits_count}){suffix}")


def enrich_with_openreview(refs: List[Reference], rows: int = 5, threshold: float = 0.8) -> None:
    base_url = "https://api.openreview.net/notes"
    total = len(refs)
    eligible = sum(
        1 for ref in refs if not ref.doi and ref.title and "openreview" not in ref.sources
    )
    hits = 0
    processed = 0
    rate_limited = False
    print(f"[OpenReview] start ({eligible}/{total} eligible)")
    if eligible == 0:
        print(f"[OpenReview] done (0/{total} processed, hits 0)")
        return
    for ref in refs:
        if ref.doi or not ref.title or "openreview" in ref.sources:
            continue
        query_title = ref.title
        if len(query_title) < 6:
            continue
        params = {"content.title": query_title, "limit": rows}
        resp = requests.get(base_url, params=params, timeout=10)
        if resp.status_code == 429:
            rate_limited = True
            break
        resp.raise_for_status()
        notes = resp.json().get("notes", [])
        if not notes and ref.raw:
            params = {"search": ref.raw, "limit": rows}
            resp = requests.get(base_url, params=params, timeout=10)
            resp.raise_for_status()
            notes = resp.json().get("notes", [])
        if not notes:
            continue
        best = None
        best_score = 0.0
        for note in notes:
            content = note.get("content") or {}
            item_title = content.get("title") or ""
            score = _title_similarity(ref.title, item_title)
            if ref.year and note.get("pdate"):
                year = int(time.gmtime(int(note["pdate"]) / 1000).tm_year)
                if abs(year - ref.year) > 1:
                    score *= 0.6
            if score > best_score:
                best_score = score
                best = content
        if best and best_score >= threshold:
            ref.doi = best.get("doi")
            if ref.doi:
                hits += 1
                if "openreview" not in ref.sources:
                    ref.sources.append("openreview")
        processed += 1
        if processed % 10 == 0:
            print(f"[OpenReview] processed {processed}/{eligible} (hits {hits})")
    suffix = " (rate limited)" if rate_limited else ""
    print(f"[OpenReview] done ({processed}/{eligible} processed, hits {hits}){suffix}")


def enrich_with_openalex(refs: List[Reference], rows: int = 10, threshold: float = 0.8) -> None:
    base_url = "https://api.openalex.org/works"
    total = len(refs)
    eligible = sum(
        1 for ref in refs if not ref.doi and ref.title and "openalex" not in ref.sources
    )
    hits = 0
    processed = 0
    rate_limited = False
    print(f"[OpenAlex] start ({eligible}/{total} eligible)")
    if eligible == 0:
        print(f"[OpenAlex] done (0/{total} processed, hits 0)")
        return
    for ref in refs:
        if ref.doi or not ref.title or "openalex" in ref.sources:
            continue
        query = ref.raw or ref.title
        if len(query) < 10:
            continue
        params = {"search": query, "per-page": rows}
        resp = requests.get(base_url, params=params, timeout=10)
        if resp.status_code == 429:
            rate_limited = True
            break
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            continue
        best = None
        best_score = 0.0
        for item in results:
            item_title = item.get("title") or ""
            score = _title_similarity(ref.title, item_title)
            item_year = item.get("publication_year")
            if ref.year and item_year:
                if abs(int(item_year) - ref.year) > 1:
                    score *= 0.6
            if score > best_score:
                best_score = score
                best = item
        if best and best_score >= threshold:
            doi = best.get("doi")
            if doi:
                ref.doi = doi.replace("https://doi.org/", "")
                hits += 1
                if "openalex" not in ref.sources:
                    ref.sources.append("openalex")
        processed += 1
        if processed % 10 == 0:
            print(f"[OpenAlex] processed {processed}/{eligible} (hits {hits})")
    suffix = " (rate limited)" if rate_limited else ""
    print(f"[OpenAlex] done ({processed}/{eligible} processed, hits {hits}){suffix}")


def build_pdf_url(ref: Reference) -> Optional[str]:
    if ref.raw and "arxiv" in ref.raw.lower():
        match = ARXIV_ID_RE.search(ref.raw)
        if match:
            return f"https://arxiv.org/pdf/{match.group(1)}.pdf"
    if ref.doi:
        return f"https://doi.org/{ref.doi}"
    return None


def build_scholar_url(title: str) -> str:
    query = urllib.parse.quote(title)
    return f"https://scholar.google.com/scholar?q={query}"


def zotero_create_items(
    api_key: str,
    library_type: str,
    library_id: str,
    refs: Iterable[Reference],
    collection_key: Optional[str] = None,
) -> List[str]:
    url = f"https://api.zotero.org/{library_type}s/{library_id}/items"
    headers = {
        "Zotero-API-Key": api_key,
        "Zotero-API-Version": "3",
        "Content-Type": "application/json",
    }
    items = []
    created_keys: List[str] = []
    for ref in refs:
        if not ref.title and not ref.doi:
            continue
        item = {
            "itemType": "journalArticle",
            "title": ref.title or "",
            "DOI": ref.doi or "",
            "creators": [],
        }
        if ref.authors:
            item["creators"] = [
                {"creatorType": "author", "firstName": "", "lastName": name}
                for name in ref.authors
            ]
        if collection_key:
            item["collections"] = [collection_key]
        items.append(item)
    if not items:
        return []

    # Zotero API rejects large payloads; send in batches.
    batch_size = 25
    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]
        resp = requests.post(url, headers=headers, data=json.dumps(batch), timeout=20)
        resp.raise_for_status()
        created = resp.json().get("successful", {})
        created_keys.extend([v["key"] for v in created.values()])
    return created_keys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import PDF references to Zotero and fetch PDFs.")
    parser.add_argument("pdf", nargs="?", help="Path to the PDF with references")
    parser.add_argument("--input", help="Read references from a JSONL file created by --output")
    parser.add_argument("--api-key", help="Zotero API key")
    parser.add_argument("--library-type", choices=["user", "group"], default="user")
    parser.add_argument("--library-id", help="User ID or Group ID")
    parser.add_argument("--collection-key", help="Optional collection key")
    parser.add_argument("--no-crossref", action="store_true", help="Skip Crossref enrichment")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to Zotero")
    parser.add_argument("--output", help="Write extracted references to a JSONL file")
    parser.add_argument("--no-zotero", action="store_true", help="Skip Zotero write and PDF fetch")
    parser.add_argument("--force-parse", action="store_true", help="Ignore existing output file and re-parse PDF")
    parser.add_argument("--crossref-threshold", type=float, default=0.8, help="Title similarity threshold for DOI match")
    parser.add_argument("--crossref-rows", type=int, default=10, help="Crossref candidate rows per query")
    parser.add_argument("--s2-fallback", action="store_true", help="Use Semantic Scholar if Crossref misses DOI")
    parser.add_argument("--s2-rows", type=int, default=5, help="Semantic Scholar candidate rows per query")
    parser.add_argument("--s2-api-key", help="Semantic Scholar API key (optional)")
    parser.add_argument("--dblp-fallback", action="store_true", help="Use DBLP if Crossref misses DOI")
    parser.add_argument("--dblp-rows", type=int, default=10, help="DBLP candidate rows per query")
    parser.add_argument("--openreview-fallback", action="store_true", help="Use OpenReview if Crossref misses DOI")
    parser.add_argument("--openreview-rows", type=int, default=5, help="OpenReview candidate rows per query")
    parser.add_argument("--openalex-fallback", action="store_true", help="Use OpenAlex if Crossref misses DOI")
    parser.add_argument("--openalex-rows", type=int, default=10, help="OpenAlex candidate rows per query")
    parser.add_argument("--missing-doi-links", help="Write Google Scholar links for entries missing DOI (CSV)")
    return parser.parse_args()


def read_references_from_jsonl(path: str) -> List[Reference]:
    refs: List[Reference] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            raw = data.get("raw")
            title = data.get("title", "")
            if raw:
                raw = normalize_text(raw)
            if title:
                title = normalize_text(title)
            refs.append(
                Reference(
                    index=data.get("index"),
                    title=title,
                    raw=raw,
                    year=data.get("year"),
                    doi=data.get("doi"),
                    authors=data.get("authors") or None,
                    sources=data.get("sources") or [],
                )
            )
    return refs


def main() -> int:
    args = parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.input:
        refs = read_references_from_jsonl(args.input)
    else:
        if args.output and os.path.exists(args.output) and not args.force_parse:
            refs = read_references_from_jsonl(args.output)
            print(f"Loaded existing references from {args.output}")
        else:
            if not args.pdf:
                print("Missing PDF path. Provide a PDF path or use --input.")
                return 2
            refs = extract_references_from_pdf(args.pdf)
    fill_arxiv_doi(refs)
    if not args.no_crossref and not args.dry_run:
        enrich_with_crossref(
            refs,
            rows=args.crossref_rows,
            threshold=args.crossref_threshold,
        )
    if args.s2_fallback and not args.dry_run:
        enrich_with_semantic_scholar(
            refs,
            rows=args.s2_rows,
            threshold=args.crossref_threshold,
            api_key=args.s2_api_key,
        )
    if args.dblp_fallback and not args.dry_run:
        enrich_with_dblp(
            refs,
            rows=args.dblp_rows,
            threshold=args.crossref_threshold,
        )
    if args.openreview_fallback and not args.dry_run:
        enrich_with_openreview(
            refs,
            rows=args.openreview_rows,
            threshold=args.crossref_threshold,
        )
    if args.openalex_fallback and not args.dry_run:
        enrich_with_openalex(
            refs,
            rows=args.openalex_rows,
            threshold=args.crossref_threshold,
        )

    if args.dry_run:
        for ref in refs:
            idx = f"[{ref.index}]" if ref.index is not None else "[?]"
            print(f"{idx} {ref.title} | {ref.doi}")
        return 0
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            for ref in refs:
                payload = {
                    "index": ref.index,
                    "title": ref.title,
                    "raw": ref.raw,
                    "year": ref.year,
                    "doi": ref.doi,
                    "pdf_url": build_pdf_url(ref),
                    "authors": ref.authors or [],
                    "sources": ref.sources,
                }
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    if args.missing_doi_links:
        missing = [ref for ref in refs if not ref.doi and ref.title]
        with open(args.missing_doi_links, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["index", "title", "scholar_url"])
            for ref in missing:
                writer.writerow([ref.index or "", ref.title, build_scholar_url(ref.title)])
        print(f"Wrote {len(missing)} Scholar links to {args.missing_doi_links}")
        if args.no_zotero:
            print(f"Wrote {len(refs)} references to {args.output}")
            return 0
    if not args.api_key or not args.library_id:
        print("Missing Zotero credentials. Provide --api-key and --library-id, or use --no-zotero.")
        return 2

    item_keys = zotero_create_items(
        api_key=args.api_key,
        library_type=args.library_type,
        library_id=args.library_id,
        refs=refs,
        collection_key=args.collection_key,
    )

    if not item_keys:
        print("No items created in Zotero.")
        return 0

    return 0



if __name__ == "__main__":
    raise SystemExit(main())
