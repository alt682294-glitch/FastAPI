"""
WePlay Unban Installer / Uploader — FastAPI backend.

Hides the loadly.io API key from the browser and proxies these endpoints:
  - POST /api/upload                    -> loadly.io /apiv2/app/upload   (NEW)
  - GET  /api/apps                      -> loadly.io /apiv2/app/listMy
  - GET  /api/apps/{buildKey}           -> loadly.io /apiv2/app/view
  - GET  /api/apps/{buildKey}/install   -> loadly.io /apiv2/app/install
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
    Ask loadly.io for the install URL for the given buildKey and return it to the
    browser so the frontend can redirect (or open) it.

    Response shape:
      { "installUrl": "<itms-services://... or https://... URL>", "raw": {...} }
    """
    data = await _loadly_post("/apiv2/app/install", {"buildKey": build_key})

    if isinstance(data, dict) and data.get("code") not in (0, None):
        raise HTTPException(
            status_code=404,
            detail=data.get("message") or data.get("msg") or "Install lookup failed.",
        )

    install_url = None
    if isinstance(data, dict):
        payload = data.get("data") or {}
        if isinstance(payload, dict):
            install_url = (
                payload.get("installUrl")
                or payload.get("install_url")
                or payload.get("url")
                or payload.get("downloadUrl")
            )
        if not install_url and isinstance(payload, str):
            install_url = payload

    if not install_url:
        raise HTTPException(status_code=502, detail="Upstream did not return an install URL.")

    return {"installUrl": install_url, "raw": data}


app.include_router(api_router)
