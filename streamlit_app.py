from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
import streamlit as st

from talend_converter.converter import convert_item_text


st.set_page_config(
    page_title="Talend .item to XML + PySpark",
    page_icon="T",
    layout="wide",
)


def _download_name(source_name: str, suffix: str) -> str:
    stem = Path(source_name).stem or "talend-conversion"
    return f"{stem}.{suffix}"


def _as_result_dict(result):
    return {
        "source_name": result.source_name,
        "properties_name": result.properties_name,
        "xml_preview": result.xml_preview,
        "pyspark_preview": result.pyspark_preview,
        "xml_is_valid": result.xml_is_valid,
        "notes": result.notes,
    }


def _normalize_github_raw_url(url: str) -> str:
    cleaned = url.strip()

    blob_match = re.match(
        r"^https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$",
        cleaned,
    )
    if blob_match:
        owner, repo, branch, path = blob_match.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"

    if cleaned.startswith("https://github.com/"):
        return cleaned.replace("https://github.com/", "https://raw.githubusercontent.com/", 1)
    if cleaned.startswith("http://github.com/"):
        return cleaned.replace("http://github.com/", "https://raw.githubusercontent.com/", 1)
    return cleaned


def _github_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "talend-item-converter/1.0",
    }
    if token and token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    return headers


def _get_secret(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


def _vault_path() -> Path:
    return Path.home() / ".talend_converter_vault.json"


def _load_vault() -> dict[str, str]:
    path = _vault_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items() if value is not None}


def _save_vault(values: dict[str, str]) -> None:
    path = _vault_path()
    path.write_text(json.dumps(values, indent=2, sort_keys=True), encoding="utf-8")


def _clear_vault() -> None:
    path = _vault_path()
    if path.exists():
        path.unlink()


def _seed_session_defaults() -> None:
    defaults = {
        "backend_url": os.getenv("BACKEND_URL", "http://localhost:8000/convert"),
        "groq_api_key": os.getenv("GROQ_API_KEY", ""),
        "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
        "github_token": os.getenv("GITHUB_TOKEN", _get_secret("GITHUB_TOKEN", "")),
        "databricks_workspace_url": os.getenv("DATABRICKS_WORKSPACE_URL", ""),
        "databricks_token": os.getenv("DATABRICKS_TOKEN", _get_secret("DATABRICKS_TOKEN", "")),
        "databricks_target_path": os.getenv("DATABRICKS_TARGET_PATH", "/Shared/talend-conversion"),
        "databricks_root_path": os.getenv("DATABRICKS_ROOT_PATH", "/Users"),
        "databricks_volume_prefix": os.getenv("DATABRICKS_VOLUME_PREFIX", "/Volumes/shared/talend-conversion"),
        "github_profile_url": "",
        "vault_name": "personal",
    }
    defaults.update(_load_vault())
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def _capture_vault_values() -> dict[str, str]:
    return {
        "backend_url": st.session_state.get("backend_url", "").strip(),
        "groq_api_key": st.session_state.get("groq_api_key", "").strip(),
        "model": st.session_state.get("model", "").strip(),
        "github_token": st.session_state.get("github_token", "").strip(),
        "databricks_workspace_url": st.session_state.get("databricks_workspace_url", "").strip(),
        "databricks_token": st.session_state.get("databricks_token", "").strip(),
        "databricks_target_path": st.session_state.get("databricks_target_path", "").strip(),
        "databricks_root_path": st.session_state.get("databricks_root_path", "").strip(),
        "databricks_volume_prefix": st.session_state.get("databricks_volume_prefix", "").strip(),
        "github_profile_url": st.session_state.get("github_profile_url", "").strip(),
    }


def _apply_vault_values(values: dict[str, str]) -> None:
    for key, value in values.items():
        st.session_state[key] = value


def _parse_github_profile_url(url: str) -> str | None:
    cleaned = url.strip()
    if not cleaned:
        return None
    parsed = urlparse(cleaned)
    if parsed.netloc.lower() != "github.com":
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None

    if parts[0].lower() == "orgs" and len(parts) >= 2:
        return parts[1]

    if len(parts) >= 1 and parts[0].lower() not in {"settings", "topics", "features", "contact", "pricing"}:
        # Accept normal profile URLs such as:
        # https://github.com/<user>
        # https://github.com/<user>/
        # https://github.com/<user>?tab=repositories
        # https://github.com/<org>
        return parts[0]

    return None


