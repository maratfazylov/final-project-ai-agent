import pytest

from research_agent.config import Settings
from research_agent.models import Paragraph, Plan, Report, Section, Source
from research_agent.validator import SECTION_TITLES


@pytest.fixture
def settings(tmp_path):
    settings = Settings(
        token="offline-test-token",
        api_key="offline-test-key",
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        embedding_backend="hash",
    )
    settings.data_dir.mkdir()
    settings.output_dir.mkdir()
    return settings


@pytest.fixture
def sources():
    return [
        Source(
            id=i,
            title=f"TEST FIXTURE source {i}",
            authors=["Test Author"],
            year=2025,
            url=f"https://example.org/fixture/{i}",
            abstract="This is a synthetic fixture for software tests. "
            "The approach compares image representations using controlled measurements.",
            text="This is a synthetic fixture for software tests. "
            "The approach compares image representations using controlled measurements.",
            provider="TEST FIXTURE",
            metadata_verified=True,
        )
        for i in range(1, 16)
    ]


@pytest.fixture
def report(sources):
    sections = []
    for index, title in enumerate(SECTION_TITLES):
        paragraphs = []
        for i in range(3):
            source = sources[(index * 3 + i) % len(sources)]
            paragraphs.append(
                Paragraph(
                    text="Это синтетический абзац для проверки программного обеспечения. "
                    "Он используется только в автоматическом тесте, не является научным результатом.",
                    source_ids=[source.id],
                    evidence_quotes={str(source.id): "compares image representations"},
                )
            )
        sections.append(Section(title=title, paragraphs=paragraphs))
    return Report(
        title="Проверка генератора исследовательских отчетов",
        abstract="Тестовый документ для проверки "
        "оформления и конвертации. Источники синтетические и не предназначены для сдачи научной работы.",
        keywords=["ТЕСТ", "ВЕРСТКА", "DOCX", "PDF", "ВАЛИДАЦИЯ"],
        sections=sections,
        sources=sources,
        plan=Plan(
            goal="Проверка",
            questions=["Вопрос 1", "Вопрос 2"],
            hypotheses=["Гипотеза"],
            queries=["test one", "test two", "test three"],
            inclusion=[],
            limitations=[],
        ),
        brief={
            "organization": "Тестовая организация",
            "author": "Тестовый исполнитель",
            "supervisor": "Не назначен",
            "city": "Москва",
            "answers": {},
        },
        metrics={},
    )
