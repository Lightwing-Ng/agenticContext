"""Local resources routes: the stored media, prompt, and chat-history surface.

These routes read the local catalogs the application factory builds and never reach
for a browser, an Agent, or a cache worker. Serialization of one stored item into its
public shape lives here too, so a template global and a JSON response cannot drift.
"""

# Code version: v1.0.0-claude.0

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from flask import (
    Blueprint,
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
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
    prompt_pointer_key,
    query_chat_history,
    resolve_browser_media_path,
    reveal_media_path,
)
from app.web.cache_sources import get_cache_source_label
from app.web.presentation import (
    build_browser_search_suggestions,
    format_media_size,
    render_cached_message,
    render_prompt_markdown,
)


LOCAL_RESOURCES_BLUEPRINT_NAME = "local_resources"


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
    }


def register_local_resource_routes(app: Flask, context: LocalResourceRouteContext) -> None:
    """Register the Local resources pages, media transport, and prompt API."""
    blueprint = Blueprint(LOCAL_RESOURCES_BLUEPRINT_NAME, __name__)
    app.template_global("browser_media_url")(browser_media_url)

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
        )
        force_refresh = request.args.get("refresh") == "1"
        prompt_page = None
        saved_prompt_keys = context.prompt_store.saved_pointer_keys()
        if filters["view"] == "text":
            media_items = context.media_catalog.snapshot(force_refresh=force_refresh)
            text_page = query_chat_history(
                context.media_catalog.local_store_root,
                source=filters["source"],
                query=filters["q"],
                sort=filters["sort"],
                page=filters["page"],
                session_view=filters["session_view"],
                session=filters["session"],
                answerer=filters["answerer"],
            )
            filters["answerer"] = text_page.selected_answerer
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
            media_page=media_page,
            text_page=text_page,
            prompt_page=prompt_page,
            media_payload=media_payload,
            filters=filters,
            browser_search_suggestions=browser_search_suggestions,
            has_any_media=bool(all_items),
            has_any_text=bool(text_page and (text_page.total_count or filters["q"])),
            has_any_prompts=context.prompt_store.has_any(),
            saved_prompt_keys=saved_prompt_keys,
            prompt_remark_options=context.prompt_store.remark_options(),
            prompt_pointer_key=prompt_pointer_key,
            format_captured_at_timestamp_label=format_captured_at_timestamp_label,
            format_chat_message_timestamp_label=format_chat_message_timestamp_label,
            format_media_size=format_media_size,
            render_prompt_markdown=render_prompt_markdown,
            render_cached_message=render_cached_message,
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

    @blueprint.get("/browser/media/<path:relative_path>")
    def browser_media(relative_path: str):
        resolved_path = resolve_browser_media_path(context.media_catalog.local_store_root, relative_path)
        if resolved_path is None:
            abort(404)
        return send_file(resolved_path, conditional=True, etag=True, max_age=0)

    @blueprint.post("/api/browser/prompts")
    def add_browser_prompt():
        payload = request.get_json(silent=True) or {}
        try:
            item, created = context.prompt_store.add_pointer(
                source=str(payload.get("source") or ""),
                conversation_id=str(payload.get("conversation_id") or ""),
                message_key=str(payload.get("message_key") or ""),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except LookupError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify({"created": created, "item": serialize_prompt_item(item)})

    @blueprint.post("/api/browser/prompts/<stable_id>/remarks")
    def add_browser_prompt_remark(stable_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            item, created = context.prompt_store.add_remark(stable_id, payload.get("remark"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except LookupError as exc:
            return jsonify({"error": str(exc)}), 404
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
            return jsonify({"error": str(exc)}), 400
        except LookupError as exc:
            return jsonify({"error": str(exc)}), 404
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
            return jsonify({"error": str(exc)}), 409
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
            return jsonify({"error": str(exc)}), 409
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
            return jsonify({"error": f"Unable to open {local_file_manager_label()}: {exc}"}), 500
        return jsonify({"revealed": True, "file_manager": local_file_manager_label()})

    app.register_blueprint(blueprint)


__all__ = [
    "LOCAL_RESOURCES_BLUEPRINT_NAME",
    "LocalResourceRouteContext",
    "browser_media_url",
    "register_local_resource_routes",
    "serialize_media_item",
    "serialize_prompt_item",
]
