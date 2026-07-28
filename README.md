# WePlay Unban Installer — Backend

A tiny FastAPI service that hides the loadly.io API key from the browser and
proxies three endpoints.

## Endpoints

| Method | Path                              | Proxies to loadly.io               |
| ------ | --------------------------------- | ---------------------------------- |
| GET    | `/api/apps?page=1`                | `POST /apiv2/app/listMy`           |
| GET    | `/api/apps/{buildKey}?appKey=...` | `POST /apiv2/app/view`             |
| GET    | `/api/apps/{buildKey}/install`    | `POST /apiv2/app/install`          |

All responses are JSON. `/install` returns `{ "installUrl": "...", "raw": {...} }`.

## Run locally

```bash
cd backend
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env            # then edit .env and paste your LOADLY_API_KEY
uvicorn main:app --reload --port 8000
```

Test it:

```bash
curl http://localhost:8000/api/
curl "http://localhost:8000/api/apps?page=1"
```

## Environment variables

| Name              | Required | Default                  | Notes                                     |
| ----------------- | :------: | ------------------------ | ----------------------------------------- |
| `LOADLY_API_KEY`  | yes      | —                        | Your loadly.io `_api_key`                 |
| `LOADLY_API_BASE` | no       | `https://api.loadly.io`  | Override for testing / self-hosted proxy  |
| `CORS_ORIGINS`    | no       | `*`                      | Comma-separated allowed origins           |

## Deploy

### Render

1. Push this repo to GitHub (see the root `README.md`).
2. In Render → **New Web Service** → connect the repo.
3. **Root directory:** `backend`
4. **Build command:** `pip install -r requirements.txt`
5. **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. **Environment:** add `LOADLY_API_KEY` (and optionally `CORS_ORIGINS`).

### Railway

1. New project → **Deploy from GitHub** → point at this repo.
2. **Root directory:** `backend`
3. Add env vars `LOADLY_API_KEY` (required) and `CORS_ORIGINS` (optional).
4. Railway auto-detects Python; if not, set the start command to
   `uvicorn main:app --host 0.0.0.0 --port $PORT`.

### Fly.io / any other host

Same idea: install `requirements.txt`, expose the port, run
`uvicorn main:app --host 0.0.0.0 --port <port>`, and set the env vars.

## After deploy

Copy the public backend URL (e.g. `https://weplay-installer-api.onrender.com`) into
`frontend/config.js`:

```js
window.WEPLAY_CONFIG = {
  BACKEND_URL: "https://weplay-installer-api.onrender.com"
};
```

Then re-upload `frontend/` to your static host.
