import csv
import hashlib
import io
import json
import smtplib
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

import pytest
from babel.dates import format_date
from babel.numbers import format_currency

from app import (
    Budget,
    Expense,
    LoginAttempt,
    PasswordResetToken,
    RecurringExpense,
    RecurringPayment,
    User,
    create_app,
    db,
    next_recurrence_date,
    utc_now,
)


@pytest.fixture()
def client():
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        }
    )
    with app.app_context():
        db.drop_all()
        db.create_all()
    with app.test_client() as test_client:
        yield test_client
    with app.app_context():
        db.session.remove()
        db.drop_all()


def csrf(client):
    response = client.get("/register")
    if response.status_code != 200:
        response = client.get("/")
    assert response.status_code == 200
    with client.session_transaction() as browser_session:
        assert browser_session is not None
        return browser_session["_csrf_token"]


def register(client, email="person@example.com"):
    token = csrf(client)
    return client.post(
        "/register",
        data={"csrf_token": token, "email": email, "password": "a-strong-password"},
        follow_redirects=True,
    )


def test_registration_and_dashboard(client):
    response = register(client)
    assert response.status_code == 200
    assert b"Your money" in response.data


def test_production_environment_requires_a_persistent_secret_key(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="SECRET_KEY must be configured for production"):
        create_app({"SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})


def test_login_is_throttled_by_email_and_ip_then_recovers_after_window(client):
    register(client)
    token = csrf(client)
    client.post("/logout", data={"csrf_token": token})

    for _ in range(5):
        response = client.post(
            "/login",
            data={
                "csrf_token": token,
                "email": "person@example.com",
                "password": "incorrect-password",
            },
            follow_redirects=True,
        )
        assert b"Email or password is incorrect." in response.data

    blocked = client.post(
        "/login",
        data={
            "csrf_token": token,
            "email": "person@example.com",
            "password": "a-strong-password",
        },
        follow_redirects=True,
    )
    assert b"Too many unsuccessful sign-in attempts" in blocked.data
    with client.application.app_context():
        attempts = LoginAttempt.query.all()
        assert len(attempts) == 5
        assert all("person@example.com" not in attempt.email_hash for attempt in attempts)
        for attempt in attempts:
            attempt.attempted_at = utc_now() - timedelta(minutes=16)
        db.session.commit()

    signed_in = client.post(
        "/login",
        data={
            "csrf_token": token,
            "email": "person@example.com",
            "password": "a-strong-password",
        },
        follow_redirects=True,
    )

    assert b"Your money" in signed_in.data
    with client.application.app_context():
        assert LoginAttempt.query.count() == 0


def test_login_ip_limit_applies_across_different_email_addresses(client):
    register(client)
    token = csrf(client)
    client.post("/logout", data={"csrf_token": token})
    client.application.config["LOGIN_ATTEMPTS_PER_IP"] = 2

    for email in ["unknown1@example.com", "unknown2@example.com"]:
        client.post(
            "/login",
            data={"csrf_token": token, "email": email, "password": "wrong-password"},
        )

    blocked = client.post(
        "/login",
        data={
            "csrf_token": token,
            "email": "person@example.com",
            "password": "a-strong-password",
        },
        follow_redirects=True,
    )
    assert b"Too many unsuccessful sign-in attempts" in blocked.data


def test_password_reset_is_single_use_and_does_not_disclose_unknown_accounts(
    client, monkeypatch
):
    register(client)
    csrf_token = csrf(client)
    client.post("/logout", data={"csrf_token": csrf_token})
    client.application.config.update(
        {
            "PUBLIC_BASE_URL": "https://expenses.example",
            "MAIL_SERVER": "smtp.example",
            "MAIL_PORT": 587,
            "MAIL_USE_TLS": False,
            "MAIL_USE_SSL": False,
            "MAIL_USERNAME": None,
            "MAIL_PASSWORD": None,
            "MAIL_DEFAULT_SENDER": "Pennywise <noreply@expenses.example>",
        }
    )
    sent_messages = []

    class FakeSMTP:
        def __init__(self, _server, _port, timeout):
            assert timeout == 10

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def send_message(self, message):
            sent_messages.append(message)

    monkeypatch.setattr("app.smtplib.SMTP", FakeSMTP)
    unknown = client.post(
        "/password-reset",
        data={"csrf_token": csrf_token, "email": "unknown@example.com"},
        follow_redirects=True,
    )
    known = client.post(
        "/password-reset",
        data={"csrf_token": csrf_token, "email": "PERSON@example.com"},
        follow_redirects=True,
    )
    generic_message = b"If an account exists for that email, reset instructions will be sent."
    assert generic_message in unknown.data
    assert generic_message in known.data
    assert len(sent_messages) == 1
    client.application.config["PASSWORD_RESET_REQUESTS_PER_EMAIL"] = 1
    repeat = client.post(
        "/password-reset",
        data={"csrf_token": csrf_token, "email": "person@example.com"},
        follow_redirects=True,
    )
    assert generic_message in repeat.data
    assert len(sent_messages) == 1

    reset_link = next(
        line
        for line in sent_messages[0].get_content().splitlines()
        if line.startswith("https://")
    )
    reset_path = urlsplit(reset_link).path
    raw_token = reset_path.rsplit("/", 1)[-1]
    with client.application.app_context():
        stored_token = PasswordResetToken.query.one()
        assert stored_token.token_hash == hashlib.sha256(raw_token.encode()).hexdigest()
    reset_form = client.get(reset_path)
    assert reset_form.status_code == 200
    assert reset_form.headers["Cache-Control"] == "no-store"
    assert reset_form.headers["Referrer-Policy"] == "no-referrer"

    response = client.post(
        reset_path,
        data={
            "csrf_token": csrf_token,
            "password": "a-new-strong-password",
            "confirm_password": "a-new-strong-password",
        },
        follow_redirects=True,
    )
    assert b"Your password has been reset." in response.data
    with client.application.app_context():
        user = User.query.filter_by(email="person@example.com").one()
        assert user.check_password("a-new-strong-password")
        assert PasswordResetToken.query.one().used_at is not None

    reused_link = client.get(reset_path, follow_redirects=True)
    assert b"invalid or expired" in reused_link.data
    signed_in = client.post(
        "/login",
        data={
            "csrf_token": csrf_token,
            "email": "person@example.com",
            "password": "a-new-strong-password",
        },
        follow_redirects=True,
    )
    assert b"Your money" in signed_in.data


def test_password_reset_delivery_failure_is_logged_and_invalidates_link(client, monkeypatch, caplog):
    register(client)
    csrf_token = csrf(client)
    client.post("/logout", data={"csrf_token": csrf_token})
    client.application.config.update(
        {
            "PUBLIC_BASE_URL": "https://expenses.example",
            "MAIL_SERVER": "smtp.example",
            "MAIL_PORT": 587,
            "MAIL_USE_TLS": False,
            "MAIL_USE_SSL": False,
            "MAIL_USERNAME": None,
            "MAIL_PASSWORD": None,
            "MAIL_DEFAULT_SENDER": "noreply@expenses.example",
        }
    )

    class FailingSMTP:
        def __init__(self, _server, _port, timeout):
            assert timeout == 10

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def send_message(self, _message):
            raise smtplib.SMTPException("delivery failed")

    monkeypatch.setattr("app.smtplib.SMTP", FailingSMTP)
    response = client.post(
        "/password-reset",
        data={"csrf_token": csrf_token, "email": "person@example.com"},
        follow_redirects=True,
    )

    assert b"If an account exists for that email" in response.data
    assert "Password reset email delivery failed." in caplog.text
    with client.application.app_context():
        reset_token = PasswordResetToken.query.one()
        assert reset_token.used_at is not None


def test_password_reset_without_mail_configuration_creates_no_link(client, caplog):
    register(client)
    csrf_token = csrf(client)
    client.post("/logout", data={"csrf_token": csrf_token})
    client.application.config.update(
        {
            "PUBLIC_BASE_URL": "",
            "MAIL_SERVER": None,
            "MAIL_DEFAULT_SENDER": None,
        }
    )

    response = client.post(
        "/password-reset",
        data={"csrf_token": csrf_token, "email": "person@example.com"},
        follow_redirects=True,
    )

    assert b"If an account exists for that email" in response.data
    assert "Password reset email is not configured safely" in caplog.text
    with client.application.app_context():
        assert PasswordResetToken.query.count() == 0


def test_expired_password_reset_link_is_rejected(client):
    register(client)
    with client.application.app_context():
        user = User.query.filter_by(email="person@example.com").one()
        raw_token = "expired-reset-link"
        db.session.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
                expires_at=utc_now() - timedelta(minutes=1),
            )
        )
        db.session.commit()

    response = client.get("/password-reset/expired-reset-link", follow_redirects=True)

    assert b"invalid or expired" in response.data
    with client.application.app_context():
        user = User.query.filter_by(email="person@example.com").one()
        assert user.check_password("a-strong-password")


