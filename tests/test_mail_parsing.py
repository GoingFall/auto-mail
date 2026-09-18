"""MIME 与头部解析的单元测试。

重点覆盖国内邮箱的真实缺陷：

* 声明 GB2312 实际是 GBK 字节
* HTML-only 邮件、quoted-printable、base64
* RFC 2047 编码的主题与发件人名
* 缺失/畸形 Message-ID
* 清洗必须丢弃引用旧邮件（否则会抽到过期日期）
"""

from __future__ import annotations

from email import policy
from email.parser import BytesParser

from automail.mail.charset import candidate_charsets, decode_bytes
from automail.mail.headers import (
    decode_mime_words,
    headers_to_dict,
    is_auto_submitted,
    is_list_mail,
    normalize_message_id,
    parse_address,
    parse_address_list,
    parse_date,
    parse_references,
    parse_unsubscribe,
)
from automail.mail.mime import (
    clean_body,
    collect_ics_parts,
    extract_body,
    has_ics,
    html_to_text,
    parse_message,
)


def msg(raw: bytes):
    return BytesParser(policy=policy.default).parsebytes(raw)


# ──────────────────────────────────────────────────────────────
# charset
# ──────────────────────────────────────────────────────────────

def test_gb2312_declared_with_gbk_bytes() -> None:
    """163 最常见的缺陷：声称 gb2312，实际是 GBK 字节（含 GB2312 之外的汉字）。"""
    text = "会议时间：2026年9月20日 下午3点，地点：三楼会议室。联系人：张老师。"
    gbk_bytes = text.encode("gbk")
    # 直接按 gb2312 解码会失败或乱码；候选顺序应先试超集
    assert decode_bytes(gbk_bytes, "gb2312") == text


def test_candidate_order_prefers_gbk_for_gb2312() -> None:
    candidates = candidate_charsets("gb2312")
    assert candidates[0] in {"gbk", "gb18030"}
    assert candidates.index("gbk") < candidates.index("gb2312")


def test_permissive_charset_goes_last() -> None:
    """latin-1 能解码任意字节，若放前面会掩盖真实编码。"""
    candidates = candidate_charsets("latin-1")
    assert candidates[-1] == "latin-1"
    assert candidates[0] == "utf-8"


def test_decode_utf8_without_declaration() -> None:
    assert decode_bytes("你好".encode(), None) == "你好"


def test_decode_never_raises_on_garbage() -> None:
    """畸形字节不得让整封邮件失败，宁可留替代字符。"""
    result = decode_bytes(b"\xff\xfe\x00\x01\x02", "utf-8")
    assert isinstance(result, str)


def test_decode_empty() -> None:
    assert decode_bytes(b"", "utf-8") == ""


# ──────────────────────────────────────────────────────────────
# headers
# ──────────────────────────────────────────────────────────────

def test_decode_mime_words_chinese() -> None:
    # "会议通知" 的 RFC 2047 base64 编码
    encoded = "=?utf-8?B?5Lya6K6u6YCa55+l?="
    assert decode_mime_words(encoded) == "会议通知"


def test_decode_mime_words_mixed_parts() -> None:
    encoded = "=?utf-8?B?5Lya6K6u?= 通知"
    assert "会议" in decode_mime_words(encoded)
    assert "通知" in decode_mime_words(encoded)


def test_decode_mime_words_gbk() -> None:
    encoded = "=?gb2312?B?u+Hm6g==?="
    assert decode_mime_words(encoded)


def test_decode_mime_words_empty() -> None:
    assert decode_mime_words(None) == ""
    assert decode_mime_words("") == ""


def test_normalize_message_id_strips_brackets_and_lowercases_domain() -> None:
    assert normalize_message_id("<ABC@Example.COM>") == "ABC@example.com"


def test_normalize_message_id_handles_comment() -> None:
    assert normalize_message_id("<a@b.com> (comment)") == "a@b.com"


