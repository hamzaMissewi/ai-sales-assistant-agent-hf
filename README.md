---
title: AI Sales Agent
emoji: 💬
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# AI Sales Agent

Multi-tenant RAG sales agent (FastAPI + Chroma). Free Docker Space on Hugging Face.

## Env vars (Settings > Variables and secrets)

| Name | Example | Required |
|---|---|---|
| `LLM_BASE_URL` | `https://api.groq.com/openai/v1` | yes |
| `LLM_API_KEY` | `gsk_...` | yes |
| `LLM_MODEL` | `llama-3.1-8b-instant` | yes |
| `FALLBACK_LLM_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai/` | no |
| `FALLBACK_LLM_API_KEY` | `AIza...` (Gemini free) | no |
| `FALLBACK_LLM_MODEL` | `gemini-2.5-flash` | no |

The secondary provider is used automatically when the primary hits rate limits or errors.

Note: the free Space disk is ephemeral. The vector DB is re-indexed from `clients/*/docs` on every restart, and saved leads/demos (`leads_*.jsonl`) reset on restart. Move leads to a database if you need durability.

## Deploy

```bash
# 1) On huggingface.co: new Space, SDK = Docker (free CPU basic)
# 2) Clone the empty Space and copy this repo's files into it
git clone https://huggingface.co/spaces/<your-org>/<space-name>
cp -r server.py ingest.py requirements.txt Dockerfile start.sh \
      .dockerignore index.html dashboard.html clients/ .  # inside the clone

# 3) Set the env vars above in the Space settings, then push
git add . && git commit -m "deploy sales agent" && git push
```

Your app will be live at `https://<your-org>-<space-name>.hf.space`.



---
title: Sales Chatbot
emoji: ⚡
colorFrom: indigo
colorTo: green
sdk: gradio
sdk_version: 6.28.0
python_version: '3.12'
app_file: app.py
pinned: false
license: apache-2.0
---

Check out the configuration reference at https://huggingface.co/docs/hub/spaces-config-reference
