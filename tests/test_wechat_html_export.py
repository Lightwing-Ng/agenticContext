"""Exercise text-only WechatExporter ingestion and archive safety boundaries.

Code version: v1.5.1
"""

import html
import hashlib
import json
import os
import struct
import weakref
import zipfile

import pytest

from app.core import wechat_html_export as export


def message(
    text="Hello & goodbye",
    *,
    local_id="42",
    kind="1",
    classes="chat right",
    native="wxid_owner",
    alias="public_alias",
):
    return (
        f'<div class="msg {classes}" msgid="{local_id}" msgtype="{kind}">'
        f'<div class="avatar-box"><img class="avatar" src="Chat_files/Portrait/{native}.jpg"></div>'
        f'<div class="nt-box"><span class="dspname" wxId="{alias}">Sender</span> 2021-08-07 10:59:03</div>'
        '<div class="content-box"><span class="msg-text"><pre>'
        + text
        + "</pre></span></div></div>"
    )


def boundary(text):
    return f'<div class="msg chat-notice fmsgtag" msgid=""><div class="content-box"><span class="msg-text"><pre>{html.escape(text)}</pre></span></div></div>'


def async_page(items):
    return """(function() {
        var msgArray = %s;
        for (var idx = 0; idx < msgArray.length; idx++)
        {
          window.moreWechatMsgs.push(msgArray[idx]);
        }
        msgArray = null;
        loadMsgsForNextPage();
    })();
    """ % json.dumps(items, ensure_ascii=False)


def fixture_zip(
    tmp_path,
    *,
    nodes=None,
    pages=None,
    root="Export",
    peer="peer@chatroom",
    additional=None,
):
    pages = pages or []
    main = "".join(nodes if nodes is not None else [message()])
    declaration = (
        "<script>var numberOfMsgs = parseInt('%s') || 0;"
        "var numberOfPages = parseInt('%s') || 0;"
        'script.src = "Chat_files/Data/msg-" + nextPage + ".js";</script>'
        % (sum(map(len, pages)), len(pages))
    )
    members = {
        f"{root}/index.html": f'<html><body><table><tr><td><img src="Chat_files/Portrait/{peer}.jpg"></td><td><a href="Chat.html">Conversation (WeChat ID: peer_alias)</a></td></tr></table></body></html>',
        f"{root}/Chat.html": '<html><head><title>Fallback title</title></head><body uid="" sid=""><span class="sname">A &amp; B</span><div class="msgs" id="msgs-div">'
        + main
        + "</div>"
        + declaration
        + "</body></html>",
        f"{root}/Chat_files/Portrait/wxid_owner.jpg": b"This is deliberately not a decoded image.",
    }
    # Write pages in reverse physical order to exercise logical page ordering.
    for number in range(len(pages), 0, -1):
        members[f"{root}/Chat_files/Data/msg-{number}.js"] = async_page(
            pages[number - 1]
        )
    members.update(additional or {})
    path = tmp_path / "synthetic-export.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    return path


