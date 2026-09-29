"""Real metadata retrieval, deduplication, full-text extraction and FAISS RAG."""

import hashlib
import io
import math
import re
import time
from datetime import UTC, date, datetime

import httpx
from defusedxml import ElementTree as ET
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.tools import StructuredTool
from pypdf import PdfReader

from .config import Settings
from .models import Source


class SearchUnavailable(RuntimeError):
    pass


class HashEmbeddings(Embeddings):
    """Offline deterministic lexical baseline, used only in tests/demo or explicit fallback."""

    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text):
        vector = [0.0] * 512
        for word in re.findall(r"\w+", text.lower()):
            slot = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big") % len(vector)
            vector[slot] += 1
        norm = math.sqrt(sum(x * x for x in vector)) or 1
        return [x / norm for x in vector]


def normalize_title(value):
    return re.sub(r"[^\w]", "", value.casefold())


def deduplicate(sources):
    result = []
    titles, dois, arxiv_ids = set(), set(), set()
    for source in sources:
        title = normalize_title(source.title)
        doi = source.doi.lower().removeprefix("https://doi.org/")
        arxiv_id = re.sub(r"v\d+$", "", source.arxiv_id)
        if title in titles or (doi and doi in dois) or (arxiv_id and arxiv_id in arxiv_ids):
            continue
        titles.add(title)
        if doi:
            dois.add(doi)
        if arxiv_id:
            arxiv_ids.add(arxiv_id)
        result.append(source)
    return result


class Retriever:
    def __init__(self, settings: Settings, audit=None):
        self.settings = settings
        self.audit = audit or (lambda *a, **k: None)
        self.client = httpx.Client(
            timeout=40,
            follow_redirects=False,
            headers={"User-Agent": "GOSTResearchAgent/1.0 (academic project)"},
        )
        self.last_arxiv = 0.0
        self.first_year = date.today().year - 4
        self.tools = {
            "semantic_scholar": StructuredTool.from_function(
                self.semantic_scholar,
                name="semantic_scholar",
                description="Search real recent scientific papers",
            ),
            "arxiv": StructuredTool.from_function(
                self.arxiv, name="arxiv", description="Search real arXiv preprints and metadata"
            ),
        }

    def get(self, url, **kwargs):
        for attempt in range(3):
            response = self.client.get(url, **kwargs)
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt == 2:
                    raise SearchUnavailable(f"Научный API временно недоступен: HTTP {response.status_code}")
                delay = response.headers.get("Retry-After", "")
                time.sleep(min(float(delay) if delay.isdigit() else 2 ** (attempt + 1), 20))
                continue
            response.raise_for_status()
            return response
        raise SearchUnavailable("Не удалось получить ответ научного API")

    def semantic_scholar(self, query: str) -> list[dict]:
        headers = {"x-api-key": self.settings.scholar_key} if self.settings.scholar_key else {}
        response = self.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            headers=headers,
            params={
                "query": query,
                "limit": 40,
                "year": f"{self.first_year}-{date.today().year}",
                "fields": "title,authors,year,url,externalIds,abstract,venue,citationCount,isOpenAccess",
            },
        )
        papers = []
        for p in response.json().get("data", []):
            if not p.get("abstract") or not p.get("authors") or not p.get("year"):
                continue
            external = p.get("externalIds") or {}
            papers.append(
                Source(
                    title=p["title"],
                    authors=[x["name"] for x in p["authors"]],
                    year=p["year"],
                    url=p["url"],
                    doi=external.get("DOI", ""),
                    arxiv_id=external.get("ArXiv", ""),
                    venue=p.get("venue") or "",
                    abstract=p["abstract"],
                    provider="Semantic Scholar",
                    citations=p.get("citationCount") or 0,
                    metadata_verified=True,
                    retrieved_at=datetime.now(UTC).isoformat(),
                    quality_notes=[
                        "Метаданные получены из API; рецензирование по одному наличию записи не доказано"
                    ],
                ).model_dump()
            )
        return papers

    def arxiv(self, query: str) -> list[dict]:
        time.sleep(max(0, 3.1 - (time.monotonic() - self.last_arxiv)))
        self.last_arxiv = time.monotonic()
        terms = re.findall(r"[A-Za-z0-9-]+", query)[:7]
        expression = " AND ".join(f'all:"{term}"' for term in terms)
        if not expression:
            return []
        response = self.get(
            "https://export.arxiv.org/api/query",
            params={
                "search_query": f"({expression}) AND submittedDate:[{self.first_year}01010000 TO "
                f"{date.today().strftime('%Y%m%d')}2359]",
                "start": 0,
                "max_results": 35,
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
        )
        ns = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
        root = ET.fromstring(response.content)
        result = []
        for entry in root.findall("a:entry", ns):
            url = entry.findtext("a:id", "", ns).replace("http://", "https://")
            published = entry.findtext("a:published", "", ns)
            if "arxiv.org/abs/" not in url or not published:
                continue
            result.append(
                Source(
                    title=" ".join(entry.findtext("a:title", "", ns).split()),
                    authors=[x.findtext("a:name", "", ns) for x in entry.findall("a:author", ns)],
                    year=int(published[:4]),
                    url=url,
                    arxiv_id=url.rsplit("/abs/", 1)[-1],
                    doi=entry.findtext("x:doi", "", ns),
                    venue=entry.findtext("x:journal_ref", "", ns),
                    abstract=" ".join(entry.findtext("a:summary", "", ns).split()),
                    provider="arXiv",
                    metadata_verified=True,
                    retrieved_at=datetime.now(UTC).isoformat(),
                    quality_notes=["Препринт; наличие независимого рецензирования не установлено"],
                ).model_dump()
            )
        return result

    def collect(self, queries: list[str], cancelled=lambda: False) -> list[Source]:
        collected = []
        failures = []
        # Both mandated APIs are called; one can cover for a temporarily unavailable provider.
        for query in queries:
            for name, tool in self.tools.items():
                if cancelled():
                    raise InterruptedError("Исследование отменено")
                try:
                    papers = tool.invoke({"query": query})
                    collected.extend(Source.model_validate(p) for p in papers)
                    self.audit("search", provider=name, query=query, count=len(papers))
                except (httpx.HTTPError, SearchUnavailable, ET.ParseError, ValueError) as exc:
                    failures.append(f"{name}: {type(exc).__name__}")
                    self.audit("search_failed", provider=name, query=query, error=type(exc).__name__)
            if len(deduplicate(collected)) >= self.settings.max_sources:
                break
        # Broaden sparse queries without inventing missing records.
        if len(deduplicate(collected)) < self.settings.min_sources:
            for query in queries[:2]:
                if cancelled():
                    raise InterruptedError("Исследование отменено")
                try:
                    collected.extend(
                        Source.model_validate(p) for p in self.arxiv(" ".join(query.split()[:2]))
                    )
                except (httpx.HTTPError, SearchUnavailable, ValueError, ET.ParseError):
                    pass
        sources = [
            s
            for s in deduplicate(collected)
            if self.first_year <= s.year <= date.today().year and len(s.abstract) >= 120
        ]
        sources.sort(key=lambda s: (bool(s.venue), math.log1p(s.citations), s.year), reverse=True)
        sources = sources[: self.settings.max_sources]
        if len(sources) < self.settings.min_sources:
            raise SearchUnavailable(
                f"Найдено {len(sources)} пригодных уникальных источников; требуется "
                f"{self.settings.min_sources}. Уточните область или повторите позже. "
                + "; ".join(sorted(set(failures)))
            )
        for index, source in enumerate(sources, 1):
            source.id = index
            source.text = source.abstract
            # Download a bounded number of arXiv PDFs to avoid excess traffic.
            if source.arxiv_id and index <= 6:
                self.full_text(source)
        self.audit("corpus", sources=[s.model_dump() for s in sources], failed_providers=failures)
        return sources

    def full_text(self, source: Source):
        # Only trusted arXiv IDs. No user-controlled URLs or arbitrary redirects.
        if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z-]+/\d{7})(?:v\d+)?", source.arxiv_id):
            return
        try:
            time.sleep(max(0, 3.1 - (time.monotonic() - self.last_arxiv)))
            self.last_arxiv = time.monotonic()
            with self.client.stream("GET", "https://arxiv.org/pdf/" + source.arxiv_id) as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 15_000_000:
                        raise ValueError("PDF слишком большой")
            if not content.startswith(b"%PDF"):
                return
            reader = PdfReader(io.BytesIO(content))
            text = "\n".join(page.extract_text() or "" for page in reader.pages[:30])
            if len(text) > 1000:
                source.text = source.abstract + "\n" + text[:80_000]
                source.evidence_level = "full_text"
        except Exception as exc:
            source.quality_notes.append("PDF недоступен или не извлечен; анализ ограничен аннотацией")
            self.audit("pdf_unavailable", source_id=source.id, error=type(exc).__name__)

    def close(self):
        self.client.close()


