"""
WePlay Unban Installer / Uploader — FastAPI backend.

Hides the loadly.io API key from the browser and proxies these endpoints:
  - POST /api/upload                    -> loadly.io /apiv2/app/upload
  - GET  /api/apps                      -> loadly.io /apiv2/app/listMy
  - GET  /api/apps/{buildKey}           -> loadly.io /apiv2/app/view
  - GET  /api/apps/{buildKey}/install   -> loadly.io /apiv2/app/install
                                          (tolerates non-JSON + redirects,
                                           falls back to buildShortcutUrl
                                           or https://loadly.io/{buildKey})
"""

import os
import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, APIRouter, HTTPException, Query, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

LOADLY_API_BASE = os.environ.get("LOADLY_API_BASE", "https://api.loadly.io")
LOADLY_API_KEY = os.environ.get("LOADLY_API_KEY")

if not LOADLY_API_KEY:
    logging.warning("LOADLY_API_KEY is not set — /api/* endpoints will return 500.")

app = FastAPI(title="WePlay Unban Installer API")

# CORS must be added BEFORE routers so it wraps every route
_cors_origins = os.environ.get("CORS_ORIGINS", "*")
if not _cors_origins or _cors_origins.strip() == "":
    _cors_origins = "*"

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins.split(",")],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=600,
)

api_router = APIRouter(prefix="/api")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("weplay")


async def _loadly_post(path: str, form: dict) -> dict:
    """POST form data to loadly.io and return parsed JSON."""
    if not LOADLY_API_KEY:
        raise HTTPException(status_code=500, detail="LOADLY_API_KEY not configured on server.")

    payload = {"_api_key": LOADLY_API_KEY, **form}
    url = f"{LOADLY_API_BASE}{path}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError as exc:
        logger.exception("loadly.io request failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}") from exc

    try:
        data = resp.json()
    except ValueError:
        logger.error("loadly.io returned non-JSON (status %s): %s", resp.status_code, resp.text[:300])
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON response.")

    return data


async def _lookup_shortcut_url(build_key: str) -> str | None:
    """Fallback: scan /apiv2/app/listMy to grab the signed buildShortcutUrl."""
    try:
        data = await _loadly_post("/apiv2/app/listMy", {"page": "1"})
    except HTTPException as exc:
        logger.warning("listMy fallback failed: %s", exc.detail)
        return None

    builds = []
    if isinstance(data, dict):
        d = data.get("data") or {}
        if isinstance(d, dict):
            builds = d.get("list") or []
        elif isinstance(d, list):
            builds = d

    for b in builds:
        if isinstance(b, dict) and b.get("buildKey") == build_key:
            return b.get("buildShortcutUrl") or b.get("shortcutUrl")
    return None


@app.get("/")
async def root_index():
    """Redirect root to /api/ so the browser never sees a 404."""
    return RedirectResponse(url="/api/")


@api_router.get("/")
async def root():
    return {"service": "weplay-unban-installer", "status": "ok"}


@api_router.post("/upload")
async def upload_app(
    file: UploadFile = File(..., description="The .ipa or .apk file to upload"),
    buildInstallType: int = Form(1, description="1 public, 2 password, 3 invitation"),
    buildPassword: str = Form("", description="Password when buildInstallType=2"),
):
    """
    Upload an .ipa/.apk to loadly.io.

    Streams the multipart file straight through to loadly.io's
    /apiv2/app/upload endpoint (the API key never reaches the browser).
    """
    if not LOADLY_API_KEY:
        raise HTTPException(status_code=500, detail="LOADLY_API_KEY not configured on server.")

    name = (file.filename or "").lower()
    if not (name.endswith(".ipa") or name.endswith(".apk")):
        raise HTTPException(status_code=400, detail="Only .ipa and .apk files are allowed.")

    contents = await file.read()

    data_fields = {
        "_api_key": LOADLY_API_KEY,
        "buildInstallType": str(buildInstallType),
    }
    if buildPassword:
        data_fields["buildPassword"] = buildPassword

    files = {"file": (file.filename, contents, file.content_type or "application/octet-stream")}
    url = f"{LOADLY_API_BASE}/apiv2/app/upload"

    try:
        # Uploads can be large — give loadly plenty of time.
        async with httpx.AsyncClient(timeout=None) as client:
            resp = await client.post(url, data=data_fields, files=files)
    except httpx.HTTPError as exc:
        logger.exception("loadly.io upload failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Upstream upload failed: {exc}") from exc

    try:
        result = resp.json()
    except ValueError:
        logger.error("loadly.io upload returned non-JSON: %s", resp.text[:300])
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON response.")

    if isinstance(result, dict) and result.get("code") not in (0, None):
        raise HTTPException(
            status_code=400,
            detail=result.get("message") or result.get("msg") or "Upload failed.",
        )

    return result