def test_unicode_paths_html_entities_and_native_identity_remain_distinct(
    tmp_path, monkeypatch
):
    path = fixture_zip(
        tmp_path, root="Bay Αιγαίο", nodes=[message("  A &lt; B &amp; C<br>next  ")]
    )
    original = path.read_bytes()
    opened = []
    real_open = zipfile.ZipFile.open

    def only_text(self, name, *args, **kwargs):
        filename = name.filename if isinstance(name, zipfile.ZipInfo) else name
        opened.append(filename)
        assert filename.endswith((".html", ".js"))
        return real_open(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", only_text)
    result = export.parse_html_export(path)
    conversation = result["conversations"][0]
    record = conversation["messages"][0]
    assert record["text"] == "  A < B & C\nnext  "
    assert conversation["title"] == "A & B"
    assert conversation["index_avatar_native_id"] == "peer@chatroom"
    assert conversation["index_public_id"] == "peer_alias"
    assert conversation["conversation_kind"] == "group"
    assert record["sender_export_id"] == "public_alias"
    assert record["sender_native_id"] == "wxid_owner"
    assert record["export_msgid"] == "42"
    assert "server_id" not in record
    assert "export_msgid_is_meslocalid" in record["flags"]
    assert result["owner_native_id_counts"] == {"wxid_owner": 1}
    assert result["warnings"] == []
    assert all(
        member["sha256"] and member["crc32"] for member in result["selected_members"]
    )
    assert len(opened) == 2
    assert path.read_bytes() == original


def test_three_async_pages_follow_page_number_and_count_native_records(tmp_path):
    root_record = message("Forwarded title", kind="49", local_id="80")
    forwarded = message(
        "Nested text",
        local_id="80_component",
        kind="",
        classes="chat left fmsg",
        native="original_sender",
    )
    pages = [
        [message("Page one", local_id="1")],
        [
            root_record
            + boundary("<< History")
            + forwarded
            + boundary("History Ends >>")
        ],
        [message("Page three", local_id="3")],
    ]
    result = export.parse_html_export(fixture_zip(tmp_path, pages=pages))
    conversation = result["conversations"][0]
    records = conversation["messages"]
    assert [record["text"] for record in records] == [
        "Hello & goodbye",
        "Page one",
        "Forwarded title",
        "Page three",
    ]
    assert records[2]["forwarded_components"][0]["text"] == "Nested text"
    assert records[2]["forwarded_components"][0]["export_msgid"] == "80_component"
    assert conversation["counts"]["async_array_items"] == 3
    assert conversation["counts"]["all_message_nodes"] == 7
    assert [record["source_locator"]["page"] for record in records] == [0, 1, 2, 3]
    assert result["warnings"] == []


def test_forwarded_media_without_fmsg_stays_inside_parent_and_out_of_owner(tmp_path):
    nodes = [
        message("Forwarded title", kind="49", local_id="80"),
        boundary("<< History"),
        message(
            "Do not promote this media",
            kind="image",
            local_id="80_image",
            classes="media right raw-img",
            native="another_person",
        ),
        message(
            "First child",
            kind="",
            local_id="80_one",
            classes="chat left fmsg",
            native="original_sender",
        ),
        boundary("<< Nested"),
        message(
            "Nested child",
            kind="",
            local_id="80_two",
            classes="chat left fmsg",
            native="third_person",
        ),
        boundary("Nested Ends >>"),
        boundary("History Ends >>"),
    ]
    result = export.parse_html_export(fixture_zip(tmp_path, nodes=nodes))
    conversation = result["conversations"][0]
    parent = conversation["messages"][0]
    assert len(conversation["messages"]) == 1
    assert parent["forwarded_components"][0]["text"] == "First child"
    assert (
        parent["forwarded_components"][0]["forwarded_components"][0]["text"]
        == "Nested child"
    )
    assert conversation["counts"]["omitted_forwarded_non_text"] == 1
    assert result["owner_native_ids"] == ["wxid_owner"]
    assert result["warnings"] == []


def test_card_text_notice_and_unknown_types_are_accounted_without_network(tmp_path):
    card = message("", local_id="10", kind="49").replace(
        '<span class="msg-text"><pre></pre></span>',
        '<a href="https://invalid.example/private"><b>Card title</b></a><span class="msg-text"><pre>Card description</pre></span><img src="https://invalid.example/tracker">',
    )
    notice = '<div class="msg chat-notice" msgid="11"><span>System notice</span></div>'
    unknown = message("Unknown rendered payload", local_id="12", kind="999")
    result = export.parse_html_export(
        fixture_zip(tmp_path, nodes=[card, notice, unknown])
    )
    records = result["conversations"][0]["messages"]
    assert [(record["text_kind"], record["text"]) for record in records] == [
        ("rendered_app_text", "Card title\n\nCard description"),
        ("notice", "System notice"),
    ]
    assert result["counts"]["deferred_top_level"] == 1


def test_rendered_location_and_channel_metadata_preserve_text_and_sender_only(
    tmp_path, monkeypatch
):
    location_text = "[Location]\nA rendered label\n(22.3000, 114.2000)"
    location = message(location_text, kind="48", local_id="48")
    direct_pre = message("", kind="48", local_id="48_pre").replace(
        '<span class="msg-text"><pre></pre></span>',
        "<pre>A second rendered location</pre>",
    )
    channel = message(
        "", kind="channels", classes="chat left channels", local_id="channel"
    ).replace(
        '<span class="msg-text"><pre></pre></span>',
        '<a href="https://invalid.example/video"><img class="channel-poster" '
        'src="Chat_files/never-open-video.jpg"></a>'
        '<div class="card-name channel-card"><img class="card-avatar" '
        'src="https://invalid.example/avatar"><span class="card-name msg-text">'
        'Card author</span></div><span class="channel-title msg-text">'
        "<pre>  Rendered title &amp; caption\nsecond line  </pre></span>"
        '<div class="card-type">Generated media footer</div>',
    )
    path = fixture_zip(
        tmp_path,
        nodes=[location, direct_pre, channel],
        additional={"Export/Chat_files/never-open-video.jpg": b"Not an image."},
    )
    opened = []
    real_open = zipfile.ZipFile.open

    def only_text(self, name, *args, **kwargs):
        filename = name.filename if isinstance(name, zipfile.ZipInfo) else name
        assert filename.endswith((".html", ".js"))
        opened.append(filename)
        return real_open(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", only_text)
    result = export.parse_html_export(path)
    records = result["conversations"][0]["messages"]
    assert result["parser_version"] == "v1.5.1"
    assert [(record["text_kind"], record["text"]) for record in records] == [
        ("rendered_location_metadata", location_text),
        ("rendered_location_metadata", "A second rendered location"),
        (
            "rendered_channel_metadata",
            "Card author\n\n  Rendered title & caption\nsecond line  ",
        ),
    ]
    assert records[2]["sender_label"] == "Sender"
    assert records[2]["sender_native_id"] == "wxid_owner"
    assert records[2]["sender_export_id"] == "public_alias"
    assert records[2]["direction"] == "received"
    assert "channel_metadata_from_rendered_export" in records[2]["flags"]
    assert "location_metadata_from_rendered_export" in records[0]["flags"]
    assert len(opened) == 2


@pytest.mark.parametrize(
    ("kind", "classes", "body"),
    [
        ("48", "chat left", "[Location]"),
        ("48", "chat left", "[位置]"),
        ("48", "chat left", "[Photo]"),
        ("48", "chat left", " \n "),
        ("channels", "chat left channels", "[Channels]"),
        ("channels", "chat left channels", "[视频号]"),
        ("channels", "chat left channels", "[Video]"),
        ("channels", "chat left channels", " \n "),
        ("50", "chat left", "[Video/Audio Call]"),
        ("999", "chat left", "Readable unknown payload"),
    ],
)
def test_metadata_extraction_does_not_promote_bare_labels_or_unknown_payloads(
    tmp_path, kind, classes, body
):
    node = message(body, kind=kind, classes=classes)
    if "channels" in classes:
        node = node.replace('class="msg-text"', 'class="msg-text channel-title"')
    result = export.parse_html_export(fixture_zip(tmp_path, nodes=[node]))
    assert result["conversations"][0]["messages"] == []
    assert result["counts"]["deferred_top_level"] == 1


def test_literal_metadata_tokens_in_type1_and_channel_unrelated_text_are_unchanged(
    tmp_path,
):
    nodes = [
        message("[Location]", kind="1"),
        message("[Video/Audio Call]", kind="1", local_id="literal_call"),
        message(
            "Unclassified card text",
            kind="channels",
            classes="chat left channels",
            local_id="not_title",
        ),
    ]
    result = export.parse_html_export(fixture_zip(tmp_path, nodes=nodes))
    assert [record["text"] for record in result["conversations"][0]["messages"]] == [
        "[Location]",
        "[Video/Audio Call]",
    ]
    assert result["counts"]["deferred_top_level"] == 1


def test_forwarded_app_and_channel_metadata_keep_boundary_and_original_sender(
    tmp_path, monkeypatch
):
    card = message(
        "",
        kind="",
        classes="chat left fmsg",
        local_id="parent_link",
        native="wxid_original_sender",
    ).replace(
        '<span class="msg-text"><pre></pre></span>',
        '<div><a href="https://invalid.example/article"><b><span class="msg-text">'
        "<pre>Rendered article title</pre></span></b></a></div>"
        '<span class="msg-text">Rendered summary</span>'
        '<div class="appinfo"><img class="app-icon" '
        'src="Chat_files/never-open-app-icon.jpg"><span class="app-name">'
        "Application attribution</span></div>",
    )
    channel = message(
        "",
        kind="channels",
        classes="chat left channels fmsg",
        local_id="parent_channel",
        native="wxid_second_original_sender",
    ).replace(
        '<span class="msg-text"><pre></pre></span>',
        '<span class="card-name msg-text">Different card author</span>'
        '<span class="channel-title msg-text">Caption</span>',
    )
    path = fixture_zip(
        tmp_path,
        nodes=[
            message("Forwarded history", kind="49", local_id="parent"),
            boundary("<< History"),
            card,
            channel,
            boundary("History Ends >>"),
        ],
        additional={"Export/Chat_files/never-open-app-icon.jpg": b"Not an image."},
    )
    real_open = zipfile.ZipFile.open

    def only_text(self, name, *args, **kwargs):
        filename = name.filename if isinstance(name, zipfile.ZipInfo) else name
        assert filename.endswith((".html", ".js"))
        return real_open(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", only_text)
    audited = []
    result = export.parse_html_export(
        path, node_audit_sink=lambda metadata, rows: audited.extend(rows)
    )
    parent = result["conversations"][0]["messages"]
    assert len(parent) == 1
    children = parent[0]["forwarded_components"]
    assert [(child["text_kind"], child["text"]) for child in children] == [
        (
            "rendered_forwarded_app_metadata",
            "Rendered article title\n\nRendered summary",
        ),
        ("rendered_channel_metadata", "Different card author\n\nCaption"),
    ]
    assert [child["sender_native_id"] for child in children] == [
        "wxid_original_sender",
        "wxid_second_original_sender",
    ]
    assert all(child["sender_label"] == "Sender" for child in children)
    assert {
        "forwarded_original_type_unavailable",
        "app_subtype_unavailable_in_html",
    }.issubset(children[0]["flags"])
    assert result["owner_native_ids"] == ["wxid_owner"]
    assert result["counts"]["retained_forwarded_text"] == 2
    assert [row["status"] for row in audited] == [
        "retained_top_level_text",
        "forward_boundary",
        "retained_forwarded_text",
        "retained_forwarded_text",
        "forward_boundary",
    ]


@pytest.mark.parametrize(
    "card_body",
    [
        '<div class="appinfo"><span class="app-name msg-text">'
        "Application attribution without icon</span></div>",
        '<span class="msg-text">Unclassified outside payload</span>'
        '<div class="appinfo"><a href="https://invalid.example/app">'
        '<span class="app-name msg-text">Application attribution</span></a></div>',
        '<div class="appinfo"><img class="app-icon" src="never-open.jpg">'
        '<span class="app-name msg-text">Application attribution only</span></div>',
        '<a href="https://invalid.example/file"><span class="msg-text">'
        '[File: private.pdf]</span></a><div class="appinfo"></div>',
        '<a href="https://invalid.example/photo"><span class="msg-text">'
        '[Photo]</span></a><div class="appinfo"></div>',
        '<a href="https://invalid.example/blank"><span class="msg-text">'
        ' \n </span></a><div class="appinfo"><span class="app-name">App</span></div>',
        '<a href="https://invalid.example/title"><span class="msg-text">'
        "Unknown card without template</span></a>",
        '<a href="Chat_files/private.pdf"><span class="msg-text">'
        'Local file name</span></a><div class="appinfo"></div>',
        '<a href="https://invalid.example/title"><span class="msg-text">'
        'Video title</span></a><video src="never-open.mp4"></video>'
        '<div class="appinfo"></div>',
        '<a href="https://invalid.example/title"><span class="msg-text">'
        'Image title</span></a><img src="never-open.jpg">'
        '<div class="appinfo"></div>',
        '<a href="https://[invalid"><span class="msg-text">'
        'Malformed link</span></a><div class="appinfo"></div>',
    ],
)
def test_forwarded_card_metadata_excludes_attribution_files_media_and_bad_templates(
    tmp_path, card_body
):
    child = message("", kind="", classes="chat left fmsg").replace(
        '<span class="msg-text"><pre></pre></span>', card_body
    )
    result = export.parse_html_export(
        fixture_zip(
            tmp_path,
            nodes=[
                message("History", kind="49", local_id="parent"),
                boundary("<< History"),
                child,
                boundary("History Ends >>"),
            ],
        )
    )
    assert result["conversations"][0]["messages"][0]["forwarded_components"] == []
    assert result["counts"]["omitted_forwarded_non_text"] == 1


def test_misflagged_utf8_zip_names_are_recovered_without_renaming(tmp_path):
    path = fixture_zip(tmp_path, root="Bay Αιγαίο")
    data = bytearray(path.read_bytes())
    # Simulate the original export's UTF-8 bytes with an unset UTF-8 flag.
    with zipfile.ZipFile(path) as archive:
        offsets = [item.header_offset for item in archive.infolist()]
        central = archive.start_dir
    for offset in offsets:
        flags = struct.unpack_from("<H", data, offset + 6)[0]
        struct.pack_into("<H", data, offset + 6, flags & ~0x800)
    for _ in offsets:
        flags = struct.unpack_from("<H", data, central + 8)[0]
        struct.pack_into("<H", data, central + 8, flags & ~0x800)
        lengths = struct.unpack_from("<HHH", data, central + 28)
        central += 46 + sum(lengths)
    path.write_bytes(data)
    original = path.read_bytes()
    result = export.parse_html_export(path)
    assert result["conversations"][0]["member_path"] == "Bay Αιγαίο/Chat.html"
    assert all(item["filename_utf8_recovered"] for item in result["selected_members"])
    assert path.read_bytes() == original


def raw_member_zip(tmp_path, name):
    """Retain literal hostile names instead of letting the host ZIP writer sanitize them."""
    path = fixture_zip(tmp_path)
    with zipfile.ZipFile(path, "a") as archive:
        member = zipfile.ZipInfo()
        member.filename = member.orig_filename = name
        archive.writestr(member, b"ignored")
    with zipfile.ZipFile(path) as archive:
        assert archive.infolist()[-1].orig_filename == name
    return path


@pytest.mark.parametrize(
    "bad_member",
    [
        "../escape.txt",
        "/absolute.txt",
        "C:/drive.txt",
        "Export/../alias.txt",
        "Export\\escape.txt",
        "Export/alias.txt\x00hidden",
    ],
)
def test_unsafe_archive_paths_are_rejected_without_extraction(tmp_path, bad_member):
    path = raw_member_zip(tmp_path, bad_member)
    original = path.read_bytes()
    with pytest.raises(ValueError, match="Unsafe ZIP"):
        export.parse_html_export(path)
    assert path.read_bytes() == original


def test_windows_separator_normalization_cannot_hide_an_unsafe_original(
    tmp_path, monkeypatch,
):
    path = raw_member_zip(tmp_path, "Export\\escape.txt")
    sanitize = zipfile._sanitize_filename
    monkeypatch.setattr(
        zipfile, "_sanitize_filename",
        lambda name: sanitize(name.replace("\\", "/")),
    )
    with zipfile.ZipFile(path) as archive:
        member = archive.infolist()[-1]
        assert member.filename == "Export/escape.txt"
        assert member.orig_filename == "Export\\escape.txt"
    with pytest.raises(ValueError, match="Unsafe ZIP"):
        export.parse_html_export(path)


@pytest.mark.parametrize(
    "names",
    [
        ("Export/same.txt", "Export/same.txt"),
        ("Export/Café.txt", "Export/Café.txt"),
        ("Export/CASE.txt", "Export/case.txt"),
    ],
)
def test_duplicate_and_unicode_colliding_paths_are_rejected(tmp_path, names):
    path = fixture_zip(tmp_path)
    with zipfile.ZipFile(path, "a") as archive:
        for name in names:
            archive.writestr(name, "value")
    with pytest.raises(ValueError, match="Duplicate or Unicode"):
        export.parse_html_export(path)


def test_symlinks_are_rejected(tmp_path):
    path = fixture_zip(tmp_path)
    with zipfile.ZipFile(path, "a") as archive:
        info = zipfile.ZipInfo("Export/link")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "/outside")
    with pytest.raises(ValueError, match="symlink"):
        export.parse_html_export(path)


@pytest.mark.parametrize(
    "replacement",
    [
        "(function(){evil(); var msgArray = [];})();",
        async_page([message()]) + "externalCall();",
        async_page([42]),
        "var msgArray = [untrusted()];",
    ],
)
def test_scripts_are_data_only_and_unknown_wrappers_fail_closed(tmp_path, replacement):
    path = fixture_zip(
        tmp_path,
        pages=[[message()]],
        additional={"Export/Chat_files/Data/msg-1.js": replacement},
    )
    with pytest.raises(ValueError):
        export.parse_html_export(path)


def test_missing_page_and_declared_count_mismatch_fail_closed(tmp_path):
    path = fixture_zip(
        tmp_path,
        pages=[[message()]],
        additional={"Export/Chat_files/Data/msg-1.js": async_page([])},
    )
    with pytest.raises(ValueError, match="declared count"):
        export.parse_html_export(path)
    path = fixture_zip(
        tmp_path,
        pages=[[message()]],
        additional={
            "Export/Chat_files/Data/msg-1.js": async_page([message()]),
            "Export/extra.js": "unreviewed",
        },
    )
    with pytest.raises(ValueError, match="Unreferenced JavaScript"):
        export.parse_html_export(path)


def test_selected_text_and_tree_resource_limits(tmp_path, monkeypatch):
    path = fixture_zip(tmp_path)
    monkeypatch.setattr(export, "MAX_TEXT_MEMBER_BYTES", 20)
    with pytest.raises(ValueError, match="resource limit"):
        export.parse_html_export(path)
    monkeypatch.setattr(export, "MAX_TEXT_MEMBER_BYTES", 1024**2)
    monkeypatch.setattr(export, "MAX_HTML_DEPTH", 3)
    with pytest.raises(ValueError, match="nesting"):
        export.parse_html_export(path)


def test_repeated_local_ids_remain_separate_observations(tmp_path):
    result = export.parse_html_export(
        fixture_zip(
            tmp_path, nodes=[message("One", local_id="1"), message("Two", local_id="1")]
        )
    )
    records = result["conversations"][0]["messages"]
    assert [record["export_msgid"] for record in records] == ["1", "1"]
    assert [record["ordinal"] for record in records] == [1, 2]
    assert all("server_id" not in record for record in records)


def test_inline_emoji_preserves_original_text_token_without_opening_image(tmp_path):
    emoji = '<img src="https://invalid.example/emoji.png" title="Translated label" rawemoji="[Original token]" class="wxemoji">'
    result = export.parse_html_export(fixture_zip(tmp_path, nodes=[message(emoji)]))
    record = result["conversations"][0]["messages"][0]
    assert record["text"] == "[Original token]"
    assert "inline_emoji_restored_from_exported_token" in record["flags"]


def test_owner_candidates_scan_all_top_level_native_portraits(tmp_path):
    result = export.parse_html_export(
        fixture_zip(
            tmp_path, nodes=[message(native="owner_one"), message(native="owner_two")]
        )
    )
    assert result["owner_native_ids"] == ["owner_one", "owner_two"]
    assert result["owner_native_id_counts"] == {"owner_one": 1, "owner_two": 1}


@pytest.mark.parametrize("bad_member", ["Export/./alias.txt", "Export//alias.txt"])
def test_noncanonical_member_paths_are_rejected(tmp_path, bad_member):
    with pytest.raises(ValueError, match="Noncanonical ZIP"):
        export.parse_html_export(
            fixture_zip(tmp_path, additional={bad_member: "value"})
        )


def test_central_directory_budget_is_checked_before_zipfile_allocation(
    tmp_path, monkeypatch
):
    path = fixture_zip(tmp_path)
    monkeypatch.setattr(export, "MAX_CENTRAL_DIRECTORY_BYTES", 1)

    def unopened(*args, **kwargs):
        raise AssertionError("The central directory must be checked before ZipFile.")

    monkeypatch.setattr(zipfile, "ZipFile", unopened)
    with pytest.raises(ValueError, match="central directory"):
        export.parse_html_export(path)


def test_missing_media_fallbacks_are_deferred_but_literal_type1_text_is_kept(tmp_path):
    nodes = [
        message("[Photo]", kind="1", local_id="1"),
        message("[File: missing.pdf]", kind="49", local_id="2"),
        message("History", kind="49", local_id="3"),
        boundary("<< History"),
        message("[Photo]", kind="", classes="chat left fmsg", local_id="3_photo"),
        message(
            "[文件: missing.pdf]", kind="", classes="chat left fmsg", local_id="3_file"
        ),
        message("A text child", kind="", classes="chat left fmsg", local_id="3_text"),
        boundary("History Ends >>"),
    ]
    result = export.parse_html_export(fixture_zip(tmp_path, nodes=nodes))
    records = result["conversations"][0]["messages"]
    assert [record["text"] for record in records] == ["[Photo]", "History"]
    assert [item["text"] for item in records[1]["forwarded_components"]] == [
        "A text child"
    ]
    assert result["counts"]["deferred_top_level"] == 1
    assert result["counts"]["omitted_forwarded_non_text"] == 2


@pytest.mark.parametrize(
    ("default_name", "reviewed_name"),
    [
        ("MAX_ARCHIVE_BYTES", "archive_bytes"),
        ("MAX_SELECTED_TEXT_BYTES", "selected_text_bytes"),
        ("MAX_HTML_NODES", "html_nodes"),
        ("MAX_MESSAGE_NODES", "message_nodes"),
        ("MAX_MEMBERS", "archive_members"),
        ("MAX_CENTRAL_DIRECTORY_BYTES", "central_directory_bytes"),
    ],
)
def test_reviewed_limits_are_explicit_and_defaults_still_reject(
    tmp_path, monkeypatch, default_name, reviewed_name
):
    path = fixture_zip(tmp_path, pages=[[message("An asynchronous record")]])
    monkeypatch.setattr(export, default_name, 1)
    with pytest.raises(ValueError, match="resource limit"):
        export.parse_html_export(path)
    result = export.parse_html_export(
        path,
        reviewed_limits={reviewed_name: export._REVIEWED_HARD_LIMITS[reviewed_name]},
    )
    assert result["counts"]["retained_top_level_text"] == 2
    assert (
        result["effective_limits"][reviewed_name]
        == export._REVIEWED_HARD_LIMITS[reviewed_name]
    )


@pytest.mark.parametrize(
    "name",
    [
        "archive_bytes",
        "selected_text_bytes",
        "html_nodes",
        "message_nodes",
        "archive_members",
        "central_directory_bytes",
    ],
)
def test_explicit_reviewed_budgets_remain_enforced(tmp_path, name):
    path = fixture_zip(tmp_path, pages=[[message()]])
    with pytest.raises(ValueError, match="resource limit"):
        export.parse_html_export(path, reviewed_limits={name: 1})


@pytest.mark.parametrize(
    "limits",
    [
        {"archive_bytes": True},
        {"archive_bytes": 1.0},
        {"archive_bytes": 0},
        {"archive_bytes": -1},
        {"archive_bytes": None},
        {"unrecognized_limit": 100},
        {"archive_bytes": 32 * 1024**3 + 1},
        {"selected_text_bytes": 2 * 1024**3 + 1},
        {"html_nodes": 32_000_001},
        {"message_nodes": 3_000_001},
        {"archive_members": 250_001},
        {"archive_members": True},
        {"central_directory_bytes": 64 * 1024**2 + 1},
        {"central_directory_bytes": 1.0},
        {"allow_zip64": 1},
        {"allow_zip64": "true"},
        {"allow_zip64": None},
        "unbounded",
    ],
)
def test_reviewed_limits_reject_unknown_invalid_or_unbounded_inputs(tmp_path, limits):
    with pytest.raises(ValueError):
        export.parse_html_export(tmp_path / "not-opened.zip", reviewed_limits=limits)


def test_reviewed_limits_dataclass_and_fixed_member_guards(tmp_path, monkeypatch):
    profile = export.ReviewedExportLimits(
        archive_bytes=4 * 1024**3,
        selected_text_bytes=256 * 1024**2,
        html_nodes=3_000_000,
        message_nodes=300_000,
    )
    path = fixture_zip(tmp_path)
    result = export.parse_html_export(path, reviewed_limits=profile)
    assert result["effective_limits"]["message_nodes"] == 300_000
    monkeypatch.setattr(export, "MAX_TEXT_MEMBER_BYTES", 20)
    with pytest.raises(ValueError, match="resource limit"):
        export.parse_html_export(path, reviewed_limits=profile)


def test_forward_context_and_source_ordinals_survive_page_boundaries(tmp_path):
    pages = [
        [
            boundary("<< History"),
            message(
                "First child", local_id="80_first", kind="", classes="chat left fmsg"
            ),
        ],
        [
            message(
                "Second child", local_id="80_second", kind="", classes="chat left fmsg"
            ),
            boundary("History Ends >>"),
        ],
        [message("After the forwarded record", local_id="81")],
    ]
    result = export.parse_html_export(
        fixture_zip(
            tmp_path, nodes=[message("History", local_id="80", kind="49")], pages=pages
        )
    )
    records = result["conversations"][0]["messages"]
    assert [record["export_msgid"] for record in records] == ["80", "81"]
    assert [record["ordinal"] for record in records] == [1, 6]
    first, second = records[0]["forwarded_components"]
    assert (first["ordinal"], second["ordinal"]) == (3, 4)
    assert first["source_locator"] == {
        "member_path": "Export/Chat_files/Data/msg-1.js",
        "page": 1,
        "message_ordinal": 3,
        "page_message_ordinal": 2,
    }
    assert second["source_locator"]["page"] == 2
    assert second["source_locator"]["page_message_ordinal"] == 1
    assert result["counts"]["all_message_nodes"] == 6
    assert result["warnings"] == []


def test_previous_page_dom_is_released_before_reading_the_next_page(
    tmp_path, monkeypatch
):
    path = fixture_zip(
        tmp_path, pages=[[message("One")], [message("Two")], [message("Three")]]
    )
    references = []
    checked = []
    real_parse = export._parse_html
    real_open = zipfile.ZipFile.open

    def tracked(value, budget):
        root = real_parse(value, budget)
        references.extend(weakref.ref(node) for node in [root, *export._walk(root)])
        return root

    def check_release(self, name, *args, **kwargs):
        member = name.filename if isinstance(name, zipfile.ZipInfo) else name
        if member.endswith(("msg-2.js", "msg-3.js")):
            assert not any(reference() is not None for reference in references)
            checked.append(member)
        return real_open(self, name, *args, **kwargs)

    monkeypatch.setattr(export, "_parse_html", tracked)
    monkeypatch.setattr(zipfile.ZipFile, "open", check_release)
    result = export.parse_html_export(path)
    assert len(checked) == 2
    assert result["counts"]["retained_top_level_text"] == 4


def test_conversation_sink_receives_text_and_result_only_retains_metadata(tmp_path):
    index = '<html><body><table><tr><td><img src="Chat_files/Portrait/peer_one.jpg"></td><td><a href="Chat.html">First</a></td></tr><tr><td><img src="Other_files/Portrait/peer_two.jpg"></td><td><a href="Other.html">Second</a></td></tr></table></body></html>'
    other = (
        '<html><body><span class="sname">Second</span>'
        + message("Other conversation")
        + "</body></html>"
    )
    path = fixture_zip(
        tmp_path, additional={"Export/index.html": index, "Export/Other.html": other}
    )
    plain = export.parse_html_export(path)
    received = []
    result = export.parse_html_export(path, conversation_sink=received.append)
    assert [conversation["messages"] for conversation in received] == [
        conversation["messages"] for conversation in plain["conversations"]
    ]
    assert [conversation["messages"] for conversation in result["conversations"]] == [
        [],
        [],
    ]
    assert all(
        conversation["messages_omitted"] for conversation in result["conversations"]
    )
    assert result["conversation_sink_used"] is True
    assert plain["conversation_sink_used"] is False
    assert result["counts"] == plain["counts"]
    assert result["selected_members"] == plain["selected_members"]
    assert result["owner_native_id_counts"] == plain["owner_native_id_counts"]


def test_sink_errors_propagate_and_invalid_sinks_are_rejected(tmp_path):
    path = fixture_zip(tmp_path)
    with pytest.raises(ValueError, match="sink must be callable"):
        export.parse_html_export(path, conversation_sink=42)

    def failed_sink(conversation):
        raise RuntimeError("The caller must discard staged output.")

    with pytest.raises(RuntimeError, match="discard staged output"):
        export.parse_html_export(path, conversation_sink=failed_sink)


def zip64_fixture(tmp_path, *, sentinel_standard=True):
    """Upgrade a tiny valid archive to ZIP64 end metadata without giant files."""
    path = fixture_zip(tmp_path)
    data = path.read_bytes()
    end = data.rfind(b"PK\x05\x06")
    standard = list(struct.unpack_from("<4s4H2LH", data, end))
    count, central_bytes, central_offset = standard[4:7]
    record = struct.pack(
        "<4sQ2H2L4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        count,
        count,
        central_bytes,
        central_offset,
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, end, 1)
    if sentinel_standard:
        standard[3:7] = [0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF]
    path.write_bytes(data[:end] + record + locator + struct.pack("<4s4H2LH", *standard))
    return path, {"record": end, "locator": end + 56, "standard": end + 76}


@pytest.mark.parametrize("sentinel_standard", [False, True])
def test_complete_tiny_zip64_requires_opt_in_and_keeps_text_semantics(
    tmp_path, sentinel_standard
):
    path, positions = zip64_fixture(tmp_path, sentinel_standard=sentinel_standard)
    original = path.read_bytes()
    with pytest.raises(ValueError, match="ZIP64 exports require"):
        export.parse_html_export(path)
    result = export.parse_html_export(path, reviewed_limits={"allow_zip64": True})
    assert result["effective_limits"]["allow_zip64"] is True
    assert result["archive_structure"]["zip64"] is True
    assert result["archive_structure"]["members"] == 3
    assert (
        result["archive_structure"]["central_directory_offset"]
        + result["archive_structure"]["central_directory_bytes"]
        == positions["record"]
    )
    assert result["counts"]["retained_top_level_text"] == 1
    record = result["conversations"][0]["messages"][0]
    assert record["text"] == "Hello & goodbye"
    assert record["sender_native_id"] == "wxid_owner"
    assert record["sender_export_id"] == "public_alias"
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "part,offset,format,value,error",
    [
        ("locator", 0, "4s", b"BAD!", "locator"),
        ("locator", 4, "L", 1, "Split ZIP64"),
        ("locator", 8, "Q", 2**64 - 1, "invalid bounds"),
        ("locator", 16, "L", 2, "Split ZIP64"),
        ("record", 0, "4s", b"BAD!", "unsupported structure"),
        ("record", 4, "Q", 43, "unsupported structure"),
        ("record", 4, "Q", 45, "unsupported structure"),
        ("record", 14, "H", 44, "invalid required version"),
        ("record", 16, "L", 1, "Split ZIP64"),
        ("record", 20, "L", 1, "Split ZIP64"),
        ("record", 24, "Q", 2, "Split ZIP64"),
        ("record", 48, "Q", 1, "central directory has invalid bounds"),
        ("standard", 8, "H", 2, "Split ZIP"),
        ("standard", 12, "L", 1, "metadata disagree"),
        ("standard", 16, "L", 1, "metadata disagree"),
    ],
)
def test_zip64_malformed_end_metadata_fails_before_zipfile_allocation(
    tmp_path, monkeypatch, part, offset, format, value, error
):
    path, positions = zip64_fixture(tmp_path)
    data = bytearray(path.read_bytes())
    struct.pack_into("<" + format, data, positions[part] + offset, value)
    path.write_bytes(data)

    def unopened(*args, **kwargs):
        raise AssertionError("Malformed end metadata must fail before ZipFile.")

    monkeypatch.setattr(zipfile, "ZipFile", unopened)
    with pytest.raises(ValueError, match=error):
        export.parse_html_export(path, reviewed_limits={"allow_zip64": True})


@pytest.mark.parametrize(
    "name,value", [("archive_members", 1), ("central_directory_bytes", 1)]
)
def test_zip64_reviewed_central_directory_budgets_precede_zipfile_allocation(
    tmp_path, monkeypatch, name, value
):
    path, _ = zip64_fixture(tmp_path)

    def unopened(*args, **kwargs):
        raise AssertionError("The ZIP64 metadata budget must precede ZipFile.")

    monkeypatch.setattr(zipfile, "ZipFile", unopened)
    with pytest.raises(ValueError, match="central directory.*resource limit"):
        export.parse_html_export(
            path, reviewed_limits={"allow_zip64": True, name: value}
        )


def test_zip64_non_sentinel_standard_count_must_agree(tmp_path, monkeypatch):
    path, positions = zip64_fixture(tmp_path, sentinel_standard=False)
    data = bytearray(path.read_bytes())
    struct.pack_into("<HH", data, positions["standard"] + 8, 2, 2)
    path.write_bytes(data)

    def unopened(*args, **kwargs):
        raise AssertionError("Conflicting end counts must fail before ZipFile.")

    monkeypatch.setattr(zipfile, "ZipFile", unopened)
    with pytest.raises(ValueError, match="metadata disagree"):
        export.parse_html_export(path, reviewed_limits={"allow_zip64": True})


@pytest.mark.parametrize("cut", [1, 7, 20, 32])
def test_zip64_truncated_end_metadata_is_rejected(tmp_path, monkeypatch, cut):
    path, _ = zip64_fixture(tmp_path)
    path.write_bytes(path.read_bytes()[:-cut])

    def unopened(*args, **kwargs):
        raise AssertionError("Truncated metadata must fail before ZipFile.")

    monkeypatch.setattr(zipfile, "ZipFile", unopened)
    with pytest.raises(ValueError, match="complete supported ZIP"):
        export.parse_html_export(path, reviewed_limits={"allow_zip64": True})


def test_standard_central_directory_bounds_are_validated_before_zipfile(
    tmp_path, monkeypatch
):
    path = fixture_zip(tmp_path)
    data = bytearray(path.read_bytes())
    end = data.rfind(b"PK\x05\x06")
    struct.pack_into("<L", data, end + 16, 1)
    path.write_bytes(data)

    def unopened(*args, **kwargs):
        raise AssertionError("Invalid standard bounds must fail before ZipFile.")

    monkeypatch.setattr(zipfile, "ZipFile", unopened)
    with pytest.raises(ValueError, match="central directory has invalid bounds"):
        export.parse_html_export(path)


def test_default_profile_remains_conservative_and_zip64_is_not_implicit():
    profile = export._limits()
    assert profile.archive_bytes == 2 * 1024**3
    assert profile.selected_text_bytes == 128 * 1024**2
    assert profile.html_nodes == 1_000_000
    assert profile.message_nodes == 250_000
    assert profile.archive_members == 100_000
    assert profile.central_directory_bytes == 32 * 1024**2
    assert profile.allow_zip64 is False


def test_zip64_locator_can_precede_the_bounded_tail_when_comment_is_maximal(tmp_path):
    path, positions = zip64_fixture(tmp_path)
    data = bytearray(path.read_bytes())
    struct.pack_into("<H", data, positions["standard"] + 20, 65_535)
    path.write_bytes(data + b"x" * 65_535)
    result = export.parse_html_export(path, reviewed_limits={"allow_zip64": True})
    assert result["archive_structure"]["zip64"] is True
    assert result["counts"]["retained_top_level_text"] == 1


def test_node_audit_accounts_for_every_disposition_without_media_or_raw_bodies(
    tmp_path, monkeypatch
):
    nodes = [
        message("Retained parent", local_id="1", kind="49"),
        boundary("<< History"),
        message("Child", local_id="1_text", kind="", classes="chat left fmsg"),
        message("[Photo]", local_id="1_media", kind="", classes="chat left fmsg"),
        boundary("History Ends >>"),
        message("Unknown payload", local_id="2", kind="999", native="wxid_other"),
        message("Retained plain", local_id="3", kind="1"),
    ]
    path = fixture_zip(tmp_path, nodes=nodes)
    received = []
    metadata = []
    real_open = zipfile.ZipFile.open

    def only_text(self, name, *args, **kwargs):
        filename = name.filename if isinstance(name, zipfile.ZipInfo) else name
        assert filename.endswith((".html", ".js"))
        return real_open(self, name, *args, **kwargs)

    def audit(conversation, rows):
        assert "messages" not in conversation
        assert iter(rows) is rows
        metadata.append(conversation)
        received.extend(rows)

    monkeypatch.setattr(zipfile.ZipFile, "open", only_text)
    conversations = []
    result = export.parse_html_export(
        path, conversation_sink=conversations.append, node_audit_sink=audit
    )
    assert len(received) == result["counts"]["all_message_nodes"] == 7
    assert result["audited_message_nodes"] == 7
    assert result["node_audit_sink_used"] is True
    counts = {
        status: sum(row["status"] == status for row in received)
        for status in {
            "retained_top_level_text",
            "deferred_top_level",
            "forward_boundary",
            "retained_forwarded_text",
            "omitted_forwarded_non_text",
        }
    }
    assert counts == {
        "retained_top_level_text": 2,
        "deferred_top_level": 1,
        "forward_boundary": 2,
        "retained_forwarded_text": 1,
        "omitted_forwarded_non_text": 1,
    }
    for status, count in counts.items():
        key = "forward_boundaries" if status == "forward_boundary" else status
        assert result["counts"][key] == count
    assert [row["source_locator"]["message_ordinal"] for row in received] == list(
        range(1, 8)
    )
    assert all(
        "text" not in row and "forwarded_components" not in row for row in received
    )
    deferred = next(row for row in received if row["status"] == "deferred_top_level")
    assert deferred["sender_native_id"] == "wxid_other"
    assert deferred["sender_export_id"] == "public_alias"
    assert deferred["avatar_path"].endswith("/wxid_other.jpg")
    assert deferred["direction"] == "sent"
    assert deferred["classes"] == sorted(deferred["classes"])
    assert len(deferred["text_sha256"]) == 64
    assert metadata[0]["counts"] == conversations[0]["counts"]


def test_audit_sink_must_consume_all_rows_and_errors_propagate(tmp_path):
    path = fixture_zip(tmp_path, nodes=[message(), message(local_id="43")])
    with pytest.raises(ValueError, match="node audit sink must be callable"):
        export.parse_html_export(path, node_audit_sink=42)

    def partial(metadata, rows):
        next(rows)

    with pytest.raises(ValueError, match="consume every node"):
        export.parse_html_export(path, node_audit_sink=partial)

    def failed(metadata, rows):
        raise RuntimeError("Discard staged node metadata.")

    with pytest.raises(RuntimeError, match="Discard staged"):
        export.parse_html_export(path, node_audit_sink=failed)


def test_node_audit_writes_are_staged_until_final_archive_checks(tmp_path):
    path = fixture_zip(tmp_path, additional={"Export/unreferenced.js": "not executed"})
    staged = []

    def audit(metadata, rows):
        staged.extend(rows)

    with pytest.raises(ValueError, match="Unreferenced JavaScript"):
        export.parse_html_export(path, node_audit_sink=audit)
    assert len(staged) == 1
    assert staged[0]["status"] == "retained_top_level_text"


def test_node_audit_output_is_not_accepted_if_source_identity_changes(tmp_path):
    path = fixture_zip(tmp_path)
    staged = []

    def audit(metadata, rows):
        staged.extend(rows)
        state = path.stat()
        os.utime(path, ns=(state.st_atime_ns, state.st_mtime_ns + 1_000_000_000))

    with pytest.raises(ValueError, match="source archive changed"):
        export.parse_html_export(path, node_audit_sink=audit)
    assert len(staged) == 1


def rewrite_fixture(path, *, replacements=None, renamed=None):
    with zipfile.ZipFile(path) as archive:
        members = {info.filename: archive.read(info) for info in archive.infolist()}
    for old, new in (renamed or {}).items():
        members[new] = members.pop(old)
    members.update(replacements or {})
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)


