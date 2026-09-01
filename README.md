# AI Stock & Option Probability Analyzer — Backend (V1, Mock Data Mode)

Python + FastAPI backend. Currently runs entirely on **mock data** —
no broker API needed yet, per the spec (Phase 1-6).

## What's included
- `app/main.py` — all API routes (auth, dashboard, option chain, signal logging)
- `app/models.py` — database tables: `User` (login) and `SignalLog` (trade history for backtesting)
- `app/auth.py` — email/password login with JWT tokens
- `app/mock_data.py` — fake stock & option data generator (swap for DhanHQ later)
- `app/probability.py` — transparent, explainable probability scoring
- SQLite database (`stock_analyzer.db`) — created automatically, no setup needed

## Deploying from your phone (Supabase + GitHub + Render)

**0. Create a Supabase project (permanent database)**
- Go to supabase.com → sign up → **New project**
- Pick any name and password (save the password somewhere safe)
- Wait ~2 minutes for it to provision
- Go to **Project Settings → Database → Connection string** → copy the **URI** (starts with `postgresql://`)
- You'll paste this as `DATABASE_URL` in Render (step 3 below) — this replaces the temporary SQLite file so your users and signal history are never lost on redeploy

**1. Create a new GitHub repository**
- Open github.com in your phone browser → tap **+** → **New repository**
- Name it e.g. `stock-analyzer-backend` → Create

**2. Upload these files**
- In the new repo, tap **Add file → Upload files**
- Upload every file from this project (keep the `app/` folder structure)
- Commit

**3. Deploy on Render (free tier)**
- Go to render.com → sign in with GitHub → **New → Web Service**
- Pick your `stock-analyzer-backend` repo
- Settings:
  - **Build Command:** `pip install -r requirements.txt`
  - **Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Under **Environment**, add two variables:
  - `SECRET_KEY` = any long random string (don't reuse the default in the code)
  - `DATABASE_URL` = the Supabase connection string from step 0
- Create Web Service — Render will give you a live URL like
  `https://stock-analyzer-backend.onrender.com`

**4. Test it**
- Visit `https://<your-url>/health` in the browser — should show `{"status":"ok",...}`
- Visit `https://<your-url>/docs` — FastAPI's built-in interactive API tester,
  works right in the browser, no extra app needed. You can register a user,
  log in, and try every endpoint from your phone here.

## Notes
- Free Render web services sleep after inactivity and take ~30s to wake up on the first request — fine for development, upgrade later if needed for a live product.
- With `DATABASE_URL` set to Supabase, your users and signal history persist permanently across redeploys — no more SQLite resets.
- If you ever run the backend locally on a computer without setting `DATABASE_URL`, it falls back to a local SQLite file automatically (see `app/database.py`) — useful for quick testing, but the live Render deployment should always have `DATABASE_URL` set to Supabase.
- Every trading output includes the required disclaimer per spec section 23.
