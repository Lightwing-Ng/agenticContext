"""Local resources routes: the stored media, prompt, and chat-history surface.

These routes read the local catalogs the application factory builds and never reach
for a browser, an Agent, or a cache worker. Serialization of one stored item into its
public shape lives here too, so a template global and a JSON response cannot drift.
"""

# Code version: v1.20.2-codex.0

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import quote, urlsplit

from flask import (
    Blueprint,
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    stream_with_context,
    url_for,
)

from app.core.agent import is_loopback_address
from app.core.foundation import APP_VERSION
from app.core.storage import (
    attach_media_references,
    build_chat_history_markdown,
    format_captured_at_label,
    format_captured_at_timestamp_label,
    format_chat_message_timestamp_label,
    local_file_manager_label,
    media_route_relative_path,
    normalize_browser_filters,
    open_directory_path,
    prompt_content_key,
    prompt_pointer_key,
    query_chat_history,
    resolve_browser_media_path,
    resolve_resource_for_reveal,
    reveal_media_path,
)
from app.web.public_errors import public_exception_message
from app.web.cache_sources import get_cache_source_label
from app.web.local_resource_navigation import (
    local_resource_source_context,
    wechat_contact_sidebar_context,
)
from app.web.presentation import (
    build_browser_search_suggestions,
    cached_message_source_links,
    format_media_size,
    format_wechat_source_clock,
    render_cached_message,
    render_prompt_markdown,
)


LOCAL_RESOURCES_BLUEPRINT_NAME = "local_resources"
_WECHAT_CONTACT_ID = re.compile(r"wechat:contact:[0-9a-f]{64}")

_PROMPT_PUBLIC_ERRORS = (
    "A valid cached message pointer is required.",
    "Only user messages can be saved as prompts.",
    "A cached media prompt requires its source conversation.",
    "This media item has no indexed prompt.",
    "A remark is required.",
    "Cached media was not found.",
    "The cached message is no longer available.",
    "The saved prompt was not found.",
    "The saved prompt is no longer available.",
    "The removed prompt can no longer be restored.",
)
_MEDIA_PUBLIC_ERRORS = (
    "ChatGPT temporary media is exempt from browser deletion.",
    "Invalid deleted-media path.",
    "Removed media has an invalid restore path.",
    "The original cache path is occupied.",
)
_WECHAT_RESOURCE_PUBLIC_ERRORS = (
    "This resource reference is unavailable or needs refreshing.",
    "The resource account or conversation could not be verified.",
    "This source preserves attachment metadata only.",
    "The local WeChat file or folder is no longer available.",
    "Not found on this Mac",
)



def _wechat_contact_return_url(value: str) -> str:
    """Keep a contact's return destination within the existing local chat page."""
    fallback = url_for(
        "local_resources.browser", view="text", source="wechat", session_view="1"
    )
    if not isinstance(value, str) or len(value) > 4096 or "\\" in value:
        return fallback
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return fallback
    try:
        parsed = urlsplit(value)
    except ValueError:
        return fallback
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.path != url_for("local_resources.browser")
    ):
        return fallback
    return value


def wechat_contact_analysis_url(contact: dict | None, message_id: str = "") -> str:
    """Link verified objects while leaving source-local names and system rows alone."""
    if (
        not isinstance(contact, dict)
        or contact.get("platform") != "wechat"
        or contact.get("identity_status") != "verified"
        or not isinstance(contact.get("id"), str)
        or _WECHAT_CONTACT_ID.fullmatch(contact["id"]) is None
    ):
        return ""
    return_url = request.full_path.rstrip("?")
    if message_id:
        return_url += "#" + quote("wechat_message_" + message_id, safe="-_")
    return url_for(
        "local_resources.wechat_contact_analysis",
        contact_id=contact["id"],
        sort="oldest",
        return_to=return_url,
    )


@dataclass(frozen=True, slots=True)
class LocalResourceRouteContext:
    """The local catalogs these routes read; no browser or Agent capability belongs here."""

    media_catalog: Any
    prompt_store: Any


def browser_media_url(relative_path: str) -> str:
    """Return the stable public URL for one stored media path."""
    return url_for(
        f"{LOCAL_RESOURCES_BLUEPRINT_NAME}.browser_media",
        relative_path=media_route_relative_path(relative_path),
    )


