from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .api_models import AnalysisResponse
from .component_versions import ANALYSIS_SCHEMA_VERSION, KOSIT_COMPONENT_VERSIONS
from .desktop_security import (
    DESKTOP_PORT_ENV,
    DESKTOP_TOKEN_ENV,
    SERVICE_MODE_ENV,
    DesktopSessionMiddleware,
    consume_api_token_environment,
    get_service_browser_sessions,
)
from .http_upload import HeaderValidationMiddleware, SecurityHeadersMiddleware, UploadProcessingMiddleware
from .processing.budgets import unavailable
from .processing.manager import manager
from .report_templates import report_environment
from .settings import settings
from .ui_contract import UI_REVISION, UI_STATIC_PREFIX
from .upload_ingress import UploadLimits, upload_openapi
from .validators.kosit import KositValidator

APP_DIR = Path(__file__).resolve().parent
EXAMPLES_DIR = APP_DIR / "examples"
REPORT_RESPONSE_HEADERS = {
    "X-Einvoice-Analysis-Schema": {
        "description": "Version des maschinenlesbaren Analysevertrags.",
        "schema": {"type": "integer", "enum": [ANALYSIS_SCHEMA_VERSION]},
    },
    "X-Einvoice-Syntax": {
        "description": "Erkannte Rechnungssyntax.",
        "schema": {"type": "string", "enum": ["CII", "UBL", "UNKNOWN"]},
    },
    "X-Einvoice-Conformity-Status": {
        "description": "Status der angeforderten offiziellen Konformitätsprüfung.",
        "schema": {
            "type": "string",
            "enum": [
                "accepted",
                "rejected",
                "not-requested",
                "unsupported",
                "unavailable",
                "indeterminate",
            ],
        },
    },
    "X-Einvoice-Internal-Status": {
        "description": "Status der internen Vorprüfungen und Plausibilitätskontrollen.",
        "schema": {
            "type": "string",
            "enum": ["clear", "attention", "errors", "not-run"],
        },
    },
    "X-Einvoice-Processing-Status": {
        "description": "Vollständigkeit der technischen Verarbeitung.",
        "schema": {
            "type": "string",
            "enum": ["complete", "limited", "incomplete"],
        },
    },
    "X-Einvoice-Report-Scope": {
        "description": "Umfang des menschenlesbaren Berichts und seiner technischen Anhänge.",
        "schema": {"type": "string", "enum": ["readable", "complete"]},
    },
}
ANALYSIS_BUSY_RESPONSE: dict[int | str, dict[str, Any]] = {
    503: {
        "description": "Die begrenzte Analysekapazität ist vorübergehend ausgelastet.",
        "headers": {
            "Retry-After": {
                "description": "Empfohlene Wartezeit bis zum nächsten Versuch in Sekunden.",
                "schema": {"type": "integer", "minimum": 5, "maximum": 600},
            }
        },
    }
}


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    manager.startup()
    try:
        yield
    finally:
        manager.shutdown()


app = FastAPI(
    title="E-Rechnungs-Viewer & Prüfer",
    version=__version__,
    description="Lokale Darstellung und Prüfung strukturierter E-Rechnungen in CII und UBL.",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)
