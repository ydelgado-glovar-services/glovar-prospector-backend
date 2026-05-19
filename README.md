# Glovar Lead Prospector Engine

## Project Overview

The Glovar Lead Prospector Engine is an advanced, automated lead generation system. The architecture relies on a **Next.js App Router** frontend that serves as a secure Server-Side Rendering (SSR) proxy, routing requests to a powerful **FastAPI Python** backend. The backend handles the orchestration of asynchronous lead generation tasks, utilizing integrations such as **Apify** (LinkedIn scraping), **Groq** (LLM-based parsing and analysis), **Tavily** (web search), and **Apollo** for robust data enrichment. All generated leads and system states are securely isolated and persisted in **Supabase** via Row-Level Security (RLS).

## Prerequisites

To run this project locally, ensure you have the following installed:

- **Python**: Version 3.10 or higher (for the FastAPI backend)
- **Node.js**: Version 18.17 or higher (for the Next.js frontend)
- **External Accounts & API Keys required**:
  - [Apify](https://apify.com/) (for LinkedIn scraping operations)
  - [Groq](https://groq.com/) (for high-speed LLM parsing)
  - [Tavily](https://tavily.com/) (for AI search)
  - [Supabase](https://supabase.com/) (Database and Auth)
  - [Apollo](https://www.apollo.io/) (Data enrichment)

## Environment Setup

You need to set up environment variables for both the backend and the frontend.

### Backend (`glovar-prospector-backend/.env`)
Create a `.env` file in the backend directory with the following keys:
```env
# Supabase Configuration
SUPABASE_URL=https://dummy-id.supabase.co
SUPABASE_KEY=dummy-supabase-anon-key

# Groq (LLM) Configuration
GROQ_API_KEY=gsk_dummy_groq_api_key

# Apify Configuration
APIFY_API_TOKEN=apify_api_dummy_token

# Tavily Configuration
TAVILY_API_KEY=tvly-dummy-tavily-key

# Apollo Configuration
APOLLO_API_KEY=dummy_apollo_key

# Encryption (for cryptography)
ENCRYPTION_KEY=dummy_fernet_encryption_key_base64
```

### Frontend (`glovar-prospector-frontend/.env.local`)
Create a `.env.local` file in the frontend directory with the following keys:
```env
# Supabase Public Keys
NEXT_PUBLIC_SUPABASE_URL=https://dummy-id.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=dummy-supabase-anon-key

# Backend Route via Ngrok or Localhost
PYTHON_BACKEND_URL=https://dummy-ngrok-id.ngrok-free.app
```

## Local Development Step-by-Step

### 1. Backend Setup (FastAPI)
Navigate to the backend directory, create a virtual environment, and install dependencies:
```bash
cd glovar-prospector-backend

# Create virtual environment
python -m venv .venv

# Activate the virtual environment
# On Windows:
.venv\Scripts\activate
# On macOS/Linux:
source .venv/bin/activate

# Install requirements
pip install -r requirements.txt
```

### 2. Start the FastAPI Server
With the virtual environment activated, start the backend server using Uvicorn:
```bash
uvicorn main:app --reload --port 8000
```

### 3. Exposing the Backend (Ngrok)
To allow the Next.js proxy to securely reach the FastAPI backend (and handle potential webhooks), expose your local port 8000 via Ngrok:
```bash
ngrok http 8000
```
*Note the generated Forwarding URL (e.g., `https://abc-123.ngrok-free.app`) and update the `PYTHON_BACKEND_URL` in your frontend `.env.local`.*

### 4. Frontend Setup (Next.js)
Open a new terminal window, navigate to the frontend directory, and install the Node dependencies:
```bash
cd glovar-prospector-frontend

# Install dependencies
npm install
```

### 5. Start the Next.js Dev Server
Start the frontend application:
```bash
npm run dev
```
Your frontend will be accessible at `http://localhost:3000`.

## Deployment Guide

### Frontend (Vercel)
The Next.js App Router is optimized for Vercel. 
1. Push your code to a Git repository.
2. Import the project into Vercel.
3. In the Vercel dashboard, add your Environment Variables (`NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`).
4. Set the `PYTHON_BACKEND_URL` to point to the production URL of your deployed FastAPI backend.
5. Deploy.

### Backend (Production)
The FastAPI backend should be deployed to a platform that supports Python web services (e.g., Render, Railway, AWS ECS, or Google Cloud Run).
1. Set all environment variables defined in the `.env` section securely in your host's dashboard.
2. The start command for production should omit `--reload` and run on the host's specified port (e.g., via Docker or native deployment):
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```
3. Ensure CORS policies in `main.py` restrict traffic to your production Vercel frontend URL.

## System Flow (Ejecutar Prospección)

When a user initiates the process by clicking **"Ejecutar Prospección"**, the following steps occur asynchronously:

1. **Proxy Initiation**: The Next.js frontend sends a secure proxy request to the FastAPI backend, initiating the prospector task.
2. **Web Scraping**: The backend triggers **Apify** jobs to scrape relevant profile and company data from LinkedIn based on the user's criteria.
3. **Polling & Background Jobs**: The FastAPI backend asynchronously polls Apify until the scraping task completes, managing this gracefully to avoid timeouts.
4. **LLM Parsing & Analysis**: The scraped raw data is fed into **Groq**, where LangChain orchestrates extraction and structuring of key insights (scoring leads).
5. **Enrichment**: **Tavily** and **Apollo** APIs are queried to fetch additional context, emails, and phone numbers.
6. **Supabase Saving**: Finally, the parsed, enriched, and scored leads are saved securely to **Supabase**. Row-Level Security ensures that the data is only accessible to the authorized user who requested the operation.
