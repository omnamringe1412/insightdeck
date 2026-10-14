"""InsightDeck parser service: reads Power BI (.pbix/.pbit), CSV and Excel files
and returns their tables as JSON for the InsightDeck web page."""
import json
import os
import tempfile

import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pbixray import PBIXRay

MAX_ROWS = 200_000          # rows returned per table
MAX_BYTES = 300 * 1024 * 1024  # 300 MB upload limit

# Hide interactive API docs so the endpoint is not advertised publicly.
app = FastAPI(title="InsightDeck Parser", docs_url=None, redoc_url=None, openapi_url=None)

# Only the InsightDeck site may call this service from a browser.
ALLOWED_ORIGINS = [
    "https://omnamringe1412.github.io",
    "http://localhost:8000",  # local testing only
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def df_to_records(df: pd.DataFrame):
    df = df.head(MAX_ROWS).copy()
    df.columns = [str(c) for c in df.columns]
    # ISO dates, NaN -> null, numpy types -> plain JSON values
    return json.loads(df.to_json(orient="records", date_format="iso"))


def parse_pbix(path: str, name: str):
    model = PBIXRay(path)
    try:
        tables = {}
        for t in model.tables:
            if t.startswith(("LocalDateTable", "DateTableTemplate")):
                continue  # Power BI's hidden auto-date tables
            try:
                tables[t] = df_to_records(model.get_table(t))
            except Exception:
                continue
        measures = []
        try:
            dm = model.dax_measures
            if hasattr(dm, "to_json"):
                measures = json.loads(dm.to_json(orient="records"))
        except Exception:
            pass
        return {"file": name, "tables": tables, "measures": measures}
    finally:
        model.close()


@app.get("/")
def health():
    return {"status": "ok", "service": "InsightDeck parser"}


@app.post("/api/parse")
async def parse(file: UploadFile = File(...)):
    name = file.filename or "upload"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File is larger than 300 MB")
    if ext not in ("pbix", "pbit", "csv", "txt", "xlsx", "xls"):
        raise HTTPException(400, "Unsupported file type")

    with tempfile.NamedTemporaryFile(suffix="." + ext, delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        if ext in ("pbix", "pbit"):
            return parse_pbix(path, name)
        if ext in ("csv", "txt"):
            return {"file": name, "tables": {name: df_to_records(pd.read_csv(path))}}
        sheets = pd.read_excel(path, sheet_name=None)
        return {"file": name, "tables": {k: df_to_records(v) for k, v in sheets.items()}}
    except Exception as e:
        raise HTTPException(422, f"Could not read file: {e}")
    finally:
        # Uploaded files are deleted immediately after they are read.
        os.remove(path)
