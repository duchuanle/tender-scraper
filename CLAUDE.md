# CLAUDE.md

Guidance for AI agents working in this repository. User-facing setup, API contract and Power Automate configuration live in `README.md` — read it before changing behaviour.

## What this is

A Python 3.12 Azure Functions (v2 programming model) app. `POST /api/crawl` takes a buy NSW tender link (often an Outlook Safe Link), resolves it, crawls the page with Firecrawl Cloud, polls **synchronously** until the crawl finishes, extracts a structured tender summary, downloads attachments, writes `summary.json` + `website.html`, uploads everything to personal OneDrive, and returns compact JSON. The caller is an organisation-owned Copilot Studio / Power Automate flow.

It is a working POC. It deploys either to an Azure Function App (Flex Consumption, source deploy) or as a Docker container on a VPS. The folder is not a git repository.

## Module map

| File | Owns |
| --- | --- |
| `function_app.py` | Routes, validation, `wait_for_crawl` polling, `build_crawl_result` orchestration, `error_response` (the single place exceptions become HTTP responses). |
| `firecrawl_client.py` | URL validation + Safe Link/tracking resolution, Firecrawl payloads/calls/pagination, exception types (`ConfigurationError`, `RequestValidationError`, `FirecrawlAPIError`, `FirecrawlConnectionError`, `RedirectResolutionError`). |
| `attachment_links.py` | Picks attachment URLs out of Firecrawl documents. |
| `attachment_downloader.py` | Downloads into `<DOWNLOAD_DIR>/<jobId>/`, clean filenames, per-file SHA-256. |
| `tender_artifacts.py` | Extraction schema, summary normalisation, `summary.json` / `website.html`. |
| `onedrive_client.py` | Token refresh, folder create, upload with replace-by-name. |
| `scripts/` | Local-only helpers (excluded from deploys). |

Configuration is read with `os.getenv` at call time; the full list is in `README.md#configuration`.

## Commands

Run from this folder. PowerShell script activation is not needed — call the venv interpreter directly.

```powershell
.\.venv\Scripts\python.exe -m pytest -q                       # all tests
.\.venv\Scripts\python.exe -m pytest -q tests/test_<module>.py # one module
func start                                                     # local host on :7071 (keys NOT enforced)
docker compose up -d --build                                   # container on :8080 (keys enforced)
docker compose logs -f ; docker compose down
func azure functionapp publish <FUNCTION_APP_NAME>             # Azure deploy
```

To exercise the container end to end, read the key from `secrets/host.json` into a variable without printing it and send `x-functions-key`. A real crawl spends Firecrawl credits — use `limit: 1` and one request.

## Invariants — do not break

- **Synchronous contract.** The POST starts, polls, processes and uploads before returning. Change this only as a deliberate caller-contract redesign.
- **Success = `status == "completed"` and `oneDrive.status == "completed"`.** Keep both fields and their meanings stable; the Power Automate condition depends on them.
- **Compact responses.** Copilot Studio rejects responses over 500 KB. Raw Firecrawl documents only with `includeData=true`.
- **Stable summary schema.** `name, agency, tenderID, category, closeDate, locations, type, description, contact, url` — always present, missing values are `""`, `url` is the resolved public URL.
- **Clean attachment filenames.** Files keep their original names so re-scrapes overwrite the stored copy (latest wins). buy NSW download URLs change every visit, so never derive names from the URL; a URL hash is only a same-crawl collision fallback.
- **Errors go through `error_response`.** New failure modes get an exception type and a mapping there with an `errorCode` and `retryable`.
- **No secrets in responses or logs** — Firecrawl key, OneDrive refresh token, function keys. Do not echo upstream payloads that could contain request headers.
- **Self-contained.** No runtime dependency on folders outside this project (URL unwrapping lives in `firecrawl_client.py`).
- **Deployment manifests.** When adding a runtime module, add it to the `COPY` line in `Dockerfile` and make sure `.funcignore` / `.dockerignore` do not exclude it. Never exclude runtime `.py` files.
- Preserve unrelated files and the sample outputs in `downloads/` unless asked.

## Secrets — never read or print

`local.settings.json`, `.env`, `secrets/host.json`. If you need to know whether a value is set, print the key name and length only. Templates are `local.settings.json.example`, `.env.example`, `secrets/host.json.example`.

## Testing expectations

- Tests mock all HTTP; keep it that way — no test may contact Firecrawl, Graph or buy NSW.
- Add or update tests alongside any behaviour change; run the complete suite after runtime changes.
- `requirements-dev.txt` adds only pytest.

## Operational gotchas

- **Power Automate's 120 s limit** bounds the whole job. The code default `FIRECRAWL_POLL_TIMEOUT_SECONDS=240` is too long for it; deployments should use ~75. Keep total work well inside 120 s.
- `func start` disables key auth, so a local 200 does not prove auth works — test auth against the container.
- Flex Consumption does not run custom containers; Docker is for VPS / Container Apps.
- The Power Automate parser depends on the email's `btn-cm link-btn-cm` class.
- OneDrive simple upload caps each file at 250 MB.
- Automatic retry in Power Automate re-runs the crawl and spends credits; it should stay off.

## Open items

1. Confirm the Function key exposed during earlier troubleshooting was rotated and the old key removed.
2. Export the Copilot Studio / Power Automate workflow as a Power Platform solution for versioning.
3. Decide whether Azure Blob Storage becomes the system of record before SharePoint publishing.
4. Move to a start/status or queue-based pattern if real crawls regularly approach 120 s.
5. Record per-run outcomes in the workflow (input URL, HTTP status, `errorCode`, `jobId`, upload status).
6. Replace personal OneDrive delegated auth with organisation SharePoint app auth (least-privilege site access).
7. Add TLS (reverse proxy) in front of the Docker deployment before exposing it publicly.
