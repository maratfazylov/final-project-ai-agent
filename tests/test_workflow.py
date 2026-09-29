import threading
from unittest.mock import patch
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from research_agent.models import Plan, Section, Paragraph, Review, Topics, Followup
from research_agent.orchestrator import Abstract, Orchestrator
from research_agent.storage import Store
from research_agent.bot import TelegramAgent, QUESTIONS


def fake_json(instruction, data, schema, role="agent"):
    if schema is Plan:
        return Plan(
            goal="test",
            questions=["q1", "q2"],
            hypotheses=["h1"],
            queries=["test1", "test2", "test3"],
            inclusion=[],
            limitations=[],
        )
    if schema is Section:
        ids = data["required_ids"]
        paragraphs = [
            Paragraph(
                text="Это проверочный абзац исследования для автоматического тестирования. "
                "Он подтверждается только синтетическим тестовым фрагментом.",
                source_ids=[sid],
                evidence_quotes={str(sid): "compares image representations"},
            )
            for sid in ids
        ]
        while len(paragraphs) < 3:
            paragraphs.append(
                Paragraph(
                    text="Проверочная интерпретация ограничений исследовательской методики.",
                    kind="interpretation",
                )
            )
        return Section(title=data["section_title"], paragraphs=paragraphs)
    if schema is Review:
        return Review(accepted=True, issues=[])
    if schema is Abstract:
        return Abstract(
            abstract="Тестовая аннотация для автоматического тестирования программного обеспечения.",
            keywords=["test", "report", "research", "quality", "workflow"],
        )
    raise AssertionError(schema)


def test_end_to_end_graph_and_persistent_resume(settings, report, sources):
    store = Store(settings.data_dir)
    brief = {"topic": {"title": "Test"}, **report.brief}
    with (
        patch("research_agent.orchestrator.LLM.json", side_effect=fake_json),
        patch("research_agent.orchestrator.Retriever.collect", return_value=sources),
        patch("research_agent.orchestrator.Reporter.export", return_value={"pdf": "test.pdf"}) as exporter,
    ):
        assert Orchestrator(settings, store, "test-run").run(brief) == {"pdf": "test.pdf"}
        assert exporter.call_count == 1
        assert Orchestrator(settings, store, "test-run").run(brief, resume=True) == {"pdf": "test.pdf"}
        assert exporter.call_count == 1


def test_graph_cancel_before_llm(settings, report):
    event = threading.Event()
    event.set()
    with pytest.raises(InterruptedError):
        Orchestrator(settings, Store(settings.data_dir), "cancel-test", cancel=event).run(report.brief)


def test_graph_resumes_failed_node_without_repeating_search(settings, report, sources):
    store = Store(settings.data_dir)
    brief = {"topic": {"title": "Test"}, **report.brief}
    with (
        patch("research_agent.orchestrator.LLM.json", side_effect=fake_json),
        patch("research_agent.orchestrator.Retriever.collect", return_value=sources) as search,
        patch("research_agent.orchestrator.Reporter.export", side_effect=RuntimeError("temporary")),
    ):
        with pytest.raises(RuntimeError):
            Orchestrator(settings, store, "failed-test").run(brief)
        assert search.call_count == 1
    with (
        patch("research_agent.orchestrator.Retriever.collect") as search,
        patch("research_agent.orchestrator.Reporter.export", return_value={"pdf": "ready.pdf"}),
    ):
        assert Orchestrator(settings, store, "failed-test").run(brief, resume=True) == {"pdf": "ready.pdf"}
        assert search.call_count == 0