@api_router.get("/apps")
async def list_apps(page: int = Query(1, ge=1)):
    """List apps on the loadly account."""
    return await _loadly_post("/apiv2/app/listMy", {"page": str(page)})


@api_router.get("/apps/{build_key}")
async def get_app_details(build_key: str, appKey: str = Query(..., description="loadly appKey")):
    """Fetch full app details (screenshots, description, etc.)."""
    return await _loadly_post(
        "/apiv2/app/view",
        {"appKey": appKey, "buildKey": build_key},
    )


@api_router.get("/apps/{build_key}/install")
async def get_install_url(build_key: str):
    """
    Ask loadly.io for the install URL for the given buildKey.

    BUG #2 FIX: loadly.io occasionally returns a 302 redirect (straight to
    the .plist) or the plist XML itself instead of a JSON envelope. The
    previous implementation raised 502 on both. We now:

      • follow_redirects=False, so we can inspect a 3xx Location and use it
      • only try resp.json() when the response actually looks like JSON
      • honour `buildShortcutUrl` from either the /install payload OR the
        /listMy fallback lookup
      • as a final safety net, return https://loadly.io/{buildKey}
        (loadly's public install-page URL — it always resolves)

    Response shape:
      { "installUrl": "<https:// URL>", "raw": {...}|null, "source": "..." }
    """
    if not LOADLY_API_KEY:
        raise HTTPException(status_code=500, detail="LOADLY_API_KEY not configured on server.")

    payload = {"_api_key": LOADLY_API_KEY, "buildKey": build_key}
    url = f"{LOADLY_API_BASE}/apiv2/app/install"

    install_url: str | None = None
    raw: dict | None = None
    source = "unknown"

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            resp = await client.post(
                url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        # 1) Redirect straight to the resource — use the Location header.
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            if location:
                install_url = location
                source = f"loadly-redirect-{resp.status_code}"

        # 2) Try to parse the body as JSON only when it plausibly is JSON.
        if not install_url:
            content_type = (resp.headers.get("content-type") or "").lower()
            body = resp.text or ""
            looks_json = "application/json" in content_type or body.lstrip().startswith(("{", "["))
            if looks_json:
                try:
                    data = resp.json()
                except ValueError:
                    data = None

                if isinstance(data, dict):
                    raw = data
                    inner = data.get("data")
                    if isinstance(inner, dict):
                        install_url = (
                            inner.get("installUrl")
                            or inner.get("install_url")
                            or inner.get("url")
                            or inner.get("downloadUrl")
                            or inner.get("buildShortcutUrl")
                            or inner.get("shortcutUrl")
                        )
                    elif isinstance(inner, str):
                        install_url = inner
                    # Some loadly deployments return the URL at the top level.
                    if not install_url:
                        install_url = data.get("installUrl") or data.get("url")
                    if install_url:
                        source = "loadly-json"
            else:
                # Non-JSON (likely the plist XML itself). Log a snippet only.
                logger.info(
                    "loadly install returned non-JSON (%s): %s",
                    content_type or "no-content-type",
                    body[:200],
                )
    except httpx.HTTPError as exc:
        logger.warning("loadly install request failed, will try fallbacks: %s", exc)

    # 3) Fallback — look up the signed buildShortcutUrl from the list endpoint.
    if not install_url:
        shortcut = await _lookup_shortcut_url(build_key)
        if shortcut:
            install_url = shortcut
            source = "listMy-buildShortcutUrl"

    # 4) Final safety net — loadly's public install page always resolves.
    if not install_url:
        install_url = f"https://loadly.io/{build_key}"
        source = "loadly-public-fallback"

    return {"installUrl": install_url, "raw": raw, "source": source}


app.include_router(api_router)
