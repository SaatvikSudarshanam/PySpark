from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.dom import minidom
from xml.etree import ElementTree as ET

from openai import OpenAI

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
DEFAULT_VOLUME_PREFIX = os.getenv("DATABRICKS_VOLUME_PREFIX", "/Volumes/shared/talend-conversion")
_STD_LIB_MODULES = set(getattr(sys, "stdlib_module_names", set()))
_SKIP_PIP_MODULES = {
    "dbutils",
    "delta",
    "pyspark",
    "python",
}
_INSTALL_ALIAS_MAP = {
    "yaml": "pyyaml",
}
_CONVERSION_CACHE: dict[tuple[str, str, str, str, str, str], ConversionResult] = {}


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


def _hash_text(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _looks_like_third_party_module(module_name: str) -> bool:
    module = module_name.split(".", 1)[0].strip().lower()
    if not module or module in _SKIP_PIP_MODULES:
        return False
    if module in _STD_LIB_MODULES:
        return False
    return True


def _extract_pip_packages(code: str) -> list[str]:
    packages: list[str] = []
    for raw_line in code.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("%pip "):
            continue

        import_match = re.match(r"^import\s+(.+)$", line)
        if import_match:
            for chunk in import_match.group(1).split(","):
                module = chunk.strip().split(" as ", 1)[0].strip()
                if not module:
                    continue
                top_level = module.split(".", 1)[0].lower()
                if _looks_like_third_party_module(top_level):
                    packages.append(_INSTALL_ALIAS_MAP.get(top_level, top_level))
            continue

        from_match = re.match(r"^from\s+([a-zA-Z0-9_\.]+)\s+import\s+", line)
        if from_match:
            module = from_match.group(1).split(".", 1)[0].lower()
            if _looks_like_third_party_module(module):
                packages.append(_INSTALL_ALIAS_MAP.get(module, module))

    deduped: list[str] = []
    seen: set[str] = set()
    for package in packages:
        if package not in seen:
            seen.add(package)
            deduped.append(package)
    return deduped


def _prepend_pip_install_block(code: str) -> str:
    stripped = _strip_code_fences(code)
    if "%pip install" in stripped:
        return stripped

    packages = _extract_pip_packages(stripped)
    if not packages:
        return stripped

    install_block = "\n".join(f"%pip install {package}" for package in packages)
    return f"{install_block}\n\n{stripped}"


def _normalize_volume_prefix(volume_prefix: str | None = None) -> str:
    prefix = (volume_prefix or DEFAULT_VOLUME_PREFIX).strip().rstrip("/")
    if not prefix:
        prefix = "/Volumes/shared/talend-conversion"
    if not prefix.startswith("/"):
        prefix = f"/{prefix}"
    return prefix


def _safe_databricks_path(filename: str, suffix: str = "", volume_prefix: str | None = None) -> str:
    prefix = _normalize_volume_prefix(volume_prefix)
    clean_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", filename).strip("_") or "output"
    if suffix and not clean_name.lower().endswith(suffix.lower()):
        clean_name = f"{clean_name}{suffix}"
    return f"{prefix}/{clean_name}"


def _rewrite_paths_for_databricks(code: str, volume_prefix: str | None = None) -> tuple[str, list[str]]:
    notes: list[str] = []
    prefix = _normalize_volume_prefix(volume_prefix)

    def replace_path(match: re.Match[str]) -> str:
        quote = match.group(1)
        path = match.group(2)
        normalized = path.replace("\\", "/")
        if re.match(r"^[A-Za-z]:/", normalized):
            basename = Path(normalized).name or "output.csv"
            safe_path = _safe_databricks_path(basename, volume_prefix=prefix)
            notes.append(f"Rewrote Windows path {path} to {safe_path}.")
            return f"{quote}{safe_path}{quote}"
        if normalized.startswith("/dbfs/") or normalized.startswith("dbfs:/"):
            basename = Path(normalized.rstrip("/")).name or "output"
            safe_path = _safe_databricks_path(basename, volume_prefix=prefix)
            notes.append(f"Rewrote DBFS path {path} to {safe_path}.")
            return f"{quote}{safe_path}{quote}"
        return match.group(0)

    rewritten = re.sub(r'([\'"])((?:[A-Za-z]:[\\/]|/?dbfs:/|/dbfs/)[^\'"]*)\1', replace_path, code)
    return rewritten, notes


def _validate_generated_code(code: str) -> list[str]:
    issues: list[str] = []
    if re.search(r'(?<![\w/])[A-Za-z]:[\\/]', code):
        issues.append("Windows drive-letter paths are not valid in Databricks Spark IO.")
    if "/dbfs/" in code:
        issues.append("The /dbfs/ mount can fail when public DBFS root is disabled.")
    if "dbfs:/" in code:
        issues.append("DBFS paths may fail in workspaces where DBFS root is disabled; prefer /Volumes/ paths.")
    return issues


def _groq_repair_pyspark_with_properties(
    xml_text: str,
    current_code: str,
    properties: dict[str, str],
    issues: list[str],
    api_key: str,
    volume_prefix: str | None = None,
    model: str = DEFAULT_MODEL,
) -> str:
    client = OpenAI(api_key=api_key, base_url=GROQ_BASE_URL)
    properties_context = _build_properties_context(properties)
    properties_block = f"\n{properties_context}\n" if properties_context else "\n"
    prefix = _normalize_volume_prefix(volume_prefix)
    prompt = (
        "Repair this Databricks PySpark notebook cell so it can run safely.\n"
        "Rules:\n"
        "- Return only runnable Python/PySpark code.\n"
        "- Keep the same Talend conversion intent and structure.\n"
        "- Keep comments short and point-to-point.\n"
        f"- Use {prefix}/... for any Databricks file read/write path.\n"
        "- Do not use dbfs:/, /dbfs/, or Windows drive-letter paths.\n"
        "- Do not use Windows drive-letter paths like C:/ or C\\\\.\n"
        "- If third-party packages are needed, put %pip install lines at the top before imports.\n"
        "- Keep the output deterministic and notebook-ready.\n\n"
        f"Issues to fix:\n- " + "\n- ".join(issues) + "\n\n"
        f"{properties_block}"
        f"Original XML:\n{xml_text}\n\n"
        f"Current code:\n{current_code}"
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You repair Talend-to-PySpark notebook code for Databricks and output only corrected code.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
    )
    content = response.choices[0].message.content or ""
    repaired = _prepend_pip_install_block(content)
    repaired, path_notes = _rewrite_paths_for_databricks(repaired, volume_prefix=prefix)
    if path_notes:
        repaired = _prepend_pip_install_block(repaired)
    return repaired


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


def _fallback_pyspark(
    xml_text: str,
    properties: dict[str, str] | None = None,
    volume_prefix: str | None = None,
) -> tuple[str, list[str]]:
    prefix = _normalize_volume_prefix(volume_prefix)
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
                    f"df_{safe_name} = spark.read.option('header', 'true').csv('{prefix}/input_{idx}.csv')"
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
                    f"df_{safe_name}.write.mode('overwrite').parquet('{prefix}/output_{idx}')"
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
    volume_prefix: str | None = None,
    model: str = DEFAULT_MODEL,
) -> str:
    client = OpenAI(api_key=api_key, base_url=GROQ_BASE_URL)
    properties_context = _build_properties_context(properties)
    properties_block = f"\n{properties_context}\n" if properties_context else "\n"
    prefix = _normalize_volume_prefix(volume_prefix)
    prompt = (
        "Convert this Talend .item XML into a deterministic Databricks PySpark notebook cell.\n"
        "Rules:\n"
        "- Return only runnable Python/PySpark code.\n"
        "- Use one main code cell, not multiple explanations or sections.\n"
        "- Keep comments short and point-to-point.\n"
        "- Do not include display(), print() status updates, or extra narration.\n"
        "- If third-party packages are needed, place %pip install lines at the top before imports.\n"
        f"- Use {prefix}/... for any Databricks file read/write path, never Windows drive letters.\n"
        "- Use stable variable names and a fixed structure for the same input every time.\n"
        "- Prefer deterministic transformations and fixed seeds where sample data is needed.\n"
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
                "content": "You are an expert Talend-to-PySpark migration assistant that produces deterministic notebook code.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
    )
    content = response.choices[0].message.content or ""
    repaired = _prepend_pip_install_block(content)
    repaired, path_notes = _rewrite_paths_for_databricks(repaired, volume_prefix=prefix)
    if path_notes:
        repaired = _prepend_pip_install_block(repaired)
    return repaired