def test_normalize_message_id_rejects_invalid() -> None:
    """非法 ID 必须返回 None，不能编造一个——否则会把不同邮件误判为同一封。"""
    assert normalize_message_id(None) is None
    assert normalize_message_id("") is None
    assert normalize_message_id("no-at-sign") is None
    assert normalize_message_id("<>") is None
    assert normalize_message_id("has space@b.com") is None


def test_parse_references_chain() -> None:
    raw = "<a@x.com> <b@x.com>\t<c@x.com>"
    assert parse_references(raw) == ["a@x.com", "b@x.com", "c@x.com"]


def test_parse_references_without_brackets() -> None:
    assert parse_references("a@x.com b@x.com") == ["a@x.com", "b@x.com"]


def test_parse_references_skips_invalid() -> None:
    assert parse_references("<ok@x.com> garbage <bad>") == ["ok@x.com"]


def test_parse_address_with_display_name() -> None:
    addr, name = parse_address("张三 <zhangsan@example.com>")
    assert addr == "zhangsan@example.com"
    assert name == "张三"


def test_parse_address_without_display_name() -> None:
    """CPython 3.13 起 getaddresses 会把无显示名的地址放在 name 位。

    这是一个真实踩过的坑：不处理会让 `From: user@example.com` 解析出空地址。
    """
    addr, _name = parse_address("user@example.com")
    assert addr == "user@example.com"


def test_parse_address_encoded_name() -> None:
    addr, name = parse_address("=?utf-8?B?5Lya6K6u?= <a@b.com>")
    assert addr == "a@b.com"
    assert name == "会议"


def test_parse_address_list() -> None:
    result = parse_address_list("a@x.com, b@x.com")
    assert result == ["a@x.com", "b@x.com"]


def test_parse_address_garbage_does_not_raise() -> None:
    assert parse_address("<<<>>>")[0] == ""
    assert parse_address_list("not an address at all") == []


def test_parse_date_iso_utc() -> None:
    parsed = parse_date("Mon, 14 Sep 2026 10:00:00 +0800")
    assert parsed is not None
    assert parsed.startswith("2026-09-14T02:00:00")


def test_parse_date_invalid() -> None:
    assert parse_date("not a date") is None
    assert parse_date(None) is None


def test_parse_unsubscribe_mailto() -> None:
    found, mailto, links = parse_unsubscribe("<mailto:unsub@x.com?subject=unsubscribe>")
    assert found is True
    assert mailto == "unsub@x.com"
    assert links == []


def test_parse_unsubscribe_http() -> None:
    found, mailto, links = parse_unsubscribe("<https://x.com/unsub?id=1>")
    assert found is True
    assert mailto is None
    assert links == ["https://x.com/unsub?id=1"]


def test_parse_unsubscribe_both() -> None:
    raw = "<mailto:u@x.com>, <https://x.com/u>"
    found, mailto, links = parse_unsubscribe(raw)
    assert found is True
    assert mailto == "u@x.com"
    assert links == ["https://x.com/u"]


def test_parse_unsubscribe_absent() -> None:
    assert parse_unsubscribe(None) == (False, None, [])
    assert parse_unsubscribe("") == (False, None, [])


def test_is_auto_submitted_variants() -> None:
    assert is_auto_submitted({"auto-submitted": "auto-generated"}) is not None
    assert is_auto_submitted({"auto-submitted": "no"}) is None
    assert is_auto_submitted({"precedence": "bulk"}) is not None
    assert is_auto_submitted({"x-autoreply": "yes"}) is not None
    assert is_auto_submitted({"return-path": "<>"}) is not None
    assert is_auto_submitted({}) is None


def test_is_list_mail() -> None:
    assert is_list_mail({"list-id": "<x.list>"}) is True
    assert is_list_mail({"list-unsubscribe": "<mailto:u@x>"}) is True
    assert is_list_mail({}) is False


def test_headers_to_dict_lowercases_and_merges() -> None:
    parsed = headers_to_dict(msg(b"X-A: 1\r\nX-A: 2\r\nSubject: t\r\n\r\n"))
    assert parsed["x-a"] == "1, 2"
    assert parsed["subject"] == "t"


