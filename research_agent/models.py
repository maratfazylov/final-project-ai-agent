from typing import Literal

from pydantic import BaseModel, Field


class Topic(BaseModel):
    title: str
    question: str
    method: str
    feasibility: str


class Topics(BaseModel):
    topics: list[Topic] = Field(min_length=3, max_length=5)


class Followup(BaseModel):
    done: bool
    question: str = ""
    rationale: str = ""


class Plan(BaseModel):
    goal: str
    questions: list[str] = Field(min_length=2, max_length=5)
    hypotheses: list[str] = Field(min_length=1, max_length=3)
    queries: list[str] = Field(min_length=3, max_length=5)
    inclusion: list[str]
    limitations: list[str]


class Source(BaseModel):
    id: int = 0
    title: str
    authors: list[str] = Field(min_length=1)
    year: int
    url: str
    doi: str = ""
    arxiv_id: str = ""
    venue: str = ""
    abstract: str = ""
    text: str = ""
    provider: str
    citations: int = 0
    evidence_level: Literal["abstract", "full_text"] = "abstract"
    retrieved_at: str = ""
    metadata_verified: bool = False
    quality_notes: list[str] = Field(default_factory=list)


class Paragraph(BaseModel):
    text: str = Field(min_length=30)
    source_ids: list[int] = Field(default_factory=list)
    evidence_quotes: dict[str, str] = Field(default_factory=dict)
    kind: Literal["evidence", "interpretation", "method", "limitation"] = "evidence"


class Section(BaseModel):
    title: str
    paragraphs: list[Paragraph] = Field(min_length=3, max_length=10)


class Review(BaseModel):
    issues: list[str] = Field(
        description="Только блокирующие нарушения поддержки фактов; пусто при accepted=true"
    )
    accepted: bool
    notes: list[str] = Field(
        default_factory=list, description="Неблокирующие замечания; успешные проверки не перечислять"
    )


class Report(BaseModel):
    title: str
    abstract: str
    keywords: list[str] = Field(min_length=5, max_length=15)
    sections: list[Section]
    sources: list[Source]
    plan: Plan
    brief: dict
    metrics: dict = Field(default_factory=dict)