class Corpus:
    def __init__(self, sources: list[Source], settings: Settings):
        self.sources = {s.id: s for s in sources}
        if settings.embedding_backend == "hash":
            embeddings = HashEmbeddings()
        elif settings.embedding_backend == "fastembed":
            from langchain_community.embeddings.fastembed import FastEmbedEmbeddings

            embeddings = FastEmbedEmbeddings(
                model_name=settings.embedding_model,
                cache_dir=str(settings.data_dir / "embeddings"),
                threads=2,
            )
        else:
            raise ValueError("EMBEDDING_BACKEND: fastembed или hash")
        documents = []
        for source in sources:
            for start in range(0, min(len(source.text), 80_000), 1800):
                documents.append(
                    Document(
                        page_content=source.text[start : start + 2200], metadata={"source_id": source.id}
                    )
                )
        self.index = FAISS.from_documents(documents, embeddings)

    def context(self, query: str, required_ids=()) -> dict:
        hits = self.index.similarity_search(query, k=12)
        snippets: dict[int, list[str]] = {}
        for doc in hits:
            snippets.setdefault(doc.metadata["source_id"], []).append(doc.page_content)
        for sid in required_ids:
            if sid in self.sources:
                snippets.setdefault(sid, []).insert(0, self.sources[sid].abstract)
        return {
            str(sid): {
                "title": self.sources[sid].title,
                "year": self.sources[sid].year,
                "evidence_level": self.sources[sid].evidence_level,
                "quality_notes": self.sources[sid].quality_notes,
                "text": (self.sources[sid].abstract + "\n" + "\n".join(parts))[:7000],
            }
            for sid, parts in snippets.items()
        }
