---
title: Eco Travel Bot
emoji: 🌍
colorFrom: green
colorTo: blue
sdk: docker
app_file: app.py
pinned: false
--


# Eco-Travel Advisor

Production-oriented Rasa Open Source chatbot for sustainable tourism planning. It supports multi-turn trip planning, slot memory, carbon estimation with Climatiq, Amadeus-backed travel search, weighted recommendations, Telegram integration, and human handover.

## Architecture

- Rasa NLU/Core with a spaCy + DIETClassifier pipeline.
- Custom action server in `actions/actions.py`.
- Climatiq carbon estimates with cached demo fallback.
- Amadeus sandbox flight search with curated eco hotel and cultural experience fallback.
- Telegram human handover with retry and full conversation summary.
- Rasa Webchat frontend in `frontend/`.
- Docker and Docker Compose deployment.

## Setup

```powershell
cd eco-travel-bot
Copy-Item .env.example .env
```

Add API keys to `.env` when available:

```text
CLIMATIQ_API_KEY=
AMADEUS_CLIENT_ID=
AMADEUS_CLIENT_SECRET=
TELEGRAM_ADMIN_CHAT_ID=
```

The provided Telegram bot token is already wired through `TELEGRAM_BOT_TOKEN`. For production, rotate tokens if they have been shared outside a trusted environment.

## Run With Docker

```powershell
docker compose up --build
```

Rasa API: `http://localhost:5005`

Action server: `http://localhost:5055`

Open `frontend/index.html` in a browser. The embedded webchat connects to `http://localhost:5005`.

## Local Development

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
rasa train
```

`requirements.txt` installs the exact `en_core_web_md` spaCy model wheel required by the Rasa pipeline, avoiding the broken auto-generated `spacy download` URL.

Start two terminals:

```powershell
rasa run actions --actions actions --debug --port 5055
```

```powershell
$env:ACTION_ENDPOINT_URL="http://localhost:5055/webhook"
rasa run --enable-api --cors "*" --debug --endpoints endpoints.yml --credentials credentials.yml
```

## Testing

```powershell
rasa data validate
rasa test nlu --cross-validation --folds 3
rasa test core --stories data/test_stories.yml --out results/core

```

Rasa writes NLU evaluation artifacts, including intent reports and confusion matrix images, under `results/`.

## Conversation Coverage

The assistant supports:

- Destination, travel dates, budget, and sustainability preference collection.
- Persistent slots across a session.
- Carbon comparison for rail, coach, and flight.
- Eco hotel, transport, and cultural experience recommendations.
- Ranking using sustainability and price weights:

```text
score = sustainability_weight * carbon_score + price_weight * price_score
```

The implementation uses normalized utility scores so lower carbon and lower price rank higher. Responses avoid absolute claims such as "zero impact" or "fully green".

## Telegram

`credentials.yml` is configured for local REST and Socket.IO webchat. For Telegram deployment, run Rasa with `credentials.telegram.yml` and set a public HTTPS webhook URL:



## HuggingFace Spaces

Create a Docker Space and upload this project. Set `PORT=7860` and the same environment variables listed above. The Dockerfile uses `${PORT:-5005}`, so it runs locally on 5005 and on Spaces on 7860.

## Ethical AI Notes

- The bot presents carbon values as estimates.
- It avoids greenwashing and absolute sustainability claims.
- It exposes trade-offs between cost, carbon, and availability.
- Human handover is available after repeated fallback or explicit request.
