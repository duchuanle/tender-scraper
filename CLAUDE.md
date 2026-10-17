# CLAUDE.md

Guidance for AI agents working in this repository. User-facing setup, API contract and Power Automate configuration live in `README.md` — read it before changing behaviour.

## What this is

A Python FastAPI service packaged as a generic Docker image (no cloud-vendor runtime dependency). `POST /api/crawl` takes a buy NSW tender link (often an Outlook Safe Link), resolves it, crawls the page with Firecrawl Cloud, polls **synchronously** until the crawl finishes, extracts a structured tender summary, downloads attachments, writes `summary.json` + `website.html`, optionally uploads everything to OneDrive, and returns compact JSON. The caller is an organisation-owned Copilot Studio / Power Automate flow.

It is a working POC. It was originally an Azure Functions app; that runtime and its `x-functions-key` / `?code=` auth have been removed.

## Module map

| File | Owns |
| --- | --- |
| `app.py` | FastAPI app, `authenticate` (API keys), `wait_for_crawl` polling, `build_crawl_result` orchestration, `error_response` (the single place exceptions become HTTP responses), `/healthz`. |
| `firecrawl_client.py` | URL validation + Safe Link/tracking resolution, Firecrawl payloads/calls/pagination, exception types (`ConfigurationError`, `RequestValidationError`, `FirecrawlAPIError`, `FirecrawlConnectionError`, `RedirectResolutionError`). |
| `attachment_links.py` | Picks attachment URLs out of Firecrawl documents. |
| `attachment_downloader.py` | Downloads into `<DOWNLOAD_DIR>/<jobId>/`, clean filenames, per-file SHA-256. |
| `tender_artifacts.py` | Extraction schema, summary normalisation, `summary.json` / `website.html`. |
| `onedrive_client.py` | Optional: token refresh, folder create, upload with replace-by-name. |
| `settings.py` | Shared env parsers (`boolean_setting`). Use it for new true/false settings instead of ad-hoc parsing. |
| `scripts/` | Local-only helpers (not in the image). |

Configuration is read with `os.getenv` at call time; the full list is in `README.md#configuration`.

## Commands

Run from this folder. PowerShell script activation is not needed — call the venv interpreter directly.

```powershell
.\.venv\Scripts\python.exe -m pytest -q                                  # all tests
.\.venv\Scripts\python.exe -m pytest -q tests/test_<module>.py            # one module
.\.venv\Scripts\python.exe -m uvicorn app:app --env-file .env --reload    # local dev server on :8000
docker compose up -d --build                                              # container on :8080
docker compose logs -f ; docker compose down
```

To exercise the container end to end, read the key from `.env` (`API_KEYS`) into a shell variable without printing it and send `x-api-key`. A real crawl spends Firecrawl credits — use `limit: 1` and one request.

## Invariants — do not break

- **Synchronous contract.** The POST starts, polls, processes and uploads before returning. Change this only as a deliberate caller-contract redesign. Blocking work runs in the thread pool (`run_in_threadpool` / plain `def` routes) — never call blocking code directly inside an `async def` route.
- **Success fields.** Keep `status`, `downloads.status` and `oneDrive.status` and their meanings stable; the Power Automate condition depends on them.
- **Compact responses.** Copilot Studio rejects responses over 500 KB. Raw Firecrawl documents only with `includeData=true`.
- **Stable summary schema.** `name, agency, tenderID, category, closeDate, locations, type, description, contact, url` — always present, missing values are `""`, `url` is the resolved public URL.
- **Fresh scrapes.** `build_crawl_payload` defaults `scrapeOptions.maxAge` to `0`. buy NSW attachment links are S3 pre-signed URLs valid for one hour; a Firecrawl cache hit returns expired links and every download fails with 403.
- **Clean attachment filenames.** Files keep their original names so re-scrapes overwrite the stored copy (latest wins). Never derive names from the URL; a URL hash is only a same-crawl collision fallback.
- **Auth fails closed.** No `API_KEYS` means `500 CONFIGURATION_ERROR`, never open access; only `API_AUTH_DISABLED=true` skips auth. Compare keys with `hmac.compare_digest`. Keys arrive only in the `x-api-key` header — never accept them in the query string. `/healthz` stays unauthenticated.
- **Errors go through `error_response`.** New failure modes get an exception type and a mapping there with an `errorCode` and `retryable`.
- **No secrets in responses or logs** — API keys, Firecrawl key, OneDrive refresh token. `clean_request_url` strips the query so caller-supplied query strings are never echoed in `statusUrl`. Do not echo upstream payloads that could contain request headers.
- **Responses via `json_response`.** It uses `CompatJSONResponse` (plain `json.dumps`, NaN tolerated) so odd upstream values never turn a processed crawl into a 500.
- **`/healthz` stays `async def`** so it never queues behind blocking crawl threads.
- **Vendor-neutral runtime.** No cloud-specific SDKs or runtime assumptions in the core path; OneDrive stays optional behind `ONEDRIVE_ENABLED`.
- **Image manifest.** When adding a runtime module, add it to the `COPY` line in `Dockerfile` and make sure `.dockerignore` does not exclude it.
- Preserve unrelated files and the sample outputs in `downloads/` unless asked.

