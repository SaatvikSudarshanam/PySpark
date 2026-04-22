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

## GitHub import

The app can also load a `.item` file from GitHub using a GitHub blob URL or a raw GitHub URL.

Example:

```text
https://github.com/<owner>/<repo>/blob/<branch>/path/to/file.item
```

## Notes

- The app will try the FastAPI backend first and fall back to local conversion if the backend is not running.
- XML preview and PySpark preview each have download buttons.