def test_critic_failure_blocks_export(settings, report, sources):
    def rejected(instruction, data, schema, role="agent"):
        if schema is Review:
            return Review(accepted=False, issues=["unsupported fact"])
        return fake_json(instruction, data, schema, role)

    with (
        patch("research_agent.orchestrator.LLM.json", side_effect=rejected),
        patch("research_agent.orchestrator.Retriever.collect", return_value=sources),
        patch("research_agent.orchestrator.Reporter.export") as export,
    ):
        with pytest.raises(RuntimeError, match="не прошел проверку"):
            Orchestrator(settings, Store(settings.data_dir), "critic-test").run(
                {"topic": {"title": "Test"}, **report.brief}
            )
        export.assert_not_called()


@pytest.mark.parametrize("change_prompt", [False, True])
def test_resume_reuses_approved_sections_only_with_same_inputs(
    settings, report, sources, monkeypatch, change_prompt
):
    from research_agent import prompts

    store = Store(settings.data_dir)
    brief = {"topic": {"title": "Test"}, **report.brief}

    def interrupted(instruction, data, schema, role="agent"):
        if schema is Section and data["section_title"] == "Обзор литературы":
            raise RuntimeError("simulated interruption")
        return fake_json(instruction, data, schema, role)

    with (
        patch("research_agent.orchestrator.LLM.json", side_effect=interrupted),
        patch("research_agent.orchestrator.Retriever.collect", return_value=sources),
    ):
        with pytest.raises(RuntimeError, match="simulated interruption"):
            Orchestrator(settings, store, "section-resume").run(brief)
    if change_prompt:
        monkeypatch.setattr(prompts, "SECTION", prompts.SECTION + " Updated instructions.")
    generated_titles = []

    def resumed(instruction, data, schema, role="agent"):
        if schema is Section:
            generated_titles.append(data["section_title"])
        return fake_json(instruction, data, schema, role)

    with (
        patch("research_agent.orchestrator.LLM.json", side_effect=resumed),
        patch("research_agent.orchestrator.Retriever.collect") as search,
        patch("research_agent.orchestrator.Reporter.export", return_value={"pdf": "ready.pdf"}),
    ):
        assert Orchestrator(settings, store, "section-resume").run(brief, resume=True) == {"pdf": "ready.pdf"}
        search.assert_not_called()
    assert ("ВВЕДЕНИЕ" in generated_titles) == change_prompt
    assert len(generated_titles) == (6 if change_prompt else 5)


@pytest.mark.asyncio
async def test_telegram_discovery_and_requirements(settings):
    agent = TelegramAgent.__new__(TelegramAgent)
    agent.settings, agent.store = settings, Store(settings.data_dir)
    agent.store.authorized(42, set(), claim=True)
    agent.jobs, agent.locks, agent.cancels = {}, {}, {}
    agent.send = AsyncMock()
    agent.launch = lambda *a, **kw: None

    def update(text):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=42, type="private"),
            effective_message=SimpleNamespace(text=text, reply_text=AsyncMock()),
        )

    topics = Topics(
        topics=[
            {"title": f"Topic {i}", "question": "Question", "method": "Review", "feasibility": "Available"}
            for i in range(4)
        ]
    )
    with patch(
        "research_agent.bot.LLM.json",
        side_effect=[topics, Followup(done=False, question="Какие метрики?"), Followup(done=True)],
    ):
        await agent.message(update("computer vision"), None)
        assert agent.store.get(42)["stage"] == "topics"
        await agent.message(update("2"), None)
        await agent.command(update("/approve"), None)
        answers = ["Сравнить методы", "Обзор", "Пять лет", "Вуз | Автор | Руководитель | Москва", "Нет"]
        for text in answers:
            await agent.message(update(text), None)
        assert agent.store.get(42)["stage"] == "adaptive"
        await agent.message(update("accuracy, latency"), None)
        state = agent.store.get(42)
        assert state["stage"] == "brief_ready"
        assert state["metadata"]["author"] == "Автор"
        assert len(state["followups"]) == 1
        await agent.command(update("/back"), None)
        assert agent.store.get(42)["stage"] == "questions"
        assert agent.store.get(42)["question_index"] == len(QUESTIONS) - 1
