import json
import threading
from typing import TypeVar

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from .config import Settings
from .prompts import SYSTEM

T = TypeVar("T", bound=BaseModel)


class LLM:
    def __init__(self, settings: Settings, audit=None):
        self.settings = settings
        self.calls = 0
        self.lock = threading.Lock()
        self.audit = audit or (lambda *a, **k: None)
        self.model = ChatOpenAI(
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=120,
            max_retries=2,
            max_tokens=6500,
            temperature=0.2,
        )
        # LCEL: prompt -> DeepSeek chat model -> output parser.
        self.chain = (
            ChatPromptTemplate.from_messages(
                [
                    ("system", SYSTEM),
                    ("user", "{instruction}\nJSON schema:\n{schema}\nДАННЫЕ:\n{data}"),
                ]
            )
            | self.model
            | StrOutputParser()
        )

    def json(self, instruction: str, data: dict, schema: type[T], role: str = "agent") -> T:
        error = ""
        for attempt in range(2):
            with self.lock:
                if self.calls >= self.settings.max_llm_calls:
                    raise RuntimeError("Достигнут лимит LLM-вызовов. Используйте /retry или упростите ТЗ.")
                self.calls += 1
            result = self.chain.invoke(
                {
                    "instruction": instruction + ("\nИсправь JSON: " + error if error else ""),
                    "schema": json.dumps(schema.model_json_schema(), ensure_ascii=False),
                    "data": json.dumps(data, ensure_ascii=False),
                }
            )
            clean = result.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            try:
                value = schema.model_validate_json(clean)
                self.audit(
                    "llm",
                    role=role,
                    attempt=attempt + 1,
                    model=self.settings.model,
                    prompt=instruction,
                    input=data,
                    output=value.model_dump(),
                )
                return value
            except (ValidationError, ValueError) as exc:
                error = str(exc)[:1500]
        raise ValueError("LLM дважды вернула некорректный ответ. Состояние исследования сохранено.")
