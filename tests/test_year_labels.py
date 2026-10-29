from app import create_app, display_year, normalize_year, year_code


def test_year_labels_normalize_to_existing_numeric_database_values():
    assert normalize_year("First Year") == "1"
    assert normalize_year("second year") == "2"
    assert normalize_year("Third Year") == "3"
    assert normalize_year("Fourth Year") == "4"
    assert normalize_year("Year 2") == "2"
    assert normalize_year("2") == "2"
    assert display_year("1") == "First Year"
    assert display_year("2") == "Second Year"
    assert display_year("3") == "Third Year"
    assert display_year("4") == "Fourth Year"
    assert year_code("Second Year") == "2"
    assert display_year("5") == "Legacy year 5"


def test_year_five_is_not_a_supported_new_year_choice():
    try:
        normalize_year("5")
    except ValueError as exc:
        assert "First Year" in str(exc) and "Fourth Year" in str(exc)
    else:
        raise AssertionError("Year 5 should not be accepted")


def test_year_fields_are_human_friendly_in_web_pages(tmp_path):
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret-key",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "test-password",
        "BRIDGE_API_TOKEN": "test-bridge-token",
        "DATABASE": str(tmp_path / "attendance.db"),
        "PHOTOS_DIR": str(tmp_path / "Photos"),
    })
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True
        session["_csrf_token"] = "test-csrf-token"

    for path in ("/students", "/lectures", "/reports"):
        response = client.get(path)
        assert response.status_code == 200
        text = response.get_data(as_text=True)
        for label in ("First Year", "Second Year", "Third Year", "Fourth Year"):
            assert label in text
        assert "Fifth Year" not in text
