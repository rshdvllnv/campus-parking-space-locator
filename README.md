# Campus Parking Space Locator (CPE 304L Final Exam, Group 12)

Flask web app for finding and holding campus parking slots. Uses SQLite locally
and PostgreSQL when `DATABASE_URL` is set (e.g. Neon on Vercel).

## Demo accounts

| Role  | Username | Password    | Notes                          |
|-------|----------|-------------|--------------------------------|
| Admin | `admin`  | `Admin@123` | Zones, users, reports, CSV     |
| Driver| `rasheed`| `Driver@123`| Vehicle `XYZ 9012`             |
| Driver| `racid`  | `Driver@123`| Vehicle `NBC 1234`             |
| Driver| `mark`   | `Driver@123`| Vehicle `ABC 5678`             |
| Driver| `villena`| `Driver@123`| Vehicle `FAC 2026`             |

Drivers may also create their own account from **Create account**; new accounts
always receive the `Driver` role. Passwords are stored as Werkzeug hashes.

## Features

- **User interface** – organised mobile-first layout that expands to a desktop
  dashboard, live availability refresh, skip link, focus styles, accessible
  labels and confirmation dialogs. All pages are responsive at 390 px and up.
- **Authentication** – sign in with username or email, sign out, driver
  registration, profile/email updates, password change and vehicle management.
- **Roles and permissions** – separate `Driver` and `Admin` areas. Drivers can
  only view and act on their own reservations and vehicles; admins manage zones,
  capacity, slot sensor events, accounts and reports.
- **Security** – session cookies are HttpOnly/SameSite (Secure on Vercel), every
  state-changing request is CSRF-protected, passwords are hashed, login failures
  are rate-limited, safe `next` redirects are enforced, security headers and a
  Content-Security-Policy are sent, and the app avoids username-enumeration
  timing hints.
- **System outputs** – driver reservation history and arrival receipts, an admin
  dashboard with live metrics, a reports page (status counts, completion rate,
  zone utilisation, busiest hours), and a downloadable CSV export.
- **Notifications** – header indicator and dashboard banner for the signed-in
  driver's active hold and remaining time.
- **Error handling** – validated forms, friendly 400/403/404/405/413/500 pages,
  race-safe reservation conflicts, and expired-hold cleanup.

## Run locally (SQLite)

    pip install -r requirements.txt
    python app.py          # http://localhost:5000

Useful pages: `/` (dashboard), `/login`, `/register`, `/admin`,
`/admin/users`, `/admin/reports`, `/reservations`, `/account`.

## Deploy on Vercel with a free Neon Postgres database

Vercel detects the Flask `app` in `app.py` automatically, so no `vercel.json`
rewrites and no `api/` folder are needed.

1. Create a free database at https://neon.tech and copy its connection string
   (looks like `postgresql://user:pass@ep-xxx.neon.tech/neondb?sslmode=require`).
2. Push this folder to a GitHub repository (do not commit `parking.db`).
3. On https://vercel.com: Add New > Project > import the repo. Leave the
   Framework Preset on the detected value (do **not** force "Other").
4. Before deploying open Environment Variables and add:
   - `DATABASE_URL` = your Neon connection string
   - `SECRET_KEY`   = any long random text
5. Deploy. Tables, demo drivers and demo accounts are created automatically on
   the first page load. On an existing database the new `app_user` table is
   created and seeded without touching other data.

If `DATABASE_URL` is missing on Vercel, the app falls back to a temporary
SQLite file in `/tmp`. It will still render, but data resets on every cold start,
so set `DATABASE_URL` for a working deployment.

To test Postgres locally: set `DATABASE_URL` in the terminal before
`python app.py` (PowerShell: `$env:DATABASE_URL="postgresql://..."`).

## Data model

`schema.sql` / `schema_pg.sql` contain `driver`, `vehicle`, `parking_zone`,
`parking_slot`, `reservation` and the extended `app_user` table used for login,
roles and ownership.

Reservation rules: a hold lasts 15 minutes and is never extended, one active
reservation per slot is enforced by a unique index, and occupied/reserved slots
cannot be selected.
