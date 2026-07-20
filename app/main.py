from __future__ import annotations

import html
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .analyzer import analyze_bytes
from .desktop_security import DESKTOP_PORT_ENV, DESKTOP_TOKEN_ENV, DesktopSessionMiddleware
from .settings import settings
from .source import extract_source
from .validators.kosit import KositValidator
from .xml_utils import InvoiceInputError

APP_DIR = Path(__file__).resolve().parent
EXAMPLES_DIR = APP_DIR / "examples"

app = FastAPI(
    title="E-Rechnungs-Viewer & Prüfer",
    version=__version__,
    description="Lokale Darstellung und Prüfung strukturierter E-Rechnungen in CII und UBL.",
    docs_url="/api/docs",
    redoc_url=None,
)
_desktop_port = os.getenv(DESKTOP_PORT_ENV)
app.add_middleware(
    DesktopSessionMiddleware,
    token=os.getenv(DESKTOP_TOKEN_ENV),
    port=int(_desktop_port) if _desktop_port else None,
)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")


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


def _safe_report_filename(value: Any) -> str:
    return _safe_download_filename(value, "Bericht", "html")


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
    return {
        "status": "ok",
        "version": __version__,
        "kosit": KositValidator(settings).configuration_state(),
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


@app.post("/api/analyze")
async def analyze(
    file: UploadFile = File(...),
    official: bool = Form(True),
):
    try:
        data = await _read_upload(file)
        result = analyze_bytes(
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


@app.post("/api/report", response_class=HTMLResponse)
async def report(
    request: Request,
    file: UploadFile = File(...),
    official: bool = Form(True),
):
    try:
        data = await _read_upload(file)
        result = analyze_bytes(
            data,
            file.filename or "rechnung.xml",
            file.content_type,
            run_official_validation=official,
        )
        return templates.TemplateResponse(
            request=request,
            name="report.html",
            context={
                "analysis": result,
                "generated_at": datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S %Z"),
                "version": __version__,
            },
            headers={
                "Content-Disposition": f"inline; filename=E-Rechnung-{_safe_report_filename(result['document'].get('id'))}"
            },
        )
    finally:
        await file.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
