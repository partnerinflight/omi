"""SecondBrain checklist plugin registration."""
from __future__ import annotations

import json
from typing import Any

from .checklist import ChecklistError, ChecklistService
from .transport import TelegramSender
from .todos import TodoError

_TOOL_SCHEMA = {
    "name": "secondbrain_checklist",
    "description": (
        "Sync selected meaningful intake IDs into ToDos/Tasks.md (sync with [] also lists all tasks and adopts manual checkboxes). Markdown checkboxes are authoritative. Display selected existing SecondBrain router IDs as a Telegram checklist, or record "
        "done, dismiss, snooze, or reopen through the same locked state writer. For display, "
        "supply a caller-controlled idempotency request_id; a daily cron should use its local date. "
        "After confirmed delivery, direct user questions MUST receive a normal acknowledgement, never [SILENT]. Only scheduled cron briefings may use [SILENT] after confirmed=true or an explicitly empty selection. Never invent router IDs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["sync", "display", "done", "dismiss", "snooze", "reopen"],
            },
            "router_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exact existing router IDs. Display may contain several; state actions require one.",
            },
            "request_id": {
                "type": "string",
                "description": "Required for display; stable per caller request (for cron, the local date).",
            },
            "until": {
                "type": "string",
                "description": "Future local YYYY-MM-DD; required only for snooze.",
            },
        },
        "required": ["action", "router_ids"],
    },
}


def _markup(rows: list[list[dict[str, str]]]):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(button["text"], callback_data=button["callback_data"]) for button in row] for row in rows]
    )


def register(ctx):
    sender = TelegramSender()
    vault = ctx.get_config("vault", default="")
    chat_id = ctx.get_config("chat_id", default="")
    owner_user_id = ctx.get_config("owner_user_id", default="")
    timezone_name = ctx.get_config("timezone", default="America/Los_Angeles")
    service = None
    config_error = None
    if not all(str(value).strip() for value in (vault, chat_id, owner_user_id)):
        config_error = (
            "secondbrain-checklist is not configured: set plugin settings vault, chat_id, "
            "and owner_user_id"
        )
    else:
        try:
            service = ChecklistService(
                vault=str(vault),
                chat_id=int(chat_id),
                owner_user_id=int(owner_user_id),
                timezone_name=str(timezone_name),
                sender=sender,
                markup_factory=_markup,
            )
        except (TypeError, ValueError, ChecklistError) as exc:
            config_error = f"secondbrain-checklist configuration is invalid: {exc}"

    async def tool_handler(args: dict[str, Any], **_: Any) -> str:
        if service is None:
            return json.dumps({"success": False, "error": config_error})
        try:
            action = str(args.get("action", ""))
            router_ids = args.get("router_ids", [])
            if not isinstance(router_ids, list):
                raise ChecklistError("router_ids must be an array")
            if action == "sync":
                result = service.sync(router_ids)
            elif action == "display":
                result = await service.display(
                    router_ids=router_ids, request_id=str(args.get("request_id", ""))
                )
            else:
                if len(router_ids) != 1:
                    raise ChecklistError("State actions require exactly one router ID")
                result = service.change(
                    action=action, router_id=router_ids[0], until=args.get("until")
                )
            return json.dumps(result, ensure_ascii=False)
        except TodoError as exc:
            return json.dumps({"success": False, "error": str(exc)})
        except Exception:
            return json.dumps({"success": False, "error": "SecondBrain checklist operation failed"})

    def wire_telegram(application, adapter):
        del adapter
        from telegram.error import BadRequest
        from telegram.ext import CallbackQueryHandler

        async def on_callback(update, context):
            del context
            query = update.callback_query
            if query is None or query.message is None or query.from_user is None:
                return
            if service is None:
                await query.answer(config_error, show_alert=True)
                return
            try:
                result = service.callback(
                    data=query.data or "",
                    user_id=query.from_user.id,
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id,
                )
            except Exception:
                await query.answer("Could not confirm reminder state; check it before retrying", show_alert=True)
                return
            if result.ok:
                try:
                    await query.edit_message_reply_markup(reply_markup=result.markup)
                except BadRequest as exc:
                    if "message is not modified" not in str(exc).lower():
                        await query.answer(
                            result.message + "; state saved, but the buttons could not refresh",
                            show_alert=True,
                        )
                        return
                except Exception:
                    await query.answer(
                        result.message + "; state saved, but the buttons could not refresh",
                        show_alert=True,
                    )
                    return
            await query.answer(result.message, show_alert=not result.ok)

        application.add_handler(CallbackQueryHandler(on_callback, pattern=r"^sbr:"))

    ctx.register_tool(
        name="secondbrain_checklist",
        toolset="secondbrain_checklist",
        schema=_TOOL_SCHEMA,
        handler=tool_handler,
        is_async=True,
    )
    ctx.register_platform_handler("telegram", wire_telegram)