def _split_github_repo_reference(repo_ref: str, default_owner: str | None = None) -> tuple[str, str]:
    cleaned = repo_ref.strip()
    if "/" in cleaned:
        owner, repo = cleaned.split("/", 1)
        owner = owner.strip()
        repo = repo.strip()
        if owner and repo:
            return owner, repo
    if default_owner:
        return default_owner, cleaned
    return cleaned, cleaned


def _parse_github_blob_url(url: str) -> tuple[str, str, str, str]:
    cleaned = url.strip()
    blob_match = re.match(
        r"^https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$",
        cleaned,
    )
    if blob_match:
        return blob_match.groups()

    raw_match = re.match(
        r"^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$",
        cleaned,
    )
    if raw_match:
        return raw_match.groups()

    raise ValueError(
        "Use a GitHub blob URL like https://github.com/<owner>/<repo>/blob/<branch>/path/to/file.item "
        "or a raw.githubusercontent.com URL."
    )


def _fetch_github_item(url: str, github_token: str | None = None) -> tuple[str, str]:
    cleaned = url.strip()
    token = (github_token or "").strip()

    if cleaned.startswith("https://github.com/") or cleaned.startswith("https://raw.githubusercontent.com/"):
        owner, repo, branch, path = _parse_github_blob_url(cleaned)
        filename = Path(path).name or "github-item.item"

        if token:
            api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.raw",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            response = requests.get(api_url, headers=headers, params={"ref": branch}, timeout=120)
            response.raise_for_status()
            return filename, response.text

        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
        response = requests.get(raw_url, timeout=120)
        response.raise_for_status()
        return filename, response.text

    raw_url = _normalize_github_raw_url(cleaned)
    response = requests.get(raw_url, timeout=120)
    response.raise_for_status()
    filename = Path(raw_url).name or "github-item.item"
    return filename, response.text


@st.cache_data(show_spinner=False, ttl=600)
def _fetch_github_repositories(username: str, token: str | None = None) -> list[str]:
    headers = _github_headers(token)
    if token and token.strip():
        response = requests.get(
            "https://api.github.com/user/repos?per_page=100&sort=updated&visibility=all&affiliation=owner,collaborator,organization_member",
            headers=headers,
            timeout=60,
        )
        if response.status_code in {401, 403}:
            message = response.json().get("message", response.text.strip())
            raise RuntimeError(
                "GitHub rejected the authenticated repository request. "
                f"{message or 'Check the fine-grained token permissions for Repository contents: read and Metadata: read.'}"
            )
        response.raise_for_status()
        repos = response.json()
        repo_names = [item.get("full_name") or f"{item.get('owner', {}).get('login', username)}/{item['name']}" for item in repos]
        deduped: list[str] = []
        seen: set[str] = set()
        for repo_name in repo_names:
            if repo_name and repo_name not in seen:
                seen.add(repo_name)
                deduped.append(repo_name)
        return deduped

    response = requests.get(
        f"https://api.github.com/users/{username}/repos?per_page=100&sort=updated&type=owner",
        headers=headers,
        timeout=60,
    )
    if response.status_code == 404:
        response = requests.get(
            f"https://api.github.com/orgs/{username}/repos?per_page=100&sort=updated&type=owner",
            headers=headers,
            timeout=60,
        )
    if response.status_code in {401, 403}:
        message = response.json().get("message", response.text.strip())
        raise RuntimeError(
            f"GitHub rejected the repository request for '{username}'. "
            f"{message or 'Add a GitHub token for private repos or if you hit rate limits.'}"
        )
    response.raise_for_status()
    return [
        item.get("full_name") or f"{item.get('owner', {}).get('login', username)}/{item['name']}"
        for item in response.json()
    ]


@st.cache_data(show_spinner=False, ttl=600)
def _fetch_github_branches(owner: str, repo: str, token: str | None = None) -> list[str]:
    response = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/branches",
        headers=_github_headers(token),
        timeout=60,
    )
    if response.status_code in {401, 403}:
        message = response.json().get("message", response.text.strip())
        raise RuntimeError(
            f"GitHub rejected the branch request for '{owner}/{repo}'. "
            f"{message or 'Add a GitHub token if the repo is private or you are rate-limited.'}"
        )
    response.raise_for_status()
    return [item["name"] for item in response.json()]


@st.cache_data(show_spinner=False, ttl=600)
def _fetch_github_item_paths(owner: str, repo: str, branch: str, token: str | None = None) -> list[str]:
    response = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1",
        headers=_github_headers(token),
        timeout=60,
    )
    if response.status_code in {401, 403}:
        message = response.json().get("message", response.text.strip())
        raise RuntimeError(
            f"GitHub rejected the job scan for '{owner}/{repo}@{branch}'. "
            f"{message or 'Add a GitHub token if the repo is private or you are rate-limited.'}"
        )
    response.raise_for_status()
    payload = response.json()
    return [
        item["path"]
        for item in payload.get("tree", [])
        if item.get("type") == "blob" and item.get("path", "").lower().endswith(".item")
    ]


