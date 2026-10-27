# Voices of the Valley

The festival features one choir for singers ages 7–15. Families can express
interest for more than one eligible singer, and helpers are also welcome.

The festival page and FastAPI API run together on the same origin. Interest
submissions go to `POST /api/interests` and are committed before success is shown.
Each family submission includes one or more child names (ages 7–15), a contact
name, contact email address, an optional adult volunteer preference, optional
notes, and a server-generated timestamp. Child names are stored separately and
in the submitted order. A submission UUID prevents duplicate family and child
records when the browser retries after a lost response.

The form starts with one child and offers **Add another child** and removal of
extra entries (up to 20 children per submission). The API accepts `children`
as a list of names, `contact_name`, `contact_email`, `volunteer` (defaults to
false), and `notes`. A notification preference is no longer collected.
Official registration opens **November 19, 2026**, two weeks before the festival
begins on December 3. Expressing interest does not complete official registration.

## Run locally (SQLite)

Use Python 3.13. From PowerShell in this directory:

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe run.py
```

With the virtual environment activated, you can also use `python run.py`.
The server reloads automatically when Python source files change; press Ctrl+C
to stop it.

Open <http://127.0.0.1:8000>. Run the page through FastAPI rather than opening
`index.html` directly. SQLite is automatically initialized at
`data/choir-festival.db`; existing local databases are upgraded automatically,
and records survive server restarts. This directory is
ignored by Git and excluded from Vercel uploads.

Copy `.env.example` to `.env` for local admin credentials. Leave `DATABASE_URL`
unset for local SQLite. No local Postgres installation or server is needed.
Existing environment variables take precedence over `.env`.

## Deploy to Vercel with Neon Postgres

1. Create a Neon database and copy its **pooled** connection string, including
   its `sslmode=require` and other connection options.
2. Add `DATABASE_URL` in your Vercel project's environment variables for the
   environments you plan to deploy. Use a separate Neon branch/database for
   Preview deployments if you want to keep test entries out of production.
3. Initialize or upgrade each target database before serving traffic. Set
   `DATABASE_URL` in your local, ignored `.env` to that database's connection
   string and run:

   ```powershell
   .venv/Scripts/python.exe manage.py init-db
   ```

4. Import this repository into Vercel using the **FastAPI** framework preset.
   Leave the build command and output directory at their framework defaults.
   `app.py` exports the application, `.python-version` selects Python 3.13, and
   `vercel.json` includes the festival and admin pages in the function bundle.
5. Deploy, then check `/api/health` for `{"status":"ok"}` and submit a test entry.

`init-db` creates missing tables and applies the family-form upgrade: a child
names table and an adult volunteer column. **Run it again for an existing Neon
database before deploying this update.** The upgrade is repeatable and preserves
all existing submissions. Earlier helper entries retain their willingness to
volunteer; earlier child names are marked as not recorded rather than guessed
from the old name field. Historical notification preferences remain in storage
but are no longer collected or shown. Future schema changes will need migrations.

Vercel requires a Postgres `DATABASE_URL`; the app refuses to fall back to an
ephemeral SQLite file there. SQLAlchemy uses psycopg 3 with connection timeouts,
no application connection pool, and prepared statements disabled for the Neon
pooler. The page and API need no separate API host or CORS configuration.

Deployment references: [Vercel FastAPI documentation](https://vercel.com/docs/frameworks/backend/fastapi),
[Vercel Python runtime](https://vercel.com/docs/functions/runtimes/python), and
[Neon SQLAlchemy guide](https://neon.com/docs/guides/sqlalchemy).

## Retrieve the interest list

### Admin portal

Set `ADMIN_PASSWORD` to a strong, unique password in your local `.env` and in
your Vercel project's environment settings. `ADMIN_USERNAME` defaults to
`admin`; set it if you'd like a different username. Restart the local server
or redeploy after changing these settings.

Open `/admin` (for example, <http://127.0.0.1:8000/admin>) or use the **Admin
portal** link in the festival footer. Your browser will prompt for your
username and password. Use HTTPS on the deployed site. The portal and its
data API are disabled until a password is configured, and both require
authentication. Credentials are never embedded in the page or stored in
JavaScript. This uses [FastAPI's HTTP Basic authentication](https://fastapi.tiangolo.com/advanced/security/http-basic-auth/).
For a temporary session on a shared device, use a private browser window and
close it when finished; HTTP Basic sign-in is managed by the browser.

The large total at the top counts all interest submissions, regardless of the
current search or page. Additional counters show child names supplied and
volunteer offers. A family form counts as one submission and can contain
multiple children. The searchable list shows child names, contact names and
emails, adult volunteer preferences, notes, and submission times, newest first. Use
**Next** and **Previous** to see all entries, and **Refresh list** to load new
submissions. Existing entries from the previous age groups are also included.

### CSV export

From a trusted terminal using the appropriate local database or Neon
`DATABASE_URL`:

```powershell
.venv/Scripts/python.exe manage.py export-interests --output interests.csv
.venv/Scripts/python.exe manage.py export-interests --volunteers-only --output volunteer-interests.csv
```

The export refuses to overwrite an existing file and neutralizes spreadsheet
formula prefixes in submitted values. Keep exported files private. CSV files
with `interests` in their names are ignored by Git; all CSVs are excluded from
Vercel uploads. The listing API is admin-only; CSV export remains a local
administration command.

CSV exports contain contact details, child names, adult volunteer preferences,
notes, and submission dates. **Email delivery is not configured.** Browser-only
demo entries from the old page are not automatically uploaded.

## Verify

```powershell
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
.venv/Scripts/python.exe -m pytest -q
```

All local tests use temporary SQLite databases. They never start or connect
to a Postgres server. Test the Neon-backed app after deploying to Vercel using
the deployment smoke check described above.

The tests cover family and child persistence, validation, retry handling,
volunteer preferences, upgrading the original SQLite schema, database failures,
admin authentication, totals, pagination, child/contact search, private data
access, CSV export, and deployment configuration checks.
