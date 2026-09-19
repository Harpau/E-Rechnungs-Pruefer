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
from .http_upload import (
    HeaderValidationMiddleware,
    SecurityHeadersMiddleware,
    UploadProcessingMiddleware,
    observation_id_from_scope,
)
from .processing.budgets import unavailable
from .processing.manager import manager
from .processing.observation import OBSERVATION_HEADER, OBSERVATION_PATH
from .report_templates import report_environment
from .settings import settings
from .ui_contract import UI_REVISION, UI_STATIC_PREFIX
from .upload_ingress import UploadError, UploadLimits, upload_openapi
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


def _observation_parameter(*, required: bool) -> dict[str, Any]:
    return {
        "name": OBSERVATION_HEADER.decode("ascii"),
        "in": "header",
        "required": required,
        "description": "Begrenzte RAM-Beobachtung; ausschließlich mit gültigem API-Bearer-Token verfügbar.",
        "schema": {"type": "string", "pattern": "^[0-9a-f]{32}$", "minLength": 32, "maxLength": 32},
    }


def _upload_documentation(path: str) -> dict[str, Any]:
    metadata = upload_openapi(path, UploadLimits(max_upload_bytes=settings.max_upload_bytes))
    # Match FastAPI's generated string keys so its response headers are merged,
    # rather than shadowed by a second numeric key during JSON serialization.
    metadata["responses"] = {str(status): value for status, value in metadata["responses"].items()}
    # These failures are produced by the isolated process controller, not FastAPI.
    metadata["responses"].update(
        {
            "403": {"description": "Für die angeforderte Beobachtung fehlt ein gültiges API-Bearer-Token."},
            "409": {"description": "Die Beobachtungskennung wird bereits verwendet."},
            "500": {"description": "Workerabbruch oder ungültiges Verarbeitungsprotokoll."},
            "504": {"description": "Die zulässige Verarbeitungsfrist wurde überschritten."},
        }
    )
    metadata["parameters"] = [_observation_parameter(required=False)]
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


@app.get(
    OBSERVATION_PATH,
    summary="Begrenzte, ausdrücklich angeforderte Verarbeitungsbeobachtung",
    description="Erfordert ein gültiges API-Bearer-Token und genau eine Beobachtungskennung; keine Abfrageparameter.",
    openapi_extra={"parameters": [_observation_parameter(required=True)]},
    responses={
        400: {"description": "Ungültige Abfrage."},
        403: {"description": "API-Bearer-Token erforderlich."},
        404: {"description": "Kein aufbewahrter Nachweis."},
        503: {"description": "Beobachtung nicht verfügbar."},
    },
)
async def processing_observation(request: Request) -> Response:
    try:
        identifier = observation_id_from_scope(request.scope, required=True)
        if (
            request.scope.get("query_string")
            or "transfer-encoding" in request.headers
            or request.headers.get("content-length", "0") != "0"
        ):
            raise UploadError(
                400, "observation_request_error", "Die Beobachtungsabfrage erlaubt keine Zusatzparameter."
            )
    except UploadError as error:
        return JSONResponse(
            {"type": error.error_type, "detail": error.detail},
            status_code=error.status,
            headers={"Cache-Control": "no-store"},
        )
    assert identifier is not None
    try:
        record = manager.observations.snapshot(identifier)
        if record is None:
            return JSONResponse(
                {"type": "observation_not_found", "detail": "Für diese Kennung ist kein Nachweis verfügbar."},
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        response = JSONResponse(record, headers={"Cache-Control": "no-store"})
        if len(response.body) <= 16 * 1024:
            return response
    except Exception:
        # Never expose diagnostics, exception text or an unbounded fallback body.
        pass
    return JSONResponse(
        {"type": "observation_unavailable", "detail": "Die Verarbeitungsbeobachtung ist derzeit nicht verfügbar."},
        status_code=503,
        headers={"Cache-Control": "no-store"},
    )


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