def session_plan(path, mapping=None, unavailable=None):
    mapping = mapping or {"Export/Chat.html": "Export/Chat.html"}
    with zipfile.ZipFile(path) as archive:
        resolutions = [
            {
                "index_member_path": logical,
                "physical_member_path": physical,
                "main_html_sha256": hashlib.sha256(archive.read(physical)).hexdigest(),
                "basis": "Explicit review of source bytes and original index metadata.",
            }
            for logical, physical in mapping.items()
        ]
    return {
        "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "resolutions": resolutions,
        "unavailable_index_paths": unavailable or [],
    }


def case_collision_fixture(tmp_path, *, nodes=None):
    if nodes is None:
        nodes = [
            message("Owner reply", native="wxid_owner", local_id="1"),
            message(
                "Peer reply",
                native="wxid_actual_peer",
                local_id="2",
                classes="chat left",
            ),
        ]
    path = fixture_zip(tmp_path, nodes=nodes, peer="wxid_original_peer")
    index = (
        '<html><body><table><tr><td><img src="Portrait/wxid_original_peer.jpg"></td>'
        '<td><a href="Leon.html">Original (WeChat ID: old_alias)</a></td></tr>'
        '<tr><td><img src="Portrait/wxid_actual_peer.jpg"></td>'
        '<td><a href="leon.html">Actual (WeChat ID: actual_alias)</a></td></tr>'
        "</table></body></html>"
    )
    rewrite_fixture(
        path,
        renamed={"Export/Chat.html": "Export/Leon.html"},
        replacements={"Export/index.html": index},
    )
    return path


