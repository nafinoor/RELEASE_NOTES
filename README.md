# Weekly Release Note Generator

Generates weekly release notes by aggregating merged PRs from Frontend and Backend repositories via a Flask API.

## Setup

### 1. Environment Variables

| Variable | Required | Description |
| ------------------------ | -------- | -------------------------------------------------- |
| `GITHUB_TOKEN` | Yes | GitHub token with repo access |
| `FE_REPO` | Yes | Frontend repo (e.g., `organization/frontend`) |
| `BE_REPO` | Yes | Backend repo (e.g., `organization/backend`) |
| `ADMIN_PASSWORD` | Yes | Password for admin API endpoints |
| `LLM_API_KEY` | No | OpenAI API key for AI summarization |
| `LLM_API_URL` | No | LLM API endpoint (defaults to OpenAI) |

### 2. Local Development

```bash
# Create virtual environment
python -m venv .venv

# Activate (Windows)
.venv\Scripts\Activate

# Activate (Linux/Mac)
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run API server
python api_server.py
```

The API runs at `http://127.0.0.1:5000`

### 3. API Endpoints

| Endpoint | Method | Description |
| ------------------------ | -------- | -------------------------------------------------- |
| `/api/health` | GET | Health check |
| `/api/v1/admin/release-notes` | POST | Fetch all release notes (requires password) |
| `/api/v1/admin/release-notes/generate` | POST | Generate new release note (requires password) |
| `/api/v1/admin/release-notes/update` | POST | Update release note content |

## Deployment Options

### Option 1: Render (Recommended)

Best for beginners - no Docker needed, Render handles everything.

1. Go to [render.com](https://render.com) → Sign Up (free account)
2. Dashboard → New → Web Service
3. Connect your GitHub repository
4. Configure:
   - Name: `release-notes-api`
   - Environment: `Python 3`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn api_server:app`
5. Add Environment Variables (Advanced → Add Environment Variable):
   - `GITHUB_TOKEN` = your_github_token
   - `FE_REPO` = yourorg/your-fe-repo
   - `BE_REPO` = yourorg/your-be-repo
   - `ADMIN_PASSWORD` = your_secure_password
6. Click Create Web Service

Your API will be live at: `https://release-notes-api.onrender.com`

**Note:** Free tier has 750 hours/month. App sleeps after 15 min of inactivity (cold start ~30s on wake).

### Option 2: Using Docker (Advanced)

Docker packages your app into a container. Use this if:
- You want to deploy to multiple platforms
- You need more control over environment

```bash
# Install Docker Desktop (https://docker.com/desktop)

# Build the image
docker build -t release-notes .

# Run locally
docker run -it -p 5000:5000 ^
  -e GITHUB_TOKEN=your_token ^
  -e FE_REPO=yourorg/frontend ^
  -e BE_REPO=yourorg/backend ^
  -e ADMIN_PASSWORD=your_password ^
  release-notes
```

To deploy: push to any container platform (Render, Railway, DigitalOcean, etc.)

## Frontend Integration

Set in frontend `.env`:
```
NEXT_PUBLIC_RELEASE_NOTES_API_URL=http://127.0.0.1:5000/api
```

For production deployment, update to your deployed API URL.

## Features

- Fetches merged PRs from configured FE and BE repositories
- AI-powered summarization using LLM (optional)
- Deduplicates FE/BE PRs for same features
- User-friendly release note formatting
- Real-time generation progress
- Admin API for generating and managing release notes