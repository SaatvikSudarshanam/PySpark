from __future__ import annotations

import os
import re
from dataclasses import dataclass
from xml.dom import minidom
from xml.etree import ElementTree as ET

from openai import OpenAI

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")


@dataclass
class ConversionResult:
    source_name: str
    xml_preview: str
    pyspark_preview: str
    xml_is_valid: bool
    notes: list[str]


def _strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:python|py)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _pretty_xml(raw_text: str) -> tuple[str, bool]:
    text = raw_text.strip()
    try:
        parsed = minidom.parseString(text)
        pretty = parsed.toprettyxml(indent="  ")
        lines = [
            line
            for line in pretty.splitlines()
            if line.strip() and line.strip() != "<?xml version=\"1.0\" ?>"
        ]
        return "\n".join(lines), True
    except Exception:
        return text, False


def _collect_component_nodes(root: ET.Element) -> list[tuple[str, str]]:
    nodes: list[tuple[str, str]] = []
    for element in root.iter():
        component_type = (
            element.attrib.get("componentName")
            or element.attrib.get("component")
            or element.attrib.get("type")
            or ""
        )
        label = (
            element.attrib.get("label")
            or element.attrib.get("name")
            or element.attrib.get("id")
            or ""
        )
        if component_type or label:
            name = label or component_type or element.tag
            nodes.append((component_type or element.tag, name))
    return nodes


def _fallback_pyspark(xml_text: str) -> tuple[str, list[str]]:
    notes = ["Groq key not provided or Groq generation failed, so a starter scaffold was produced locally."]
    try:
        root = ET.fromstring(xml_text)
        components = _collect_component_nodes(root)
    except Exception:
        components = []
        notes.append("The uploaded file could not be parsed as XML, so no Talend components were detected.")

    lines = [
        "from pyspark.sql import SparkSession",
        "",
        "spark = SparkSession.builder.appName('TalendConversion').getOrCreate()",
        "",
    ]

    if components:
        for idx, (component_type, name) in enumerate(components, start=1):
            safe_name = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_") or f"step_{idx}"
            lower_type = component_type.lower()
            lines.append(f"# Component {idx}: {name} ({component_type})")
            if "fileinputdelimited" in lower_type or "input" in lower_type:
                lines.append(
                    f"df_{safe_name} = spark.read.option('header', 'true').csv('path/to/input_{idx}.csv')"
                )
            elif "filter" in lower_type:
                lines.append(f"df_{safe_name} = df_{safe_name}  # add your filter expression here")
            elif "map" in lower_type:
                lines.append(f"df_{safe_name} = df_{safe_name}  # map / transform logic from Talend")
            elif "aggregate" in lower_type or "group" in lower_type:
                lines.append(f"df_{safe_name} = df_{safe_name}  # groupBy / aggregation logic")
            elif "output" in lower_type:
                lines.append(
                    f"df_{safe_name}.write.mode('overwrite').parquet('path/to/output_{idx}')"
                )
            else:
                lines.append(f"# Review and convert this Talend step manually: {component_type}")
            lines.append("")
    else:
        lines.extend(
            [
                "# No specific Talend components were detected.",
                "# Add your ETL logic here after reviewing the XML.",
            ]
        )

    lines.extend([
        "# spark.stop()  # uncomment for standalone jobs",
    ])
    return "\n".join(lines), notes


def _groq_generate_pyspark(xml_text: str, api_key: str, model: str = DEFAULT_MODEL) -> str:
    client = OpenAI(api_key=api_key, base_url=GROQ_BASE_URL)
    prompt = (
        "Convert this Talend .item XML into a PySpark script.\n"
        "Rules:\n"
        "- Return only runnable Python/PySpark code.\n"
        "- Keep comments concise.\n"
        "- If the XML contains Talend component names, reflect them in the code comments.\n"
        "- Prefer a clear ETL skeleton over a verbose explanation.\n\n"
        f"XML:\n{xml_text}"
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You are an expert Talend-to-PySpark migration assistant.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    content = response.choices[0].message.content or ""
    return _strip_code_fences(content)


def convert_item_text(
    raw_text: str,
    source_name: str = "uploaded.item",
    groq_api_key: str | None = None,
    model: str = DEFAULT_MODEL,
) -> ConversionResult:
    xml_preview, xml_is_valid = _pretty_xml(raw_text)
    notes: list[str] = []

    if groq_api_key:
        try:
            pyspark_preview = _groq_generate_pyspark(xml_preview, groq_api_key, model=model)
        except Exception as exc:  # pragma: no cover - surfaced in UI
            pyspark_preview, fallback_notes = _fallback_pyspark(xml_preview)
            notes.extend(fallback_notes)
            notes.append(f"Groq generation error: {exc}")
    else:
        pyspark_preview, notes = _fallback_pyspark(xml_preview)

    if xml_is_valid:
        notes.insert(0, "XML parsed successfully.")
    else:
        notes.insert(0, "The uploaded file was not valid XML, so the raw text was shown instead.")

    return ConversionResult(
        source_name=source_name,
        xml_preview=xml_preview,
        pyspark_preview=pyspark_preview,
        xml_is_valid=xml_is_valid,
        notes=notes,
    )
