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
    properties_name: str | None
    xml_preview: str
    pyspark_preview: str
    xml_is_valid: bool
    notes: list[str]


def _strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:python|py)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _unescape_properties_value(value: str) -> str:
    value = value.replace(r"\n", "\n").replace(r"\t", "\t").replace(r"\r", "\r")
    value = value.replace(r"\=", "=").replace(r"\:", ":").replace(r"\ ", " ")
    value = value.replace(r"\\", "\\")
    return value


def _parse_properties_text(raw_text: str) -> dict[str, str]:
    props: dict[str, str] = {}
    pending = ""
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if line.endswith("\\") and not line.endswith("\\\\"):
            pending += line[:-1].rstrip() + " "
            continue
        line = (pending + line).strip()
        pending = ""
        if "=" in line:
            key, value = line.split("=", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue
        props[key.strip()] = _unescape_properties_value(value.strip())
    return props


def _build_properties_context(properties: dict[str, str]) -> str:
    if not properties:
        return ""

    interesting_prefixes = ("db.", "context.", "talend.", "project.", "job.", "app.")
    selected: list[tuple[str, str]] = []
    for key, value in properties.items():
        if key.startswith(interesting_prefixes) or key.lower() in {"name", "version", "purpose", "description"}:
            selected.append((key, value))
    if not selected:
        selected = list(properties.items())[:15]

    lines = ["Properties metadata:"]
    for key, value in selected[:25]:
        lines.append(f"- {key}={value}")
    if len(properties) > len(selected):
        lines.append(f"- ... {len(properties) - len(selected)} more entries")
    return "\n".join(lines)


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


def _fallback_pyspark(xml_text: str, properties: dict[str, str] | None = None) -> tuple[str, list[str]]:
    notes = ["Groq key not provided or Groq generation failed, so a starter scaffold was produced locally."]
    if properties:
        notes.append(f"Loaded {len(properties)} property values from the matching .properties file.")
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
            lines.append(f"# Step {idx}: {component_type} - {name}")
            if "fileinputdelimited" in lower_type or "input" in lower_type:
                lines.append("# Input")
                lines.append(
                    f"df_{safe_name} = spark.read.option('header', 'true').csv('path/to/input_{idx}.csv')"
                )
            elif "filter" in lower_type:
                lines.append("# Filter")
                lines.append(f"df_{safe_name} = df_{safe_name}  # add filter logic")
            elif "map" in lower_type:
                lines.append("# Map")
                lines.append(f"df_{safe_name} = df_{safe_name}  # add transform logic")
            elif "aggregate" in lower_type or "group" in lower_type:
                lines.append("# Aggregate")
                lines.append(f"df_{safe_name} = df_{safe_name}  # add groupBy logic")
            elif "output" in lower_type:
                lines.append("# Output")
                lines.append(
                    f"df_{safe_name}.write.mode('overwrite').parquet('path/to/output_{idx}')"
                )
            else:
                lines.append("# Review")
                lines.append(f"# Convert this Talend step manually: {component_type}")
            lines.append("")
    else:
        lines.extend(
            [
                "# No specific Talend components were detected.",
                "# Add ETL logic here after reviewing the XML.",
            ]
        )

    return "\n".join(lines), notes


def _groq_generate_pyspark(xml_text: str, api_key: str, model: str = DEFAULT_MODEL) -> str:
    return _groq_generate_pyspark_with_properties(xml_text, {}, api_key, model)


def _groq_generate_pyspark_with_properties(
    xml_text: str,
    properties: dict[str, str],
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> str:
    client = OpenAI(api_key=api_key, base_url=GROQ_BASE_URL)
    properties_context = _build_properties_context(properties)
    properties_block = f"\n{properties_context}\n" if properties_context else "\n"
    prompt = (
        "Convert this Talend .item XML into a single Databricks PySpark notebook cell.\n"
        "Rules:\n"
        "- Return only runnable Python/PySpark code.\n"
        "- Use one main code cell, not multiple explanations or sections.\n"
        "- Keep comments short and point-to-point.\n"
        "- Do not include display(), print() status updates, or extra narration.\n"
        "- If the XML contains Talend component names, reflect them briefly in comments.\n"
        "- Prefer a clean ETL skeleton over verbose logic.\n\n"
        f"{properties_block}"
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
    properties_text: str | None = None,
    properties_name: str | None = None,
    groq_api_key: str | None = None,
    model: str = DEFAULT_MODEL,
) -> ConversionResult:
    xml_preview, xml_is_valid = _pretty_xml(raw_text)
    notes: list[str] = []
    properties = _parse_properties_text(properties_text) if properties_text else {}

    if groq_api_key:
        try:
            pyspark_preview = _groq_generate_pyspark_with_properties(
                xml_preview,
                properties,
                groq_api_key,
                model=model,
            )
        except Exception as exc:  # pragma: no cover - surfaced in UI
            pyspark_preview, fallback_notes = _fallback_pyspark(xml_preview, properties)
            notes.extend(fallback_notes)
            notes.append(f"Groq generation error: {exc}")
    else:
        pyspark_preview, notes = _fallback_pyspark(xml_preview, properties)

    if xml_is_valid:
        notes.insert(0, "XML parsed successfully.")
    else:
        notes.insert(0, "The uploaded file was not valid XML, so the raw text was shown instead.")
    if properties_name:
        notes.insert(1, f"Matched properties file: {properties_name}")

    return ConversionResult(
        source_name=source_name,
        properties_name=properties_name,
        xml_preview=xml_preview,
        pyspark_preview=pyspark_preview,
        xml_is_valid=xml_is_valid,
        notes=notes,
    )