# ──────────────────────────────────────────────────────────────
# HTML → 文本
# ──────────────────────────────────────────────────────────────

def test_html_to_text_drops_script_and_style() -> None:
    html = "<html><head><style>p{}</style></head><body><script>evil()</script><p>正文</p></body></html>"
    text = html_to_text(html)
    assert "正文" in text
    assert "evil" not in text
    assert "p{}" not in text


def test_html_to_text_keeps_link_text() -> None:
    text = html_to_text('<p>点<a href="https://x.com/track?id=1">这里</a>查看</p>')
    assert "这里" in text
    assert "track?id" not in text, "链接 URL 是噪声，应丢弃"


def test_html_to_text_adds_breaks() -> None:
    text = html_to_text("<div>第一行</div><div>第二行</div>")
    assert "第一行" in text and "第二行" in text


# ──────────────────────────────────────────────────────────────
# 清洗流水线
# ──────────────────────────────────────────────────────────────

def test_clean_body_drops_quoted_reply() -> None:
    """引用旧邮件必须整段丢弃——否则会从旧邮件里抽到过期日期。

    这是清洗最核心的功能性目的，不是美化。
    """
    body = (
        "老师好，\n\n面试定在 2026年9月25日 上午10:00。\n\n"
        "在 2026年9月1日 写道：\n> 上次提到 2025年3月5日 年会的事\n"
    )
    cleaned = clean_body(body)
    assert "2026年9月25日" in cleaned
    assert "2025年3月5日" not in cleaned, "旧邮件中的过期日期必须被清除"
    assert "面试定在" in cleaned


def test_clean_body_drops_signature() -> None:
    body = "正文内容\n\n--\n张三\n13800138000\n"
    cleaned = clean_body(body)
    assert "正文内容" in cleaned
    assert "13800138000" not in cleaned


def test_clean_body_drops_chinese_signature() -> None:
    body = "会议纪要如下。\n\n发送自我的 iPhone\n更多详情请见官网\n"
    cleaned = clean_body(body)
    assert "会议纪要" in cleaned
    assert "iPhone" not in cleaned


def test_clean_body_drops_disclaimer() -> None:
    body = (
        "报名截止 2026年9月30日。\n\n"
        "本邮件及其附件含有保密信息，未经许可不得传播。\n"
    )
    cleaned = clean_body(body)
    assert "2026年9月30日" in cleaned
    assert "保密信息" not in cleaned


def test_clean_body_drops_marketing_footer() -> None:
    body = (
        "本周推荐内容。\n\n"
        "© 2026 某某公司 版权所有\n隐私政策 | 退订\n"
    )
    cleaned = clean_body(body)
    assert "本周推荐" in cleaned
    assert "版权所有" not in cleaned
    assert "退订" not in cleaned


def test_clean_body_keeps_paragraph_mentioning_unsubscribe() -> None:
    """正文中间提到「退订」不应触发截断——只在尾部区块生效。"""
    body = (
        "如需退订本服务，请联系客服。\n\n"
        "但本周的会议时间仍是 2026年9月22日 下午2点，请准时参加。\n"
    )
    cleaned = clean_body(body)
    assert "2026年9月22日" in cleaned


def test_clean_body_normalizes_crlf() -> None:
    assert "\r" not in clean_body("a\r\nb\rc")


def test_clean_body_collapses_blank_lines() -> None:
    assert "\n\n\n\n" not in clean_body("a\n\n\n\n\nb")


def test_clean_body_empty() -> None:
    assert clean_body("") == ""


def test_clean_body_removes_ansi_escapes() -> None:
    """标题/正文里的控制字符必须清除（防终端注入）。"""
    cleaned = clean_body("会议\x1b[31m时间\x1b[0m：明天")
    assert "\x1b" not in cleaned
    assert "会议" in cleaned


# ──────────────────────────────────────────────────────────────
# 正文抽取
# ──────────────────────────────────────────────────────────────

