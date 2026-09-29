import argparse
import json
import logging
import os
from pathlib import Path

import httpx

from .config import Settings


def doctor(settings, online=False):
    result = {
        "credentials_configured": bool(settings.token and settings.api_key),
        "model": settings.model,
        "libreoffice_configured": bool(settings.soffice),
        "libreoffice_exists": bool(settings.soffice and Path(settings.soffice).exists()),
        "embedding_backend": settings.embedding_backend,
        "min_sources": settings.min_sources,
    }
    if online:
        settings.require_credentials()
        with httpx.Client(timeout=30) as client:
            try:
                response = client.get("https://api.telegram.org/bot" + settings.token + "/getMe")
                data = response.json()
                result["telegram"] = {
                    "ok": data.get("ok", False),
                    "username": data.get("result", {}).get("username", ""),
                    "http_status": response.status_code,
                }
                response = client.get(
                    settings.base_url.rstrip("/") + "/models",
                    headers={"Authorization": "Bearer " + settings.api_key},
                )
                ids = [entry["id"] for entry in response.json().get("data", [])]
                result["deepseek"] = {
                    "http_status": response.status_code,
                    "models": ids,
                    "configured_model_available": settings.model in ids,
                }
            except Exception as exc:
                result["network_error"] = type(exc).__name__
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not all((result["credentials_configured"], result["libreoffice_configured"])):
        return 1
    if online and (
        not result.get("telegram", {}).get("ok")
        or not result.get("deepseek", {}).get("configured_model_available")
    ):
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description="Telegram researcher with DeepSeek and GOST reporting")
    parser.add_argument("command", choices=["bot", "doctor", "research"])
    parser.add_argument(
        "--online", action="store_true", help="Verify credentials through read-only API calls"
    )
    parser.add_argument("--brief", type=Path, help="JSON research brief for non-Telegram execution")
    parser.add_argument("--run-id", default="cli-research")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    settings = Settings.load()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.command == "doctor":
        raise SystemExit(doctor(settings, args.online))
    settings.require_credentials()
    if args.command == "research":
        from .orchestrator import Orchestrator
        from .storage import Store

        if not args.brief:
            parser.error("Для research укажите --brief path.json")
        if not args.run_id.replace("-", "").replace("_", "").isalnum():
            parser.error("run-id должен содержать только буквы, цифры, - и _")
        brief = json.loads(args.brief.read_text(encoding="utf-8"))
        for field in ("organization", "author", "supervisor", "city", "topic"):
            if not brief.get(field):
                parser.error(f"В ТЗ отсутствует {field}")
        artifacts = Orchestrator(settings, Store(settings.data_dir), args.run_id, print).run(
            brief, args.resume
        )
        print(json.dumps(artifacts, ensure_ascii=False, indent=2))
        return
    # A single polling process owns this bot. Concurrent launches are rejected locally.
    import fcntl

    with (settings.data_dir / "bot.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Этот бот уже запущен в другом процессе")
        (settings.data_dir / "bot.pid").write_text(str(os.getpid()), encoding="utf-8")
        from .bot import TelegramAgent

        try:
            TelegramAgent(settings).run()
        finally:
            (settings.data_dir / "bot.pid").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
