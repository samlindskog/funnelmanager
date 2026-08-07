"""Pure-transform tests for the sync connectors' defensive parsers."""

from app.sync.base import as_list, json_list, parse_dt, split_address
from app.sync.sources import message_text, parse_message_fields


def test_split_address_forms():
    assert split_address("Jane Roe <Jane@Acme.COM>") == ("Jane Roe", "jane@acme.com")
    assert split_address('"Roe, Jane" <j@a.co>') == ("Roe, Jane", "j@a.co")
    assert split_address("bare@addr.io") == ("", "bare@addr.io")
    assert split_address("") == ("", "")


def test_json_list_accepts_both_shapes():
    assert json_list(["a", "b"]) == ["a", "b"]
    assert json_list('["x","y"]') == ["x", "y"]
    assert json_list("not json") == []
    assert json_list(None) == []


def test_as_list_tolerates_shape_drift():
    rows = [{"id": 1}]
    assert as_list(rows) == rows
    assert as_list({"sessions": rows}, "sessions") == rows
    assert as_list({"anything": rows}) == rows  # first list value fallback
    assert as_list({"n": 3}) == []
    assert as_list(None) == []


def test_direction_outbound_via_sent_label():
    fields = parse_message_fields(
        {
            "gmail_id": "g1",
            "label_ids": '["SENT"]',
            "from_addr": "Me <me@mybox.com>",
            "to_addrs": '["Jane Roe <jane@acme.com>"]',
            "subject": "hello",
            "internal_date": "2026-08-01T10:00:00Z",
        },
        account_email="me@mybox.com",
        owner="sam",
    )
    assert fields["direction"] == "outbound"
    assert fields["counterparty_email"] == "jane@acme.com"
    assert fields["counterparty_name"] == "Jane Roe"
    assert fields["owner"] == "sam"
    assert fields["occurred_at"] is not None


def test_direction_outbound_via_from_match_without_sent_label():
    fields = parse_message_fields(
        {"gmail_id": "g2", "from_addr": "me@mybox.com", "to_addrs": '["x@y.z"]'},
        account_email="me@mybox.com",
        owner="sam",
    )
    assert fields["direction"] == "outbound"
    assert fields["counterparty_email"] == "x@y.z"


def test_direction_inbound_counterparty_is_sender():
    fields = parse_message_fields(
        {"gmail_id": "g3", "label_ids": '["INBOX"]', "from_addr": "Jane <jane@acme.com>"},
        account_email="me@mybox.com",
        owner="sam",
    )
    assert fields["direction"] == "inbound"
    assert fields["counterparty_email"] == "jane@acme.com"


def test_message_text_extraction_shapes():
    assert message_text({"content": "plain"}) == "plain"
    assert (
        message_text({"content": {"parts": [{"content": "a"}, {"content": " b "}, {"other": 1}]}})
        == "a\nb"
    )
    assert message_text({"content": {"parts": ["raw", ""]}}) == "raw"
    assert message_text({"content": None}) == ""


def test_parse_dt_tolerance():
    assert parse_dt("2026-08-01T10:00:00Z") is not None
    assert parse_dt("") is None
    assert parse_dt("garbage") is None


def test_message_fields_accept_wire_date_key():
    fields = parse_message_fields(
        {"gmail_id": "g9", "from_addr": "x@y.z", "date": "2026-08-01T10:00:00Z"},
        account_email="me@box.com",
        owner="sam",
    )
    assert fields["occurred_at"] is not None