def correct_case_plan(path):
    return session_plan(
        path,
        {"Export/leon.html": "Export/Leon.html"},
        [
            {
                "index_member_path": "Export/Leon.html",
                "reason": "Its original bytes are unavailable; the surviving physical file belongs to the other indexed peer.",
            }
        ],
    )


def test_unicode_resolution_is_explicit_pinned_and_preserves_original_index_identity(
    tmp_path,
):
    logical, physical = "Export/Café.html", "Export/Café.html"
    path = fixture_zip(tmp_path, peer="wxid_peer")
    index = (
        '<html><body><table><tr><td><img src="Portrait/wxid_peer.jpg"></td>'
        '<td><a href="Café.html">Peer (WeChat ID: source_alias)</a></td></tr></table></body></html>'
    )
    rewrite_fixture(
        path,
        renamed={"Export/Chat.html": physical},
        replacements={"Export/index.html": index},
    )
    original = path.read_bytes()
    with pytest.raises(ValueError, match="Unreferenced HTML"):
        export.parse_html_export(path)
    metadata = []

    def audit(conversation, rows):
        metadata.append(conversation)
        assert all(row["source_locator"]["member_path"] == physical for row in rows)

    plan = session_plan(path, {logical: physical})
    result = export.parse_html_export(
        path, reviewed_session_plan=plan, node_audit_sink=audit
    )
    conversation = result["conversations"][0]
    assert conversation["index_member_path"] == logical
    assert conversation["member_path"] == physical
    assert conversation["index_avatar_native_id"] == "wxid_peer"
    assert conversation["index_public_id"] == "source_alias"
    assert (
        conversation["reviewed_resolution"]["main_html_sha256"]
        == plan["resolutions"][0]["main_html_sha256"]
    )
    assert conversation["reviewed_resolution"]["native_peer_evidence"] is None
    assert metadata[0]["reviewed_resolution"] == conversation["reviewed_resolution"]
    assert result["reviewed_session_plan_used"] is True
    assert result["unavailable_index_conversations"] == []
    assert path.read_bytes() == original


