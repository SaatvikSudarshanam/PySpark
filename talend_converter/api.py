from __future__ import annotations

import os

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .converter import convert_item_text

app = FastAPI(title="Talend Converter API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    groq_api_key: str | None = Form(default=None),
    model: str = Form(default=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")),
    properties_text: str | None = Form(default=None),
    properties_name: str | None = Form(default=None),
    volume_prefix: str | None = Form(default=os.getenv("DATABRICKS_VOLUME_PREFIX", "/Volumes/shared/talend-conversion")),
):
    try:
        raw_bytes = await file.read()
        raw_text = raw_bytes.decode("utf-8", errors="ignore")
        result = convert_item_text(
            raw_text=raw_text,
            source_name=file.filename or "uploaded.item",
            properties_text=properties_text,
            properties_name=properties_name,
            groq_api_key=groq_api_key or os.getenv("GROQ_API_KEY"),
            volume_prefix=volume_prefix,
            model=model,
        )
        return {
            "source_name": result.source_name,
            "properties_name": result.properties_name,
            "xml_preview": result.xml_preview,
            "pyspark_preview": result.pyspark_preview,
            "xml_is_valid": result.xml_is_valid,
            "notes": result.notes,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
