# Pennywise Expense Tracker

A personal expense dashboard built with Flask, SQLAlchemy, pandas, and Plotly. Users can record and categorize expenses, set monthly budgets, import/export CSV files, and review interactive spending charts.

Expenses can include a merchant/payee, notes, and free-form tags. Use Quick add for a shorter entry form; the last category and payment method are remembered for each account. Duplicate on an expense opens a prefilled form dated today so recurring purchases take only a few edits.

The expense history supports case-insensitive search across descriptions, merchants, and tags; sorting by date, description, merchant, category, or amount; and paginated results with adjustable page sizes.

Monthly budgets can be updated in place. The dashboard shows how much remains (or how far over the limit spending is) and flags budgets at 80% usage.

Recurring weekly and monthly expenses appear under Upcoming charges. Marking a charge paid adds a dated transaction and advances its schedule; stopped schedules are hidden while their recorded expenses remain.

The dashboard compares the selected date range with the preceding range of equal length, plots category trends by day/week/month as the range grows, and highlights the largest transactions and repeated descriptions.

Interactive charts show loading or error status, and a server-rendered text summary remains available if JavaScript or the Plotly CDN cannot load.

Each account can choose a supported currency and number/date locale from Settings. Currency changes relabel saved amounts and do not perform exchange-rate conversions. CSV exports include the currency code; imports with a currency column must match the account currency.

## Account recovery and login protection

The login page links to a password-reset flow. Reset links are sent by SMTP, expire after 60 minutes, are stored as hashes, and can only be used once. Responses do not disclose whether an email belongs to an account. Configure `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USE_TLS` (or `MAIL_USE_SSL`), `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_DEFAULT_SENDER`, and `PUBLIC_BASE_URL` before enabling recovery. Use an app-specific SMTP credential and keep it outside source control. If mail settings are missing or delivery fails, the request remains generic and the failure is logged; no usable reset link is created.

Failed sign-ins are limited to 5 attempts per email and 20 per source IP in a 15-minute window. Reset-email requests are limited to 3 per email and 10 per source IP in the same window. Only keyed hashes of email addresses and source IPs are stored for these limits. If running behind a reverse proxy, set `TRUSTED_PROXY_COUNT` only when the app is unreachable directly and the proxy overwrites forwarded-address headers.

## Before public launch

The following operational work is a launch plan; it is not yet automated by this repository.

1. **Versioned database migrations:** the current app calls `db.create_all()` at startup and contains manual SQLite column upgrades. Before launch, add Flask-Migrate/Alembic, create and review a baseline migration for the current schema, and move those upgrades into versioned migrations. Test upgrades against a restored copy of the real database, take a backup, then run `flask db upgrade` once as a deployment step before starting web workers. Remove schema-changing startup behavior from the production path.
2. **Automated backups:** for SQLite, schedule a daily job using SQLite’s online backup API (not a file copy while the database is open), retain daily and monthly copies, encrypt and transfer backups off-host, and alert on failures. Take a pre-migration snapshot and test a restore at least monthly. For a multi-user public deployment, prefer managed PostgreSQL with automated snapshots and point-in-time recovery.
3. **Production deployment:** use the WSGI entry point `wsgi:app` with Gunicorn on Linux behind a TLS-terminating reverse proxy (for example, `gunicorn --workers 2 --bind 127.0.0.1:8000 wsgi:app`). Bind Gunicorn to localhost, run it under a service manager, and configure health checks, log collection, and restart policies. Set `APP_ENV=production` (which requires a persistent `SECRET_KEY` and enables secure session cookies), `PUBLIC_BASE_URL` to the HTTPS origin, the SMTP settings above, and `DATABASE_URL` for the chosen database. Install the database driver appropriate to that provider. Keep `FLASK_DEBUG` unset or set to `0`; never expose the development server. Set `TRUSTED_PROXY_COUNT` only for the exact trusted proxy count.

## Run locally

Requires Python 3.10 or later.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:SECRET_KEY = (python -c "import secrets; print(secrets.token_hex(32))")
python app.py
```

Open http://127.0.0.1:5000 and create an account. The SQLite database is created at `instance/expenses.db`. Each account can access only its own expenses and budgets.

For development-only debug output, set `FLASK_DEBUG=1` before running the app. Do not enable Flask debug mode in production. In production, set a persistent, randomly generated `SECRET_KEY`, run behind a production WSGI server, and use HTTPS.

## CSV format

`date`, `amount`, and `category` columns are required. `description`, `merchant`, `notes`, `tags`, `payment_method`, and `currency` are optional; payment method defaults to `Other`, and a supplied currency must match the account setting.

```csv
date,amount,category,description,merchant,notes,tags,payment_method
2026-01-03,18.50,Food & dining,Coffee and breakfast,Corner Cafe,Team breakfast,work,Card
```

CSV dates and decimal amounts remain locale-independent (`YYYY-MM-DD` and `.` decimal separator) so files stay portable and can be re-imported. Import accepts UTF-8 CSV files up to 2 MB and 5,000 rows. A file with any invalid row is rejected as a whole.

## Tests

```powershell
python -m pytest
```