@st.cache_data(show_spinner=False, ttl=600)
def _fetch_github_branch_tree(owner: str, repo: str, branch: str, token: str | None = None) -> list[str]:
    response = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1",
        headers=_github_headers(token),
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    return [item["path"] for item in payload.get("tree", []) if item.get("type") == "blob"]


def _match_properties_path(item_path: str, paths: list[str]) -> str | None:
    item_stem = Path(item_path).stem.lower()
    item_dir = str(Path(item_path).parent).replace("\\", "/")
    preferred = f"{item_dir}/{item_stem}.properties" if item_dir and item_dir != "." else f"{item_stem}.properties"
    for path in paths:
        normalized = path.replace("\\", "/")
        if normalized.lower() == preferred.lower():
            return path
    for path in paths:
        normalized = path.replace("\\", "/")
        if Path(normalized).stem.lower() == item_stem and normalized.lower().endswith(".properties"):
            return path
    return None


def _job_checkbox_key(username: str, repo: str, branch: str, idx: int) -> str:
    return f"github_job_{username}_{repo}_{branch}_{idx}"


def _split_local_uploads(files) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
    item_file = None
    prop_file = None
    for file in files or []:
        name = file.name.lower()
        text = file.getvalue().decode("utf-8", errors="ignore")
        if name.endswith(".item") and item_file is None:
            item_file = (file.name, text)
        elif name.endswith(".properties") and prop_file is None:
            prop_file = (file.name, text)
    return item_file, prop_file


def _convert_via_backend(
    uploaded_file,
    backend_url: str,
    groq_api_key: str,
    model: str,
    properties_text: str | None = None,
    properties_name: str | None = None,
    volume_prefix: str | None = None,
):
    endpoint = backend_url.strip()
    if not endpoint:
        return None
    if endpoint.endswith("/"):
        endpoint = endpoint[:-1]
    if not endpoint.endswith("/convert"):
        endpoint = f"{endpoint}/convert"

    files = {
        "file": (
            uploaded_file.name,
            uploaded_file.getvalue(),
            uploaded_file.type or "application/octet-stream",
        )
    }
    data = {
        "groq_api_key": groq_api_key.strip(),
        "model": model.strip() or "openai/gpt-oss-20b",
    }
    if properties_text:
        data["properties_text"] = properties_text
    if properties_name:
        data["properties_name"] = properties_name
    if volume_prefix:
        data["volume_prefix"] = volume_prefix
    response = requests.post(endpoint, files=files, data=data, timeout=120)
    response.raise_for_status()
    return response.json()


def _convert_source_text(
    source_name: str,
    raw_text: str,
    backend_url: str,
    groq_api_key: str,
    model: str,
    properties_text: str | None = None,
    properties_name: str | None = None,
    volume_prefix: str | None = None,
):
    memory_upload = _MemoryUpload(source_name, raw_text)
    try:
        return _convert_via_backend(
            memory_upload,
            backend_url,
            groq_api_key,
            model,
            properties_text=properties_text,
            properties_name=properties_name,
            volume_prefix=volume_prefix,
        )
    except Exception:
        local = convert_item_text(
            raw_text=raw_text,
            source_name=source_name,
            properties_text=properties_text,
            properties_name=properties_name,
            groq_api_key=groq_api_key.strip() or None,
            volume_prefix=volume_prefix,
            model=model.strip() or "openai/gpt-oss-20b",
        )
        return _as_result_dict(local)


@st.cache_data(show_spinner=False, ttl=300)
def _databricks_list(workspace_url: str, token: str, path: str) -> list[dict]:
    endpoint = f"{_normalize_databricks_url(workspace_url)}/api/2.0/workspace/list"
    response = requests.get(
        endpoint,
        headers={"Authorization": f"Bearer {token.strip()}"},
        params={"path": path},
        timeout=60,
    )
    response.raise_for_status()
    return response.json().get("objects", [])


def _databricks_root_candidates(root_path: str) -> list[str]:
    cleaned = root_path.strip().rstrip("/")
    candidates = [cleaned or "/Users", "/", "/Users"]
    if cleaned.startswith("/Workspace/"):
        candidates.insert(0, cleaned[len("/Workspace"):])
    elif cleaned.startswith("/Workspace"):
        candidates.insert(0, cleaned[len("/Workspace"):].lstrip("/") or "/")
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        normalized = candidate if candidate.startswith("/") else f"/{candidate}"
        if normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    return ordered


