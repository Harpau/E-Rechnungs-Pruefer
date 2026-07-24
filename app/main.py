from __future__ import annotations

import html
import os
import re
import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import __version__
from .analyzer import analyze_bytes
from .desktop_security import (
    DESKTOP_PORT_ENV,
    DESKTOP_TOKEN_ENV,
    SERVICE_MODE_ENV,
    DesktopSessionMiddleware,
    consume_api_token_environment,
    get_service_browser_sessions,
)
from .pdf_report import render_pdf_report
from .settings import settings
from .source import extract_source
from .validators.kosit import KositValidator
from .xml_utils import InvoiceInputError

APP_DIR = Path(__file__).resolve().parent
EXAMPLES_DIR = APP_DIR / "examples"
REPORT_RESPONSE_HEADERS = {
    "X-Einvoice-Syntax": {
        "description": "Erkannte Rechnungssyntax.",
        "schema": {"type": "string", "enum": ["CII", "UBL", "UNKNOWN"]},
    },
    "X-Einvoice-Validation-Status": {
        "description": "Gemeinsamer Status der internen und gegebenenfalls offiziellen Prüfung.",
        "schema": {"type": "string", "enum": ["ok", "warning", "invalid"]},
    },
    "X-Einvoice-Official-Status": {
        "description": "Differenzierter Ausführungs- und Entscheidungsstatus der KoSIT-Prüfung.",
        "schema": {
            "type": "string",
            "enum": ["accepted", "rejected", "not-requested", "unavailable", "indeterminate"],
        },
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

app = FastAPI(
    title="E-Rechnungs-Viewer & Prüfer",
    version=__version__,
    description="Lokale Darstellung und Prüfung strukturierter E-Rechnungen in CII und UBL.",
    docs_url="/api/docs",
    redoc_url=None,
)
_desktop_port = os.getenv(DESKTOP_PORT_ENV)
_configured_api_token = consume_api_token_environment(os.environ)
app.add_middleware(
    DesktopSessionMiddleware,
    token=os.getenv(DESKTOP_TOKEN_ENV),
    port=int(_desktop_port) if _desktop_port else None,
    api_token=_configured_api_token,
    browser_sessions=get_service_browser_sessions() if os.getenv(SERVICE_MODE_ENV) == "1" else None,
)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")
_analysis_slots = threading.BoundedSemaphore(2)
_pdf_render_slots = threading.BoundedSemaphore(2)
_ANALYSIS_RETRY_AFTER_SECONDS = min(max(settings.kosit_timeout_seconds + 5, 5), 600)


class _AnalysisCapacityError(RuntimeError):
    pass


def _analyze_bytes_limited(
    data: bytes,
    filename: str,
    media_type: str | None,
    *,
    run_official_validation: bool,
) -> dict[str, Any]:
    if not _analysis_slots.acquire(blocking=False):
        raise _AnalysisCapacityError
    try:
        return analyze_bytes(
            data,
            filename,
            media_type,
            run_official_validation=run_official_validation,
        )
    finally:
        _analysis_slots.release()


def _render_pdf_report_limited(
    result: dict[str, Any],
    *,
    generated_at: str,
    version: str,
) -> bytes:
    with _pdf_render_slots:
        return render_pdf_report(result, generated_at=generated_at, version=version)


def _format_number(value: Any, digits: int | None = None) -> str:
    if value is None or value == "":
        return "–"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    if digits is not None:
        number = number.quantize(Decimal(1).scaleb(-digits))
        raw = f"{number:.{digits}f}"
    else:
        raw = format(number, "f")
        if "." in raw:
            raw = raw.rstrip("0").rstrip(".")
    integer, dot, fraction = raw.partition(".")
    sign = ""
    if integer.startswith("-"):
        sign, integer = "-", integer[1:]
    grouped = ".".join(
        [integer[max(0, len(integer) - offset - 3) : len(integer) - offset] for offset in range(0, len(integer), 3)][
            ::-1
        ]
    )
    return f"{sign}{grouped}{',' + fraction if dot else ''}"


def _format_money(value: Any, currency: str | None = None) -> str:
    formatted = _format_number(value, 2)
    return f"{formatted} {currency}".strip() if formatted != "–" else formatted


def _format_date(value: Any) -> str:
    if not value:
        return "–"
    text = str(value)
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return text


def _format_bytes(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "–"
    units = ["B", "KB", "MB", "GB"]
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.0f} {units[index]}" if index == 0 else f"{size:.1f} {units[index]}"


def _party_has_data(party: Any) -> bool:
    if not isinstance(party, dict):
        return False
    for key, value in party.items():
        if key in {"address", "contact"} and isinstance(value, dict):
            if any(item not in (None, "", [], {}) for item in value.values()):
                return True
        elif value not in (None, "", [], {}):
            return True
    return False


templates.env.filters["de_number"] = _format_number
templates.env.filters["money"] = _format_money
templates.env.filters["de_date"] = _format_date
templates.env.filters["bytes"] = _format_bytes
templates.env.filters["party_has_data"] = _party_has_data


def _safe_download_filename(value: Any, fallback: str, extension: str) -> str:
    raw = Path(str(value or fallback)).name
    stem = Path(raw).stem if Path(raw).suffix else raw
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")
    return f"{(text[:120] or fallback)}.{extension.lstrip('.')}"


def _official_report_status(result: dict[str, Any], *, requested: bool) -> str:
    if not requested:
        return "not-requested"

    official = result["validation"]["official"]
    if official.get("executed"):
        if official.get("accepted") is True:
            return "accepted"
        if official.get("accepted") is False:
            return "rejected"
        return "indeterminate"
    if not official.get("configured"):
        return "unavailable"
    return "indeterminate"


def _report_status_headers(result: dict[str, Any], *, official_requested: bool) -> dict[str, str]:
    return {
        "X-Einvoice-Syntax": str(result["document"]["syntax"]),
        "X-Einvoice-Validation-Status": str(result["validation"]["status"]),
        "X-Einvoice-Official-Status": _official_report_status(result, requested=official_requested),
    }


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
    )
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


async def _read_upload(upload: UploadFile) -> bytes:
    data = bytearray()
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > settings.max_upload_bytes:
            limit_mb = settings.max_upload_bytes / (1024 * 1024)
            raise InvoiceInputError(f"Die Datei ist größer als die zulässigen {limit_mb:g} MB.")
    return bytes(data)


@app.exception_handler(InvoiceInputError)
async def input_error_handler(request: Request, exc: InvoiceInputError):
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=422, content={"detail": str(exc), "type": "invoice_input_error"})
    return HTMLResponse(
        status_code=422, content=f"<h1>Datei konnte nicht verarbeitet werden</h1><p>{html.escape(str(exc))}</p>"
    )