def test_expense_is_saved_and_reflected_in_dashboard(client):
    register(client)
    token = csrf(client)
    response = client.post(
        "/expenses/new",
        data={
            "csrf_token": token,
            "amount": "12.45",
            "date": date.today().isoformat(),
            "category": "Food & dining",
            "payment_method": "Card",
            "description": "Lunch",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Lunch" in response.data
    assert b"12.45" in response.data


def test_currency_and_locale_preferences_format_dashboard_values(client):
    register(client)
    token = csrf(client)
    response = client.post(
        "/settings",
        data={"csrf_token": token, "currency": "EUR", "locale": "de_DE"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"preferences have been saved" in response.data
    with client.application.app_context():
        user = User.query.filter_by(email="person@example.com").one()
        assert user.currency == "EUR"
        assert user.locale == "de_DE"
        db.session.add(
            Expense(
                user_id=user.id,
                amount=Decimal("1234.50"),
                date=date.today(),
                category="Other",
                description="Market",
                payment_method="Card",
            )
        )
        db.session.commit()

    dashboard = client.get("/")
    assert format_currency(Decimal("1234.50"), "EUR", locale="de_DE").encode() in dashboard.data
    assert format_date(date.today(), format="medium", locale="de_DE").encode() in dashboard.data
    analytics = parse_dashboard_analytics(dashboard)
    assert analytics["currency"] == "EUR"
    assert analytics["locale"] == "de_DE"
    assert b"USD" not in dashboard.data


def test_settings_are_account_specific(client):
    register(client)
    token = csrf(client)
    client.post(
        "/settings",
        data={"csrf_token": token, "currency": "GBP", "locale": "en_GB"},
    )
    with client.application.app_context():
        second_user = User(email="second@example.com")
        second_user.set_password("a-strong-password")
        db.session.add(second_user)
        db.session.commit()
        assert second_user.currency == "USD"
        assert second_user.locale == "en_US"


def test_existing_accounts_receive_default_preferences_on_schema_upgrade(tmp_path):
    database_path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            'CREATE TABLE "user" (id INTEGER PRIMARY KEY, email VARCHAR(254) NOT NULL, '
            "password_hash VARCHAR(256) NOT NULL)"
        )
        connection.execute(
            'INSERT INTO "user" (id, email, password_hash) VALUES (1, ?, ?)',
            ("legacy@example.com", "password-hash"),
        )
    legacy_app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "SQLALCHEMY_DATABASE_URI": "sqlite:///" + str(database_path).replace("\\", "/"),
        }
    )
    with legacy_app.app_context():
        legacy_user = db.session.get(User, 1)
        assert legacy_user is not None
        assert legacy_user.currency == "USD"
        assert legacy_user.locale == "en_US"
        assert legacy_user.last_expense_category == "Other"
        assert legacy_user.last_payment_method == "Card"
        db.session.remove()
        db.engine.dispose()


def test_existing_expenses_receive_new_detail_columns_on_schema_upgrade(tmp_path):
    database_path = tmp_path / "legacy-expenses.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            'CREATE TABLE "user" (id INTEGER PRIMARY KEY, email VARCHAR(254) NOT NULL, '
            "password_hash VARCHAR(256) NOT NULL)"
        )
        connection.execute(
            'INSERT INTO "user" (id, email, password_hash) VALUES (1, ?, ?)',
            ("legacy@example.com", "password-hash"),
        )
        connection.execute(
            "CREATE TABLE expense (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
            "amount NUMERIC(12, 2) NOT NULL, date DATE NOT NULL, category VARCHAR(40) NOT NULL, "
            "description VARCHAR(160) NOT NULL, payment_method VARCHAR(30) NOT NULL, "
            "created_at DATETIME NOT NULL)"
        )
        connection.execute(
            "INSERT INTO expense (id, user_id, amount, date, category, description, "
            "payment_method, created_at) VALUES (1, 1, 4.50, '2026-01-03', 'Other', "
            "'Coffee', 'Card', '2026-01-03 12:00:00')"
        )
    legacy_app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "SQLALCHEMY_DATABASE_URI": "sqlite:///" + str(database_path).replace("\\", "/"),
        }
    )
    with legacy_app.app_context():
        legacy_expense = db.session.get(Expense, 1)
        assert legacy_expense is not None
        assert legacy_expense.merchant == ""
        assert legacy_expense.notes == ""
        assert legacy_expense.tags == ""
        db.session.remove()
        db.engine.dispose()