def media_prompt_conversation_id(item) -> str:
    """Return the conversation that owns the prompt recorded with one media item."""
    return item.chatgpt_session_key or f"media:{item.stable_id}"


def media_prompt_key(item) -> str:
    """Return the saved-prompt key of the prompt recorded with one media item."""
    return prompt_content_key(item.source, media_prompt_conversation_id(item), item.prompt_markdown)


def serialize_media_item(item) -> dict[str, Any]:
    """Serialize one browser item without exposing local absolute paths."""
    media_url = (
        url_for(f"{LOCAL_RESOURCES_BLUEPRINT_NAME}.browser_deleted_preview", stable_id=item.stable_id)
        if item.is_deleted
        else url_for(
            f"{LOCAL_RESOURCES_BLUEPRINT_NAME}.browser_media",
            relative_path=media_route_relative_path(item.relative_path),
        )
    )
    return {
        "id": item.stable_id,
        "source": item.source,
        "source_label": get_cache_source_label(item.source),
        "media_kind": item.media_kind,
        "media_kind_label": item.media_kind.title(),
        "relative_path": item.relative_path,
        "filename": item.filename,
        "title": item.title,
        "description": item.description,
        "prompt_markdown": item.prompt_markdown,
        "creator": item.creator,
        "project_name": item.project_name,
        "source_url": item.source_url,
        "resource_key": item.resource_key,
        "captured_at_label": format_captured_at_label(item.captured_at),
        "content_bytes": item.content_bytes,
        "size_label": format_media_size(item.content_bytes),
        "media_url": media_url,
        "preview_url": media_url,
        "alt_text": item.alt_text,
        "width": item.width,
        "height": item.height,
        "is_deleted": item.is_deleted,
    }

def serialize_prompt_item(item) -> dict[str, Any]:
    """Serialize one resolved prompt without exposing duplicated storage."""
    return {
        "id": item.stable_id,
        "source": item.source,
        "source_label": get_cache_source_label(item.source),
        "conversation_id": item.conversation_id,
        "message_key": item.message_key,
        "conversation_title": item.conversation_title,
        "conversation_url": item.conversation_url,
        "author_label": item.author_label,
        "content_text": item.content_text,
        "captured_at": item.captured_at,
        "added_at": item.added_at,
        "remarks": list(item.remarks),
        "duplicate_count": item.duplicate_count,
        "text_key": item.text_key,
    }


