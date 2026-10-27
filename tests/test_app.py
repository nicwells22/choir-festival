import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import create_app
from database import Base, Interest, InterestChild, build_engine, initialize_database
from manage import main


@pytest.fixture
def application(tmp_path, monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setenv("ADMIN_USERNAME", "festival-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-admin-password")
    application = create_app(f"sqlite:///{(tmp_path / 'interests.db').as_posix()}")
    try:
        yield application
    finally:
        application.state.engine.dispose()


@pytest.fixture
def client(application):
    with TestClient(application) as client:
        yield client


@pytest.fixture
def submission():
    return {
        "submission_id": str(uuid4()),
        "contact_name": "  Alex Parent  ",
        "contact_email": "Alex@EXAMPLE.COM",
        "children": ["  Jamie Singer  "],
        "volunteer": False,
        "notes": "  Happy to help with piano.  ",
    }


def test_page_and_health(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Voices of the Valley" in response.text
    assert 'fetch("/api/interests"' in response.text
    assert "localStorage" not in response.text
    assert client.get("/index.html").status_code == 200
    assert client.get("/api/health").json() == {"status": "ok"}


def test_submission_is_durably_saved(client, application, submission):
    response = client.post("/api/interests", json=submission)
    assert response.status_code == 201
    assert response.json() == {"status": "recorded"}
    assert response.headers["cache-control"] == "no-store"
    with Session(application.state.engine) as session:
        record = session.scalar(select(Interest))
        assert record.name == "Alex Parent"
        assert record.email == "alex@example.com"
        assert record.choir == "children"
        assert record.notes == "Happy to help with piano."
        assert record.notify is False
        assert record.volunteer is False
        assert [child.name for child in record.children] == ["Jamie Singer"]
        assert record.submitted_at is not None
    # A separate engine/app instance sees the committed record.
    another_app = create_app(application.state.engine.url.render_as_string(hide_password=False))
    try:
        with Session(another_app.state.engine) as session:
            assert session.scalar(select(func.count()).select_from(Interest)) == 1
    finally:
        another_app.state.engine.dispose()


@pytest.mark.parametrize("children,volunteer", [
    (["Jamie Singer"], False), (["Jamie Singer"], True),
    (["Jamie Singer", "Casey Singer"], False), (["Jamie Singer", "Casey Singer"], True),
])
def test_family_and_volunteer_options(client, application, submission, children, volunteer):
    submission.update(children=children, volunteer=volunteer)
    assert client.post("/api/interests", json=submission).status_code == 201
    with Session(application.state.engine) as session:
        record = session.scalar(select(Interest))
        assert [child.name for child in record.children] == children
        assert record.volunteer is volunteer


@pytest.mark.parametrize("field,value", [
    ("contact_name", "  "), ("contact_name", "x" * 121),
    ("contact_email", "not-an-email"), ("contact_email", "a" * 250 + "@example.com"),
    ("children", []), ("children", [" "]), ("children", ["x" * 121]),
    ("children", ["Child"] * 21), ("children", "not-a-list"),
    ("notes", "x" * 2001), ("volunteer", "false"), ("submission_id", "invalid"),
])
def test_invalid_data_is_rejected_without_saving(client, application, submission, field, value):
    submission[field] = value
    response = client.post("/api/interests", json=submission)
    assert response.status_code == 422
    assert any(error["field"] == field for error in response.json()["detail"])
    assert submission["contact_email"] not in response.text
    with Session(application.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(Interest)) == 0
        assert session.scalar(select(func.count()).select_from(InterestChild)) == 0


def test_retry_does_not_duplicate_or_overwrite(client, application, submission):
    assert client.post("/api/interests", json=submission).status_code == 201
    assert client.post("/api/interests", json=submission).status_code == 200
    submission["contact_name"] = "Someone Else"
    assert client.post("/api/interests", json=submission).status_code == 409
    with Session(application.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(Interest)) == 1
        assert session.scalar(select(Interest.name)) == "Alex Parent"
        assert session.scalar(select(func.count()).select_from(InterestChild)) == 1


def test_family_retry_preserves_child_order_without_duplicates(client, application, submission):
    submission["children"] = ["Jamie Singer", "Casey Singer"]
    assert client.post("/api/interests", json=submission).status_code == 201
    assert client.post("/api/interests", json=submission).status_code == 200
    submission["children"].reverse()
    assert client.post("/api/interests", json=submission).status_code == 409
    with Session(application.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(InterestChild)) == 2
        assert [child.name for child in session.scalar(select(Interest)).children] == ["Jamie Singer", "Casey Singer"]


def test_volunteering_is_optional_and_notifications_are_not_accepted(client, submission, admin_auth):
    submission.pop("volunteer")
    assert client.post("/api/interests", json=submission).status_code == 201
    result = client.get("/api/admin/interests", auth=admin_auth).json()
    assert result["items"][0]["volunteer"] is False
    submission.update(submission_id=str(uuid4()), notify=True)
    assert client.post("/api/interests", json=submission).status_code == 422


def test_child_validation_identifies_the_correct_child(client, submission):
    submission["children"] = ["Jamie Singer", " "]
    response = client.post("/api/interests", json=submission)
    assert response.status_code == 422
    assert response.json()["detail"][0]["field"] == "children"
    assert response.json()["detail"][0]["path"] == ["children", 1]


def test_same_email_can_represent_multiple_family_members(client, submission):
    assert client.post("/api/interests", json=submission).status_code == 201
    submission.update(submission_id=str(uuid4()), children=["Another Singer"])
    assert client.post("/api/interests", json=submission).status_code == 201


def test_private_data_cannot_be_read_over_http(client, submission):
    client.post("/api/interests", json=submission)
    assert client.get("/api/interests").status_code == 405
    assert client.get("/data/choir-festival.db").status_code == 404
    assert client.get("/.env").status_code == 404


def test_database_failure_has_no_false_success_or_sensitive_details(client, application, submission):
    Interest.__table__.drop(application.state.engine)
    response = client.post("/api/interests", json=submission)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert submission["contact_email"] not in response.text
    assert "INSERT" not in response.text
    assert client.get("/api/health").status_code == 503


def test_invalid_json_is_rejected(client):
    assert client.post("/api/interests", content="{", headers={"Content-Type": "application/json"}).status_code == 422


def test_vercel_requires_postgres(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        build_engine()
    with pytest.raises(RuntimeError, match="Postgres"):
        build_engine("sqlite:///temporary.db")


def test_csv_export_includes_children_and_filters_volunteers(client, application, submission, tmp_path, monkeypatch):
    client.post("/api/interests", json=submission)
    submission.update(submission_id=str(uuid4()), volunteer=True, contact_name="=SOME_FORMULA()", children=["Jamie", "Casey"])
    client.post("/api/interests", json=submission)
    import manage
    monkeypatch.setattr(manage, "build_engine", lambda: build_engine(application.state.engine.url.render_as_string(hide_password=False)))
    output = tmp_path / "interests.csv"
    monkeypatch.setattr("sys.argv", ["manage.py", "export-interests", "--volunteers-only", "--output", str(output)])
    main()
    with output.open(encoding="utf-8-sig", newline="") as file:
        records = list(csv.DictReader(file))
    assert len(records) == 1
    assert records[0]["contact_name"] == "'=SOME_FORMULA()"
    assert records[0]["children"] == "Jamie\nCasey"
    assert records[0]["volunteer"] == "True"
    assert "notify" not in records[0]


def test_schema_initialization_is_repeatable(client, application, submission, monkeypatch):
    client.post("/api/interests", json=submission)
    import manage
    monkeypatch.setattr(manage, "build_engine", lambda: build_engine(application.state.engine.url.render_as_string(hide_password=False)))
    monkeypatch.setattr("sys.argv", ["manage.py", "init-db"])
    main()
    main()
    with Session(application.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(Interest)) == 1
        assert session.scalar(select(func.count()).select_from(InterestChild)) == 1


def test_original_sqlite_database_upgrades_without_losing_submissions(tmp_path, monkeypatch, submission):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setenv("ADMIN_USERNAME", "festival-admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-admin-password")
    url = f"sqlite:///{(tmp_path / 'original.db').as_posix()}"
    engine = build_engine(url)
    original_id = uuid4()
    try:
        with engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE interests (
                    id CHAR(32) PRIMARY KEY, name VARCHAR(120) NOT NULL,
                    email VARCHAR(254) NOT NULL, choir VARCHAR(16) NOT NULL,
                    notes VARCHAR(2000) NOT NULL, notify BOOLEAN NOT NULL,
                    submitted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """))
            connection.execute(text("""
                INSERT INTO interests (id, name, email, choir, notes, notify)
                VALUES (:id, 'Original Helper', 'legacy@example.com', 'helper', 'Original notes', TRUE)
            """), {"id": original_id.hex})
        application = create_app(url)
        with TestClient(application) as client:
            # Startup alone upgrades the old schema before the first request.
            with Session(engine) as session:
                original = session.get(Interest, original_id)
                assert original.name == "Original Helper"
                assert original.notes == "Original notes"
                assert original.notify is True
                assert original.volunteer is True
                assert original.children == []
            assert client.post("/api/interests", json=submission).status_code == 201
            result = client.get("/api/admin/interests", auth=("festival-admin", "test-only-admin-password")).json()
            assert result["total"] == 2
            assert result["total_children"] == 1
            assert result["total_volunteers"] == 1
        # Another cold start preserves both the legacy and new family records.
        with TestClient(create_app(url)) as client:
            assert client.get("/api/health").json() == {"status": "ok"}
            assert client.post("/api/interests", json=submission).status_code == 200
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(Interest)) == 2
            assert session.scalar(select(func.count()).select_from(InterestChild)) == 1
    finally:
        engine.dispose()


def test_simultaneous_sqlite_initialization_is_safe(tmp_path):
    url = f"sqlite:///{(tmp_path / 'concurrent.db').as_posix()}"
    engines = [build_engine(url), build_engine(url)]
    barrier = Barrier(2)

    def initialize(engine):
        barrier.wait(timeout=5)
        initialize_database(engine)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(initialize, engine) for engine in engines]
            for future in futures:
                future.result(timeout=15)
        assert set(inspect(engines[0]).get_table_names()) == {"interests", "interest_children"}
        with Session(engines[0]) as session:
            assert session.scalar(select(func.count()).select_from(Interest)) == 0
            assert session.scalar(select(func.count()).select_from(InterestChild)) == 0
    finally:
        for engine in engines:
            engine.dispose()


def test_failed_schema_creation_rolls_back_and_can_retry(tmp_path, monkeypatch):
    engine = build_engine(f"sqlite:///{(tmp_path / 'retry.db').as_posix()}")
    create_all = Base.metadata.create_all

    def interrupted_creation(connection):
        create_all(connection)
        raise OperationalError("schema initialization", {}, RuntimeError("interrupted"))

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Base.metadata, "create_all", interrupted_creation)
            with pytest.raises(OperationalError):
                initialize_database(engine)
        assert inspect(engine).get_table_names() == []
        initialize_database(engine)
        assert set(inspect(engine).get_table_names()) == {"interests", "interest_children"}
    finally:
        engine.dispose()


def test_initializer_restores_missing_index(client, application):
    engine = application.state.engine
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX ix_interests_email")
    initialize_database(engine)
    assert "ix_interests_email" in {index["name"] for index in inspect(engine).get_indexes("interests")}


def test_database_diagnostics_log_error_code_without_private_data(caplog):
    from app import log_database_failure

    class DatabasePermissionError(Exception):
        sqlstate = "42501"

    error = OperationalError(
        "INSERT INTO interests", {"email": "private@example.com"},
        DatabasePermissionError("sensitive credentials"),
    )
    log_database_failure(error, "initialization")
    assert "driver=DatabasePermissionError" in caplog.text
    assert "SQLSTATE=42501" in caplog.text
    assert "private@example.com" not in caplog.text
    assert "sensitive credentials" not in caplog.text


@pytest.fixture
def admin_auth():
    return ("festival-admin", "test-only-admin-password")


@pytest.mark.parametrize("path", ["/admin", "/admin/", "/api/admin/interests"])
def test_admin_requires_authentication(client, submission, admin_auth, path):
    client.post("/api/interests", json=submission)
    for auth in [None, ("festival-admin", "wrong"), ("wrong", admin_auth[1])]:
        response = client.get(path, auth=auth)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Basic")
        assert response.headers["cache-control"] == "no-store"
        assert "alex@example.com" not in response.text


def test_admin_page_is_protected_and_not_cacheable(client, admin_auth):
    response = client.get("/admin", auth=admin_auth)
    assert response.status_code == 200
    assert "Total interest submissions" in response.text
    assert 'id="total-count"' in response.text
    assert admin_auth[1] not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/admin.html").status_code == 404


def test_admin_is_disabled_without_password(application, monkeypatch):
    # Explicitly blank so a developer's local .env cannot enable this test app.
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    another_app = create_app(application.state.engine.url.render_as_string(hide_password=False))
    with TestClient(another_app) as client:
        assert client.get("/admin").status_code == 503
        response = client.get("/api/admin/interests", auth=("admin", ""))
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"


def test_admin_empty_list_has_zero_total(client, admin_auth):
    response = client.get("/api/admin/interests", auth=admin_auth)
    assert response.status_code == 200
    assert response.json() == {"total": 0, "total_children": 0, "total_volunteers": 0, "matched": 0, "page": 1, "page_size": 50, "items": []}
    assert response.headers["cache-control"] == "no-store"


def test_admin_lists_saved_fields_and_timezone(client, submission, admin_auth):
    client.post("/api/interests", json=submission)
    result = client.get("/api/admin/interests", auth=admin_auth).json()
    assert result["total"] == result["matched"] == 1
    record = result["items"][0]
    assert record["id"] == submission["submission_id"]
    assert record["contact_name"] == "Alex Parent"
    assert record["contact_email"] == "alex@example.com"
    assert record["children"] == ["Jamie Singer"]
    assert record["notes"] == "Happy to help with piano."
    assert record["volunteer"] is False
    assert "notify" not in record
    assert result["total_children"] == 1
    assert result["total_volunteers"] == 0
    assert datetime.fromisoformat(record["submitted_at"]).utcoffset() is not None


def test_admin_can_page_through_all_submissions_and_search(client, application, admin_auth):
    start = datetime(2026, 10, 26, tzinfo=timezone.utc)
    with Session(application.state.engine) as session:
        session.add_all([
            Interest(
                id=uuid4(), name=f"Contact {index:02}", email=f"singer{index}@example.com",
                choir="children", notes="Piano" if index == 0 else "", notify=True,
                children=[InterestChild(name=f"Singer {index:02}", position=0)],
                volunteer=index % 2 == 0,
                submitted_at=start + timedelta(minutes=index),
            ) for index in range(55)
        ])
        session.commit()
    first = client.get("/api/admin/interests", auth=admin_auth).json()
    second = client.get("/api/admin/interests?page=2", auth=admin_auth).json()
    assert first["total"] == second["total"] == 55
    assert len(first["items"]) == 50
    assert len(second["items"]) == 5
    assert first["total_children"] == 55
    assert first["total_volunteers"] == 28
    assert first["items"][0]["children"] == ["Singer 54"]
    assert second["items"][-1]["children"] == ["Singer 00"]
    assert len({item["id"] for item in first["items"] + second["items"]}) == 55
    result = client.get("/api/admin/interests?q=SINGER%200&page_size=3", auth=admin_auth).json()
    assert result["total"] == 55
    assert result["matched"] == 10
    assert len(result["items"]) == 3
    assert result["items"][0]["children"] == ["Singer 09"]
    assert result["total_children"] == 55
    assert result["total_volunteers"] == 28
    assert client.get("/api/admin/interests?q=PIANO", auth=admin_auth).json()["matched"] == 1
    assert client.get("/api/admin/interests?q=singer54@example.com", auth=admin_auth).json()["matched"] == 1
    assert client.get("/api/admin/interests?page=999", auth=admin_auth).json()["page"] == 2


def test_admin_search_escapes_wildcards_and_injection(client, submission, admin_auth):
    submission["children"] = ["100% Child"]
    client.post("/api/interests", json=submission)
    submission.update(submission_id=str(uuid4()), children=["Another Child"])
    client.post("/api/interests", json=submission)
    for query, expected in [("%", 1), ("_", 0), ("' OR 1=1 --", 0)]:
        result = client.get("/api/admin/interests", params={"q": query}, auth=admin_auth).json()
        assert result["total"] == 2
        assert result["matched"] == expected


def test_admin_includes_legacy_entries(client, application, admin_auth):
    with Session(application.state.engine) as session:
        session.add(Interest(id=uuid4(), name="Legacy Singer", email="legacy@example.com", choir="adult", notes="", notify=False))
        session.commit()
    result = client.get("/api/admin/interests", auth=admin_auth).json()
    assert result["total"] == 1
    assert result["items"][0]["contact_name"] == "Legacy Singer"
    assert result["items"][0]["children"] == []


@pytest.mark.parametrize("query", ["page=0", "page_size=0", "page_size=101", "q=" + "x" * 121])
def test_admin_validates_pagination(client, admin_auth, query):
    assert client.get("/api/admin/interests?" + query, auth=admin_auth).status_code == 422
    assert client.get("/api/admin/interests?" + query).status_code == 401


def test_admin_database_failure_does_not_look_like_empty_list(client, application, admin_auth):
    Interest.__table__.drop(application.state.engine)
    response = client.get("/api/admin/interests", auth=admin_auth)
    assert response.status_code == 503
    assert "items" not in response.json()
    assert response.headers["cache-control"] == "no-store"