def _collect_databricks_tree(workspace_url: str, token: str, root_path: str) -> tuple[list[str], list[str]]:
    folders: set[str] = set()
    notebooks: list[str] = []
    queue = [root_path]
    seen = set()

    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        try:
            objects = _databricks_list(workspace_url, token, current)
        except Exception:
            continue
        for obj in objects:
            obj_type = obj.get("object_type")
            path = obj.get("path", "")
            if obj_type == "DIRECTORY":
                folders.add(path)
                queue.append(path)
            elif obj_type == "NOTEBOOK":
                folders.add(str(Path(path).parent).replace("\\", "/"))
                notebooks.append(path)
            elif obj_type == "FILE" and path.lower().endswith(".py"):
                folders.add(str(Path(path).parent).replace("\\", "/"))
                notebooks.append(path)
    folders.add(root_path)
    return sorted(folders), sorted(set(notebooks))


def _load_databricks_tree(workspace_url: str, token: str, root_path: str) -> tuple[list[str], list[str], str]:
    candidates = _databricks_root_candidates(root_path)
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            folders, notebooks = _collect_databricks_tree(workspace_url, token, candidate)
            return folders, notebooks, candidate
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    return [], [], root_path


def _databricks_import_notebook(workspace_url: str, token: str, path: str, code: str) -> dict:
    endpoint = f"{_normalize_databricks_url(workspace_url)}/api/2.0/workspace/import"
    notebook_source = "\n".join(
        [
            "# Databricks notebook source",
            "# COMMAND ----------",
            code.strip(),
            "",
        ]
    )
    payload = {
        "path": path,
        "content": base64.b64encode(notebook_source.encode("utf-8")).decode("ascii"),
        "format": "SOURCE",
        "language": "PYTHON",
        "overwrite": True,
    }
    response = requests.post(
        endpoint,
        headers={"Authorization": f"Bearer {token.strip()}"},
        json=payload,
        timeout=120,
    )
    response.raise_for_status()
    return response.json() if response.content else {}


def _normalize_databricks_url(url: str) -> str:
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        return ""
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        return cleaned
    return f"https://{cleaned}"


def _resolve_databricks_target_path(base_path: str, source_name: str, extension: str) -> str:
    cleaned = base_path.strip().rstrip("/")
    filename = f"{Path(source_name).stem or 'talend-conversion'}{extension}"

    if not cleaned:
        return f"/Shared/{filename}"
    if cleaned.endswith(extension):
        return cleaned
    if "/" not in Path(cleaned).name and "." not in Path(cleaned).name:
        return f"{cleaned}/{filename}"
    if cleaned.endswith("/"):
        return f"{cleaned}{filename}"
    return cleaned


def _unique_databricks_target_path(base_path: str, source_name: str, extension: str, job_name: str | None = None) -> str:
    cleaned = base_path.strip().rstrip("/")
    stem_source = job_name or source_name
    stem = Path(stem_source).stem or "talend-conversion"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{stem}_{timestamp}{extension}"

    if not cleaned:
        return f"/Shared/{filename}"
    if cleaned.endswith(extension):
        folder = str(Path(cleaned).parent).replace("\\", "/")
        return f"{folder.rstrip('/')}/{filename}"
    if "/" not in Path(cleaned).name and "." not in Path(cleaned).name:
        return f"{cleaned}/{filename}"
    if cleaned.endswith("/"):
        return f"{cleaned}{filename}"
    folder = str(Path(cleaned).parent).replace("\\", "/")
    return f"{folder.rstrip('/')}/{filename}"


def _converted_databricks_target_path(folder_path: str, extension: str, existing_paths: list[str] | None = None) -> str:
    cleaned = folder_path.strip().rstrip("/")
    candidate = f"{cleaned}/converted{extension}" if cleaned else f"/Shared/converted{extension}"
    normalized_existing = {path.rstrip("/") for path in (existing_paths or [])}
    if candidate not in normalized_existing:
        return candidate

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{cleaned or '/Shared'}/converted_{timestamp}{extension}"


def _build_databricks_payload(result: dict, export_mode: str) -> tuple[str, str, str | None, str]:
    if export_mode == "Markdown file (.md)":
        content_lines = [
            f"# Talend conversion for {result['source_name']}",
            "",
            "## XML Preview",
            "```xml",
            result["xml_preview"],
            "```",
            "",
            "## PySpark Preview",
            "```python",
            result["pyspark_preview"],
            "```",
            "",
            "## Notes",
        ]
        content_lines.extend(f"- {note}" for note in result["notes"])
        return "\n".join(content_lines).rstrip() + "\n", "RAW", None, ".md"

    notebook_lines = [
        "# Databricks notebook source",
        "# COMMAND ----------",
        "",
        result["pyspark_preview"].rstrip(),
        "",
    ]
    return "\n".join(notebook_lines), "SOURCE", "PYTHON", ".py"


