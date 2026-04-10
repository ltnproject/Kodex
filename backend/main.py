"""
DrkCode Improver — FastAPI Backend
===================================
Endpoints:
  POST /api/search   — search GitHub for repos matching a keyword
  POST /api/fetch    — clone repo + find + return a specific file
  POST /api/improve  — send file content to Groq LLM for improvement
  GET  /api/download/{job_id} — return the improved file as a download
  GET  /api/logs     — return recent request log entries (in-memory)

Security:
  - No arbitrary code execution — only git clone + file read
  - All clones go into a per-request tempdir, deleted after use
  - File content is capped at 50 KB before sending to LLM
  - Sandbox: only .py .js .ts .html .css .json .md .txt extensions allowed
"""

import os
import shutil
import tempfile
import subprocess
import uuid
import time
import re
from pathlib import Path
from datetime import datetime
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, JSONResponse
from pydantic import BaseModel

# ── App setup ────────────────────────────────────────────────────────────────
app = FastAPI(title="DrkCode Improver API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Config ───────────────────────────────────────────────────────────────────
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
GROQ_BASE      = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL     = "llama-3.3-70b-versatile"
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")   # optional, raises rate limit
GITHUB_SEARCH  = "https://api.github.com/search/repositories"
GITHUB_CONTENT = "https://api.github.com/repos/{owner}/{repo}/contents/{path}"

# Only these extensions are allowed — prevents binary/executable handling
ALLOWED_EXT = {".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css",
               ".json", ".md", ".txt", ".yaml", ".yml", ".toml", ".sh"}

MAX_FILE_BYTES = 50_000   # 50 KB cap sent to LLM
MAX_CLONE_SEC  = 30       # git clone timeout

# ── In-memory job store & log ─────────────────────────────────────────────────
# { job_id: { "content": str, "filename": str, "created": float } }
job_store: dict[str, dict] = {}

# Simple ring-buffer log (last 100 entries)
request_log: list[dict] = []

def log_entry(action: str, detail: str, status: str = "ok"):
    request_log.append({
        "ts": datetime.utcnow().isoformat(),
        "action": action,
        "detail": detail,
        "status": status,
    })
    if len(request_log) > 100:
        request_log.pop(0)

# ── Pydantic models ───────────────────────────────────────────────────────────
class SearchRequest(BaseModel):
    keyword: str

class FetchRequest(BaseModel):
    repo_full_name: str   # e.g. "torvalds/linux"
    filename: str         # e.g. "README.md" or just "README"

class ImproveRequest(BaseModel):
    job_id: str           # id returned by /api/fetch
    instructions: Optional[str] = None   # extra user instructions for LLM

# ── Helpers ───────────────────────────────────────────────────────────────────
def gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h

def is_safe_extension(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXT

def cleanup_tempdir(path: str):
    """Delete a temp directory — called in background after response sent."""
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass

def find_file_in_dir(root: Path, filename: str) -> Optional[Path]:
    """
    Walk the cloned repo and find the first file whose name matches
    (case-insensitive, partial match allowed — e.g. 'main' matches 'main.py').
    """
    filename_lower = filename.lower()
    for f in root.rglob("*"):
        if f.is_file():
            name = f.name.lower()
            stem = f.stem.lower()
            if name == filename_lower or stem == filename_lower:
                return f
    return None

async def call_groq(system: str, user: str) -> str:
    """Send a chat completion request to Groq and return the text."""
    if not GROQ_API_KEY:
        raise HTTPException(500, "GROQ_API_KEY not configured on server.")
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            GROQ_BASE,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                "max_tokens": 4096,
                "temperature": 0.3,
            },
        )
        if r.status_code != 200:
            raise HTTPException(502, f"Groq error: {r.text[:300]}")
        return r.json()["choices"][0]["message"]["content"]

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "DrkCode Improver API running"}


@app.post("/api/search")
async def search_repos(body: SearchRequest):
    """
    Step 1 — Search GitHub for repos matching the keyword.
    Returns top 10 results with name, description, stars, URL.
    """
    keyword = body.keyword.strip()
    if not keyword or len(keyword) > 100:
        raise HTTPException(400, "Invalid keyword.")

    log_entry("search", keyword)

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            GITHUB_SEARCH,
            headers=gh_headers(),
            params={"q": keyword, "sort": "stars", "per_page": 10},
        )

    if r.status_code == 403:
        raise HTTPException(429, "GitHub rate limit hit. Add GITHUB_TOKEN env var.")
    if r.status_code != 200:
        raise HTTPException(502, f"GitHub API error: {r.status_code}")

    items = r.json().get("items", [])
    results = [
        {
            "full_name":    i["full_name"],
            "description":  i.get("description", ""),
            "stars":        i["stargazers_count"],
            "language":     i.get("language", ""),
            "url":          i["html_url"],
            "default_branch": i.get("default_branch", "main"),
        }
        for i in items
    ]
    return {"results": results}