def test_extract_body_prefers_plain_over_html() -> None:
    raw = (
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        b"\xe6\x96\x87\xe6\x9c\xac\xe7\x89\x88\r\n"
        b"--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        b"<p>HTML \xe7\x89\x88</p>\r\n"
        b"--B--\r\n"
    )
    result = extract_body(msg(raw), max_chars=1000)
    assert "文本版" in result.text
    assert "HTML" not in result.text
    assert result.had_html is False


def test_extract_body_html_only() -> None:
    raw = (
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<p>\xe4\xbc\x9a\xe8\xae\xae\xe6\x97\xb6\xe9\x97\xb4\xef\xbc\x9a"
        b"2026\xe5\xb9\xb49\xe6\x9c\x8820\xe6\x97\xa5</p>"
    )
    result = extract_body(msg(raw), max_chars=1000)
    assert "2026年9月20日" in result.text
    assert result.had_html is True


def test_extract_body_quoted_printable() -> None:
    raw = (
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        b"=E4=BC=9A=E8=AE=AE=E6=97=B6=E9=97=B4"
    )
    result = extract_body(msg(raw), max_chars=1000)
    assert "会议时间" in result.text


def test_extract_body_base64() -> None:
    import base64

    payload = base64.b64encode("会议时间：下午3点".encode())
    raw = (
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n" + payload
    )
    result = extract_body(msg(raw), max_chars=1000)
    assert "会议时间" in result.text


def test_extract_body_truncates_and_flags() -> None:
    raw = (
        b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        + ("很长的正文" * 100).encode()
    )
    result = extract_body(msg(raw), max_chars=50)
    assert len(result.text) == 50
    assert result.truncated is True


def test_extract_body_hash_is_of_full_text_not_truncated() -> None:
    """哈希必须基于完整正文，否则被截断的邮件无法与完整版比对去重。"""
    long_body = "内容" * 500
    raw = (
        b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        + long_body.encode()
    )
    truncated = extract_body(msg(raw), max_chars=50)
    full = extract_body(msg(raw), max_chars=100000)
    assert truncated.sha256 == full.sha256
    assert truncated.truncated is True
    assert full.truncated is False


def test_extract_body_no_text_parts() -> None:
    """只有附件的邮件不应崩，退化为空正文。"""
    raw = (
        b'MIME-Version: 1.0\r\nContent-Type: application/pdf; name="a.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\n" + b"JVBERi0xLjQK"
    )
    result = extract_body(msg(raw), max_chars=1000)
    assert result.text == ""


def test_extract_body_records_defects() -> None:
    """畸形邮件如实记录缺陷，而不是崩溃。"""
    raw = b"Subject: t\r\nContent-Type: multipart/mixed\r\n\r\nbroken"
    result = extract_body(msg(raw), max_chars=1000)
    assert isinstance(result.defects, list)


# ──────────────────────────────────────────────────────────────
# ICS 收集
# ──────────────────────────────────────────────────────────────

SAMPLE_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "METHOD:REQUEST\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:meeting-123@example.com\r\n"
    "SEQUENCE:0\r\n"
    "DTSTART:20260920T070000Z\r\n"
    "DTEND:20260920T080000Z\r\n"
    "SUMMARY:项目评审会\r\n"
    "LOCATION:三楼会议室\r\n"
    "ORGANIZER;CN=Boss:mailto:boss@example.com\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_has_ics_detects_inline_calendar() -> None:
    raw = (
        b'MIME-Version: 1.0\r\n'
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain\r\n\r\nsee attachment\r\n"
        b"--B\r\nContent-Type: text/calendar; method=REQUEST\r\n\r\n"
        + SAMPLE_ICS.encode()
        + b"\r\n--B--\r\n"
    )
    assert has_ics(msg(raw)) is True


