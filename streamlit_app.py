from __future__ import annotations

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


def _fetch_github_item(url: str) -> tuple[str, str]:
    raw_url = _normalize_github_raw_url(url)
    response = requests.get(raw_url, timeout=120)
    response.raise_for_status()
    filename = Path(raw_url).name or "github-item.item"
    return filename, response.text


def _convert_via_backend(uploaded_file, backend_url: str, groq_api_key: str, model: str):
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
    response = requests.post(endpoint, files=files, data=data, timeout=120)
    response.raise_for_status()
    return response.json()


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
    

st.subheader("Input source")
source_mode = st.radio("Choose how to load the `.item` file", ["Local file", "GitHub URL"], horizontal=True)

uploaded = None
github_loaded = st.session_state.get("github_loaded")
if source_mode == "Local file":
    uploaded = st.file_uploader("Upload a `.item` file", type=["item", "xml"])
    if uploaded is not None:
        st.session_state.pop("github_loaded", None)
        github_loaded = None
else:
    github_url = st.text_input(
        "GitHub file URL",
        placeholder="https://github.com/<owner>/<repo>/blob/<branch>/path/to/file.item",
        help="Paste a GitHub blob URL or raw.githubusercontent.com URL for a .item file.",
    )
    load_github = st.button("Load from GitHub", use_container_width=True, disabled=not github_url.strip())
    if load_github and github_url.strip():
        try:
            github_source_name, github_source_text = _fetch_github_item(github_url)
            github_loaded = {
                "source_name": github_source_name,
                "source_text": github_source_text,
                "source_url": github_url.strip(),
            }
            st.session_state["github_loaded"] = github_loaded
            st.success(f"Loaded {github_source_name} from GitHub.")
        except Exception as exc:
            st.error(f"Could not load the GitHub file: {exc}")

    if github_loaded:
        st.info(f"Loaded from GitHub: {github_loaded['source_name']}")

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
    st.rerun()

if (uploaded or github_loaded) and (convert_pressed or "conversion_result" not in st.session_state):
    backend_result = None
    try:
        if uploaded:
            backend_result = _convert_via_backend(uploaded, backend_url, groq_api_key, model)
        else:
            memory_upload = _MemoryUpload(github_loaded["source_name"], github_loaded["source_text"])
            backend_result = _convert_via_backend(memory_upload, backend_url, groq_api_key, model)
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
            groq_api_key=groq_api_key.strip() or None,
            model=model.strip() or "openai/gpt-oss-20b",
        )
        st.session_state["conversion_result"] = _as_result_dict(local)

result = st.session_state.get("conversion_result")

if result:
    st.subheader(f"Loaded file: {result['source_name']}")
    st.write(" | ".join(result["notes"]))

    left, right = st.columns(2)
    with left:
        st.markdown("### XML Preview")
        st.code(result["xml_preview"], language="xml")
        st.download_button(
            "Download XML",
            data=result["xml_preview"],
            file_name=_download_name(result["source_name"], "xml"),
            mime="application/xml",
            use_container_width=True,
        )
    with right:
        st.markdown("### PySpark Preview")
        st.code(result["pyspark_preview"], language="python")
        st.download_button(
            "Download PySpark",
            data=result["pyspark_preview"],
            file_name=_download_name(result["source_name"], "py"),
            mime="text/x-python",
            use_container_width=True,
        )
else:
    st.info("Upload a Talend `.item` file or load one from GitHub to see the XML and PySpark previews.")