def _convert_item_text_uncached(
    raw_text: str,
    source_name: str = "uploaded.item",
    properties_text: str | None = None,
    properties_name: str | None = None,
    groq_api_key: str | None = None,
    volume_prefix: str | None = None,
    model: str = DEFAULT_MODEL,
) -> ConversionResult:
    xml_preview, xml_is_valid = _pretty_xml(raw_text)
    notes: list[str] = []
    properties = _parse_properties_text(properties_text) if properties_text else {}
    review_issues: list[str] = []
    prefix = _normalize_volume_prefix(volume_prefix)

    if groq_api_key:
        try:
            pyspark_preview = _groq_generate_pyspark_with_properties(
                xml_preview,
                properties,
                groq_api_key,
                volume_prefix=prefix,
                model=model,
            )
        except Exception as exc:  # pragma: no cover - surfaced in UI
            pyspark_preview, fallback_notes = _fallback_pyspark(xml_preview, properties, volume_prefix=prefix)
            notes.extend(fallback_notes)
            notes.append(f"Groq generation error: {exc}")
    else:
        pyspark_preview, notes = _fallback_pyspark(xml_preview, properties, volume_prefix=prefix)
    pyspark_preview = _prepend_pip_install_block(pyspark_preview)
    pyspark_preview, path_notes = _rewrite_paths_for_databricks(pyspark_preview, volume_prefix=prefix)
    notes.extend(path_notes)

    review_issues = _validate_generated_code(pyspark_preview)
    if review_issues:
        notes.extend(review_issues)
        if groq_api_key:
            try:
                repaired = _groq_repair_pyspark_with_properties(
                    xml_preview,
                    pyspark_preview,
                    properties,
                    review_issues,
                    groq_api_key,
                    volume_prefix=prefix,
                    model=model,
                )
                repaired = _prepend_pip_install_block(repaired)
                repaired, repair_path_notes = _rewrite_paths_for_databricks(repaired, volume_prefix=prefix)
                notes.extend(repair_path_notes)
                post_repair_issues = _validate_generated_code(repaired)
                if post_repair_issues:
                    notes.extend([f"Post-repair validation still found: {issue}" for issue in post_repair_issues])
                else:
                    pyspark_preview = repaired
                    notes.append("Validation and repair pass completed successfully.")
            except Exception as exc:  # pragma: no cover - surfaced in UI
                notes.append(f"Validation repair failed: {exc}")

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


def convert_item_text(
    raw_text: str,
    source_name: str = "uploaded.item",
    properties_text: str | None = None,
    properties_name: str | None = None,
    groq_api_key: str | None = None,
    volume_prefix: str | None = None,
    model: str = DEFAULT_MODEL,
) -> ConversionResult:
    raw_hash = _hash_text(raw_text)
    properties_hash = _hash_text(properties_text)
    prefix = _normalize_volume_prefix(volume_prefix)
    cache_key = (
        raw_hash,
        properties_hash,
        source_name,
        properties_name or "",
        prefix,
        model.strip() or DEFAULT_MODEL,
        "groq" if groq_api_key else "fallback",
    )
    if cache_key not in _CONVERSION_CACHE:
        _CONVERSION_CACHE[cache_key] = _convert_item_text_uncached(
            raw_text=raw_text,
            source_name=source_name,
            properties_text=properties_text,
            properties_name=properties_name,
            groq_api_key=groq_api_key,
            volume_prefix=prefix,
            model=model,
        )
    return _CONVERSION_CACHE[cache_key]
