"""Tests SM6 Plex navigator SOAP helpers."""

from dlna.sm6_navigator import (
    build_is_registered_navigator_name_body,
    build_register_named_navigator_body,
    parse_is_registered_navigator_name,
    parse_queue_folder_result,
    parse_ret_navigator_id,
)


def test_parse_is_registered_navigator_name_registered() -> None:
    xml = """
    <s:Envelope>
      <IsRegistered>1</IsRegistered>
      <RetNavigatorId>nav-42</RetNavigatorId>
    </s:Envelope>
    """
    registered, navigator_id = parse_is_registered_navigator_name(xml)
    assert registered is True
    assert navigator_id == "nav-42"


def test_parse_is_registered_navigator_name_not_registered() -> None:
    xml = "<IsRegistered>0</IsRegistered>"
    registered, navigator_id = parse_is_registered_navigator_name(xml)
    assert registered is False
    assert navigator_id is None


def test_parse_ret_navigator_id_from_register_response() -> None:
    xml = "<RetNavigatorId>abc123</RetNavigatorId>"
    assert parse_ret_navigator_id(xml) == "abc123"


def test_build_is_registered_navigator_name_escapes_xml() -> None:
    body = build_is_registered_navigator_name_body('Plex "test" & more')
    assert "&amp;" in body
    assert 'Plex "test"' in body


def test_build_register_named_navigator_includes_name() -> None:
    body = build_register_named_navigator_body("My Plex Server")
    assert "My Plex Server" in body
    assert "RegisterNamedNavigator" in body


def test_parse_queue_folder_result_ok_and_missing() -> None:
    assert parse_queue_folder_result("<Result>OK</Result>") == "OK"
    assert parse_queue_folder_result("<s:Body></s:Body>") is None