def test_csv_exports_currency_and_import_rejects_mismatched_currency(client):
    register(client)
    token = csrf(client)
    client.post(
        "/settings",
        data={"csrf_token": token, "currency": "EUR", "locale": "fr_FR"},
    )
    client.post(
        "/expenses/new",
        data={
            "csrf_token": token,
            "amount": "12.50",
            "date": date.today().isoformat(),
            "category": "Other",
            "payment_method": "Cash",
            "description": "Market",
        },
    )

    exported = client.get("/export")
    rows = list(csv.DictReader(io.StringIO(exported.get_data(as_text=True).lstrip("\ufeff"))))
    assert rows[0]["amount"] == "12.50"
    assert rows[0]["currency"] == "EUR"

    imported = client.post(
        "/import",
        data={
            "csrf_token": token,
            "file": (
                io.BytesIO(
                    b"date,amount,currency,category\n"
                    b"2026-01-03,18.50,USD,Other\n"
                ),
                "expenses.csv",
            ),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"currency USD does not match your account currency (EUR)" in imported.data
    with client.application.app_context():
        assert Expense.query.count() == 1


def test_quick_add_remembers_choices_and_saves_expense_details(client):
    register(client)
    token = csrf(client)
    response = client.get("/expenses/quick")
    assert response.status_code == 200
    assert b"Quick add an expense" in response.data

    response = client.post(
        "/expenses/quick",
        data={
            "csrf_token": token,
            "amount": "18.25",
            "date": date.today().isoformat(),
            "category": "Food & dining",
            "payment_method": "Cash",
            "description": "Lunch",
            "merchant": "Market Cafe",
            "notes": "Lunch with the team",
            "tags": "work, lunch",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Expense added." in response.data
    with client.application.app_context():
        expense = Expense.query.one()
        user = User.query.filter_by(email="person@example.com").one()
        assert expense.merchant == "Market Cafe"
        assert expense.notes == "Lunch with the team"
        assert expense.tags == "work, lunch"
        assert user.last_expense_category == "Food & dining"
        assert user.last_payment_method == "Cash"

    assert b"Market Cafe" in client.get("/?q=Market+Cafe").data
    assert b"Market Cafe" in client.get("/?q=work").data
    quick_form = client.get("/expenses/quick")
    assert b'value="Food &amp; dining" selected' in quick_form.data
    assert b'value="Cash" selected' in quick_form.data


def test_duplicate_expense_prefills_details_and_uses_today(client):
    register(client)
    token = csrf(client)
    client.post(
        "/expenses/new",
        data={
            "csrf_token": token,
            "amount": "32.40",
            "date": "2025-03-01",
            "category": "Transport",
            "payment_method": "Card",
            "description": "Monthly pass",
            "merchant": "City Transit",
            "notes": "Work commute",
            "tags": "commute",
        },
    )
    with client.application.app_context():
        expense_id = Expense.query.one().id

    form = client.get("/expenses/{}/duplicate".format(expense_id))
    assert form.status_code == 200
    assert b'value="' + date.today().isoformat().encode() + b'"' in form.data
    assert b'value="City Transit"' in form.data
    assert b"Work commute" in form.data

    response = client.post(
        "/expenses/{}/duplicate".format(expense_id),
        data={
            "csrf_token": token,
            "amount": "32.40",
            "date": date.today().isoformat(),
            "category": "Transport",
            "payment_method": "Card",
            "description": "Monthly pass",
            "merchant": "City Transit",
            "notes": "Work commute",
            "tags": "commute",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Expense duplicated." in response.data
    with client.application.app_context():
        expenses = Expense.query.order_by(Expense.id).all()
        assert len(expenses) == 2
        assert expenses[1].date == date.today()
        assert expenses[1].merchant == expenses[0].merchant
        assert expenses[1].notes == expenses[0].notes
        assert expenses[1].tags == expenses[0].tags


def test_csv_round_trips_merchant_notes_and_tags(client):
    register(client)
    token = csrf(client)
    imported = client.post(
        "/import",
        data={
            "csrf_token": token,
            "file": (
                io.BytesIO(
                    b"date,amount,category,description,merchant,notes,tags,payment_method\n"
                    b"2026-01-03,18.50,Food & dining,Coffee,Corner Cafe,Team breakfast,work,Card\n"
                ),
                "expenses.csv",
            ),
        },
        content_type="multipart/form-data",
    )
    assert imported.status_code == 302
    exported = client.get("/export")
    rows = list(csv.DictReader(io.StringIO(exported.get_data(as_text=True).lstrip("\ufeff"))))
    assert rows[0]["merchant"] == "Corner Cafe"
    assert rows[0]["notes"] == "Team breakfast"
    assert rows[0]["tags"] == "work"


def test_users_cannot_access_another_users_expense(client):
    register(client)
    token = csrf(client)
    client.post(
        "/expenses/new",
        data={
            "csrf_token": token,
            "amount": "10",
            "date": date.today().isoformat(),
            "category": "Other",
            "payment_method": "Cash",
        },
    )
    with client.application.app_context():
        expense_id = db.session.query(Expense.id).first()[0]
        second_user = User(email="second@example.com")
        second_user.set_password("a-strong-password")
        db.session.add(second_user)
        db.session.commit()
    client.post("/logout", data={"csrf_token": token})
    client.post(
        "/login",
        data={"csrf_token": token, "email": "second@example.com", "password": "a-strong-password"},
    )
    assert client.get("/expenses/{}/edit".format(expense_id)).status_code == 404
    assert client.get("/expenses/{}/duplicate".format(expense_id)).status_code == 404


def test_csv_import_validates_all_rows_before_saving(client):
    register(client)
    token = csrf(client)
    response = client.post(
        "/import",
        data={
            "csrf_token": token,
            "file": (
                io.BytesIO(
                    b"date,amount,category\n"
                    b"2026-01-01,10,Food & dining\n"
                    b"not-a-date,5,Other\n"
                ),
                "expenses.csv",
            ),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"Row 3" in response.data
    with client.application.app_context():
        assert Expense.query.count() == 0


def test_budget_is_saved_per_user(client):
    register(client)
    token = csrf(client)
    response = client.post(
        "/budgets",
        data={"csrf_token": token, "category": "Transport", "amount": "200"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Transport" in response.data
    with client.application.app_context():
        assert Budget.query.one().monthly_limit == 200


def test_existing_budget_can_be_updated_without_recreating_it(client):
    register(client)
    token = csrf(client)
    client.post(
        "/budgets",
        data={"csrf_token": token, "category": "Transport", "amount": "200"},
    )

    response = client.post(
        "/budgets/Transport/edit",
        data={"csrf_token": token, "amount": "350.50"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Transport budget updated." in response.data
    with client.application.app_context():
        budgets = Budget.query.all()
        assert len(budgets) == 1
        assert budgets[0].monthly_limit == 350.50


def test_budget_shows_remaining_and_near_limit_or_over_alerts(client):
    register(client)
    with client.application.app_context():
        user = User.query.filter_by(email="person@example.com").one()
        db.session.add_all(
            [
                Budget(user_id=user.id, category="Food & dining", monthly_limit=100),
                Budget(user_id=user.id, category="Transport", monthly_limit=100),
                Expense(
                    user_id=user.id,
                    amount=80,
                    date=date.today(),
                    category="Food & dining",
                    description="Groceries",
                    payment_method="Card",
                ),
                Expense(
                    user_id=user.id,
                    amount=112,
                    date=date.today(),
                    category="Transport",
                    description="Transit",
                    payment_method="Card",
                ),
            ]
        )
        db.session.commit()

    response = client.get("/")

    assert response.status_code == 200
    assert b"$20.00 left" in response.data
    assert b"Approaching limit" in response.data
    assert b"$12.00 over" in response.data
    assert b"Monthly limit exceeded" in response.data


def test_recurring_expense_generates_a_paid_transaction_and_advances_monthly_due_date(client):
    register(client)
    token = csrf(client)
    response = client.post(
        "/recurring",
        data={
            "csrf_token": token,
            "description": "Rent",
            "amount": "1250.00",
            "frequency": "monthly",
            "next_due": "2025-01-31",
            "category": "Bills & utilities",
            "payment_method": "Bank transfer",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Rent" in response.data
    with client.application.app_context():
        recurring = RecurringExpense.query.one()
        recurring_id = recurring.id
        assert recurring.next_due == date(2025, 1, 31)

    response = client.post(
        "/recurring/{}/paid".format(recurring_id),
        data={"csrf_token": token, "due_date": "2025-01-31"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Charge marked paid and added to expenses." in response.data
    with client.application.app_context():
        recurring = db.session.get(RecurringExpense, recurring_id)
        assert recurring is not None
        assert recurring.next_due == date(2025, 2, 28)
        assert recurring.day_of_month == 31
        assert Expense.query.count() == 1
        generated = Expense.query.one()
        assert generated.date == date(2025, 1, 31)
        assert generated.amount == 1250
        assert RecurringPayment.query.count() == 1

    stale_response = client.post(
        "/recurring/{}/paid".format(recurring_id),
        data={"csrf_token": token, "due_date": "2025-01-31"},
        follow_redirects=True,
    )
    assert b"scheduled date has changed" in stale_response.data
    with client.application.app_context():
        assert Expense.query.count() == 1


def test_weekly_recurring_expense_advances_seven_days():
    assert next_recurrence_date(date(2025, 1, 31), "weekly", 31) == date(2025, 2, 7)


def test_user_cannot_mark_another_users_recurring_charge_paid(client):
    register(client)
    token = csrf(client)
    client.post(
        "/recurring",
        data={
            "csrf_token": token,
            "description": "Private subscription",
            "amount": "10",
            "frequency": "monthly",
            "next_due": date.today().isoformat(),
            "category": "Entertainment",
            "payment_method": "Card",
        },
    )
    with client.application.app_context():
        recurring_id = RecurringExpense.query.one().id
        other_user = User(email="other@example.com")
        other_user.set_password("a-strong-password")
        db.session.add(other_user)
        db.session.commit()

    client.post("/logout", data={"csrf_token": token})
    client.post(
        "/login",
        data={
            "csrf_token": csrf(client),
            "email": "other@example.com",
            "password": "a-strong-password",
        },
    )

    response = client.post(
        "/recurring/{}/paid".format(recurring_id),
        data={"csrf_token": csrf(client), "due_date": date.today().isoformat()},
    )

    assert response.status_code == 404
    with client.application.app_context():
        assert Expense.query.count() == 0
        assert RecurringPayment.query.count() == 0


def test_stopping_recurring_expense_removes_upcoming_charge(client):
    register(client)
    token = csrf(client)
    client.post(
        "/recurring",
        data={
            "csrf_token": token,
            "description": "Streaming subscription",
            "amount": "9.99",
            "frequency": "monthly",
            "next_due": date.today().isoformat(),
            "category": "Entertainment",
            "payment_method": "Card",
        },
    )
    with client.application.app_context():
        recurring_id = RecurringExpense.query.one().id

    response = client.post(
        "/recurring/{}/stop".format(recurring_id),
        data={"csrf_token": token},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Streaming subscription" not in response.data
    with client.application.app_context():
        assert not db.session.get(RecurringExpense, recurring_id).active


def test_post_without_csrf_token_is_rejected(client):
    response = client.post("/register", data={"email": "person@example.com"})
    assert response.status_code == 400


def test_csv_export_neutralizes_spreadsheet_formulas(client):
    register(client)
    token = csrf(client)
    client.post(
        "/expenses/new",
        data={
            "csrf_token": token,
            "amount": "10",
            "date": date.today().isoformat(),
            "category": "Other",
            "payment_method": "Cash",
            "description": "=1+1",
            "merchant": "+2+2",
            "notes": "@SUM(1,1)",
            "tags": "-3+3",
        },
    )
    response = client.get("/export")
    assert response.status_code == 200
    assert b"'=1+1" in response.data
    assert b"'+2+2" in response.data
    assert b"'@SUM(1,1)" in response.data
    assert b"'-3+3" in response.data


def test_dashboard_filters_by_category_and_payment_method(client):
    register(client)
    token = csrf(client)
    for amount, category, payment_method in [
        ("12", "Food & dining", "Card"),
        ("25", "Transport", "Cash"),
    ]:
        client.post(
            "/expenses/new",
            data={
                "csrf_token": token,
                "amount": amount,
                "date": date.today().isoformat(),
                "category": category,
                "payment_method": payment_method,
                "description": category,
            },
        )
    response = client.get(
        "/?start={}&end={}&category=Food+%26+dining&payment_method=Card".format(
            date.today().isoformat(), date.today().isoformat()
        )
    )
    assert response.status_code == 200
    assert b"Food &amp; dining" in response.data
    assert b"25.00" not in response.data


def add_expenses(client, descriptions_and_amounts):
    with client.application.app_context():
        user = User.query.filter_by(email="person@example.com").one()
        db.session.add_all(
            [
                Expense(
                    user_id=user.id,
                    amount=amount,
                    date=date.today(),
                    category="Other",
                    description=description,
                    payment_method="Card",
                )
                for description, amount in descriptions_and_amounts
            ]
        )
        db.session.commit()


def parse_dashboard_analytics(response):
    marker = b'<script id="analytics-data" type="application/json">'
    start = response.data.index(marker) + len(marker)
    end = response.data.index(b"</script>", start)
    return json.loads(response.data[start:end])


def test_dashboard_searches_descriptions_case_insensitively(client):
    register(client)
    add_expenses(client, [("Coffee shop", 8), ("COFFEE beans", 12), ("Monthly rent", 900)])

    response = client.get("/?q=coffee")

    assert response.status_code == 200
    assert b"Coffee shop" in response.data
    assert b"COFFEE beans" in response.data
    assert b"<strong>Monthly rent</strong>" not in response.data
    assert b"2 matching expenses" in response.data


def test_dashboard_has_chart_loading_states_and_accessible_summary_without_plotly(client):
    register(client)
    add_expenses(client, [("Coffee shop", 8), ("Groceries", 12)])

    response = client.get("/")

    assert response.status_code == 200
    assert response.data.count(b'role="status" aria-live="polite"') == 3
    assert response.data.count(b"Loading interactive chart") == 3
    assert b"Read a text summary of the chart data" in response.data
    assert b"Spending by category" in response.data
    assert b"The highest daily total was $20.00" in response.data
    analytics = parse_dashboard_analytics(response)
    assert analytics["dailySummary"]["dayCount"] == 1
    assert analytics["dailySummary"]["peakAmount"] == 20
    assert analytics["categoryTrendSummary"][0]["firstAmount"] == 20


def test_dashboard_paginates_and_sorts_all_matching_expenses(client):
    register(client)
    add_expenses(
        client,
        [("Expense {:02d}".format(number), number) for number in range(1, 13)],
    )

    first_page = client.get("/?sort=amount&direction=asc&per_page=10")
    assert first_page.status_code == 200
    assert b"Expense 01" in first_page.data
    assert b"Expense 10" in first_page.data
    assert first_page.data.count(b'class="transaction-name"') == 10
    assert b"Page 1 of 2" in first_page.data
    assert b"sort=amount" in first_page.data

    second_page = client.get("/?sort=amount&direction=asc&per_page=10&page=2")
    assert second_page.status_code == 200
    assert b"Expense 11" in second_page.data
    assert b"Expense 12" in second_page.data
    assert second_page.data.count(b'class="transaction-name"') == 2
    assert b"Page 2 of 2" in second_page.data


def test_pagination_preserves_description_search(client):
    register(client)
    add_expenses(
        client,
        [("Coffee {:02d}".format(number), number) for number in range(1, 12)]
        + [("Different expense", 50)],
    )

    response = client.get("/?q=Coffee&sort=description&direction=asc&per_page=10&page=2")

    assert response.status_code == 200
    assert b"Coffee 11" in response.data
    assert b"Different expense" not in response.data
    assert b"q=Coffee" in response.data


def test_insights_compare_periods_and_surface_largest_frequent_and_category_trends(client):
    register(client)
    today = date.today()
    start = today - timedelta(days=2)
    previous_start = start - timedelta(days=3)
    with client.application.app_context():
        user = User.query.filter_by(email="person@example.com").one()
        db.session.add_all(
            [
                Expense(
                    user_id=user.id,
                    amount=10,
                    date=previous_start,
                    category="Food & dining",
                    description="Coffee",
                    payment_method="Card",
                ),
                Expense(
                    user_id=user.id,
                    amount=7,
                    date=start,
                    category="Food & dining",
                    description="Coffee",
                    payment_method="Card",
                ),
                Expense(
                    user_id=user.id,
                    amount=8,
                    date=start + timedelta(days=1),
                    category="Food & dining",
                    description="Coffee",
                    payment_method="Card",
                ),
                Expense(
                    user_id=user.id,
                    amount=30,
                    date=today,
                    category="Food & dining",
                    description="Lunch",
                    payment_method="Card",
                ),
                Expense(
                    user_id=user.id,
                    amount=4,
                    date=start + timedelta(days=1),
                    category="Transport",
                    description="Bus",
                    payment_method="Cash",
                ),
            ]
        )
        db.session.commit()

    response = client.get(
        "/?start={}&end={}".format(start.isoformat(), today.isoformat())
    )

    assert response.status_code == 200
    assert b"390.0%" in response.data
    assert b"previous $10.00" in response.data
    assert b"Lunch" in response.data
    assert b"2 transactions" in response.data
    analytics = parse_dashboard_analytics(response)
    assert analytics["trendGranularity"] == "day"
    assert len(analytics["categoryTrendPeriods"]) == 3
    trends = {item["name"]: item["values"] for item in analytics["categoryTrendSeries"]}
    assert trends["Food & dining"] == [7.0, 8.0, 30.0]
    assert trends["Transport"] == [0, 4.0, 0]


@pytest.mark.parametrize(
    ("period_days", "granularity"),
    [(91, "week"), (400, "month")],
)
def test_category_trends_roll_up_longer_periods(client, period_days, granularity):
    register(client)
    today = date.today()
    start = today - timedelta(days=period_days - 1)
    with client.application.app_context():
        user = User.query.filter_by(email="person@example.com").one()
        db.session.add_all(
            [
                Expense(
                    user_id=user.id,
                    amount=7,
                    date=start + timedelta(days=5),
                    category="Food & dining",
                    description="Groceries",
                    payment_method="Card",
                ),
                Expense(
                    user_id=user.id,
                    amount=8,
                    date=today - timedelta(days=5),
                    category="Food & dining",
                    description="Groceries",
                    payment_method="Card",
                ),
            ]
        )
        db.session.commit()

    response = client.get(
        "/?start={}&end={}".format(start.isoformat(), today.isoformat())
    )
    analytics = parse_dashboard_analytics(response)

    assert response.status_code == 200
    assert analytics["trendGranularity"] == granularity
    values = analytics["categoryTrendSeries"][0]["values"]
    assert len([amount for amount in values if amount]) == 2
    assert sum(values) == 15