def test_reviewed_case_collision_uses_native_frames_and_retains_missing_identity(
    tmp_path,
):
    path = case_collision_fixture(tmp_path)
    result = export.parse_html_export(
        path, reviewed_session_plan=correct_case_plan(path)
    )
    conversation = result["conversations"][0]
    assert conversation["index_member_path"] == "Export/leon.html"
    assert conversation["member_path"] == "Export/Leon.html"
    assert conversation["index_avatar_native_id"] == "wxid_actual_peer"
    assert conversation["index_public_id"] == "actual_alias"
    evidence = conversation["reviewed_resolution"]["native_peer_evidence"]
    assert evidence == {
        "received_native_ids": ["wxid_actual_peer"],
        "sent_native_ids": ["wxid_owner"],
        "received_observations": 1,
        "sent_observations": 1,
    }
    assert (
        conversation["reviewed_resolution"]["declared_data_path"] == "Chat_files/Data"
    )
    (missing,) = result["unavailable_index_conversations"]
    assert missing["index_member_path"] == "Export/Leon.html"
    assert missing["index_avatar_native_id"] == "wxid_original_peer"
    assert missing["index_public_id"] == "old_alias"
    assert (
        missing["reason"]
        == correct_case_plan(path)["unavailable_index_paths"][0]["reason"]
    )
    assert result["warnings"] == []
    assert len(conversation["messages"]) == 2


