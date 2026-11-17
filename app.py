import calendar
import csv
import hashlib
import hmac
import io
import os
import secrets
import smtplib
import ssl
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from urllib.parse import urlsplit

import pandas as pd
from babel.dates import format_date
from babel.numbers import format_currency, format_decimal
from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Numeric, UniqueConstraint, inspect, or_, text
from sqlalchemy.exc import IntegrityError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message_category = "info"

CATEGORIES = [
    "Food & dining",
    "Transport",
    "Shopping",
    "Bills & utilities",
    "Health",
    "Entertainment",
    "Travel",
    "Education",
    "Other",
]
PAYMENT_METHODS = ["Card", "Cash", "Bank transfer", "Other"]
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
CURRENCIES = {
    "USD": "US Dollar",
    "CAD": "Canadian Dollar",
    "GBP": "British Pound",
    "EUR": "Euro",
    "AUD": "Australian Dollar",
    "NZD": "New Zealand Dollar",
    "JPY": "Japanese Yen",
    "CNY": "Chinese Yuan",
    "INR": "Indian Rupee",
    "KRW": "South Korean Won",
    "CHF": "Swiss Franc",
    "SEK": "Swedish Krona",
    "NOK": "Norwegian Krone",
    "DKK": "Danish Krone",
    "PLN": "Polish Zloty",
    "CZK": "Czech Koruna",
    "BRL": "Brazilian Real",
    "MXN": "Mexican Peso",
    "SGD": "Singapore Dollar",
    "HKD": "Hong Kong Dollar",
    "ZAR": "South African Rand",
}
LOCALES = {
    "en_US": "English (United States)",
    "en_GB": "English (United Kingdom)",
    "en_CA": "English (Canada)",
    "en_AU": "English (Australia)",
    "fr_FR": "French (France)",
    "fr_CA": "French (Canada)",
    "de_DE": "German (Germany)",
    "es_ES": "Spanish (Spain)",
    "it_IT": "Italian (Italy)",
    "ja_JP": "Japanese (Japan)",
    "hi_IN": "Hindi (India)",
    "pt_BR": "Portuguese (Brazil)",
    "zh_CN": "Chinese (China)",
    "ko_KR": "Korean (South Korea)",
    "nl_NL": "Dutch (Netherlands)",
    "sv_SE": "Swedish (Sweden)",
    "da_DK": "Danish (Denmark)",
    "nb_NO": "Norwegian Bokmål (Norway)",
    "pl_PL": "Polish (Poland)",
    "cs_CZ": "Czech (Czechia)",
    "af_ZA": "Afrikaans (South Africa)",
}


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    currency = db.Column(db.String(3), nullable=False, default="USD", server_default="USD")
    locale = db.Column(db.String(12), nullable=False, default="en_US", server_default="en_US")
    last_expense_category = db.Column(
        db.String(40), nullable=False, default="Other", server_default="Other"
    )
    last_payment_method = db.Column(
        db.String(30), nullable=False, default="Card", server_default="Card"
    )
    expenses = db.relationship("Expense", backref="user", cascade="all, delete-orphan")
    budgets = db.relationship("Budget", backref="user", cascade="all, delete-orphan")
    password_reset_tokens = db.relationship(
        "PasswordResetToken", backref="user", cascade="all, delete-orphan"
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    amount = db.Column(Numeric(12, 2), nullable=False)
    date = db.Column(db.Date, nullable=False, index=True)
    category = db.Column(db.String(40), nullable=False)
    description = db.Column(db.String(160), nullable=False, default="")
    merchant = db.Column(db.String(120), nullable=False, default="")
    notes = db.Column(db.Text, nullable=False, default="")
    tags = db.Column(db.String(200), nullable=False, default="")
    payment_method = db.Column(db.String(30), nullable=False, default="Card")
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)


class Budget(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    category = db.Column(db.String(40), nullable=False)
    monthly_limit = db.Column(Numeric(12, 2), nullable=False)
    __table_args__ = (
        db.UniqueConstraint("user_id", "category", name="uq_budget_user_category"),
    )


class RecurringExpense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    amount = db.Column(Numeric(12, 2), nullable=False)
    category = db.Column(db.String(40), nullable=False)
    description = db.Column(db.String(160), nullable=False)
    payment_method = db.Column(db.String(30), nullable=False)
    frequency = db.Column(db.String(10), nullable=False)
    next_due = db.Column(db.Date, nullable=False, index=True)
    day_of_month = db.Column(db.Integer, nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)


class RecurringPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    recurring_expense_id = db.Column(
        db.Integer, db.ForeignKey("recurring_expense.id"), nullable=False
    )
    expense_id = db.Column(db.Integer, db.ForeignKey("expense.id"), nullable=False, unique=True)
    due_date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        UniqueConstraint(
            "recurring_expense_id", "due_date", name="uq_recurring_payment_due_date"
        ),
    )


class PasswordResetToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    used_at = db.Column(db.DateTime)


class LoginAttempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    purpose = db.Column(db.String(12), nullable=False, default="login", index=True)
    email_hash = db.Column(db.String(64), nullable=False, index=True)
    ip_hash = db.Column(db.String(64), nullable=False, index=True)
    attempted_at = db.Column(db.DateTime, nullable=False, default=utc_now, index=True)


def next_recurrence_date(current_date, frequency, day_of_month):
    if frequency == "weekly":
        return current_date + timedelta(days=7)
    if frequency == "monthly":
        month = current_date.month % 12 + 1
        year = current_date.year + (current_date.month == 12)
        day = min(day_of_month, calendar.monthrange(year, month)[1])
        return date(year, month, day)
    raise ValueError("Unsupported recurring expense frequency.")


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


def auth_fingerprint(secret_key, purpose, value):
    key = secret_key if isinstance(secret_key, bytes) else str(secret_key).encode("utf-8")
    message = "{}\0{}".format(purpose, value).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def reset_mail_is_configured(app):
    base_url = app.config.get("PUBLIC_BASE_URL") or ""
    try:
        parsed_base_url = urlsplit(base_url)
    except ValueError:
        return False
    return bool(
        app.config.get("MAIL_SERVER")
        and app.config.get("MAIL_DEFAULT_SENDER")
        and parsed_base_url.netloc
        and parsed_base_url.scheme in {"http", "https"}
        and (
            app.config.get("APP_ENV") != "production"
            or parsed_base_url.scheme == "https"
        )
        and (
            not app.config.get("MAIL_USERNAME")
            or app.config.get("MAIL_USE_TLS")
            or app.config.get("MAIL_USE_SSL")
        )
    )


