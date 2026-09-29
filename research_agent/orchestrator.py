import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from . import prompts
from .analysis import analyze_csv
from .config import Settings
from .llm import LLM
from .models import Plan, Report, Review, Section, Source
from .reporter import Reporter
from .retriever import Corpus, Retriever
from .storage import Store
from .validator import SECTION_TITLES, ValidationFailed, validate_report, validate_section


class Abstract(BaseModel):
    abstract: str
    keywords: list[str] = Field(min_length=5, max_length=15)


class ResearchState(TypedDict, total=False):
    brief: dict
    plan: dict
    sources: list[dict]
    sections: list[dict]
    metrics: dict
    report: dict
    artifacts: dict


class Orchestrator:
    """Plan-and-Execute graph with persistent checkpoints and bounded correction."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        run_id: str,
        progress=lambda text: None,
        cancel: threading.Event | None = None,
    ):
        self.settings, self.store, self.run_id = settings, store, run_id
        self.progress = progress
        self.cancel = cancel or threading.Event()
        self.audit = lambda event, **kw: store.audit(run_id, event, **kw)
        self.llm = LLM(settings, self.audit)
        self.retriever = Retriever(settings, self.audit)
        self.corpus = None
        self.checkpoint_db = sqlite3.connect(settings.data_dir / "graph.sqlite", check_same_thread=False)
        graph = StateGraph(ResearchState)
        for name, method in (
            ("plan", self.plan),
            ("retrieve", self.retrieve),
            ("analyze", self.analyze),
            ("validate", self.validate),
            ("report", self.report),
        ):
            graph.add_node(name, method)
        graph.add_edge(START, "plan")
        graph.add_edge("plan", "retrieve")
        graph.add_edge("retrieve", "analyze")
        graph.add_edge("analyze", "validate")
        graph.add_edge("validate", "report")
        graph.add_edge("report", END)
        self.graph = graph.compile(checkpointer=SqliteSaver(self.checkpoint_db))

    def check(self):
        if self.cancel.is_set():
            raise InterruptedError("Исследование отменено")

    def announce(self, text):
        self.check()
        self.progress(text)
        self.audit("stage", message=text)

    def plan(self, state):
        self.announce("Формирую вопросы, гипотезы и план поиска.")
        brief = state["brief"]
        metrics = analyze_csv(Path(brief["csv_path"])) if brief.get("csv_path") else {}
        plan = self.llm.json(prompts.PLAN, {"brief": brief, "metrics": metrics}, Plan, "planner")
        return {"plan": plan.model_dump(), "metrics": metrics}

    def retrieve(self, state):
        self.announce("Ищу публикации в Semantic Scholar и arXiv; проверяю метаданные и доступные PDF.")
        sources = self.retriever.collect(state["plan"]["queries"], self.cancel.is_set)
        self.check()
        return {"sources": [s.model_dump() for s in sources]}

    def section_fingerprint(self, data, sources):
        payload = {
            "input": data,
            "sources": [source.model_dump() for source in sources],
            "system": prompts.SYSTEM,
            "section_prompt": prompts.SECTION,
            "review_prompt": prompts.REVIEW,
            "section_schema": Section.model_json_schema(),
            "review_schema": Review.model_json_schema(),
            "model": self.settings.model,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def approved_sections(self):
        path = self.settings.data_dir / "runs" / self.run_id / "audit.jsonl"
        approved = {}
        if path.exists():
            with path.open(encoding="utf-8") as file:
                for line in file:
                    try:
                        record = json.loads(line)
                        if record.get("event") == "section_complete" and record.get("fingerprint"):
                            section = Section.model_validate(record["section"])
                            review = Review.model_validate(record["review"])
                            if review.accepted and not review.issues:
                                approved[record["fingerprint"]] = section
                    except (ValueError, KeyError):
                        # A truncated final journal line after interruption cannot authorize reuse.
                        continue
        return approved

    def analyze(self, state):
        self.announce("Строю FAISS RAG и анализирую источники по разделам.")
        sources = [Source.model_validate(s) for s in state["sources"]]
        self.corpus = Corpus(sources, self.settings)
        sections = []
        approved = self.approved_sections()
        for index, title in enumerate(SECTION_TITLES):
            self.announce(f"Пишу раздел: {title}.")
            required = [s.id for i, s in enumerate(sources) if i % len(SECTION_TITLES) == index]
            context = self.corpus.context(state["brief"]["topic"]["title"] + " " + title, required)
            data = {
                "section_title": title,
                "brief": state["brief"],
                "plan": state["plan"],
                "metrics": state.get("metrics", {}),
                "sources": context,
                "required_ids": required,
                "rule": "Процитируй все required_ids содержательно в этом разделе",
            }
            data["corpus_summary"] = {
                "total_sources": len(sources),
                "full_text_extracted": sum(s.evidence_level == "full_text" for s in sources),
                "abstract_only": sum(s.evidence_level == "abstract" for s in sources),
                "scope": "Модели переданы аннотации и отобранные фрагменты; весь текст статей не передан",
                "actual_selection": "Автоматический поиск по queries плана, дедупликация, фильтр года/аннотации, "
                "ранжирование по наличию площадки/цитируемости/году. Полная независимая ручная "
                "проверка критериев inclusion не проводилась; косвенные/фоновые работы могут "
                "быть в корпусе. Не называй этот отбор строгим систематическим обзором.",
            }
            fingerprint = self.section_fingerprint(data, sources)
            cached = approved.get(fingerprint)
            if (
                cached
                and cached.title == title
                and not validate_section(cached, sources)
                and set(required).issubset(
                    {sid for paragraph in cached.paragraphs for sid in paragraph.source_ids}
                )
            ):
                sections.append(cached.model_dump())
                self.audit("section_reused", section=title, fingerprint=fingerprint)
                continue
            for attempt in range(3):
                self.check()
                section = self.llm.json(
                    prompts.SECTION, data, Section, "analyzer" if attempt == 0 else "repair"
                )
                errors = validate_section(section, sources)
                errors += [
                    f"Не использован обязательный источник {sid}"
                    for sid in required
                    if sid not in {x for paragraph in section.paragraphs for x in paragraph.source_ids}
                ]
                if section.title != title:
                    errors.append("Заголовок не совпадает с section_title")
                review = self.llm.json(
                    prompts.REVIEW,
                    {
                        "section": section.model_dump(),
                        "sources": context,
                        "metrics": state.get("metrics", {}),
                        "corpus_summary": data["corpus_summary"],
                        "brief": state["brief"],
                        "plan": state["plan"],
                    },
                    Review,
                    "critic",
                )
                if not errors and review.accepted:
                    break
                data["corrections"] = errors + review.issues
                data["draft"] = section.model_dump()
                self.audit(
                    "repair_required", section=title, attempt=attempt + 1, corrections=data["corrections"]
                )
            else:
                raise ValidationFailed(
                    f"Раздел {title} не прошел проверку: " + "; ".join(errors + review.issues)
                )
            sections.append(section.model_dump())
            # Per-section journal survives even if this graph node fails halfway.
            self.audit(
                "section_complete",
                section=section.model_dump(),
                review=review.model_dump(),
                fingerprint=fingerprint,
            )
        return {"sections": sections}

    def validate(self, state):
        self.announce("Проверяю ссылки, подтверждающие фрагменты и охват не менее 15 источников.")
        abstract = self.llm.json(
            prompts.ABSTRACT,
            {"brief": state["brief"], "sections": state["sections"], "metrics": state.get("metrics", {})},
            Abstract,
            "abstract",
        )
        report = Report(
            title=state["brief"]["topic"]["title"],
            abstract=abstract.abstract,
            keywords=abstract.keywords,
            sections=state["sections"],
            sources=state["sources"],
            plan=state["plan"],
            brief=state["brief"],
            metrics=state.get("metrics", {}),
        )
        validate_report(report, self.settings.min_sources)
        self.audit("content_validation_passed")
        return {"report": report.model_dump()}

    def report(self, state):
        self.announce("Создаю DOCX по ГОСТ, конвертирую в PDF и проверяю поля, страницы и содержание.")
        artifacts = Reporter(self.settings).export(
            Report.model_validate(state["report"]), self.settings.output_dir / self.run_id
        )
        self.check()
        self.audit("export_complete", artifacts=artifacts)
        return {"artifacts": artifacts}

    def run(self, brief: dict, resume=False):
        config = {"configurable": {"thread_id": self.run_id}, "recursion_limit": 20}
        try:
            snapshot = self.graph.get_state(config)
            value = None if resume and snapshot.values and snapshot.next else {"brief": brief}
            if resume and snapshot.values and not snapshot.next and snapshot.values.get("artifacts"):
                return snapshot.values["artifacts"]
            state = self.graph.invoke(value, config)
            return state["artifacts"]
        finally:
            self.retriever.close()
            self.checkpoint_db.close()