# Starlette inserts each new middleware at the outside. Header normalization
# therefore runs before auth, while job admission remains strictly behind auth.
app.add_middleware(UploadProcessingMiddleware, get_manager=lambda: manager, get_settings=lambda: settings)
_desktop_port = os.getenv(DESKTOP_PORT_ENV)
_configured_api_token = consume_api_token_environment(os.environ)
app.add_middleware(
    DesktopSessionMiddleware,
    token=os.getenv(DESKTOP_TOKEN_ENV),
    port=int(_desktop_port) if _desktop_port else None,
    api_token=_configured_api_token,
    browser_sessions=get_service_browser_sessions() if os.getenv(SERVICE_MODE_ENV) == "1" else None,
    ui_revision=UI_REVISION,
)
app.add_middleware(HeaderValidationMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.mount(UI_STATIC_PREFIX, StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters.update(report_environment().filters)
_ANALYSIS_RETRY_AFTER_SECONDS = min(max(settings.kosit_timeout_seconds + 5, 5), 600)


def _upload_documentation(path: str) -> dict[str, Any]:
    metadata = upload_openapi(path, UploadLimits(max_upload_bytes=settings.max_upload_bytes))
    # Match FastAPI's generated string keys so its response headers are merged,
    # rather than shadowed by a second numeric key during JSON serialization.
    metadata["responses"] = {str(status): value for status, value in metadata["responses"].items()}
    # These failures are produced by the isolated process controller, not FastAPI.
    metadata["responses"].update(
        {
            "500": {"description": "Workerabbruch oder ungültiges Verarbeitungsprotokoll."},
            "504": {"description": "Die zulässige Verarbeitungsfrist wurde überschritten."},
        }
    )
    return metadata


def _unavailable_upload() -> JSONResponse:
    # No parser/rendering fallback if an upload ever bypasses the ASGI controller.
    error = unavailable()
    return JSONResponse({"detail": error.detail, "type": error.error_type}, status_code=error.status)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    kosit_state = KositValidator(settings).configuration_state()
    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "version": __version__,
            "ui_revision": UI_REVISION,
            "static_url_prefix": UI_STATIC_PREFIX,
            "max_upload_mb": settings.max_upload_bytes // (1024 * 1024),
            "kosit_configured": kosit_state["configured"],
            "kosit_problem": " ".join(kosit_state.get("problems") or []),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/health")
async def health() -> dict[str, Any]:
    kosit_state = KositValidator(settings).configuration_state()
    return {
        "status": "ok",
        "version": __version__,
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        "kosit": {
            "configured": bool(kosit_state["configured"]),
            "components": KOSIT_COMPONENT_VERSIONS,
        },
    }


@app.get("/api/examples/{example_name}")
async def example(example_name: str):
    mapping = {
        "cii": EXAMPLES_DIR / "cii-rechnung-demo.xml",
        "ubl": EXAMPLES_DIR / "ubl-rechnung-demo.xml",
    }
    path = mapping.get(example_name)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Beispiel nicht gefunden.")
    return FileResponse(path, media_type="application/xml", filename=path.name)


@app.post(
    "/api/analyze",
    response_model=AnalysisResponse,
    responses=ANALYSIS_BUSY_RESPONSE,
    openapi_extra=_upload_documentation("/api/analyze"),
)
async def analyze() -> Response:
    return _unavailable_upload()


@app.post(
    "/api/xml",
    responses=ANALYSIS_BUSY_RESPONSE,
    openapi_extra=_upload_documentation("/api/xml"),
)
async def export_xml() -> Response:
    return _unavailable_upload()


@app.post(
    "/api/report",
    response_class=HTMLResponse,
    responses={
        200: {
            "description": "Eigenständiger HTML-Bericht mit maschinenlesbarer Statuszusammenfassung.",
            "headers": REPORT_RESPONSE_HEADERS,
        },
        **ANALYSIS_BUSY_RESPONSE,
    },
    openapi_extra=_upload_documentation("/api/report"),
)
async def report() -> Response:
    return _unavailable_upload()


@app.post(
    "/api/report/pdf",
    response_class=Response,
    responses={
        200: {
            "description": "Eigenständiger PDF-Bericht mit maschinenlesbarer Statuszusammenfassung.",
            "content": {"application/pdf": {}},
            "headers": REPORT_RESPONSE_HEADERS,
        },
        **ANALYSIS_BUSY_RESPONSE,
    },
    openapi_extra=_upload_documentation("/api/report/pdf"),
)
async def pdf_report() -> Response:
    return _unavailable_upload()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
