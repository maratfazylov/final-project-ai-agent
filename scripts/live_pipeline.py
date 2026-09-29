"""Real full workflow check, stored under output/acceptance. This uses the configured LLM key."""

import json
from pathlib import Path
from research_agent.config import Settings
from research_agent.orchestrator import Orchestrator
from research_agent.storage import Store

settings = Settings.load()
settings.max_sources = 15
brief = json.loads(Path("docs/brief.example.json").read_text(encoding="utf-8"))
brief.update(
    organization="Проверка программного обеспечения",
    author="Тестовый запуск агента",
    supervisor="Не назначен",
    city="Москва",
)
brief["answers"]["goal"] = (
    "Сравнить подходы к классификации изображений по доступным публикациям, "
    "без собственных экспериментов и без вымышленных числовых результатов"
)
try:
    artifacts = Orchestrator(
        settings, Store(settings.data_dir), "acceptance", lambda text: print(text, flush=True)
    ).run(brief, resume=True)
    print(json.dumps({"full_pipeline": "passed", "artifacts": artifacts}, ensure_ascii=False), flush=True)
except Exception as exc:
    from research_agent.validator import ValidationFailed
    from research_agent.retriever import SearchUnavailable

    print(
        json.dumps(
            {
                "full_pipeline": "failed",
                "error": type(exc).__name__,
                "details": str(exc) if isinstance(exc, (ValidationFailed, SearchUnavailable)) else "",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    raise SystemExit(1)
