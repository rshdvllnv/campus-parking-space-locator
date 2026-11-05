# Campus Parking Space Locator (CPE 304L Final Exam, Group 12)

Flask web app. Uses SQLite locally and PostgreSQL when `DATABASE_URL` is set.

## Run locally (SQLite)
    pip install -r requirements.txt
    python app.py          # then open http://localhost:5000  and  /admin

## Deploy on Vercel with a free Neon Postgres database
1. Create a free database at https://neon.tech and copy its connection string
   (looks like `postgresql://user:pass@ep-xxx.neon.tech/neondb?sslmode=require`).
2. Push this folder to a GitHub repository (do not commit parking.db).
3. On https://vercel.com: Add New > Project > import the repo (Framework Preset: Other).
4. Before deploying open Environment Variables and add:
   - `DATABASE_URL` = your Neon connection string
   - `SECRET_KEY`   = any long random text
5. Deploy. Tables and demo data are created automatically on the first page load.

To test Postgres locally: set `DATABASE_URL` in the terminal before `python app.py`
(PowerShell: `$env:DATABASE_URL="postgresql://..."`).