def register_local_resource_routes(app: Flask, context: LocalResourceRouteContext) -> None:
    """Register the Local resources pages, media transport, and prompt API."""
    blueprint = Blueprint(LOCAL_RESOURCES_BLUEPRINT_NAME, __name__)
    app.template_global("browser_media_url")(browser_media_url)
    app.template_global("wechat_contact_analysis_url")(wechat_contact_analysis_url)
    app.template_filter("wechat_source_clock")(format_wechat_source_clock)

    @blueprint.get("/browser/wechat/contacts/<contact_id>")
    def wechat_contact_analysis(contact_id: str):
        """Project all proven source occurrences of one contact into a standard table."""
        from app.core.storage import (
            build_local_store_pagination,
            ContactAnalysisSourceChanged,
            query_contact_analysis,
        )

        query = request.args.get("q", "").replace("\x00", "").strip()[:120]
        sort = "newest" if request.args.get("sort") == "newest" else "oldest"
        try:
            analysis = query_contact_analysis(
                context.media_catalog.local_store_root,
                contact_id,
                query=query,
                sort=sort,
                page=request.args.get("page", "1"),
                page_size=50,
                background=True,
            )
        except ValueError:
            abort(404)
        except ContactAnalysisSourceChanged:
            analysis = {
                "status": "indexing",
                "contact": {
                    "id": contact_id,
                    "display_name": "Contact analysis",
                    "username": "",
                },
                "items": [],
                "counts": {
                    "messages": 0,
                    "conversations": 0,
                    "source_accounts": 0,
                    "sources": 0,
                },
                "total_count": 0,
                "total_pages": 1,
                "current_page": 1,
                "page_size": 50,
            }
        if analysis is None:
            abort(404)
        return_url = _wechat_contact_return_url(request.args.get("return_to", ""))
        canonical_contact_id = analysis["contact"]["id"]
        if canonical_contact_id != contact_id:
            return redirect(
                url_for(
                    "local_resources.wechat_contact_analysis",
                    contact_id=canonical_contact_id,
                    q=query or None,
                    sort=sort,
                    page=request.args.get("page", "1"),
                    return_to=return_url,
                )
            )
        for item in analysis["items"]:
            item["source_url"] = url_for(
                "local_resources.wechat_message_source", message_id=item["id"]
            )
            for observation in item.get("observations", ()):
                observation["source_url"] = url_for(
                    "local_resources.wechat_message_source",
                    message_id=observation["id"],
                )
        analysis["conversation_count"] = analysis["counts"]["conversations"]
        analysis["account_count"] = analysis["counts"]["source_accounts"]

        def page_url(page):
            return url_for(
                "local_resources.wechat_contact_analysis",
                contact_id=contact_id,
                q=query or None,
                sort=sort,
                page=page,
                return_to=return_url,
            )

        template_context = {
            "analysis": analysis,
            "contact_filters": {"q": query, "sort": sort},
            "return_url": return_url,
            "contact_form_url": url_for(
                "local_resources.wechat_contact_analysis", contact_id=contact_id
            ),
            "contact_page_url": page_url,
            "pagination_items": build_local_store_pagination(
                analysis["total_pages"], analysis["current_page"]
            ),
            "version": APP_VERSION,
        }
        if request.accept_mimetypes.best == "application/json":
            payload = {
                "status": analysis["status"],
                "contact_id": analysis["contact"]["id"],
                "display_name": analysis["contact"]["display_name"],
                "username": analysis["contact"]["username"],
            }
            if analysis["status"] != "indexing":
                payload["html"] = render_template(
                    "_wechat_contact_analysis_content.html", **template_context
                )
            response = jsonify(payload)
            response.cache_control.no_store = True
        else:
            template_context.update(
                wechat_contact_sidebar_context(
                    context.media_catalog.local_store_root, return_url
                )
            )
            response = app.make_response(
                render_template("wechat_contact_analysis.html", **template_context)
            )
        response.vary.add("Accept")
        if analysis["status"] == "indexing":
            response.status_code = 202
        elif analysis["status"] == "error":
            response.status_code = 503
        return response

    @blueprint.get("/browser/wechat/contacts/<contact_id>/export.csv")
    def wechat_contact_csv(contact_id: str):
        """Stream every filtered statement without writing an export to local storage."""
        from app.core.storage import (
            ContactAnalysisSourceChanged,
            contact_analysis_export_context,
            export_contact_analysis_items,
        )
        from app.web.wechat_contact_csv import contact_csv_chunks, contact_csv_filename

        query = request.args.get("q", "").replace("\x00", "").strip()[:120]
        sort = "newest" if request.args.get("sort") == "newest" else "oldest"
        try:
            analysis = contact_analysis_export_context(
                context.media_catalog.local_store_root,
                contact_id,
            )
            if analysis is None:
                abort(404)
            if analysis["status"] != "ready":
                abort(
                    503,
                    description="Contact statements are not ready. Please try again shortly.",
                )
            items = export_contact_analysis_items(
                context.media_catalog.local_store_root,
                contact_id,
                query=query,
                sort=sort,
                source_fingerprint=analysis["source_fingerprint"],
            )
            first = next(items, None)
        except ValueError:
            abort(404)
        except ContactAnalysisSourceChanged:
            abort(
                503,
                description="Source records are updating. Please try again shortly.",
            )
        filename = contact_csv_filename(analysis["contact"])
        response = Response(
            stream_with_context(
                contact_csv_chunks(
                    items,
                    first=first,
                    contact=analysis["contact"],
                    source_url=lambda item: url_for(
                        "local_resources.wechat_message_source",
                        message_id=item["id"],
                        _external=True,
                    ),
                )
            ),
            mimetype="text/csv",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="wechat-contact-{contact_id[-12:]}.csv"; '
                    f"filename*=UTF-8''{quote(filename, safe='')}"
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )
        response.cache_control.no_store = True
        response.call_on_close(items.close)
        return response

    @blueprint.get("/browser/wechat/messages/<message_id>")
    def wechat_message_source(message_id: str):
        """Open the exact source message, including its original account and page."""
        from app.core.storage import (
            ContactAnalysisSourceChanged,
            message_source_location,
        )

        try:
            location = message_source_location(
                context.media_catalog.local_store_root, message_id
            )
        except ContactAnalysisSourceChanged:
            abort(503, description="Source records are updating. Please try again shortly.")
        if location is None:
            abort(404)
        return redirect(
            url_for(
                "local_resources.browser",
                view="text",
                source="wechat",
                account=location["account_key"],
                chat_kind=location["kind"],
                session=location["conversation_id"],
                page=location["page"],
                sort="newest",
                _anchor=location.get("anchor_id") or "wechat_message_" + message_id,
            )
        )

    @blueprint.get("/browser")
    def browser():
        filters = normalize_browser_filters(
            source=request.args.get("source"),
            media_kind=request.args.get("kind"),
            query=request.args.get("q"),
            sort=request.args.get("sort"),
            page=request.args.get("page"),
            session=request.args.get("session"),
            session_view=request.args.get("session_view"),
            view=request.args.get("view"),
            media_id=request.args.get("media_id"),
            session_page=request.args.get("session_page"),
            answerer=request.args.get("answerer"),
            project=request.args.get("project"),
            session_index=request.args.get("session_index"),
            media_view=request.args.get("media_view"),
            account=request.args.get("account"),
            chat_kind=request.args.get("chat_kind"),
        )
        force_refresh = request.args.get("refresh") == "1"
        prompt_page = None
        saved_prompt_keys = context.prompt_store.saved_pointer_keys()
        saved_prompt_content_keys = (
            context.prompt_store.saved_content_keys() if filters["view"] == "media" else frozenset()
        )
        if filters["view"] == "text":
            media_items = () if filters["source"] == "wechat" else context.media_catalog.snapshot(force_refresh=force_refresh)
            text_page = query_chat_history(
                context.media_catalog.local_store_root,
                source=filters["source"],
                query=filters["q"],
                sort=filters["sort"],
                page=filters["page"],
                session_view=filters["session_view"],
                session=filters["session"],
                answerer=filters["answerer"],
                project=filters["project"],
                account=filters["account"],
                chat_kind=filters["chat_kind"],
            )
            filters["answerer"] = text_page.selected_answerer
            filters["project"] = text_page.selected_project
            text_page = attach_media_references(
                text_page,
                media_items,
                lambda stable_id: url_for(
                    f"{LOCAL_RESOURCES_BLUEPRINT_NAME}.browser",
                    view="media",
                    media_id=stable_id,
                    source="all",
                    kind="all",
                    q="",
                    sort="newest",
                ),
                lambda item: (
                    url_for(f"{LOCAL_RESOURCES_BLUEPRINT_NAME}.browser_deleted_preview", stable_id=item.stable_id)
                    if item.is_deleted
                    else url_for(
                        f"{LOCAL_RESOURCES_BLUEPRINT_NAME}.browser_media",
                        relative_path=media_route_relative_path(item.relative_path),
                    )
                ),
            )
            all_items = ()
            media_page = None
            media_payload = []
        elif filters["view"] == "prompts":
            prompt_page = context.prompt_store.query(
                source=filters["source"],
                query=filters["q"],
                sort=filters["sort"],
                page=filters["page"],
            )
            all_items = ()
            media_page = None
            text_page = None
            media_payload = []
        else:
            all_items = context.media_catalog.snapshot(force_refresh=force_refresh)
            media_page = context.media_catalog.query(
                source=filters["source"],
                media_kind=filters["kind"],
                query=filters["q"],
                sort=filters["sort"],
                page=filters["page"],
                chatgpt_session_key=filters["session"],
                chatgpt_session_view=filters["session_view"],
                chatgpt_session_index=filters["session_index"],
                chatgpt_all_media=filters["media_view"] == "all",
                media_id=filters["media_id"],
            )
            text_page = None
            media_payload = [serialize_media_item(item) for item in media_page.items]
        browser_search_suggestions = build_browser_search_suggestions(
            view=filters["view"],
            media_items=media_page.items if media_page is not None else all_items,
            text_page=text_page,
            prompt_page=prompt_page,
        )
        return render_template(
            "browser.html",
            **local_resource_source_context(),
            media_page=media_page,
            text_page=text_page,
            prompt_page=prompt_page,
            media_payload=media_payload,
            filters=filters,
            browser_search_suggestions=browser_search_suggestions,
            has_any_media=bool(all_items),
            has_any_text=bool(text_page and (text_page.total_count or filters["q"] or filters["project"])),
            has_any_prompts=context.prompt_store.has_any(),
            saved_prompt_keys=saved_prompt_keys,
            saved_prompt_content_keys=saved_prompt_content_keys,
            media_prompt_key=media_prompt_key,
            prompt_remark_options=context.prompt_store.remark_options(),
            prompt_pointer_key=prompt_pointer_key,
            format_captured_at_label=format_captured_at_label,
            format_captured_at_timestamp_label=format_captured_at_timestamp_label,
            format_chat_message_timestamp_label=format_chat_message_timestamp_label,
            format_media_size=format_media_size,
            render_prompt_markdown=render_prompt_markdown,
            render_cached_message=render_cached_message,
            cached_message_source_links=cached_message_source_links,
            file_manager_label=local_file_manager_label(),
            version=APP_VERSION,
        )

    @blueprint.get("/browser/session/<session_id>/export")
    def browser_session_export(session_id: str):
        """Download a complete resource group unless page scope is explicit."""
        source = request.args.get("source", "all")
        sort = request.args.get("sort", "newest")
        export_scope = request.args.get("scope", "all").strip().lower()
        page_only = export_scope == "page"
        text_page = query_chat_history(
            context.media_catalog.local_store_root,
            source=source,
            sort=sort,
            page=request.args.get("page", "1") if page_only else 1,
            page_size=100 if page_only else 1_000_000,
            session_view=True,
            session=session_id,
            account=request.args.get("account", ""),
            chat_kind=request.args.get("chat_kind", "all"),
        )
        markdown = build_chat_history_markdown(
            text_page,
            message_count=len(text_page.items),
        )
        if not markdown:
            abort(404)
        title = text_page.current_session.conversation_title if text_page.current_session else "session"
        filename = "".join(
            character if character.isalnum() or character in {"-", "_", " "} else "_"
            for character in str(title)
        ).strip()
        filename = "_".join(filename.split()) or "session"
        if page_only:
            filename = f"{filename}_page_{text_page.current_page}"
        ascii_filename = "".join(
            character
            if character.isascii() and (character.isalnum() or character in {"-", "_", " "})
            else "_"
            for character in filename
        ).strip()
        ascii_filename = "_".join(ascii_filename.split()) or "session"
        download_filename = f"{filename}.md"
        return Response(
            markdown,
            mimetype="text/markdown",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{ascii_filename}.md"; '
                    f"filename*=UTF-8''{quote(download_filename, safe='')}"
                )
            },
        )

    @blueprint.get("/browser/wechat/session/<session_id>/export.csv")
    def browser_wechat_session_csv(session_id: str):
        """Download the complete scoped timeline in chronological order."""
        from app.core.storage import (
            WechatSessionExport,
            WechatSessionSourceChanged,
        )
        from app.web.wechat_session_csv import session_csv_chunks, session_csv_filename

        try:
            export = WechatSessionExport(
                context.media_catalog.local_store_root,
                session_id,
                account=request.args.get("account", ""),
                kind=request.args.get("chat_kind", "all"),
            )
        except ValueError:
            abort(404)
        except (OSError, WechatSessionSourceChanged):
            abort(503, description="Source records are updating. Please try again shortly.")
        filename = session_csv_filename(export.conversation)
        response = Response(
            stream_with_context(session_csv_chunks(export)),
            mimetype="text/csv",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="wechat-conversation-{session_id[-12:]}.csv"; '
                    f"filename*=UTF-8''{quote(filename, safe='')}"
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )
        response.cache_control.no_store = True
        response.call_on_close(export.close)
        return response

    @blueprint.get("/browser/media/<path:relative_path>")
    def browser_media(relative_path: str):
        resolved_path = resolve_browser_media_path(context.media_catalog.local_store_root, relative_path)
        if resolved_path is None:
            abort(404)
        return send_file(resolved_path, conditional=True, etag=True, max_age=0)

    @blueprint.post("/api/browser/prompts")
    def add_browser_prompt():
        payload = request.get_json(silent=True) or {}
        media_id = str(payload.get("media_id") or "").strip()
        try:
            if media_id:
                # A media card saves the prompt the local catalog recorded for that item.
                media_item = next(
                    (
                        candidate
                        for candidate in context.media_catalog.snapshot()
                        if candidate.stable_id == media_id
                    ),
                    None,
                )
                if media_item is None:
                    raise LookupError("Cached media was not found.")
                item, created = context.prompt_store.add_media_prompt(
                    source=media_item.source,
                    conversation_id=media_prompt_conversation_id(media_item),
                    prompt_text=media_item.prompt_markdown,
                    conversation_title=media_item.creator,
                    conversation_url=media_item.source_url,
                    captured_at=media_item.captured_at,
                )
            else:
                item, created = context.prompt_store.add_pointer(
                    source=str(payload.get("source") or ""),
                    conversation_id=str(payload.get("conversation_id") or ""),
                    message_key=str(payload.get("message_key") or ""),
                )
        except ValueError as exc:
            return jsonify({"error": public_exception_message(
                exc, fallback="The saved prompt could not be updated.",
                allowed=_PROMPT_PUBLIC_ERRORS, operation="saved prompt update",
            )}), 400
        except LookupError as exc:
            return jsonify({"error": public_exception_message(
                exc, fallback="The saved prompt could not be updated.",
                allowed=_PROMPT_PUBLIC_ERRORS, operation="saved prompt update",
            )}), 404
        return jsonify({"created": created, "item": serialize_prompt_item(item)})

    @blueprint.delete("/api/browser/prompts/<stable_id>")
    def remove_browser_prompt(stable_id: str):
        try:
            duplicate_count = context.prompt_store.remove(stable_id)
        except LookupError as exc:
            return jsonify({"error": public_exception_message(
                exc, fallback="The saved prompt could not be updated.",
                allowed=_PROMPT_PUBLIC_ERRORS, operation="saved prompt update",
            )}), 404
        return jsonify(
            {
                "removed": True,
                "duplicate_count": duplicate_count,
                "remark_options": context.prompt_store.remark_options(),
            }
        )

    @blueprint.post("/api/browser/prompts/<stable_id>/restore")
    def restore_browser_prompt(stable_id: str):
        try:
            item = context.prompt_store.restore(stable_id)
        except LookupError as exc:
            return jsonify({"error": public_exception_message(
                exc, fallback="The saved prompt could not be updated.",
                allowed=_PROMPT_PUBLIC_ERRORS, operation="saved prompt update",
            )}), 404
        return jsonify(
            {
                "item": serialize_prompt_item(item),
                "remark_options": context.prompt_store.remark_options(),
            }
        )

    @blueprint.post("/api/browser/prompts/<stable_id>/remarks")
    def add_browser_prompt_remark(stable_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            item, created = context.prompt_store.add_remark(stable_id, payload.get("remark"))
        except ValueError as exc:
            return jsonify({"error": public_exception_message(
                exc, fallback="The saved prompt could not be updated.",
                allowed=_PROMPT_PUBLIC_ERRORS, operation="saved prompt update",
            )}), 400
        except LookupError as exc:
            return jsonify({"error": public_exception_message(
                exc, fallback="The saved prompt could not be updated.",
                allowed=_PROMPT_PUBLIC_ERRORS, operation="saved prompt update",
            )}), 404
        return jsonify(
            {
                "created": created,
                "item": serialize_prompt_item(item),
                "remark_options": context.prompt_store.remark_options(),
            }
        )

    @blueprint.delete("/api/browser/prompts/<stable_id>/remarks")
    def remove_browser_prompt_remark(stable_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            item = context.prompt_store.remove_remark(stable_id, payload.get("remark"))
        except ValueError as exc:
            return jsonify({"error": public_exception_message(
                exc, fallback="The saved prompt could not be updated.",
                allowed=_PROMPT_PUBLIC_ERRORS, operation="saved prompt update",
            )}), 400
        except LookupError as exc:
            return jsonify({"error": public_exception_message(
                exc, fallback="The saved prompt could not be updated.",
                allowed=_PROMPT_PUBLIC_ERRORS, operation="saved prompt update",
            )}), 404
        return jsonify(
            {
                "item": serialize_prompt_item(item),
                "remark_options": context.prompt_store.remark_options(),
            }
        )

    @blueprint.get("/browser/deleted-preview/<stable_id>")
    def browser_deleted_preview(stable_id: str):
        resolved_path = context.media_catalog.deleted_preview_path(stable_id)
        if resolved_path is None:
            abort(404)
        return send_file(resolved_path, conditional=True, etag=True, max_age=0)

    @blueprint.post("/api/browser/media/<stable_id>/delete")
    def delete_browser_media(stable_id: str):
        try:
            item = context.media_catalog.delete(stable_id)
        except KeyError:
            return jsonify({"error": "Cached media was not found."}), 404
        except FileNotFoundError:
            return jsonify({"error": "Cached media is no longer available."}), 404
        except (OSError, RuntimeError, ValueError) as exc:
            return jsonify({"error": public_exception_message(
                exc, fallback="The cached media could not be updated. Please try again.",
                allowed=_MEDIA_PUBLIC_ERRORS, operation="cached media update",
            )}), 409
        return jsonify({"item": serialize_media_item(item)})

    @blueprint.post("/api/browser/media/<stable_id>/restore")
    def restore_browser_media(stable_id: str):
        try:
            item = context.media_catalog.restore(stable_id)
        except KeyError:
            return jsonify({"error": "Removed media was not found."}), 404
        except FileNotFoundError:
            return jsonify({"error": "The retained preview is no longer available."}), 404
        except (OSError, ValueError) as exc:
            return jsonify({"error": public_exception_message(
                exc, fallback="The cached media could not be updated. Please try again.",
                allowed=_MEDIA_PUBLIC_ERRORS, operation="cached media update",
            )}), 409
        return jsonify({"item": serialize_media_item(item)})

    @blueprint.post("/api/browser/media/<stable_id>/reveal")
    def reveal_browser_media(stable_id: str):
        if not is_loopback_address(request.remote_addr):
            return jsonify({"error": "Local files can only be revealed from this computer."}), 403

        resolved_path = context.media_catalog.resolved_media_path(stable_id)
        if resolved_path is None:
            return jsonify({"error": "Cached media is no longer available."}), 404
        try:
            reveal_media_path(resolved_path)
        except OSError as exc:
            return jsonify({"error": public_exception_message(
                exc, fallback=f"Unable to open {local_file_manager_label()}. Please try again.",
                operation="reveal cached media",
            )}), 500
        return jsonify({"revealed": True, "file_manager": local_file_manager_label()})

    @blueprint.post("/api/browser/wechat/resources/<message_id>/reveal")
    def reveal_wechat_resource(message_id: str):
        if not is_loopback_address(request.remote_addr):
            return jsonify({"error": "WeChat resources can only be opened from this computer."}), 403
        try:
            destination = resolve_resource_for_reveal(context.media_catalog.local_store_root, message_id)
            if destination["status"] == "folder":
                open_directory_path(destination["path"])
            else:
                reveal_media_path(destination["path"])
        except LookupError as exc:
            return jsonify({"error": public_exception_message(
                exc, fallback="The WeChat resource is unavailable or needs refreshing.",
                allowed=_WECHAT_RESOURCE_PUBLIC_ERRORS, operation="reveal WeChat resource",
            )}), 404
        except (OSError, ValueError):
            return jsonify({"error": f"Unable to open {local_file_manager_label()}. The local file or folder may have moved."}), 409
        return jsonify({"revealed": True, "target_kind": destination["status"], "file_manager": local_file_manager_label()})

    app.register_blueprint(blueprint)


__all__ = [
    "LOCAL_RESOURCES_BLUEPRINT_NAME",
    "LocalResourceRouteContext",
    "browser_media_url",
    "register_local_resource_routes",
    "serialize_media_item",
    "serialize_prompt_item",
]
