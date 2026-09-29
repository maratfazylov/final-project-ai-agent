"""Small real-API checks. No fabricated research report and no messages to Telegram users."""

import json
from research_agent.config import Settings
from research_agent.llm import LLM
from research_agent.models import Topics
from research_agent.prompts import DISCOVERY
from research_agent.retriever import Retriever, Corpus

settings = Settings.load()
topics = LLM(settings).json(
    DISCOVERY, {"area": "Компьютерное зрение: классификация изображений"}, Topics, "discovery"
)
print(json.dumps({"deepseek_discovery": "passed", "topics": len(topics.topics)}, ensure_ascii=False))
r = Retriever(settings)
results = {}
sources = []
for name, tool in r.tools.items():
    try:
        entries = tool.invoke({"query": "vision transformer classification"})
        results[name] = {"count": len(entries), "status": "passed"}
        if entries and not sources:
            from research_agent.models import Source

            source = Source.model_validate(entries[0])
            source.id, source.text = 1, source.abstract
            sources = [source]
    except Exception as exc:
        results[name] = {"status": "unavailable", "error": type(exc).__name__}
r.close()
print(json.dumps({"scientific_apis": results}, ensure_ascii=False))
if sources:
    corpus = Corpus(sources, settings)
    print(
        json.dumps(
            {"fastembed_faiss": "passed", "context_sources": len(corpus.context("image classification"))}
        )
    )
