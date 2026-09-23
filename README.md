# Tiramisu — event vendor recommendations

Small local demo that recommends up to three event vendors and explains each match. The demo catalog contains 18 **synthetic** profiles; it does not contain the hackathon dataset.

> Project note: still can not apply gpt pro :)

## Run locally

Requires Python 3.10 or newer. From the repository root:

```powershell
python backend/server.py
```

Open <http://127.0.0.1:8000>. The API and page use the same local server. To change the port, set `PORT` before starting the server.

Without an API key, the service uses a stable local text-matching fallback. To enable OpenAI embeddings, set `OPENAI_API_KEY` in the environment before starting the service. The server never sends profile data from the browser; it makes embeddings requests server-side. If the provider is unavailable, recommendations continue in fallback mode and the page labels that state.

## API

- `GET /api/options` — cities, categories, event formats, languages, and supported dates.
- `POST /api/recommendations` — request fields: `city`, `date` (`YYYY-MM-DD`), `event_type`, `category`, `budget_kzt`; optional `duration_hours` and `language`.
- Outcomes: `recommended`, `no_category_in_city`, `no_eligible_candidates`.

The date window in the demo is 2026-09-23 through 2026-12-31. Replace `backend/data/profiles.jsonl` with the supplied dataset to use its profiles; keep `synthetic: true` visible for any demo-only records.

## Saved agent role descriptions

The source descriptions for the frontend developer, UI designer, and backend architect are saved in [`agent-skills/`](agent-skills/).