def test_even_exact_case_mapping_cannot_keep_the_wrong_index_peer(tmp_path):
    path = case_collision_fixture(tmp_path)
    wrong = session_plan(
        path,
        {"Export/Leon.html": "Export/Leon.html"},
        [{"index_member_path": "Export/leon.html", "reason": "Reviewed missing entry"}],
    )
    with pytest.raises(ValueError, match="independent native-frame proof"):
        export.parse_html_export(path, reviewed_session_plan=wrong)


@pytest.mark.parametrize(
    "nodes",
    [
        [message(native="wxid_owner")],
        [message(native="wxid_actual_peer", classes="chat left")],
        [
            message(native="wxid_owner"),
            message(native="wxid_actual_peer", classes="chat left"),
            message(native="wxid_other", classes="chat left"),
        ],
        [
            message(native="wxid_actual_peer"),
            message(native="wxid_actual_peer", classes="chat left"),
        ],
        [message(native="wxid_owner"), message(native="", classes="chat left")],
    ],
)
def test_reassigned_direct_peer_needs_unambiguous_received_and_distinct_sent_frames(
    tmp_path, nodes
):
    path = case_collision_fixture(tmp_path, nodes=nodes)
    with pytest.raises(ValueError, match="independent native-frame proof"):
        export.parse_html_export(path, reviewed_session_plan=correct_case_plan(path))