@app.exception_handler(_AnalysisCapacityError)
async def analysis_capacity_handler(request: Request, exc: _AnalysisCapacityError):
    del request, exc
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Der Prüfdienst ist ausgelastet. Bitte versuchen Sie es später erneut.",
            "type": "analysis_capacity_error",
        },
        headers={"Retry-After": str(_ANALYSIS_RETRY_AFTER_SECONDS)},
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    kosit_state = KositValidator(settings).configuration_state()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "version": __version__,
            "max_upload_mb": settings.max_upload_bytes // (1024 * 1024),
            "kosit_configured": kosit_state["configured"],
            "kosit_problem": " ".join(kosit_state.get("problems") or []),
        },
    )


@app.get("/api/health")
async def health() -> dict[str, Any]:
    kosit_state = KositValidator(settings).configuration_state()
    return {
        "status": "ok",
        "version": __version__,
        "kosit": {"configured": bool(kosit_state["configured"])},
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


@app.post("/api/analyze", responses=ANALYSIS_BUSY_RESPONSE)
async def analyze(
    file: UploadFile = File(...),
    official: bool = Form(True),
):
    try:
        data = await _read_upload(file)
        result = await run_in_threadpool(
            _analyze_bytes_limited,
            data,
            file.filename or "rechnung.xml",
            file.content_type,
            run_official_validation=official,
        )
        return JSONResponse(result)
    finally:
        await file.close()


@app.post("/api/xml")
async def export_xml(file: UploadFile = File(...)):
    try:
        data = await _read_upload(file)
        source = extract_source(
            data,
            file.filename or "rechnung.xml",
            file.content_type,
            max_embedded_bytes=settings.max_upload_bytes,
        )
        filename = _safe_download_filename(source.xml_filename, "rechnung", "xml")
        return Response(
            content=source.xml_bytes,
            media_type="application/xml",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    finally:
        await file.close()


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
)
async def report(
    request: Request,
    file: UploadFile = File(...),
    official: bool = Form(True),
):
    try:
        data = await _read_upload(file)
        result = await run_in_threadpool(
            _analyze_bytes_limited,
            data,
            file.filename or "rechnung.xml",
            file.content_type,
            run_official_validation=official,
        )
        response_headers = _report_status_headers(result, official_requested=official)
        response_headers["Content-Disposition"] = 'inline; filename="E-Rechnungs-Pruefbericht.html"'
        return templates.TemplateResponse(
            request=request,
            name="report.html",
            context={
                "analysis": result,
                "generated_at": datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S %Z"),
                "version": __version__,
            },
            headers=response_headers,
        )
    finally:
        await file.close()


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
)
async def pdf_report(
    file: UploadFile = File(...),
    official: bool = Form(True),
):
    try:
        data = await _read_upload(file)
        result = await run_in_threadpool(
            _analyze_bytes_limited,
            data,
            file.filename or "rechnung.xml",
            file.content_type,
            run_official_validation=official,
        )
        generated_at = datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S %Z")
        response_headers = _report_status_headers(result, official_requested=official)
        response_headers["Content-Disposition"] = 'attachment; filename="E-Rechnungs-Pruefbericht.pdf"'
        payload = await run_in_threadpool(
            _render_pdf_report_limited,
            result,
            generated_at=generated_at,
            version=__version__,
        )
        return Response(
            content=payload,
            media_type="application/pdf",
            headers=response_headers,
        )
    finally:
        await file.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