## Secrets — never read or print

`.env` and any legacy `local.settings.json`. If you need to know whether a value is set, print the key name and length only. The template is `.env.example`.

## Never commit

- `.env` or any `.env.*` file other than `.env.example` (also legacy `local.settings.json`).
- Planning documents: design specs, implementation plans, brainstorming notes, handoff notes (e.g. anything under `docs/superpowers/`, `*-plan.md`, `*-spec.md`).
- API keys or other secrets in any file — `API_KEYS` values, Firecrawl keys, OneDrive client secrets or refresh tokens, function keys. Examples and docs use placeholders such as `<API_KEY>` or `fc-replace-me`.

Before every commit, run `git status` and `git diff --cached` and check that nothing in the list above is staged. If such a file is already tracked, stop and tell the user instead of committing it.

## Testing expectations

- Tests mock all HTTP; keep it that way — no test may contact Firecrawl, Graph or buy NSW.
- Route tests use FastAPI's `TestClient` (`httpx2` backend). Patch `app.time` (the module reference), not `time.monotonic` globally — the test client's event loop also uses the clock.
- Add or update tests alongside any behaviour change; run the complete suite after runtime changes.

## Operational gotchas

- **Power Automate's 120 s limit** bounds the whole job. The code default `FIRECRAWL_POLL_TIMEOUT_SECONDS=240` is too long for it; deployments should use ~75. Reverse proxies (nginx default 60 s) and platform ingress timeouts must also exceed the job time.
- Behind a reverse proxy, `statusUrl` takes its scheme from `X-Forwarded-Proto` (uvicorn `--proxy-headers`, `FORWARDED_ALLOW_IPS=*`) and its host from the forwarded `Host` header — uvicorn ignores `X-Forwarded-Host`. Compose binds to `127.0.0.1` by default (`BIND_ADDRESS`) so the trusted forwarded headers can only come from the local proxy.
- The container runs as uid 10001; outputs go to the named volume at `/data`. Bind-mounting a host directory needs matching ownership.
- The Power Automate parser depends on the email's `btn-cm link-btn-cm` class.
- OneDrive simple upload caps each file at 250 MB.
- Automatic retry in Power Automate re-runs the crawl and spends credits; it should stay off.

## Open items

1. Issue a fresh `API_KEYS` value for the new deployment.
2. Add TLS (reverse proxy) in front of the VPS deployment before exposing it publicly.
3. Point the Power Automate HTTP action at the new host and send the key in the `x-api-key` header (`x-functions-key` is no longer accepted).
4. Export the Copilot Studio / Power Automate workflow as a Power Platform solution for versioning.
5. Decide on durable storage (S3-compatible / Blob) before SharePoint publishing; consider pruning old job folders on the volume.
6. Move to a start/status or queue-based pattern if real crawls regularly approach 120 s.
7. Replace personal OneDrive delegated auth with organisation SharePoint app auth (least-privilege site access).