@pytest.mark.parametrize(
    "failure",
    [
        "duplicate_index",
        "duplicate_physical",
        "unlisted_index",
        "extra_reference",
        "unsafe_reference",
        "unsafe_physical",
        "unclaimed_html",
        "identity_override",
    ],
)
def test_reviewed_plan_rejects_incomplete_duplicate_unsafe_or_override_claims(
    tmp_path, failure
):
    path = case_collision_fixture(tmp_path)
    if failure == "unclaimed_html":
        rewrite_fixture(path, replacements={"Export/Unclaimed.html": "<html></html>"})
    plan = correct_case_plan(path)
    if failure == "duplicate_index":
        plan["resolutions"].append(dict(plan["resolutions"][0]))
    elif failure == "duplicate_physical":
        duplicate = {**plan["resolutions"][0], "index_member_path": "Export/Leon.html"}
        plan["resolutions"].append(duplicate)
        plan["unavailable_index_paths"] = []
    elif failure == "unlisted_index":
        plan["unavailable_index_paths"] = []
    elif failure == "extra_reference":
        plan["unavailable_index_paths"].append(
            {"index_member_path": "Export/Unknown.html", "reason": "Not in index"}
        )
    elif failure == "unsafe_reference":
        plan["unavailable_index_paths"][0]["index_member_path"] = "../unsafe.html"
    elif failure == "unsafe_physical":
        plan["resolutions"][0]["physical_member_path"] = "../unsafe.html"
    elif failure == "identity_override":
        plan["resolutions"][0]["index_avatar_native_id"] = "invented_peer"
    with pytest.raises(ValueError):
        export.parse_html_export(path, reviewed_session_plan=plan)