@app.post("/api/fetch")
async def fetch_file(body: FetchRequest, background_tasks: BackgroundTasks):
    """
    Step 2 — Clone the repo into a temp dir, find the requested file,
    read it, store in job_store, schedule cleanup, return job_id.

    Security:
      - Only ALLOWED_EXT files are accepted
      - Clone is sandboxed in a random tmpdir
      - Subprocess has a 30-second timeout
      - No code is executed — only read
    """
    repo = body.repo_full_name.strip()
    filename = body.filename.strip()

    # Validate repo name format (owner/repo)
    if not re.match(r'^[\w.\-]+/[\w.\-]+$', repo):
        raise HTTPException(400, "Invalid repo name. Use format: owner/repo")

    log_entry("fetch", f"{repo} → {filename}")

    # Create isolated temp directory for this request
    tmpdir = tempfile.mkdtemp(prefix="drk_")

    try:
        clone_url = f"https://github.com/{repo}.git"

        # Clone — shallow (depth=1) to keep it fast and small
        result = subprocess.run(
            ["git", "clone", "--depth=1", "--single-branch", clone_url, tmpdir],
            capture_output=True,
            text=True,
            timeout=MAX_CLONE_SEC,
        )

        if result.returncode != 0:
            raise HTTPException(502, f"Git clone failed: {result.stderr[:200]}")

        # Find the requested file anywhere in the cloned repo
        found = find_file_in_dir(Path(tmpdir), filename)

        if not found:
            raise HTTPException(404, f"File '{filename}' not found in {repo}")

        # Security: only allow safe extensions
        if not is_safe_extension(found.name):
            raise HTTPException(400, f"File type '{found.suffix}' is not allowed.")

        # Read content — cap at MAX_FILE_BYTES
        raw = found.read_bytes()
        if len(raw) > MAX_FILE_BYTES:
            raw = raw[:MAX_FILE_BYTES]

        try:
            content = raw.decode("utf-8", errors="replace")
        except Exception:
            raise HTTPException(400, "File does not appear to be text.")

        # Store job
        job_id = str(uuid.uuid4())
        job_store[job_id] = {
            "original":  content,
            "improved":  None,
            "filename":  found.name,
            "repo":      repo,
            "created":   time.time(),
        }

        # Schedule temp directory cleanup (runs after response is sent)
        background_tasks.add_task(cleanup_tempdir, tmpdir)

        return {
            "job_id":   job_id,
            "filename": found.name,
            "lines":    content.count("\n") + 1,
            "size":     len(content),
            "preview":  content[:500],   # first 500 chars as preview
        }

    except subprocess.TimeoutExpired:
        background_tasks.add_task(cleanup_tempdir, tmpdir)
        raise HTTPException(504, "Git clone timed out.")
    except HTTPException:
        background_tasks.add_task(cleanup_tempdir, tmpdir)
        raise
    except Exception as e:
        background_tasks.add_task(cleanup_tempdir, tmpdir)
        raise HTTPException(500, str(e))


@app.post("/api/improve")
async def improve_file(body: ImproveRequest):
    """
    Step 3 — Send the fetched file to Groq LLM for improvement.
    Stores improved version in job_store under same job_id.

    The LLM is asked to:
      - Fix bugs and anti-patterns
      - Add type hints / JSDoc
      - Improve readability
      - Add/update comments
      - Follow language best practices
    """
    job = job_store.get(body.job_id)
    if not job:
        raise HTTPException(404, "Job not found. Please fetch a file first.")

    filename = job["filename"]
    original = job["original"]
    extra    = body.instructions or ""

    log_entry("improve", f"{job['repo']} / {filename}")

    ext = Path(filename).suffix.lower()
    lang_hint = {
        ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
        ".jsx": "React JSX", ".tsx": "React TSX", ".html": "HTML",
        ".css": "CSS", ".json": "JSON", ".md": "Markdown",
        ".yaml": ".yml", ".yml": "YAML", ".sh": "Bash",
    }.get(ext, "code")

    system_prompt = f"""You are an expert {lang_hint} code reviewer and improver.
Your job is to take the provided file and return an improved version.

Rules:
1. Fix any bugs, errors, or anti-patterns you find.
2. Add or improve comments and docstrings explaining what code does.
3. Add type hints (Python) or JSDoc (JS/TS) where missing.
4. Improve variable/function naming where unclear.
5. Follow {lang_hint} best practices and modern conventions.
6. Do NOT change the overall logic or behavior of the code.
7. Return ONLY the improved file content — no explanation, no markdown fences.
   Just the raw improved file, ready to save and use.

{f'Extra instructions from user: {extra}' if extra else ''}"""

    user_prompt = f"File: {filename}\n\n{original}"

    improved = await call_groq(system_prompt, user_prompt)

    # Strip accidental markdown fences if LLM adds them
    improved = re.sub(r'^```[\w]*\n?', '', improved, flags=re.MULTILINE)
    improved = re.sub(r'\n?```$', '', improved, flags=re.MULTILINE)

    job_store[body.job_id]["improved"] = improved.strip()

    # Auto-expire jobs older than 1 hour to keep memory clean
    now = time.time()
    expired = [k for k, v in job_store.items() if now - v["created"] > 3600]
    for k in expired:
        del job_store[k]

    return {
        "job_id":   body.job_id,
        "filename": filename,
        "improved": improved.strip(),
        "lines":    improved.count("\n") + 1,
    }


@app.get("/api/download/{job_id}")
async def download_file(job_id: str):
    """
    Step 4 — Return the improved file as a plain-text download.
    The browser receives it with Content-Disposition: attachment.
    """
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found or expired.")
    if not job["improved"]:
        raise HTTPException(400, "File has not been improved yet. Call /api/improve first.")

    filename = f"improved_{job['filename']}"
    return PlainTextResponse(
        content=job["improved"],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/logs")
async def get_logs():
    """Return recent action log (last 100 entries). For debugging."""
    return {"logs": list(reversed(request_log))}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    """Manually delete a job from memory."""
    if job_id in job_store:
        del job_store[job_id]
        return {"deleted": job_id}
    raise HTTPException(404, "Job not found.")
