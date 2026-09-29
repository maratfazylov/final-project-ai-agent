import asyncio
import logging
import threading
import uuid
from pathlib import Path

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import prompts
from .analysis import analyze_csv
from .config import Settings
from .llm import LLM
from .models import Followup, Topics
from .orchestrator import Orchestrator
from .storage import Store

HELP = """Я помогу выбрать тему и подготовить исследовательский отчет.
1. /new - новый проект. Напишите приблизительную область.
2. Выберите предложенную тему номером или напишите свою тему.
3. /approve - утвердить тему. Ответьте на вопросы по одному.
4. Утвердите итоговое ТЗ командой /run. Получите DOCX, PDF и журнал источников.

/status - этап и текущее ТЗ
/back - вернуться на предыдущий шаг уточнения
/retry - продолжить исследование после ошибки или перезапуска
/cancel - отменить текущую работу
/files - повторно получить готовые файлы
/id - ваш Telegram ID
/help - эта справка

Можно прикрепить CSV до /run: UTF-8, заголовок, до 5 МБ и 100000 строк.
Я выполняю обзор литературы и описательную статистику CSV. Для выводов использую
найденные статьи; собственные эксперименты и обучение моделей не имитирую.
Первое приватное /start закрепляет владельца, если allowlist не настроен."""

# Fixed essential requirements, followed by adaptive LLM questions.
QUESTIONS = [
    ("goal", "Какова цель и практический результат работы? Какие подходы или методы нужно сравнить?"),
    (
        "mode",
        "Как выполнять работу: обзор литературы или обзор с анализом вашего CSV? "
        "Если нужна реализация/обучение модели, укажите это: я согласую доступный вариант без имитации эксперимента.",
    ),
    (
        "scope",
        "Укажите границы исследования, критерии сравнения и ограничения. "
        "Источники ищутся за пять последних календарных лет; минимум 15.",
    ),
    (
        "metadata",
        "Пришлите данные титульного листа в одной строке через | :\n"
        "Название вуза/организации | Ваше ФИО и группа | Руководитель или «не назначен» | Город",
    ),
    (
        "local_rules",
        "Укажите дополнительные требования кафедры и регистрационные сведения НИР, если есть. "
        "Если их нет, напишите «нет». Базовое оформление: ГОСТ 7.32-2017, "
        "библиография ГОСТ 7.1-2003. Объем определяется содержанием, обычно 15-25 страниц; "
        "гарантировать число страниц заранее нельзя.",
    ),
]


