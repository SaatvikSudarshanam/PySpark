from __future__ import annotations

import base64
import os
import re
from pathlib import Path

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
    headers = {"Accept": "application/vnd.github+json"}
    if token and token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    return headers


def _get_secret(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


def _parse_github_profile_url(url: str) -> str | None:
    cleaned = url.strip().rstrip("/")
    match = re.match(r"^https?://github\.com/([^/]+)$", cleaned)
    if match:
        return match.group(1)
    return None


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
    response = requests.get(
        f"https://api.github.com/users/{username}/repos?per_page=100&sort=updated&type=owner",
        headers=_github_headers(token),
        timeout=60,
    )
    if response.status_code == 404:
        response = requests.get(
            f"https://api.github.com/orgs/{username}/repos?per_page=100&sort=updated&type=owner",
            headers=_github_headers(token),
            timeout=60,
        )
    response.raise_for_status()
    return [item["name"] for item in response.json()]


@st.cache_data(show_spinner=False, ttl=600)
def _fetch_github_branches(owner: str, repo: str, token: str | None = None) -> list[str]:
    response = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/branches",
        headers=_github_headers(token),
        timeout=60,
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
    response = requests.post(endpoint, files=files, data=data, timeout=120)
    response.raise_for_status()
    return response.json()


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

with st.sidebar:
    st.header("Groq settings")
    backend_url = st.text_input(
        "FastAPI convert endpoint",
        value=os.getenv("BACKEND_URL", "http://localhost:8000/convert"),
        help="The app will try this FastAPI endpoint first, then fall back to local conversion if it cannot reach it.",
    )
    groq_api_key = st.text_input(
        "Paste your Groq API key here",
        type="password",
        value=os.getenv("GROQ_API_KEY", ""),
        help="For local testing, paste the key here. For deployment, use the GROQ_API_KEY environment variable or Streamlit secrets.",
    )
    model = st.text_input(
        "Groq model",
        value=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
        help="You can change this if you want a different Groq-supported model.",
    )
    github_token = st.text_input(
        "GitHub token",
        type="password",
        value=os.getenv("GITHUB_TOKEN", _get_secret("GITHUB_TOKEN", "")),
        help="Optional. Needed only for private GitHub repos. Keep it in an env var or Streamlit secrets, not in code.",
    )

    st.divider()
    st.header("Databricks push")
    databricks_workspace_url = st.text_input(
        "Databricks workspace URL",
        value=os.getenv("DATABRICKS_WORKSPACE_URL", ""),
        placeholder="https://adb-1234567890123456.7.azuredatabricks.net",
        help="Paste the workspace base URL. The app will push the current output directly into Databricks.",
    )
    databricks_token = st.text_input(
        "Databricks token",
        type="password",
        value=os.getenv("DATABRICKS_TOKEN", _get_secret("DATABRICKS_TOKEN", "")),
        help="Personal access token used to import the notebook or file into your workspace.",
    )
    databricks_target_path = st.text_input(
        "Databricks target path",
        value=os.getenv("DATABRICKS_TARGET_PATH", "/Shared/talend-conversion"),
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
        value=os.getenv("DATABRICKS_ROOT_PATH", "/Users"),
        help="Use a workspace path like /Users or /Workspace/Users. The app will browse from there.",
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
github_loaded = st.session_state.get("github_loaded")
if source_mode == "Local file":
    uploaded_files = st.file_uploader(
        "Upload `.item` and optional `.properties` files",
        type=["item", "xml", "properties"],
        accept_multiple_files=True,
    )
    uploaded, uploaded_properties = _split_local_uploads(uploaded_files)
    if uploaded is not None:
        st.session_state.pop("github_loaded", None)
        github_loaded = None
else:
    github_profile_url = st.text_input(
        "GitHub profile link",
        placeholder="https://github.com/<username>",
        help="Paste your GitHub profile link. The app will show repo, branch, and job dropdowns next.",
    )
    github_username = _parse_github_profile_url(github_profile_url) if github_profile_url.strip() else None
    selected_repo = None
    selected_branch = None
    selected_job = None

    if github_username:
        st.markdown(f"**GitHub Profile:** `{github_username}`")
        try:
            repos = _fetch_github_repositories(github_username, github_token or None)
        except Exception as exc:
            st.error(f"Could not load repositories for {github_username}: {exc}")
            repos = []

        if repos:
            selected_repo = st.selectbox("Repository Name", repos, index=0)
            try:
                branches = _fetch_github_branches(github_username, selected_repo, github_token or None)
            except Exception as exc:
                st.error(f"Could not load branches for {selected_repo}: {exc}")
                branches = []

            if branches:
                selected_branch = st.selectbox("Branch Name", branches, index=0)
                try:
                    jobs = _fetch_github_item_paths(github_username, selected_repo, selected_branch, github_token or None)
                except Exception as exc:
                    st.error(f"Could not load `.item` files for {selected_repo}@{selected_branch}: {exc}")
                    jobs = []

                if jobs:
                    selected_job = st.selectbox("Job Name", jobs, index=0)
                    load_github = st.button("Load selected job", use_container_width=True)
                    if load_github:
                        try:
                            github_source_name, github_source_text = _fetch_github_item(
                                f"https://github.com/{github_username}/{selected_repo}/blob/{selected_branch}/{selected_job}",
                                github_token=github_token,
                            )
                            github_properties_name = None
                            github_properties_text = None
                            try:
                                branch_tree = _fetch_github_branch_tree(github_username, selected_repo, selected_branch, github_token or None)
                                matched_properties = _match_properties_path(selected_job, branch_tree)
                                if matched_properties:
                                    github_properties_name, github_properties_text = _fetch_github_item(
                                        f"https://github.com/{github_username}/{selected_repo}/blob/{selected_branch}/{matched_properties}",
                                        github_token=github_token,
                                    )
                            except Exception:
                                github_properties_name = None
                                github_properties_text = None
                            github_loaded = {
                                "source_name": github_source_name,
                                "source_text": github_source_text,
                                "properties_name": github_properties_name,
                                "properties_text": github_properties_text,
                                "source_url": f"https://github.com/{github_username}/{selected_repo}/blob/{selected_branch}/{selected_job}",
                                "repo": selected_repo,
                                "branch": selected_branch,
                                "job": selected_job,
                            }
                            st.session_state["github_loaded"] = github_loaded
                            st.success(f"Loaded {github_source_name} from GitHub.")
                            if github_properties_name:
                                st.info(f"Matched properties file: {github_properties_name}")
                        except Exception as exc:
                            st.error(f"Could not load the selected job: {exc}")
                else:
                    st.info("No `.item` files were found on this branch.")
            else:
                st.info("Choose a repository first, then a branch.")
        else:
            st.info("Enter your GitHub profile link to see repositories.")

    if github_loaded:
        st.info(
            f"Loaded from GitHub: {github_loaded['repo']} | {github_loaded['branch']} | {github_loaded['job']}"
        )
        if github_loaded.get("properties_name"):
            st.caption(f"Matched properties: {github_loaded['properties_name']}")

col_a, col_b, col_c = st.columns(3)
with col_a:
    convert_pressed = st.button(
        "Convert",
        use_container_width=True,
        disabled=(uploaded is None and github_loaded is None),
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
    st.session_state.pop("github_loaded", None)
    st.session_state.pop("databricks_tree", None)
    st.rerun()

if (uploaded or github_loaded) and (convert_pressed or "conversion_result" not in st.session_state):
    backend_result = None
    active_properties_name = uploaded_properties[0] if uploaded_properties else github_loaded.get("properties_name") if github_loaded else None
    active_properties_text = uploaded_properties[1] if uploaded_properties else github_loaded.get("properties_text") if github_loaded else None
    try:
        if uploaded:
            backend_result = _convert_via_backend(
                uploaded,
                backend_url,
                groq_api_key,
                model,
                properties_text=active_properties_text,
                properties_name=active_properties_name,
            )
        else:
            memory_upload = _MemoryUpload(github_loaded["source_name"], github_loaded["source_text"])
            backend_result = _convert_via_backend(
                memory_upload,
                backend_url,
                groq_api_key,
                model,
                properties_text=active_properties_text,
                properties_name=active_properties_name,
            )
    except Exception as exc:
        st.warning(f"FastAPI endpoint was not reachable, so Streamlit will convert locally instead. {exc}")

    if backend_result:
        st.session_state["conversion_result"] = backend_result
    else:
        raw_text = uploaded.getvalue().decode("utf-8", errors="ignore") if uploaded else github_loaded["source_text"]
        source_name = uploaded.name if uploaded else github_loaded["source_name"]
        local = convert_item_text(
            raw_text=raw_text,
            source_name=source_name,
            properties_text=active_properties_text,
            properties_name=active_properties_name,
            groq_api_key=groq_api_key.strip() or None,
            model=model.strip() or "openai/gpt-oss-20b",
        )
        st.session_state["conversion_result"] = _as_result_dict(local)

result = st.session_state.get("conversion_result")
db_tree = st.session_state.get("databricks_tree")

if result:
    st.subheader(f"Loaded file: {result['source_name']}")
    st.write(" | ".join(result["notes"]))

    st.markdown("### Databricks Push")
    if db_tree:
        st.caption("Pick a folder and notebook from the loaded workspace tree, then push the current PySpark preview there.")
        db_folder = st.selectbox("Workspace folder", db_tree["folders"], index=0)
        notebook_options = [path for path in db_tree["notebooks"] if path.startswith(db_folder)]
        if not notebook_options:
            notebook_options = db_tree["notebooks"]
        db_notebook = st.selectbox("Notebook file", notebook_options, index=0) if notebook_options else None
        new_db_notebook_name = st.text_input(
            "New notebook name",
            value=f"{Path(result['source_name']).stem}.py",
            help="Used only if you want to create a new notebook path inside the selected folder.",
        )
        push_target_path = db_notebook or f"{db_folder.rstrip('/')}/{new_db_notebook_name.strip() or 'converted.py'}"
    else:
        st.caption("Enter the Databricks workspace details in the sidebar and load the workspace tree first.")
        push_target_path = _resolve_databricks_target_path(
            databricks_target_path,
            result["source_name"],
            ".py" if databricks_export_mode == "Python notebook (.py)" else ".md",
        )

    push_pressed = st.button(
        "Push to Databricks",
        use_container_width=True,
        disabled=not (databricks_workspace_url.strip() and databricks_token.strip()),
    )
    st.caption("This sends the current output to your workspace using the Databricks Workspace Import API.")

    if push_pressed:
        try:
            export_content, export_format, language, extension = _build_databricks_payload(
                result,
                databricks_export_mode,
            )
            target_path = push_target_path if db_tree else _resolve_databricks_target_path(
                push_target_path,
                result["source_name"],
                extension,
            )
            _push_to_databricks(
                databricks_workspace_url,
                databricks_token,
                target_path,
                export_content,
                export_format,
                language,
                databricks_overwrite,
            )
            st.success(f"Pushed to Databricks at {target_path}")
        except Exception as exc:
            st.error(f"Could not push to Databricks: {exc}")

    preview_left, preview_right = st.tabs(["XML Preview", "PySpark Preview"])
    with preview_left:
        st.text_area("XML Preview", value=result["xml_preview"], height=420)
        st.download_button(
            "Download XML",
            data=result["xml_preview"],
            file_name=_download_name(result["source_name"], "xml"),
            mime="application/xml",
            use_container_width=True,
        )
    with preview_right:
        st.text_area("PySpark Preview", value=result["pyspark_preview"], height=420)
        st.download_button(
            "Download PySpark",
            data=result["pyspark_preview"],
            file_name=_download_name(result["source_name"], "py"),
            mime="text/x-python",
            use_container_width=True,
        )
else:
    st.info("Upload a Talend `.item` file or load one from GitHub to see the XML and PySpark previews.")