def _build_databricks_batch_payload(results: list[dict], export_mode: str) -> tuple[str, str, str | None, str]:
    if not results:
        raise ValueError("No converted jobs available to push.")

    if export_mode == "Markdown file (.md)":
        sections = []
        for item in results:
            sections.extend(
                [
                    f"# Talend conversion for {item['source_name']}",
                    "",
                    "## XML Preview",
                    "```xml",
                    item["xml_preview"],
                    "```",
                    "",
                    "## PySpark Preview",
                    "```python",
                    item["pyspark_preview"],
                    "```",
                    "",
                    "## Notes",
                ]
            )
            sections.extend(f"- {note}" for note in item["notes"])
            sections.append("")
        return "\n".join(sections).rstrip() + "\n", "RAW", None, ".md"

    notebook_lines = ["# Databricks notebook source"]
    for idx, item in enumerate(results):
        notebook_lines.extend(
            [
                "# COMMAND ----------",
                f"# Job: {item.get('job', item['source_name'])}",
                item["pyspark_preview"].rstrip(),
                "",
            ]
        )
    return "\n".join(notebook_lines), "SOURCE", "PYTHON", ".py"


def _push_to_databricks(
    workspace_url: str,
    token: str,
    target_path: str,
    content: str,
    export_format: str,
    language: str | None,
    overwrite: bool,
) -> None:
    endpoint = f"{_normalize_databricks_url(workspace_url)}/api/2.0/workspace/import"
    payload: dict[str, object] = {
        "path": target_path,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "format": export_format,
        "overwrite": overwrite,
    }
    if language:
        payload["language"] = language

    response = requests.post(
        endpoint,
        headers={"Authorization": f"Bearer {token.strip()}"},
        json=payload,
        timeout=120,
    )
    response.raise_for_status()


class _MemoryUpload:
    def __init__(self, name: str, text: str):
        self.name = name
        self._text = text
        self.type = "application/xml"

    def getvalue(self):
        return self._text.encode("utf-8")