class TelegramAgent:
    def __init__(self, settings: Settings):
        settings.require_credentials()
        self.settings = settings
        self.store = Store(settings.data_dir)
        self.store.recover()
        self.jobs: dict[int, asyncio.Task] = {}
        self.cancels: dict[int, threading.Event] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.application = (
            Application.builder().token(settings.token).concurrent_updates(8).post_init(self.init).build()
        )
        for command in (
            "start",
            "help",
            "new",
            "approve",
            "status",
            "run",
            "retry",
            "cancel",
            "back",
            "files",
            "id",
        ):
            self.application.add_handler(CommandHandler(command, self.command))
        self.application.add_handler(
            MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, self.upload)
        )
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, self.message)
        )
        self.application.add_error_handler(self.error)

    async def init(self, app):
        await app.bot.set_my_commands(
            [
                BotCommand(name, description)
                for name, description in (
                    ("start", "Начать"),
                    ("new", "Новая работа"),
                    ("approve", "Утвердить тему"),
                    ("run", "Выполнить утвержденное ТЗ"),
                    ("status", "Текущее состояние"),
                    ("retry", "Продолжить после ошибки"),
                    ("files", "Получить файлы"),
                    ("cancel", "Отменить"),
                    ("back", "Вернуться"),
                    ("help", "Справка"),
                    ("id", "Мой Telegram ID"),
                )
            ]
        )

    async def access(self, update, claim=False):
        if not update.effective_user or not update.effective_chat or update.effective_chat.type != "private":
            return False
        if not self.store.authorized(update.effective_user.id, self.settings.allowed_users, claim):
            await update.effective_message.reply_text(
                "Доступ закрыт. Владелец задается ALLOWED_USER_IDS или первым /start."
            )
            return False
        return True

    async def send(self, chat_id, text):
        for offset in range(0, len(text), 3800):
            await self.application.bot.send_message(chat_id=chat_id, text=text[offset : offset + 3800])

    async def command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        command = update.effective_message.text.split()[0].split("@")[0][1:]
        if command == "id":
            await update.effective_message.reply_text(f"Telegram ID: {update.effective_user.id}")
            return
        if not await self.access(update, claim=command == "start"):
            return
        uid = update.effective_user.id
        lock = self.locks.setdefault(uid, asyncio.Lock())
        async with lock:
            state = self.store.get(uid)
            if command in ("start", "help"):
                await self.send(uid, HELP + "\n\nТекущий этап: " + state["stage"])
            elif command == "status":
                await self.send(uid, f"Этап: {state['stage']}\n" + self.summary(state))
            elif command == "cancel":
                if uid in self.cancels:
                    self.cancels[uid].set()
                    await self.send(
                        uid, "Отмена запрошена. Текущий API-вызов завершится, затем работа остановится."
                    )
                else:
                    state["stage"] = "cancelled"
                    self.store.save(uid, state)
                    await self.send(uid, "Работа отменена. /new - новый проект.")
            elif uid in self.jobs:
                await self.send(uid, "Работа выполняется. Доступны /status и /cancel.")
            elif command == "new":
                state = {"stage": "area", "answers": {}, "followups": []}
                self.store.save(uid, state)
                await self.send(
                    uid,
                    "Напишите приблизительную область исследования, например: компьютерное зрение в медицине.",
                )
            elif command == "approve":
                if state["stage"] != "approve_topic":
                    await self.send(uid, "Сначала выберите тему из предложений или напишите собственную.")
                else:
                    state.update(stage="questions", question_index=0)
                    self.store.save(uid, state)
                    await self.send(uid, QUESTIONS[0][1])
            elif command in ("run", "retry"):
                if state["stage"] not in (("brief_ready",) if command == "run" else ("failed",)):
                    await self.send(uid, "Для /run сначала согласуйте ТЗ; /retry доступен после ошибки.")
                    return
                if state["answers"].get("csv_required") and not state.get("csv_path"):
                    await self.send(
                        uid, "Прикрепите CSV, который вы выбрали для анализа, затем повторите команду."
                    )
                    return
                if not self.settings.soffice:
                    await self.send(
                        uid, "Для DOCX → PDF нужен LibreOffice. Владелец должен задать SOFFICE_PATH в .env."
                    )
                    return
                self.launch(uid, state, resume=command == "retry")
                await self.send(
                    uid, "ТЗ утверждено. Начинаю автономное исследование; этапы буду сообщать здесь."
                )
            elif command == "back":
                if state["stage"] in ("questions", "adaptive", "brief_ready"):
                    state["question_index"] = max(0, state.get("question_index", len(QUESTIONS)) - 1)
                    state["stage"] = "questions"
                    state["followups"] = []
                    self.store.save(uid, state)
                    await self.send(uid, QUESTIONS[state["question_index"]][1])
                else:
                    await self.send(uid, "Возврат доступен во время уточнения ТЗ. /new начнет заново.")
            elif command == "files":
                if state.get("artifacts"):
                    await self.deliver(uid, state["artifacts"])
                else:
                    await self.send(uid, "Готовых файлов еще нет.")

    def summary(self, state):
        topic = state.get("topic", {}).get("title", "Тема не выбрана")
        lines = ["Тема: " + topic]
        lines.extend(f"{key}: {value}" for key, value in state.get("answers", {}).items())
        lines.extend(
            f"Уточнение: {item['question']}\nОтвет: {item['answer']}" for item in state.get("followups", [])
        )
        if state.get("csv_path"):
            lines.append("CSV приложен")
        return "\n".join(lines)

    async def message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.access(update):
            return
        uid, text = update.effective_user.id, update.effective_message.text.strip()
        if len(text) > 8000:
            await self.send(uid, "Сообщение слишком длинное; максимум 8000 символов.")
            return
        async with self.locks.setdefault(uid, asyncio.Lock()):
            state = self.store.get(uid)
            try:
                if state["stage"] == "area":
                    state["area"] = text
                    llm = LLM(self.settings)
                    await self.send(uid, "Подбираю выполнимые темы по вашей области.")
                    topics = await asyncio.to_thread(
                        llm.json, prompts.DISCOVERY, {"area": text}, Topics, "discovery"
                    )
                    state.update(stage="topics", topics=topics.model_dump()["topics"])
                    self.store.save(uid, state)
                    lines = [
                        f"{i}. {topic.title}\nВопрос: {topic.question}\nМетод: {topic.method}\n"
                        f"Выполнимость: {topic.feasibility}"
                        for i, topic in enumerate(topics.topics, 1)
                    ]
                    await self.send(uid, "\n\n".join(lines) + "\n\nВыберите номер или напишите свою тему.")
                elif state["stage"] in ("topics", "approve_topic"):
                    if text.isdigit():
                        choice = int(text) - 1
                        if choice < 0 or choice >= len(state["topics"]):
                            await self.send(uid, "Укажите номер из списка.")
                            return
                        topic = state["topics"][choice]
                    else:
                        topic = {
                            "title": text,
                            "question": "Уточняется",
                            "method": "Уточняется",
                            "feasibility": "Уточняется",
                        }
                    state.update(stage="approve_topic", topic=topic)
                    self.store.save(uid, state)
                    await self.send(
                        uid,
                        "Выбрана тема: "
                        + topic["title"]
                        + "\n/approve - утвердить, либо напишите другую тему.",
                    )
                elif state["stage"] == "questions":
                    index = state["question_index"]
                    key = QUESTIONS[index][0]
                    if key == "metadata":
                        parts = [x.strip() for x in text.split("|")]
                        if len(parts) != 4 or any(not x for x in parts):
                            await self.send(
                                uid, "Нужно 4 поля через |, без пустых значений.\n" + QUESTIONS[index][1]
                            )
                            return
                        state["metadata"] = dict(zip(("organization", "author", "supervisor", "city"), parts))
                    if key == "mode":
                        state["answers"]["csv_required"] = "csv" in text.casefold()
                    state["answers"][key] = text
                    state["question_index"] += 1
                    self.store.save(uid, state)
                    if state["question_index"] < len(QUESTIONS):
                        await self.send(uid, QUESTIONS[state["question_index"]][1])
                    else:
                        await self.next_question(uid, state)
                elif state["stage"] == "adaptive":
                    state["followups"].append({"question": state["pending_question"], "answer": text})
                    self.store.save(uid, state)
                    await self.next_question(uid, state)
                elif state["stage"] == "brief_ready":
                    await self.send(
                        uid, "ТЗ готово. /run - начать, /back - исправить последний ответ, /new - новая тема."
                    )
                else:
                    await self.send(uid, "Используйте /status, /retry, /files или /new.")
            except Exception as exc:
                # Do not expose exception strings containing request URLs or credentials.
                self.store.audit("dialog", "dialog_error", user_id=uid, error=type(exc).__name__)
                await self.send(
                    uid,
                    f"Не удалось обработать ответ ({type(exc).__name__}). "
                    "Ваш предыдущий этап сохранен. Повторите сообщение.",
                )

    async def next_question(self, uid, state):
        if len(state["followups"]) >= 3:
            return await self.ready(uid, state)
        llm = LLM(self.settings)
        answer = await asyncio.to_thread(
            llm.json,
            prompts.FOLLOWUP,
            {
                "topic": state["topic"],
                "answers": state["answers"],
                "previous": state["followups"],
                "has_csv": bool(state.get("csv_path")),
            },
            Followup,
            "requirements",
        )
        if answer.done or not answer.question.strip():
            await self.ready(uid, state)
        else:
            state.update(stage="adaptive", pending_question=answer.question)
            self.store.save(uid, state)
            await self.send(uid, answer.question)

    async def ready(self, uid, state):
        state["stage"] = "brief_ready"
        self.store.save(uid, state)
        await self.send(
            uid,
            "Проверьте итоговое ТЗ:\n"
            + self.summary(state)
            + "\n\nФормат: аналитический обзор, при наличии CSV - описательная статистика. "
            "Минимум 15 реальных источников, DOCX/PDF и проверка оформления. "
            "Оформление кафедры сверх реализованного профиля требует ручного нормоконтроля. "
            "/run - утвердить и выполнить; /back - изменить последний ответ.",
        )

    async def upload(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.access(update):
            return
        uid, attachment = update.effective_user.id, update.effective_message.document
        async with self.locks.setdefault(uid, asyncio.Lock()):
            state = self.store.get(uid)
            if state["stage"] in ("running", "done", "cancelled") or uid in self.jobs:
                await self.send(uid, "Прикрепить CSV можно до начала исследования. /new - новый проект.")
                return
            if (
                not (attachment.file_name or "").lower().endswith(".csv")
                or (attachment.file_size or 0) > 5_000_000
            ):
                await self.send(uid, "Поддерживается CSV до 5 МБ. PDF и другие файлы здесь не принимаются.")
                return
            directory = self.settings.data_dir / "uploads" / str(uid)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (uuid.uuid4().hex + ".csv")
            try:
                file = await attachment.get_file()
                await file.download_to_drive(path)
                metrics = await asyncio.to_thread(analyze_csv, path)
                state["csv_path"] = str(path)
                # A new input invalidates an old checkpoint.
                if state["stage"] == "failed":
                    state.pop("run_id", None)
                    state["stage"] = "brief_ready"
                self.store.save(uid, state)
                await self.send(
                    uid, f"CSV принят: {metrics['rows']} строк, {len(metrics['columns'])} числовых столбцов."
                )
            except Exception as exc:
                path.unlink(missing_ok=True)
                await self.send(
                    uid, f"CSV не принят ({type(exc).__name__}). Проверьте UTF-8 и числовые столбцы."
                )

    def launch(self, uid, state, resume=False):
        if not resume or not state.get("run_id"):
            state["run_id"] = uuid.uuid4().hex[:12]
        state["stage"] = "running"
        self.store.save(uid, state)
        self.cancels[uid] = threading.Event()
        task = asyncio.create_task(self.research(uid, state, resume))
        self.jobs[uid] = task
        self.application.create_task(self.monitor(task), name=f"research-{uid}")

    async def monitor(self, task):
        await task

    async def research(self, uid, state, resume):
        loop = asyncio.get_running_loop()

        def progress(text):
            self.store.audit(state["run_id"], "progress", text=text)
            asyncio.run_coroutine_threadsafe(self.send(uid, text), loop)

        brief = {
            "topic": state["topic"],
            **state["metadata"],
            "answers": state["answers"],
            "followups": state["followups"],
            "csv_path": state.get("csv_path", ""),
        }
        try:
            orchestrator = Orchestrator(
                self.settings, self.store, state["run_id"], progress, self.cancels[uid]
            )
            artifacts = await asyncio.to_thread(orchestrator.run, brief, resume)
            state.update(stage="done", artifacts=artifacts)
            self.store.save(uid, state)
            await self.send(
                uid,
                "Отчет готов и прошел автоматические проверки. "
                "Перед сдачей проверьте титульный лист, формулировки и правила кафедры. "
                "Автоматический нормоконтроль не является сертификацией соответствия ГОСТ.",
            )
            await self.deliver(uid, artifacts)
        except InterruptedError:
            state["stage"] = "cancelled"
            self.store.save(uid, state)
            await self.send(uid, "Работа остановлена. /new - новое исследование.")
        except Exception as exc:
            state.update(stage="failed", error=type(exc).__name__)
            self.store.save(uid, state)
            self.store.audit(state["run_id"], "failure", error=type(exc).__name__)
            from .retriever import SearchUnavailable
            from .validator import ValidationFailed

            safe = (
                str(exc)[:1800]
                if isinstance(exc, (SearchUnavailable, ValidationFailed))
                else type(exc).__name__
            )
            await self.send(
                uid,
                "Исследование остановилось: "
                + safe
                + "\nСостояние сохранено. /retry продолжит с последнего узла графа; /new сменит тему.",
            )
        finally:
            self.jobs.pop(uid, None)
            self.cancels.pop(uid, None)

    async def deliver(self, uid, artifacts):
        for name in ("docx", "pdf", "quality", "sources", "evidence"):
            path = Path(artifacts[name])
            if path.exists():
                with path.open("rb") as file:
                    await self.application.bot.send_document(uid, file, filename=path.name)

    async def error(self, update, context):
        logging.getLogger(__name__).error("Telegram handler failed: %s", type(context.error).__name__)

    def run(self):
        # HTTP logs contain the Telegram token inside the URL, so do not enable them.
        for name in ("httpx", "httpcore", "telegram", "openai"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
        self.application.run_polling(drop_pending_updates=False, allowed_updates=["message"])