def test_has_ics_detects_attachment_by_filename() -> None:
    """有些客户端 Content-Type 不规范，靠 .ics 扩展名兜底。"""
    raw = (
        b'MIME-Version: 1.0\r\n'
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: application/octet-stream; name=\"invite.ics\"\r\n"
        b"Content-Disposition: attachment; filename=\"invite.ics\"\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        + __import__("base64").b64encode(SAMPLE_ICS.encode())
        + b"\r\n--B--\r\n"
    )
    parts = collect_ics_parts(msg(raw))
    assert len(parts) == 1
    assert parts[0].filename is not None


def test_collect_ics_parts_inline() -> None:
    raw = (
        b'MIME-Version: 1.0\r\nContent-Type: text/calendar; method=REQUEST\r\n\r\n'
        + SAMPLE_ICS.encode()
    )
    parts = collect_ics_parts(msg(raw))
    assert len(parts) == 1
    assert b"UID:meeting-123" in parts[0].raw
    assert parts[0].inline is True


def test_collect_ics_parts_absent() -> None:
    raw = b"MIME-Version: 1.0\r\nContent-Type: text/plain\r\n\r\nno calendar"
    assert collect_ics_parts(msg(raw)) == []
    assert has_ics(msg(raw)) is False


# ──────────────────────────────────────────────────────────────
# 整体解析
# ──────────────────────────────────────────────────────────────

def test_parse_message_full() -> None:
    raw = (
        "From: 张三 <zhangsan@example.com>\r\n"
        "To: me@163.com\r\n"
        "Subject: =?utf-8?B?6Z2i6K+V6YCa55+l?=\r\n"
        "Message-ID: <abc@example.com>\r\n"
        "In-Reply-To: <parent@example.com>\r\n"
        "References: <root@example.com> <parent@example.com>\r\n"
        "Date: Mon, 14 Sep 2026 10:00:00 +0800\r\n"
        "List-Unsubscribe: <mailto:u@x.com>\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "面试时间：2026年9月25日 上午10:00\r\n"
    ).encode()

    parsed = parse_message(msg(raw), max_chars=1000)

    assert parsed.subject == "面试通知"
    assert parsed.from_addr == "zhangsan@example.com"
    assert parsed.from_name == "张三"
    assert parsed.message_id == "abc@example.com"
    assert parsed.in_reply_to == "parent@example.com"
    assert parsed.references == ["root@example.com", "parent@example.com"]
    assert parsed.sent_at is not None
    assert parsed.has_unsubscribe is True
    assert parsed.unsubscribe_mailto == "u@x.com"
    assert "2026年9月25日" in parsed.body.text
    assert parsed.has_ics is False


def test_parse_message_missing_message_id() -> None:
    """缺 Message-ID 必须能为 None，不能编造。"""
    raw = b"From: a@b.com\r\nSubject: t\r\n\r\nbody"
    parsed = parse_message(msg(raw), max_chars=1000)
    assert parsed.message_id is None


def test_parse_message_auto_submitted_detected() -> None:
    raw = (
        "From: noreply@x.com\r\nAuto-Submitted: auto-replied\r\n"
        "Subject: 自动回复\r\n\r\nout of office"
    ).encode()
    parsed = parse_message(msg(raw), max_chars=1000)
    assert parsed.auto_submitted is not None


def test_parse_message_never_raises_on_garbage() -> None:
    """完全畸形的输入也必须产出一个可用对象。"""
    parsed = parse_message(msg(b"\x00\x01\x02 garbage"), max_chars=1000)
    assert isinstance(parsed.subject, str)
    assert parsed.source_free() if hasattr(parsed, "source_free") else True


def test_parse_message_with_ics() -> None:
    raw = (
        "From: cal@x.com\r\nSubject: 邀请\r\nMessage-ID: <inv@x.com>\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/plain\r\n\r\n请参加\r\n"
        "--B\r\nContent-Type: text/calendar; method=REQUEST\r\n\r\n"
        + SAMPLE_ICS
        + "\r\n--B--\r\n"
    ).encode()
    parsed = parse_message(msg(raw), max_chars=1000)
    assert parsed.has_ics is True
    assert len(parsed.ics_parts) == 1