def send_password_reset_email(app, recipient, reset_url):
    if not reset_mail_is_configured(app):
        app.logger.error(
            "Password reset email is not configured safely; check SMTP credentials, "
            "TLS settings, MAIL_DEFAULT_SENDER, and PUBLIC_BASE_URL."
        )
        return False

    mail_server = app.config["MAIL_SERVER"]
    sender = app.config["MAIL_DEFAULT_SENDER"]
    message = EmailMessage()
    message["Subject"] = "Reset your Pennywise password"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(
        "We received a request to reset your Pennywise password.\n\n"
        "Use this link within the next {} minutes to choose a new password:\n"
        "{}\n\n"
        "If you did not request a reset, you can ignore this message.".format(
            app.config["PASSWORD_RESET_TOKEN_TTL_MINUTES"], reset_url
        )
    )
    try:
        if app.config.get("MAIL_USE_SSL"):
            with smtplib.SMTP_SSL(
                mail_server,
                app.config.get("MAIL_PORT", 465),
                timeout=10,
                context=ssl.create_default_context(),
            ) as mail:
                if app.config.get("MAIL_USERNAME"):
                    mail.login(app.config["MAIL_USERNAME"], app.config.get("MAIL_PASSWORD", ""))
                mail.send_message(message)
        else:
            with smtplib.SMTP(
                mail_server, app.config.get("MAIL_PORT", 587), timeout=10
            ) as mail:
                if app.config.get("MAIL_USE_TLS", True):
                    mail.starttls(context=ssl.create_default_context())
                if app.config.get("MAIL_USERNAME"):
                    mail.login(app.config["MAIL_USERNAME"], app.config.get("MAIL_PASSWORD", ""))
                mail.send_message(message)
    except (OSError, smtplib.SMTPException):
        app.logger.exception("Password reset email delivery failed.")
        return False
    return True


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    app_environment = os.environ.get("APP_ENV", "development").lower()
    secret_key = os.environ.get("SECRET_KEY")
    if app_environment == "production" and not secret_key and not (
        test_config and test_config.get("SECRET_KEY")
    ):
        raise RuntimeError("SECRET_KEY must be configured for production.")
    app.config.from_mapping(
        SECRET_KEY=secret_key or secrets.token_hex(32),
        APP_ENV=app_environment,
        SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL")
        or "sqlite:///" + os.path.join(app.instance_path, "expenses.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        MAIL_SERVER=os.environ.get("MAIL_SERVER"),
        MAIL_PORT=int(os.environ.get("MAIL_PORT", "587")),
        MAIL_USE_TLS=os.environ.get("MAIL_USE_TLS", "true").lower()
        in {"1", "true", "yes"},
        MAIL_USE_SSL=os.environ.get("MAIL_USE_SSL", "false").lower()
        in {"1", "true", "yes"},
        MAIL_USERNAME=os.environ.get("MAIL_USERNAME"),
        MAIL_PASSWORD=os.environ.get("MAIL_PASSWORD"),
        MAIL_DEFAULT_SENDER=os.environ.get("MAIL_DEFAULT_SENDER"),
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL", ""),
        PASSWORD_RESET_TOKEN_TTL_MINUTES=int(
            os.environ.get("PASSWORD_RESET_TOKEN_TTL_MINUTES", "60")
        ),
        LOGIN_ATTEMPT_WINDOW_MINUTES=int(
            os.environ.get("LOGIN_ATTEMPT_WINDOW_MINUTES", "15")
        ),
        LOGIN_ATTEMPTS_PER_EMAIL=int(os.environ.get("LOGIN_ATTEMPTS_PER_EMAIL", "5")),
        LOGIN_ATTEMPTS_PER_IP=int(os.environ.get("LOGIN_ATTEMPTS_PER_IP", "20")),
        PASSWORD_RESET_REQUESTS_PER_EMAIL=int(
            os.environ.get("PASSWORD_RESET_REQUESTS_PER_EMAIL", "3")
        ),
        PASSWORD_RESET_REQUESTS_PER_IP=int(
            os.environ.get("PASSWORD_RESET_REQUESTS_PER_IP", "10")
        ),
        TRUSTED_PROXY_COUNT=int(os.environ.get("TRUSTED_PROXY_COUNT", "0")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=app_environment == "production",
    )
    if test_config:
        app.config.update(test_config)

    db.init_app(app)
    login_manager.init_app(app)
    if app.config["TRUSTED_PROXY_COUNT"] < 0:
        raise ValueError("TRUSTED_PROXY_COUNT cannot be negative.")
    if app.config["TRUSTED_PROXY_COUNT"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=app.config["TRUSTED_PROXY_COUNT"])
    with app.app_context():
        db.create_all()
        user_columns = {column["name"] for column in inspect(db.engine).get_columns("user")}
        expense_columns = {
            column["name"] for column in inspect(db.engine).get_columns("expense")
        }
        with db.engine.begin() as connection:
            if "currency" not in user_columns:
                connection.execute(
                    text('ALTER TABLE "user" ADD COLUMN currency VARCHAR(3) NOT NULL DEFAULT \'USD\'')
                )
            if "locale" not in user_columns:
                connection.execute(
                    text('ALTER TABLE "user" ADD COLUMN locale VARCHAR(12) NOT NULL DEFAULT \'en_US\'')
                )
            if "last_expense_category" not in user_columns:
                connection.execute(
                    text(
                        'ALTER TABLE "user" ADD COLUMN last_expense_category VARCHAR(40) '
                        "NOT NULL DEFAULT 'Other'"
                    )
                )
            if "last_payment_method" not in user_columns:
                connection.execute(
                    text(
                        'ALTER TABLE "user" ADD COLUMN last_payment_method VARCHAR(30) '
                        "NOT NULL DEFAULT 'Card'"
                    )
                )
            if "merchant" not in expense_columns:
                connection.execute(
                    text(
                        "ALTER TABLE expense ADD COLUMN merchant VARCHAR(120) "
                        "NOT NULL DEFAULT ''"
                    )
                )
            if "notes" not in expense_columns:
                connection.execute(
                    text("ALTER TABLE expense ADD COLUMN notes TEXT NOT NULL DEFAULT ''")
                )
            if "tags" not in expense_columns:
                connection.execute(
                    text(
                        "ALTER TABLE expense ADD COLUMN tags VARCHAR(200) "
                        "NOT NULL DEFAULT ''"
                    )
                )

    @app.context_processor
    def inject_template_values():
        return {
            "categories": CATEGORIES,
            "currencies": CURRENCIES,
            "locales": LOCALES,
        }

    @app.template_filter("money")
    def money_filter(amount):
        currency = current_user.currency if current_user.is_authenticated else "USD"
        locale = current_user.locale if current_user.is_authenticated else "en_US"
        return format_currency(amount, currency, locale=locale)

    @app.template_filter("localdate")
    def localdate_filter(value, style="medium"):
        locale = current_user.locale if current_user.is_authenticated else "en_US"
        return format_date(value, format=style, locale=locale)

    @app.template_filter("localnumber")
    def localnumber_filter(value, decimal_places=2):
        locale = current_user.locale if current_user.is_authenticated else "en_US"
        decimal_places = max(0, min(int(decimal_places), 6))
        pattern = "#,##0"
        if decimal_places:
            pattern += "." + ("0" * decimal_places)
        return format_decimal(value, format=pattern, locale=locale)

    @app.before_request
    def protect_forms():
        if request.method == "POST":
            submitted = request.form.get("csrf_token", "")
            expected = session.get("_csrf_token", "")
            if not expected or not secrets.compare_digest(submitted, expected):
                abort(400, description="The form expired. Please reload and try again.")

    @app.context_processor
    def inject_csrf_token():
        def csrf_token():
            token = session.get("_csrf_token")
            if not token:
                token = secrets.token_urlsafe(32)
                session["_csrf_token"] = token
            return token

        return {"csrf_token": csrf_token}

    def dashboard_url_from_filters():
        return url_for(
            "dashboard",
            start=request.args.get("start"),
            end=request.args.get("end"),
            category=request.args.get("category"),
            payment_method=request.args.get("payment_method"),
            q=request.args.get("q"),
            sort=request.args.get("sort"),
            direction=request.args.get("direction"),
            per_page=request.args.get("per_page"),
            page=request.args.get("page"),
        )

    @app.get("/")
    @login_required
    def dashboard():
        today = date.today()
        start_raw = request.args.get("start", today.replace(day=1).isoformat())
        end_raw = request.args.get("end", today.isoformat())
        try:
            start_date = date.fromisoformat(start_raw)
            end_date = date.fromisoformat(end_raw)
            if end_date < start_date:
                raise ValueError
        except ValueError:
            flash("Choose a valid date range.", "error")
            start_date = today.replace(day=1)
            end_date = today

        selected_category = request.args.get("category", "")
        selected_payment = request.args.get("payment_method", "")
        if selected_category and selected_category not in CATEGORIES:
            flash("Choose a valid category filter.", "error")
            selected_category = ""
        if selected_payment and selected_payment not in PAYMENT_METHODS:
            flash("Choose a valid payment method filter.", "error")
            selected_payment = ""
        filtered_expense_query = Expense.query.filter_by(user_id=current_user.id)
        search_query = request.args.get("q", "").strip()
        if len(search_query) > 100:
            flash("Search text must be 100 characters or fewer.", "error")
            search_query = search_query[:100]
        if selected_category:
            filtered_expense_query = filtered_expense_query.filter(
                Expense.category == selected_category
            )
        if selected_payment:
            filtered_expense_query = filtered_expense_query.filter(
                Expense.payment_method == selected_payment
            )
        if search_query:
            escaped_search = (
                search_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            filtered_expense_query = filtered_expense_query.filter(
                or_(
                    Expense.description.ilike("%{}%".format(escaped_search), escape="\\"),
                    Expense.merchant.ilike("%{}%".format(escaped_search), escape="\\"),
                    Expense.tags.ilike("%{}%".format(escaped_search), escape="\\"),
                )
            )
        expense_query = filtered_expense_query.filter(
            Expense.date >= start_date, Expense.date <= end_date
        )
        period_days = (end_date - start_date).days + 1
        previous_end = start_date - timedelta(days=1)
        previous_start = start_date - timedelta(days=period_days)
        previous_query = filtered_expense_query.filter(
            Expense.date >= previous_start, Expense.date <= previous_end
        )
        previous_total = float(
            previous_query.with_entities(
                db.func.coalesce(db.func.sum(Expense.amount), 0)
            ).scalar()
        )
        previous_count = previous_query.count()

        sort_by = request.args.get("sort", "date")
        sort_columns = {
            "date": Expense.date,
            "description": Expense.description,
            "merchant": Expense.merchant,
            "category": Expense.category,
            "amount": Expense.amount,
        }
        if sort_by not in sort_columns:
            flash("Choose a valid sort option.", "error")
            sort_by = "date"
        sort_direction = request.args.get("direction", "desc")
        if sort_direction not in {"asc", "desc"}:
            flash("Choose a valid sort direction.", "error")
            sort_direction = "desc"
        try:
            per_page = int(request.args.get("per_page", "10"))
        except ValueError:
            per_page = 10
            flash("Choose a valid page size.", "error")
        if per_page not in {10, 25, 50}:
            per_page = 10
            flash("Choose a valid page size.", "error")

        total_count = expense_query.count()
        page_count = max(1, (total_count + per_page - 1) // per_page)
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            page = 1
            flash("Choose a valid page.", "error")
        if page < 1:
            page = 1
            flash("Choose a valid page.", "error")
        elif page > page_count:
            page = page_count

        sort_column = sort_columns[sort_by]
        ordered_query = expense_query.order_by(
            sort_column.asc() if sort_direction == "asc" else sort_column.desc(),
            Expense.id.desc(),
        )
        expenses = ordered_query.offset((page - 1) * per_page).limit(per_page).all()
        frame = pd.DataFrame(
            [
                {"category": row.category, "amount": float(row.amount)}
                for row in expense_query.with_entities(
                    Expense.category.label("category"),
                    db.func.sum(Expense.amount).label("amount"),
                )
                .group_by(Expense.category)
                .all()
            ],
            columns=["category", "amount"],
        )
        total = float(frame["amount"].sum()) if not frame.empty else 0
        category_totals = (
            frame.groupby("category")["amount"].sum().sort_values(ascending=False)
            if not frame.empty
            else pd.Series(dtype=float)
        )
        daily_frame = pd.DataFrame(
            [
                {"date": row.date, "amount": float(row.amount)}
                for row in expense_query.with_entities(
                    Expense.date.label("date"),
                    db.func.sum(Expense.amount).label("amount"),
                )
                .group_by(Expense.date)
                .all()
            ],
            columns=["date", "amount"],
        )
        daily = (
            daily_frame.set_index("date")["amount"].sort_index()
            if not daily_frame.empty
            else pd.Series(dtype=float)
        )
        category_date_rows = expense_query.with_entities(
            Expense.date.label("date"),
            Expense.category.label("category"),
            db.func.sum(Expense.amount).label("amount"),
        ).group_by(Expense.date, Expense.category).all()
        category_date_frame = pd.DataFrame(
            [
                {"date": row.date, "category": row.category, "amount": float(row.amount)}
                for row in category_date_rows
            ],
            columns=["date", "category", "amount"],
        )
        if period_days <= 45:
            trend_granularity = "day"
            trend_frequency = "D"
        elif period_days <= 365:
            trend_granularity = "week"
            trend_frequency = "W-SUN"
        else:
            trend_granularity = "month"
            trend_frequency = "M"
        if category_date_frame.empty:
            category_trend_periods = []
            category_trend_series = []
        else:
            category_date_frame["period"] = (
                pd.to_datetime(category_date_frame["date"])
                .dt.to_period(trend_frequency)
                .dt.start_time.dt.strftime("%Y-%m-%d")
            )
            category_trend = (
                category_date_frame.pivot_table(
                    index="period",
                    columns="category",
                    values="amount",
                    aggfunc="sum",
                    fill_value=0,
                )
                .sort_index()
            )
            category_trend_periods = category_trend.index.tolist()
            category_trend_series = [
                {
                    "name": category,
                    "values": [
                        round(float(value), 2)
                        for value in category_trend[category].tolist()
                    ],
                }
                for category in category_trend.columns.tolist()
            ]
        daily_peak_date = daily.idxmax() if not daily.empty else None
        category_trend_summary = [
            {
                "name": series["name"],
                "firstAmount": series["values"][0] if series["values"] else 0,
                "lastAmount": series["values"][-1] if series["values"] else 0,
            }
            for series in category_trend_series
        ]
        category_trend_start_date = (
            date.fromisoformat(category_trend_periods[0]) if category_trend_periods else None
        )
        category_trend_end_date = (
            date.fromisoformat(category_trend_periods[-1]) if category_trend_periods else None
        )
        largest_expenses = expense_query.order_by(
            Expense.amount.desc(), Expense.date.desc(), Expense.id.desc()
        ).limit(5).all()
        expense_name = db.func.coalesce(
            db.func.nullif(db.func.trim(Expense.merchant), ""),
            db.func.nullif(db.func.trim(Expense.description), ""),
            Expense.category,
        )
        frequent_expense_rows = (
            expense_query.with_entities(
                db.func.min(expense_name).label("name"),
                db.func.min(Expense.category).label("category"),
                db.func.count(Expense.id).label("count"),
                db.func.sum(Expense.amount).label("total"),
            )
            .group_by(db.func.lower(expense_name), Expense.category)
            .having(db.func.count(Expense.id) > 1)
            .order_by(
                db.func.count(Expense.id).desc(),
                db.func.sum(Expense.amount).desc(),
            )
            .limit(5)
            .all()
        )
        frequent_expenses = [
            {
                "name": row.name,
                "category": row.category,
                "count": row.count,
                "total": float(row.total),
            }
            for row in frequent_expense_rows
        ]
        if previous_total:
            comparison_percent = abs((total - previous_total) / previous_total * 100)
            comparison_direction = (
                "increase"
                if total > previous_total
                else "decrease"
                if total < previous_total
                else "steady"
            )
            comparison_label = None
        elif total:
            comparison_percent = None
            comparison_direction = "increase"
            comparison_label = "New spending this period"
        else:
            comparison_percent = None
            comparison_direction = "steady"
            comparison_label = "No spending in either period"
        budget_rows = Budget.query.filter_by(user_id=current_user.id).all()
        budgets = {item.category: float(item.monthly_limit) for item in budget_rows}
        recurring_expenses = (
            RecurringExpense.query.filter_by(user_id=current_user.id, active=True)
            .order_by(RecurringExpense.next_due, RecurringExpense.id)
            .all()
        )
        current_month_total = (
            db.session.query(db.func.coalesce(db.func.sum(Expense.amount), 0))
            .filter(
                Expense.user_id == current_user.id,
                Expense.date >= today.replace(day=1),
                Expense.date <= today,
            )
            .scalar()
        )
        budget_progress = []
        for item in budget_rows:
            spent = (
                db.session.query(db.func.coalesce(db.func.sum(Expense.amount), 0))
                .filter(
                    Expense.user_id == current_user.id,
                    Expense.category == item.category,
                    Expense.date >= today.replace(day=1),
                    Expense.date <= today,
                )
                .scalar()
            )
            limit = float(item.monthly_limit)
            spent_amount = float(spent)
            over = spent_amount > limit
            budget_progress.append(
                {
                    "category": item.category,
                    "limit": limit,
                    "spent": spent_amount,
                    "remaining": max(limit - spent_amount, 0),
                    "overage": max(spent_amount - limit, 0),
                    "percent": min(spent_amount / limit * 100, 100) if limit else 0,
                    "over": over,
                    "approaching": not over and spent_amount >= limit * 0.8,
                }
            )
        analytics = {
            "categories": category_totals.index.tolist(),
            "categoryAmounts": [round(float(value), 2) for value in category_totals.tolist()],
            "dates": [item.isoformat() for item in daily.index.tolist()],
            "dailyAmounts": [round(float(value), 2) for value in daily.tolist()],
            "categoryTrendPeriods": category_trend_periods,
            "categoryTrendSeries": category_trend_series,
            "categoryTrendSummary": category_trend_summary,
            "categoryTrendStart": category_trend_periods[0] if category_trend_periods else None,
            "categoryTrendEnd": category_trend_periods[-1] if category_trend_periods else None,
            "dailySummary": {
                "dayCount": len(daily),
                "peakAmount": round(float(daily.max()), 2) if not daily.empty else 0,
            },
            "trendGranularity": trend_granularity,
            "currency": current_user.currency,
            "locale": current_user.locale,
            "trendLabel": {
                "day": "Daily",
                "week": "Weekly",
                "month": "Monthly",
            }[trend_granularity],
        }
        return render_template(
            "dashboard.html",
            expenses=expenses,
            today=today,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            selected_category=selected_category,
            selected_payment=selected_payment,
            search_query=search_query,
            sort_by=sort_by,
            sort_direction=sort_direction,
            per_page=per_page,
            page=page,
            page_count=page_count,
            total_count=total_count,
            pagination_params={
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "category": selected_category,
                "payment_method": selected_payment,
                "q": search_query,
                "sort": sort_by,
                "direction": sort_direction,
                "per_page": per_page,
            },
            total=total,
            previous_total=previous_total,
            previous_start=previous_start,
            previous_end=previous_end,
            previous_count=previous_count,
            comparison_percent=comparison_percent,
            comparison_direction=comparison_direction,
            comparison_label=comparison_label,
            transaction_count=total_count,
            average=total / total_count if total_count else 0,
            largest_expenses=largest_expenses,
            frequent_expenses=frequent_expenses,
            current_month_total=float(current_month_total),
            budgets=budgets,
            budget_progress=budget_progress,
            recurring_expenses=recurring_expenses,
            analytics=analytics,
            daily_peak_date=daily_peak_date,
            chart_start_date=start_date,
            chart_end_date=end_date,
            category_trend_start_date=category_trend_start_date,
            category_trend_end_date=category_trend_end_date,
        )

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            if not email or len(email) > 254 or "@" not in email:
                flash("Enter a valid email address.", "error")
            elif len(password) < 8:
                flash("Use a password with at least 8 characters.", "error")
            elif User.query.filter_by(email=email).first():
                flash("An account with that email already exists.", "error")
            else:
                user = User(email=email)
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                login_user(user)
                flash("Your account is ready. Welcome!", "success")
                return redirect(url_for("dashboard"))
        return render_template("auth.html", mode="register")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            now = utc_now()
            cutoff = now - timedelta(minutes=app.config["LOGIN_ATTEMPT_WINDOW_MINUTES"])
            email_hash = auth_fingerprint(app.config["SECRET_KEY"], "login-email", email)
            ip_hash = auth_fingerprint(
                app.config["SECRET_KEY"], "login-ip", request.remote_addr or "unknown"
            )
            LoginAttempt.query.filter(LoginAttempt.attempted_at < cutoff).delete(
                synchronize_session=False
            )
            email_attempts = LoginAttempt.query.filter_by(
                purpose="login", email_hash=email_hash
            ).count()
            ip_attempts = LoginAttempt.query.filter_by(
                purpose="login", ip_hash=ip_hash
            ).count()
            if (
                email_attempts >= app.config["LOGIN_ATTEMPTS_PER_EMAIL"]
                or ip_attempts >= app.config["LOGIN_ATTEMPTS_PER_IP"]
            ):
                db.session.commit()
                flash(
                    "Too many unsuccessful sign-in attempts. Try again in {} minutes.".format(
                        app.config["LOGIN_ATTEMPT_WINDOW_MINUTES"]
                    ),
                    "error",
                )
                return redirect(url_for("login"))

            user = User.query.filter_by(email=email).first()
            if user and user.check_password(request.form.get("password", "")):
                LoginAttempt.query.filter_by(email_hash=email_hash).delete(
                    synchronize_session=False
                )
                db.session.commit()
                login_user(user)
                flash("Welcome back.", "success")
                return redirect(url_for("dashboard"))
            db.session.add(
                LoginAttempt(
                    purpose="login",
                    email_hash=email_hash,
                    ip_hash=ip_hash,
                    attempted_at=now,
                )
            )
            db.session.commit()
            flash("Email or password is incorrect.", "error")
        return render_template("auth.html", mode="login")

    @app.route("/password-reset", methods=["GET", "POST"])
    def request_password_reset():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            user = User.query.filter_by(email=email).first() if len(email) <= 254 else None
            now = utc_now()
            cutoff = now - timedelta(minutes=app.config["LOGIN_ATTEMPT_WINDOW_MINUTES"])
            email_hash = auth_fingerprint(app.config["SECRET_KEY"], "reset-email", email)
            ip_hash = auth_fingerprint(
                app.config["SECRET_KEY"], "reset-ip", request.remote_addr or "unknown"
            )
            LoginAttempt.query.filter(LoginAttempt.attempted_at < cutoff).delete(
                synchronize_session=False
            )
            reset_email_requests = LoginAttempt.query.filter_by(
                purpose="reset", email_hash=email_hash
            ).count()
            reset_ip_requests = LoginAttempt.query.filter_by(
                purpose="reset", ip_hash=ip_hash
            ).count()
            rate_limited = (
                reset_email_requests >= app.config["PASSWORD_RESET_REQUESTS_PER_EMAIL"]
                or reset_ip_requests >= app.config["PASSWORD_RESET_REQUESTS_PER_IP"]
            )
            if not rate_limited:
                db.session.add(
                    LoginAttempt(
                        purpose="reset",
                        email_hash=email_hash,
                        ip_hash=ip_hash,
                        attempted_at=now,
                    )
                )
                mail_configured = reset_mail_is_configured(app)
                if user and mail_configured:
                    raw_token = secrets.token_urlsafe(32)
                    db.session.query(PasswordResetToken).filter_by(
                        user_id=user.id
                    ).delete(synchronize_session=False)
                    reset_token = PasswordResetToken(
                        user_id=user.id,
                        token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
                        expires_at=now
                        + timedelta(minutes=app.config["PASSWORD_RESET_TOKEN_TTL_MINUTES"]),
                    )
                    db.session.add(reset_token)
                    db.session.commit()
                    reset_url = "{}{}".format(
                        app.config["PUBLIC_BASE_URL"].rstrip("/"),
                        url_for("reset_password", token=raw_token),
                    )
                    if not send_password_reset_email(app, user.email, reset_url):
                        reset_token.used_at = utc_now()
                        db.session.commit()
                else:
                    db.session.commit()
                    if user:
                        send_password_reset_email(app, user.email, "")
            else:
                db.session.commit()
            flash(
                "If an account exists for that email, reset instructions will be sent.",
                "info",
            )
            return redirect(url_for("request_password_reset"))
        return render_template("password_reset_request.html")

    def get_valid_reset_token(raw_token):
        if len(raw_token) > 128:
            return None
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        reset_token = PasswordResetToken.query.filter_by(token_hash=token_hash).first()
        if (
            reset_token is None
            or reset_token.used_at is not None
            or reset_token.expires_at <= utc_now()
        ):
            return None
        return reset_token

    @app.route("/password-reset/<token>", methods=["GET", "POST"])
    def reset_password(token):
        reset_token = get_valid_reset_token(token)
        if reset_token is None:
            flash("That reset link is invalid or expired. Request a new one.", "error")
            return redirect(url_for("request_password_reset"))

        if request.method == "POST":
            password = request.form.get("password", "")
            confirmation = request.form.get("confirm_password", "")
            if len(password) < 8:
                flash("Use a password with at least 8 characters.", "error")
            elif len(password) > 256:
                flash("Use a password with no more than 256 characters.", "error")
            elif password != confirmation:
                flash("The passwords do not match.", "error")
            else:
                now = utc_now()
                user = db.session.get(User, reset_token.user_id)
                if user is None:
                    flash("That reset link is invalid or expired. Request a new one.", "error")
                    return redirect(url_for("request_password_reset"))
                claimed_token = db.session.query(PasswordResetToken).filter(
                    PasswordResetToken.id == reset_token.id,
                    PasswordResetToken.used_at.is_(None),
                    PasswordResetToken.expires_at > now,
                ).update({"used_at": now}, synchronize_session=False)
                if claimed_token != 1:
                    db.session.rollback()
                    flash("That reset link is invalid or expired. Request a new one.", "error")
                    return redirect(url_for("request_password_reset"))
                user.set_password(password)
                reset_token.used_at = now
                db.session.query(PasswordResetToken).filter(
                    PasswordResetToken.user_id == user.id,
                    PasswordResetToken.id != reset_token.id,
                    PasswordResetToken.used_at.is_(None),
                ).update({"used_at": now}, synchronize_session=False)
                email_hash = auth_fingerprint(
                    app.config["SECRET_KEY"], "login-email", user.email
                )
                LoginAttempt.query.filter_by(
                    purpose="login", email_hash=email_hash
                ).delete(synchronize_session=False)
                db.session.commit()
                flash("Your password has been reset. Sign in with your new password.", "success")
                return redirect(url_for("login"))
        return render_template("password_reset.html")

    @app.after_request
    def protect_password_reset_response(response):
        if request.endpoint == "reset_password":
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Robots-Tag"] = "noindex"
        return response

    @app.post("/logout")
    @login_required
    def logout():
        logout_user()
        flash("You have been signed out.", "info")
        return redirect(url_for("login"))

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        if request.method == "POST":
            currency = request.form.get("currency", "")
            locale = request.form.get("locale", "")
            if currency not in CURRENCIES:
                flash("Choose a supported currency.", "error")
            elif locale not in LOCALES:
                flash("Choose a supported locale.", "error")
            else:
                current_user.currency = currency
                current_user.locale = locale
                db.session.commit()
                flash("Your currency and locale preferences have been saved.", "success")
                return redirect(url_for("settings"))
        return render_template("settings.html")

    def expense_values(form):
        try:
            amount = Decimal((form.get("amount") or "").strip())
            expense_date = date.fromisoformat(form.get("date") or "")
        except (InvalidOperation, ValueError):
            raise ValueError("Enter a valid amount and date.") from None
        category = form.get("category", "")
        payment_method = form.get("payment_method", "")
        description = form.get("description", "").strip()
        merchant = form.get("merchant", "").strip()
        notes = form.get("notes", "").strip()
        tags = form.get("tags", "").strip()
        if not amount.is_finite() or amount <= 0 or amount > Decimal("9999999999.99"):
            raise ValueError("Amount must be greater than zero and within the supported limit.")
        if category not in CATEGORIES:
            raise ValueError("Choose a valid category.")
        if payment_method not in PAYMENT_METHODS:
            raise ValueError("Choose a valid payment method.")
        if len(description) > 160:
            raise ValueError("Description must be 160 characters or fewer.")
        if len(merchant) > 120:
            raise ValueError("Merchant or payee must be 120 characters or fewer.")
        if len(notes) > 2000:
            raise ValueError("Notes must be 2,000 characters or fewer.")
        if len(tags) > 200:
            raise ValueError("Tags must be 200 characters or fewer.")
        return {
            "amount": amount.quantize(Decimal("0.01")),
            "date": expense_date,
            "category": category,
            "payment_method": payment_method,
            "description": description,
            "merchant": merchant,
            "notes": notes,
            "tags": tags,
        }

    def remember_expense_defaults(expense):
        current_user.last_expense_category = expense.category
        current_user.last_payment_method = expense.payment_method

    @app.route("/expenses/new", methods=["GET", "POST"])
    @login_required
    def new_expense():
        if request.method == "POST":
            try:
                expense = Expense(user_id=current_user.id, **expense_values(request.form))
                remember_expense_defaults(expense)
                db.session.add(expense)
                db.session.commit()
                flash("Expense added.", "success")
                return redirect(url_for("dashboard"))
            except ValueError as error:
                flash(str(error), "error")
        return render_template(
            "expense_form.html",
            expense=None,
            duplicate=False,
            quick_add=False,
            today=date.today().isoformat(),
        )

    @app.route("/expenses/quick", methods=["GET", "POST"])
    @login_required
    def quick_add_expense():
        if request.method == "POST":
            try:
                expense = Expense(user_id=current_user.id, **expense_values(request.form))
                remember_expense_defaults(expense)
                db.session.add(expense)
                db.session.commit()
                flash("Expense added.", "success")
                return redirect(url_for("quick_add_expense"))
            except ValueError as error:
                flash(str(error), "error")
        return render_template(
            "expense_form.html",
            expense=None,
            duplicate=False,
            quick_add=True,
            today=date.today().isoformat(),
        )

    @app.route("/expenses/<int:expense_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_expense(expense_id):
        expense = Expense.query.filter_by(id=expense_id, user_id=current_user.id).first_or_404()
        if request.method == "POST":
            try:
                for key, value in expense_values(request.form).items():
                    setattr(expense, key, value)
                db.session.commit()
                flash("Expense updated.", "success")
                return redirect(dashboard_url_from_filters())
            except ValueError as error:
                flash(str(error), "error")
        return render_template(
            "expense_form.html",
            expense=expense,
            duplicate=False,
            quick_add=False,
            today=date.today().isoformat(),
        )

    @app.route("/expenses/<int:expense_id>/duplicate", methods=["GET", "POST"])
    @login_required
    def duplicate_expense(expense_id):
        expense = Expense.query.filter_by(id=expense_id, user_id=current_user.id).first_or_404()
        if request.method == "POST":
            try:
                duplicate = Expense(
                    user_id=current_user.id,
                    **expense_values(request.form)
                )
                remember_expense_defaults(duplicate)
                db.session.add(duplicate)
                db.session.commit()
                flash("Expense duplicated.", "success")
                return redirect(dashboard_url_from_filters())
            except ValueError as error:
                flash(str(error), "error")
        return render_template(
            "expense_form.html",
            expense=expense,
            duplicate=True,
            quick_add=False,
            today=date.today().isoformat(),
        )

    @app.post("/expenses/<int:expense_id>/delete")
    @login_required
    def delete_expense(expense_id):
        expense = Expense.query.filter_by(id=expense_id, user_id=current_user.id).first_or_404()
        db.session.delete(expense)
        db.session.commit()
        flash("Expense deleted.", "success")
        return redirect(dashboard_url_from_filters())

    def parse_budget_amount(raw_amount):
        try:
            amount = Decimal((raw_amount or "").strip())
            if not amount.is_finite() or amount <= 0 or amount > Decimal("9999999999.99"):
                raise ValueError
        except (InvalidOperation, ValueError):
            return None
        return amount.quantize(Decimal("0.01"))

    @app.post("/budgets")
    @login_required
    def save_budget():
        category = request.form.get("category", "")
        amount = parse_budget_amount(request.form.get("amount"))
        if amount is None:
            flash("Enter a valid monthly budget greater than zero.", "error")
            return redirect(url_for("dashboard"))
        if category not in CATEGORIES:
            flash("Choose a valid budget category.", "error")
            return redirect(url_for("dashboard"))
        budget = Budget.query.filter_by(user_id=current_user.id, category=category).first()
        if budget is None:
            budget = Budget(user_id=current_user.id, category=category)
            db.session.add(budget)
        budget.monthly_limit = amount
        db.session.commit()
        flash("Monthly budget saved.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/budgets/<string:category>/edit")
    @login_required
    def edit_budget(category):
        budget = Budget.query.filter_by(user_id=current_user.id, category=category).first_or_404()
        amount = parse_budget_amount(request.form.get("amount"))
        if amount is None:
            flash("Enter a valid monthly budget greater than zero.", "error")
            return redirect(url_for("dashboard"))
        budget.monthly_limit = amount
        db.session.commit()
        flash("{} budget updated.".format(category), "success")
        return redirect(url_for("dashboard"))

    @app.post("/budgets/<string:category>/delete")
    @login_required
    def delete_budget(category):
        budget = Budget.query.filter_by(user_id=current_user.id, category=category).first_or_404()
        db.session.delete(budget)
        db.session.commit()
        flash("Budget removed.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/recurring")
    @login_required
    def create_recurring():
        try:
            amount = Decimal((request.form.get("amount") or "").strip())
            next_due = date.fromisoformat(request.form.get("next_due") or "")
        except (InvalidOperation, ValueError):
            flash("Enter a valid amount and next due date for the recurring expense.", "error")
            return redirect(url_for("dashboard"))
        category = request.form.get("category", "")
        description = (request.form.get("description") or "").strip()
        payment_method = request.form.get("payment_method", "")
        frequency = request.form.get("frequency", "")
        if not amount.is_finite() or amount <= 0 or amount > Decimal("9999999999.99"):
            flash("Recurring amount must be greater than zero and within the supported limit.", "error")
            return redirect(url_for("dashboard"))
        if category not in CATEGORIES or payment_method not in PAYMENT_METHODS:
            flash("Choose a valid category and payment method.", "error")
            return redirect(url_for("dashboard"))
        if frequency not in {"weekly", "monthly"}:
            flash("Choose a weekly or monthly schedule.", "error")
            return redirect(url_for("dashboard"))
        if not description or len(description) > 160:
            flash("Add a description of 1 to 160 characters for this recurring expense.", "error")
            return redirect(url_for("dashboard"))

        recurring = RecurringExpense(
            user_id=current_user.id,
            amount=amount.quantize(Decimal("0.01")),
            category=category,
            description=description,
            payment_method=payment_method,
            frequency=frequency,
            next_due=next_due,
            day_of_month=next_due.day,
        )
        db.session.add(recurring)
        db.session.commit()
        flash("Recurring expense scheduled.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/recurring/<int:recurring_id>/paid")
    @login_required
    def mark_recurring_paid(recurring_id):
        recurring = RecurringExpense.query.filter_by(
            id=recurring_id, user_id=current_user.id, active=True
        ).first_or_404()
        try:
            due_date = date.fromisoformat(request.form.get("due_date") or "")
        except ValueError:
            flash("Could not verify the scheduled charge. Refresh and try again.", "error")
            return redirect(url_for("dashboard"))
        if due_date != recurring.next_due:
            flash("This scheduled date has changed. Review the latest charge before marking it paid.", "info")
            return redirect(url_for("dashboard"))

        already_paid = RecurringPayment.query.filter_by(
            recurring_expense_id=recurring.id, due_date=due_date
        ).first()
        if already_paid:
            flash("This scheduled charge has already been marked paid.", "info")
            return redirect(url_for("dashboard"))

        expense = Expense(
            user_id=current_user.id,
            amount=recurring.amount,
            date=due_date,
            category=recurring.category,
            description=recurring.description,
            payment_method=recurring.payment_method,
        )
        db.session.add(expense)
        try:
            db.session.flush()
            db.session.add(
                RecurringPayment(
                    recurring_expense_id=recurring.id,
                    expense_id=expense.id,
                    due_date=due_date,
                )
            )
            recurring.next_due = next_recurrence_date(
                due_date, recurring.frequency, recurring.day_of_month
            )
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            already_paid = RecurringPayment.query.filter_by(
                recurring_expense_id=recurring_id, due_date=due_date
            ).first()
            if already_paid:
                flash("This scheduled charge has already been marked paid.", "info")
                return redirect(url_for("dashboard"))
            raise

        flash("Charge marked paid and added to expenses.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/recurring/<int:recurring_id>/stop")
    @login_required
    def stop_recurring(recurring_id):
        recurring = RecurringExpense.query.filter_by(
            id=recurring_id, user_id=current_user.id, active=True
        ).first_or_404()
        recurring.active = False
        db.session.commit()
        flash("Recurring expense stopped.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/import")
    @login_required
    def import_csv():
        upload = request.files.get("file")
        if not upload or not upload.filename:
            flash("Choose a CSV file to import.", "error")
            return redirect(url_for("dashboard"))
        if secure_filename(upload.filename).lower().rsplit(".", 1)[-1] != "csv":
            flash("Upload a .csv file.", "error")
            return redirect(url_for("dashboard"))
        contents = upload.stream.read(MAX_UPLOAD_BYTES + 1)
        if len(contents) > MAX_UPLOAD_BYTES:
            flash("CSV files must be 2 MB or smaller.", "error")
            return redirect(url_for("dashboard"))
        try:
            reader = csv.DictReader(io.StringIO(contents.decode("utf-8-sig")))
            required = {"date", "amount", "category"}
            if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
                raise ValueError("CSV needs date, amount, and category columns.")
            if len(reader.fieldnames) > 20:
                raise ValueError("CSV has too many columns.")
            rows = list(reader)
            if not rows or len(rows) > 5000:
                raise ValueError("CSV must contain between 1 and 5,000 expense rows.")
            entries = []
            for row_number, row in enumerate(rows, start=2):
                row_currency = (row.get("currency") or "").strip().upper()
                if row_currency and row_currency != current_user.currency:
                    raise ValueError(
                        "Row {}: currency {} does not match your account currency ({}). "
                        "No currency conversion is performed.".format(
                            row_number, row_currency, current_user.currency
                        )
                    )
                try:
                    values = expense_values(
                        {
                            "date": row.get("date", ""),
                            "amount": row.get("amount", ""),
                            "category": row.get("category", ""),
                            "payment_method": row.get("payment_method") or "Other",
                            "description": row.get("description") or "",
                            "merchant": row.get("merchant") or "",
                            "notes": row.get("notes") or "",
                            "tags": row.get("tags") or "",
                        }
                    )
                except ValueError as error:
                    raise ValueError("Row {}: {}".format(row_number, error)) from None
                entries.append(Expense(user_id=current_user.id, **values))
        except (UnicodeDecodeError, csv.Error, ValueError) as error:
            flash("Import failed: {}".format(error), "error")
            return redirect(url_for("dashboard"))
        db.session.add_all(entries)
        db.session.commit()
        flash("{} expenses imported.".format(len(entries)), "success")
        return redirect(url_for("dashboard"))

    @app.get("/export")
    @login_required
    def export_csv():
        query = Expense.query.filter_by(user_id=current_user.id)
        start_raw, end_raw = request.args.get("start"), request.args.get("end")
        if bool(start_raw) != bool(end_raw):
            flash("Provide both dates or neither when exporting.", "error")
            return redirect(url_for("dashboard"))
        if start_raw and end_raw:
            try:
                start_date = date.fromisoformat(start_raw)
                end_date = date.fromisoformat(end_raw)
                if end_date < start_date:
                    raise ValueError
            except ValueError:
                flash("Choose a valid date range before exporting.", "error")
                return redirect(url_for("dashboard"))
            query = query.filter(Expense.date >= start_date, Expense.date <= end_date)
        category = request.args.get("category", "")
        payment_method = request.args.get("payment_method", "")
        if category and category not in CATEGORIES:
            flash("Choose a valid category before exporting.", "error")
            return redirect(url_for("dashboard"))
        if payment_method and payment_method not in PAYMENT_METHODS:
            flash("Choose a valid payment method before exporting.", "error")
            return redirect(url_for("dashboard"))
        if category:
            query = query.filter(Expense.category == category)
        if payment_method:
            query = query.filter(Expense.payment_method == payment_method)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "date",
                "amount",
                "currency",
                "category",
                "description",
                "merchant",
                "notes",
                "tags",
                "payment_method",
            ]
        )

        def safe_csv_text(value):
            value = value or ""
            if value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
                return "'" + value
            return value

        for expense in query.order_by(Expense.date.desc()).all():
            writer.writerow(
                [
                    expense.date.isoformat(),
                    "{:.2f}".format(expense.amount),
                    current_user.currency,
                    expense.category,
                    safe_csv_text(expense.description),
                    safe_csv_text(expense.merchant),
                    safe_csv_text(expense.notes),
                    safe_csv_text(expense.tags),
                    expense.payment_method,
                ]
            )
        return Response(
            "\ufeff" + output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=expenses.csv"},
        )

    @app.errorhandler(413)
    def upload_too_large(_error):
        flash("The upload is too large. CSV files must be 2 MB or smaller.", "error")
        return redirect(url_for("dashboard"))

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