st.markdown(
    """
    <style>
      .stApp {
        background: radial-gradient(circle at top left, rgba(96, 165, 250, 0.18), transparent 24%),
                    radial-gradient(circle at top right, rgba(94, 234, 212, 0.16), transparent 22%),
                    linear-gradient(180deg, #0b1020 0%, #111827 100%);
        color: #e5eefb;
      }
      div[data-testid="stSidebar"] {
        background: rgba(15, 23, 42, 0.95);
      }
      .block-container {
        padding-top: 2rem;
        padding-bottom: 2rem;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


st.title("Talend .item to XML + PySpark")
st.caption("Upload a local file or import one from GitHub, then preview XML and generated PySpark.")

_seed_session_defaults()

with st.sidebar:
    st.header("Vault")
    st.caption("Saved locally on this machine only. Use Save once, then the fields will auto-fill next time.")
    vault_name = st.text_input("Vault name", key="vault_name")
    vault_action_left, vault_action_right, vault_action_clear = st.columns(3)
    with vault_action_left:
        save_vault = st.button("Save", use_container_width=True)
    with vault_action_right:
        load_vault = st.button("Load", use_container_width=True)
    with vault_action_clear:
        clear_vault = st.button("Clear", use_container_width=True)

    if save_vault:
        _save_vault(_capture_vault_values())
        st.success(f"Saved vault '{vault_name or 'personal'}'.")
    if load_vault:
        _apply_vault_values(_load_vault())
        st.rerun()
    if clear_vault:
        _clear_vault()
        st.success("Vault cleared.")
        st.rerun()

    st.divider()
    st.header("Groq settings")
    backend_url = st.text_input(
        "FastAPI convert endpoint",
        key="backend_url",
        help="The app will try this FastAPI endpoint first, then fall back to local conversion if it cannot reach it.",
    )
    groq_api_key = st.text_input(
        "Paste your Groq API key here",
        type="password",
        key="groq_api_key",
        help="For local testing, paste the key here. For deployment, use the GROQ_API_KEY environment variable or Streamlit secrets.",
    )
    model = st.text_input(
        "Groq model",
        key="model",
        help="You can change this if you want a different Groq-supported model.",
    )
    github_token = st.text_input(
        "GitHub token",
        type="password",
        key="github_token",
        help="Optional. Needed only for private GitHub repos. Keep it in an env var or Streamlit secrets, not in code.",
    )

    st.divider()
    st.header("Databricks push")
    databricks_workspace_url = st.text_input(
        "Databricks workspace URL",
        key="databricks_workspace_url",
        placeholder="https://adb-1234567890123456.7.azuredatabricks.net",
        help="Paste the workspace base URL. The app will push the current output directly into Databricks.",
    )
    databricks_token = st.text_input(
        "Databricks token",
        type="password",
        key="databricks_token",
        help="Personal access token used to import the notebook or file into your workspace.",
    )
    databricks_target_path = st.text_input(
        "Databricks target path",
        key="databricks_target_path",
        help="Enter a folder or a full file path. If you enter a folder, the app will add a file name automatically.",
    )
    databricks_export_mode = st.selectbox(
        "Publish as",
        ["Python notebook (.py)", "Markdown file (.md)"],
        help="Choose whether to import the generated code as a Databricks notebook or a markdown file.",
    )
    databricks_overwrite = st.checkbox("Overwrite existing path", value=True)
    databricks_root_path = st.text_input(
        "Databricks workspace root",
        key="databricks_root_path",
        help="Use a workspace path like /Users or /Workspace/Users. The app will browse from there.",
    )
    databricks_volume_prefix = st.text_input(
        "Databricks volume prefix",
        key="databricks_volume_prefix",
        help="Use the exact Unity Catalog volume prefix, for example /Volumes/main/default/my_volume. The app will rewrite file paths to this prefix.",
    )
    load_databricks = st.button("Load Databricks workspace", use_container_width=True)
    if load_databricks:
        if not databricks_workspace_url.strip() or not databricks_token.strip():
            st.error("Please provide both the Databricks workspace URL and token.")
        else:
            try:
                folders, notebooks, scanned_root = _load_databricks_tree(
                    databricks_workspace_url,
                    databricks_token,
                    databricks_root_path,
                )
                st.session_state["databricks_tree"] = {
                    "folders": folders,
                    "notebooks": notebooks,
                    "workspace_url": databricks_workspace_url.strip(),
                    "token": databricks_token.strip(),
                    "root_path": scanned_root,
                }
                st.success(f"Loaded {len(notebooks)} notebooks and {len(folders)} folders from Databricks.")
            except Exception as exc:
                st.error(f"Could not load Databricks workspace: {exc}")
                st.session_state.pop("databricks_tree", None)
    

st.subheader("Input source")
source_mode = st.radio("Choose how to load the `.item` file", ["Local file", "GitHub profile"], horizontal=True)

uploaded = None
uploaded_properties = None
github_batch_results = st.session_state.get("github_batch_results")
if source_mode == "Local file":
    uploaded_files = st.file_uploader(
        "Upload `.item` and optional `.properties` files",
        type=["item", "xml", "properties"],
        accept_multiple_files=True,
    )
    uploaded, uploaded_properties = _split_local_uploads(uploaded_files)
    if uploaded is not None:
        st.session_state.pop("github_batch_results", None)
        st.session_state.pop("active_batch_preview", None)
else:
    github_profile_url = st.text_input(
        "GitHub profile link",
        placeholder="https://github.com/<username>",
        key="github_profile_url",
        help="Paste your GitHub profile link. The app will show repo, branch, and job dropdowns next.",
    )
    github_username = _parse_github_profile_url(github_profile_url) if github_profile_url.strip() else None
    selected_repo = None
    selected_branch = None
    if github_username:
        st.markdown(f"**GitHub Profile:** `{github_username}`")
        try:
            repos = _fetch_github_repositories(github_username, github_token or None)
        except Exception as exc:
            st.error(f"Could not load repositories for {github_username}: {exc}")
            repos = []

        if repos:
            selected_repo = st.selectbox("Repository Name", repos, index=0)
            repo_owner, repo_name = _split_github_repo_reference(selected_repo, github_username)
            try:
                branches = _fetch_github_branches(repo_owner, repo_name, github_token or None)
            except Exception as exc:
                st.error(f"Could not load branches for {selected_repo}: {exc}")
                branches = []

            if branches:
                selected_branch = st.selectbox("Branch Name", branches, index=0)
                try:
                    jobs = _fetch_github_item_paths(repo_owner, repo_name, selected_branch, github_token or None)
                except Exception as exc:
                    st.error(f"Could not load `.item` files for {selected_repo}@{selected_branch}: {exc}")
                    jobs = []

                if jobs:
                    st.markdown("**Select jobs to convert**")
                    action_left, action_right, action_spacer = st.columns([1, 1, 2])
                    job_keys = [
                        _job_checkbox_key(repo_owner, repo_name, selected_branch, idx)
                        for idx in range(len(jobs))
                    ]
                    with action_left:
                        select_all = st.button("Select all", use_container_width=True)
                    with action_right:
                        clear_all = st.button("Clear all", use_container_width=True)
                    if select_all:
                        for key in job_keys:
                            st.session_state[key] = True
                        st.rerun()
                    if clear_all:
                        for key in job_keys:
                            st.session_state[key] = False
                        st.rerun()

                    selected_jobs: list[str] = []
                    job_columns = st.columns(3)
                    for idx, job in enumerate(jobs):
                        column = job_columns[idx % 3]
                        checkbox_label = Path(job).name or job
                        with column:
                            if st.checkbox(checkbox_label, key=job_keys[idx]):
                                st.caption(job)
                                selected_jobs.append(job)
                            else:
                                st.caption(job)
                    st.caption(f"{len(selected_jobs)} of {len(jobs)} jobs selected")

                    convert_selected_jobs = st.button(
                        "Convert selected jobs",
                        use_container_width=True,
                        disabled=not selected_jobs,
                    )
                    if convert_selected_jobs:
                        batch_results = []
                        branch_tree = []
                        try:
                            branch_tree = _fetch_github_branch_tree(repo_owner, repo_name, selected_branch, github_token or None)
                        except Exception:
                            branch_tree = []

                        progress = st.progress(0, text="Converting selected jobs...")
                        for idx, selected_job in enumerate(selected_jobs, start=1):
                            try:
                                github_source_name, github_source_text = _fetch_github_item(
                                    f"https://github.com/{repo_owner}/{repo_name}/blob/{selected_branch}/{selected_job}",
                                    github_token=github_token,
                                )
                                github_properties_name = None
                                github_properties_text = None
                                try:
                                    matched_properties = _match_properties_path(selected_job, branch_tree)
                                    if matched_properties:
                                        github_properties_name, github_properties_text = _fetch_github_item(
                                            f"https://github.com/{repo_owner}/{repo_name}/blob/{selected_branch}/{matched_properties}",
                                            github_token=github_token,
                                        )
                                except Exception:
                                    github_properties_name = None
                                    github_properties_text = None

                                converted = _convert_source_text(
                                    github_source_name,
                                    github_source_text,
                                    backend_url,
                                    groq_api_key,
                                    model,
                                    properties_text=github_properties_text,
                                    properties_name=github_properties_name,
                                    volume_prefix=databricks_volume_prefix,
                                )
                                converted.update(
                                    {
                                        "repo": selected_repo,
                                        "branch": selected_branch,
                                        "job": selected_job,
                                        "source_url": f"https://github.com/{repo_owner}/{repo_name}/blob/{selected_branch}/{selected_job}",
                                        "preview_label": f"{selected_job}",
                                    }
                                )
                                batch_results.append(converted)
                            except Exception as exc:
                                batch_results.append(
                                    {
                                        "source_name": selected_job,
                                        "properties_name": None,
                                        "xml_preview": "",
                                        "pyspark_preview": "",
                                        "xml_is_valid": False,
                                        "notes": [f"Conversion failed for {selected_job}: {exc}"],
                                        "repo": selected_repo,
                                        "branch": selected_branch,
                                        "job": selected_job,
                                        "source_url": f"https://github.com/{repo_owner}/{repo_name}/blob/{selected_branch}/{selected_job}",
                                        "preview_label": f"{selected_job}",
                                    }
                                )
                            progress.progress(idx / len(selected_jobs), text=f"Converted {idx} of {len(selected_jobs)} jobs")

                        st.session_state["github_batch_results"] = batch_results
                        st.session_state["active_batch_preview"] = batch_results[0]["preview_label"] if batch_results else None
                        st.session_state.pop("conversion_result", None)
                        st.success(f"Converted {len(batch_results)} selected jobs.")
                else:
                    st.info("No `.item` files were found on this branch.")
            else:
                st.info("Choose a repository first, then a branch.")
        else:
            st.info("Enter your GitHub profile link to see repositories.")

col_a, col_b, col_c = st.columns(3)
with col_a:
    convert_pressed = st.button(
        "Convert",
        use_container_width=True,
        disabled=(source_mode != "Local file" or uploaded is None),
    )
with col_b:
    reset_pressed = st.button("Reset", use_container_width=True)
with col_c:
    st.download_button(
        "Download starter template",
        data="Upload a file first.",
        file_name="README.txt",
        use_container_width=True,
    )

if reset_pressed:
    st.session_state.pop("conversion_result", None)
    st.session_state.pop("github_batch_results", None)
    st.session_state.pop("active_batch_preview", None)
    st.session_state.pop("databricks_tree", None)
    st.rerun()

if source_mode == "Local file" and uploaded and (convert_pressed or "conversion_result" not in st.session_state):
    active_properties_name = uploaded_properties[0] if uploaded_properties else None
    active_properties_text = uploaded_properties[1] if uploaded_properties else None
    source_name, raw_text = uploaded
    try:
        converted = _convert_source_text(
            source_name=source_name,
            raw_text=raw_text,
            backend_url=backend_url,
            groq_api_key=groq_api_key,
            model=model,
            properties_text=active_properties_text,
            properties_name=active_properties_name,
            volume_prefix=databricks_volume_prefix,
        )
        st.session_state["conversion_result"] = converted
    except Exception as exc:
        st.error(f"Could not convert the local upload: {exc}")

result = st.session_state.get("conversion_result")
batch_results = [] if source_mode == "Local file" else (st.session_state.get("github_batch_results") or [])
db_tree = st.session_state.get("databricks_tree")

active_result = None
if batch_results:
    labels = [item.get("preview_label") or item["source_name"] for item in batch_results]
    active_label = st.session_state.get("active_batch_preview") or labels[0]
    if hasattr(st, "segmented_control"):
        active_label = st.segmented_control("Converted jobs", options=labels, default=active_label)
    else:
        active_label = st.radio("Converted jobs", options=labels, index=labels.index(active_label) if active_label in labels else 0, horizontal=True)
    st.session_state["active_batch_preview"] = active_label
    active_result = next((item for item in batch_results if (item.get("preview_label") or item["source_name"]) == active_label), batch_results[0])
elif result:
    active_result = result

if active_result:
    if batch_results:
        st.subheader(f"Converted jobs from: {active_result.get('repo', 'GitHub')} / {active_result.get('branch', 'branch')}")
    else:
        st.subheader(f"Loaded file: {active_result['source_name']}")
    st.write(" | ".join(active_result["notes"]))

    st.markdown("### Databricks Push")
    if db_tree:
        st.caption("Pick a folder and notebook from the loaded workspace tree, then overwrite that notebook with one cell per converted job.")
        db_folder = st.selectbox("Workspace folder", db_tree["folders"], index=0)
        notebook_options = [path for path in db_tree["notebooks"] if path.startswith(db_folder)]
        if not notebook_options:
            notebook_options = db_tree["notebooks"]
        db_notebook = st.selectbox("Notebook file", notebook_options, index=0) if notebook_options else None
        push_target_path = db_notebook or f"{db_folder.rstrip('/')}/converted.py"
    else:
        st.caption("Enter the Databricks workspace details in the sidebar and load the workspace tree first.")
        push_target_path = databricks_target_path

    push_pressed = st.button(
        "Push to Databricks",
        use_container_width=True,
        disabled=not (databricks_workspace_url.strip() and databricks_token.strip()),
    )
    st.caption("This sends the current output to your workspace using the Databricks Workspace Import API.")

    if push_pressed:
        try:
            if batch_results:
                export_content, export_format, language, extension = _build_databricks_batch_payload(
                    batch_results,
                    databricks_export_mode,
                )
            else:
                export_content, export_format, language, extension = _build_databricks_payload(
                    active_result,
                    databricks_export_mode,
                )
            target_path = push_target_path
            _push_to_databricks(
                databricks_workspace_url,
                databricks_token,
                target_path,
                export_content,
                export_format,
                language,
                True,
            )
            st.success(f"Pushed to Databricks at {target_path}")
        except Exception as exc:
            st.error(f"Could not push to Databricks: {exc}")

    preview_left, preview_right = st.tabs(["XML Preview", "PySpark Preview"])
    with preview_left:
        st.text_area("XML Preview", value=active_result["xml_preview"], height=420)
        st.download_button(
            "Download XML",
            data=active_result["xml_preview"],
            file_name=_download_name(active_result["source_name"], "xml"),
            mime="application/xml",
            use_container_width=True,
        )
    with preview_right:
        st.text_area("PySpark Preview", value=active_result["pyspark_preview"], height=420)
        st.download_button(
            "Download PySpark",
            data=active_result["pyspark_preview"],
            file_name=_download_name(active_result["source_name"], "py"),
            mime="text/x-python",
            use_container_width=True,
        )
else:
    st.info("Upload a Talend `.item` file or load one from GitHub to see the XML and PySpark previews.")

