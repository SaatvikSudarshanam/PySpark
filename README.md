# Talend .item Converter

This project has two parts:

- `talend_converter/api.py` for the FastAPI backend
- `streamlit_app.py` for the Streamlit frontend

## Install

```bash
python -m pip install -r requirements.txt
```

## Run the backend

Use this command in your current shell:

```bash
python -m uvicorn talend_converter.api:app --reload
```

If you want a different host or port:

```bash
python -m uvicorn talend_converter.api:app --reload --host 127.0.0.1 --port 8000
```

## Run the frontend

In a second terminal:

```bash
streamlit run streamlit_app.py
```

## Where to paste the Groq API key

You can paste the key directly into the Streamlit sidebar field labeled `Paste your Groq API key here`.

Other options:

- Set `GROQ_API_KEY` in your shell
- Put it in `.streamlit/secrets.toml` as `GROQ_API_KEY="your_key"`

## Private GitHub repos

The GitHub loader can fetch from private repos if you provide a GitHub personal access token with access to that repo.

Other options:

- Paste the token into the Streamlit sidebar field labeled `GitHub token`
- Set `GITHUB_TOKEN` in your shell
- Put it in `.streamlit/secrets.toml` as `GITHUB_TOKEN="your_token"`

Use a token that can read the private repository. Do not commit the token to the repo.

## Databricks push

You can push the generated output into a Databricks workspace from the sidebar.

Other options:

- Set `DATABRICKS_WORKSPACE_URL` in your shell
- Set `DATABRICKS_TOKEN` in your shell
- Set `DATABRICKS_TARGET_PATH` if you want a default workspace folder or file path

The `Publish as` dropdown lets you choose whether to import the output as:

- a Python notebook (`.py`)
- a markdown file (`.md`)

After you provide the workspace URL and token, load the workspace tree and choose:

- Workspace folder
- Notebook file

The XML and PySpark previews are shown in scrollable tabs, so the push controls stay easy to reach.

## GitHub import

The app can also load a `.item` file from a GitHub profile by choosing:

- Repository name
- Branch name
- Job name

Paste the GitHub profile link first, then pick a project name, then pick a branch, then pick the `.item` job from the dropdown.

Example:

```text
https://github.com/<username>
```

If a matching `.properties` file exists next to the `.item`, the app loads it automatically and passes its values into the converter.

## Notes

- The app will try the FastAPI backend first and fall back to local conversion if the backend is not running.
- XML preview and PySpark preview each have download buttons.
