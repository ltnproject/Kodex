# DrkCode Improver

> GitHub → AI Code Improvement → Download  
> Built with FastAPI + Groq LLaMA 3.3 + Vanilla JS

---

## Architecture

```
Browser (index.html)
  │
  ├─ POST /api/search   → GitHub Search API → returns repo list
  ├─ POST /api/fetch    → git clone (tmpdir) → find file → read → delete tmpdir
  ├─ POST /api/improve  → Groq LLM → returns improved code
  └─ GET  /api/download/{job_id} → file download
```

---

## Quickstart (Local)

```bash
# 1. Clone / copy files
cd backend/

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set environment variables
export GROQ_API_KEY=gsk_your_key_here
export GITHUB_TOKEN=ghp_optional_raises_rate_limit

# 4. Run
uvicorn main:app --reload --port 8000

# 5. Open frontend
# Open frontend/index.html in browser
# Set API = "http://localhost:8000" (already default for localhost)
```

---

## Deploy to Render (Free)

1. Push `backend/` folder to GitHub
2. Go to render.com → New Web Service
3. Connect your repo
4. Set:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Add environment variables:
   - `GROQ_API_KEY` = your key
   - `GITHUB_TOKEN` = optional
6. Deploy
7. In `frontend/index.html` change `const API = 'https://your-app.onrender.com'`

---

## Deploy to Railway

```bash
railway login
railway init
railway up
railway variables set GROQ_API_KEY=gsk_...
```

---

## Sample Run

```
User searches: "python web scraper beautifulsoup"

→ GET /api/search?keyword=python+web+scraper
  Returns: [
    { full_name: "scrapy/scrapy", stars: 52000, language: "Python" },
    { full_name: "MechanicalSoup/MechanicalSoup", stars: 4500 },
    ...
  ]

User selects: "MechanicalSoup/MechanicalSoup"
User enters filename: "browser.py"

→ POST /api/fetch { repo_full_name: "MechanicalSoup/MechanicalSoup", filename: "browser.py" }
  Backend:
    1. Creates tmpdir: /tmp/drk_abc123/
    2. Runs: git clone --depth=1 https://github.com/MechanicalSoup/MechanicalSoup.git /tmp/drk_abc123/
    3. Walks directory tree, finds: /tmp/drk_abc123/mechanicalsoup/browser.py
    4. Reads file (< 50KB), stores in job_store with job_id = "uuid-xyz"
    5. Schedules background cleanup of /tmp/drk_abc123/
    6. Returns: { job_id: "uuid-xyz", filename: "browser.py", lines: 184, preview: "..." }

User clicks "Improve with AI"

→ POST /api/improve { job_id: "uuid-xyz", instructions: "add async support" }
  Backend:
    1. Retrieves original content from job_store["uuid-xyz"]
    2. Sends to Groq:
       System: "You are an expert Python code reviewer..."
       User: "File: browser.py\n\n<content>"
    3. Groq returns improved code with:
       - Added type hints to all functions
       - Added docstrings to undocumented methods
       - Fixed deprecated httpx usage
       - Improved error handling
       - Better variable names
    4. Stores improved version in job_store
    5. Returns improved content

User clicks "Download"

→ GET /api/download/uuid-xyz
  Returns: improved_browser.py as file attachment

Temp files: already deleted after /api/fetch completed ✓
```

---

## Security Notes

- **No code execution** — only `git clone` and file read
- **Extension allowlist** — only `.py .js .ts .html .css .json .md .txt .yaml .sh`
- **File size cap** — 50 KB max sent to LLM
- **Clone timeout** — 30 seconds max
- **Repo name validation** — regex ensures `owner/repo` format only
- **Auto job expiry** — jobs deleted from memory after 1 hour
- **Temp dir cleanup** — always deleted in background after fetch

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY` | ✅ Yes | Your Groq API key (free at console.groq.com) |
| `GITHUB_TOKEN` | Optional | Raises GitHub rate limit from 10 to 30 req/min |

---

## File Structure

```
project/
├── backend/
│   ├── main.py           ← FastAPI app (all endpoints)
│   ├── requirements.txt  ← Python dependencies
│   └── Procfile          ← For Railway/Render deployment
└── frontend/
    └── index.html        ← Single-file frontend (self-contained)
```
