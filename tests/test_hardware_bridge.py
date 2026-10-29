from hardware_bridge import parse_uid_line


def test_reader_banner_is_ignored():
    assert parse_uid_line(b"NFC reader ready\r\n") is None


def test_labelled_uid_is_normalized():
    assert parse_uid_line(b"UID: 04:58:51:EA:0B:13:90\r\n") == "045851EA0B1390"


def test_raw_uid_is_supported():
    assert parse_uid_line("045851ea0b1390") == "045851EA0B1390"


def test_invalid_serial_text_is_ignored():
    assert parse_uid_line("connecting...") is None


def test_empty_line_is_ignored():
    assert parse_uid_line(b"\r\n") is None


def test_equal_uid_scans_have_stable_value():
    first = parse_uid_line("UID: 045851EA0B1390")
    second = parse_uid_line("UID: 045851EA0B1390")
    assert first == second