@pytest.mark.parametrize("stale", ["archive", "main_html"])
def test_reviewed_plan_stale_sha_never_reaches_a_sink(tmp_path, stale):
    path = fixture_zip(tmp_path)
    plan = session_plan(path)
    if stale == "archive":
        plan["archive_sha256"] = "0" * 64
    else:
        plan["resolutions"][0]["main_html_sha256"] = "0" * 64

    def unopened(*args):
        raise AssertionError("A stale reviewed source must not reach a sink.")

    with pytest.raises(ValueError, match="SHA-256"):
        export.parse_html_export(
            path,
            reviewed_session_plan=plan,
            conversation_sink=unopened,
            node_audit_sink=unopened,
        )


def test_reviewed_plan_does_not_waive_unreferenced_javascript_or_final_identity(
    tmp_path,
):
    path = fixture_zip(tmp_path, additional={"Export/orphan.js": "never executed"})
    staged = []
    with pytest.raises(ValueError, match="Unreferenced JavaScript"):
        export.parse_html_export(
            path,
            reviewed_session_plan=session_plan(path),
            conversation_sink=staged.append,
        )
    assert len(staged) == 1

    path = fixture_zip(tmp_path)
    plan = session_plan(path)

    def changed(conversation):
        state = path.stat()
        os.utime(path, ns=(state.st_atime_ns, state.st_mtime_ns + 1_000_000_000))

    with pytest.raises(ValueError, match="source archive changed"):
        export.parse_html_export(
            path, reviewed_session_plan=plan, conversation_sink=changed
        )
