# Tender Scraper Function

A Python Azure Functions app that turns a buy NSW tender email link into a saved tender package. One HTTP call:

1. resolves an Outlook Safe Link or buy NSW tracking link to the public opportunity page,
2. crawls it with [Firecrawl](https://firecrawl.dev) and waits for the crawl to finish,
3. extracts a structured tender summary and the attachment links,
4. downloads every attachment and saves `summary.json` and `website.html`,
5. uploads the package to OneDrive (personal-account POC), and
6. returns a compact JSON result suitable for Copilot Studio and Power Automate.

It is called from an organisation-owned Copilot Studio / Power Automate flow and runs either in a personal Azure subscription (Flex Consumption) or as a Docker container on any VPS.

![Tender scraping POC architecture](tender-scraping-poc-process.png)

## Contents

- [Project layout](#project-layout)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [API](#api)
- [Storage and filenames](#storage-and-filenames)
- [Personal OneDrive setup](#personal-onedrive-setup)
- [Power Automate and Copilot Studio](#power-automate-and-copilot-studio)
- [Testing](#testing)
- [Limitations and production direction](#limitations-and-production-direction)

## Project layout

| Path | Responsibility |
| --- | --- |
| `function_app.py` | HTTP routes, request validation, synchronous polling, orchestration, response contract, error mapping. |
| `firecrawl_client.py` | URL validation, Safe Link / tracking-link resolution, Firecrawl payloads, API calls, result pagination. |
| `attachment_links.py` | Finds and deduplicates likely attachment URLs in Firecrawl documents. |
| `attachment_downloader.py` | Streams attachments into a traversal-safe job folder; returns size and SHA-256 per file. |
| `tender_artifacts.py` | Extraction schema, summary normalisation, primary-page selection, writes `summary.json` and `website.html`. |
| `onedrive_client.py` | Delegated OAuth token refresh, folder creation, upload and replace-by-name via Microsoft Graph. |
| `scripts/get_onedrive_refresh_token.py` | One-time device-code login helper (local only, not deployed). |
| `tests/` | pytest unit tests for every module. |
| `Dockerfile`, `docker-compose.yml`, `.env.example`, `secrets/host.json.example` | Container packaging for VPS deployment. |
| `host.json` | Functions host settings (10-minute function timeout). |
| `.funcignore`, `.dockerignore` | Keep secrets, tests, scripts and docs out of Azure and Docker deployments. |

Runtime dependencies are only `azure-functions` and `requests`.

## Quick start

### Run locally with Core Tools

Requires Python 3.12 and [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) v4.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item local.settings.json.example local.settings.json   # add your Firecrawl key
func start
```

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:7071/api/crawl" `
  -ContentType "application/json" -TimeoutSec 600 `
  -Body '{"url":"https://buy.nsw.gov.au/prcOpportunity/...","limit":1}'
```

Core Tools does not enforce function keys locally; Azure and the Docker image do.

### Run with Docker (VPS)

The image is built on the official Azure Functions Python 3.12 base, so routes and function-key authorization behave exactly as in Azure.

```bash
cp .env.example .env                              # Firecrawl and OneDrive settings
cp secrets/host.json.example secrets/host.json    # set long random keys, e.g. openssl rand -hex 32
docker compose up -d --build
```

The service listens on host port `8080` (override with `HOST_PORT` in `.env`):

```bash
curl -X POST http://<HOST>:8080/api/crawl \
  -H "Content-Type: application/json" \
  -H "x-functions-key: <FUNCTION_KEY>" \
  -d '{"url":"https://buy.nsw.gov.au/prcOpportunity/...","limit":1}'
```

- `secrets/host.json` holds the keys (`functionKeys[].value` or `masterKey.value`). If it is missing, the host generates random keys there on first start.
- `.env` and `secrets/host.json` are git-ignored and never copied into the image.
- Downloads are staged in `/tmp/downloads` inside the container and are not persisted.
- Put a TLS-terminating reverse proxy (Caddy, nginx) in front before exposing the port publicly; the function key must not travel over plain HTTP.
- Stop with `docker compose down`; view logs with `docker compose logs -f`.

### Deploy to Azure Functions

The existing Function App runs Python 3.12 on Flex Consumption (which does not support custom containers, so Azure uses source deployment):

```powershell
func azure functionapp publish <FUNCTION_APP_NAME>
```

Dependencies are installed from `requirements.txt` during the remote build. `local.settings.json` is not deployed; configure settings in **Function App → Settings → Environment variables** (or `az functionapp config appsettings set`) and set `DOWNLOAD_DIR=/tmp/downloads`.

## Configuration

Local runs read `local.settings.json` (from `local.settings.json.example`), Docker reads `.env` (from `.env.example`), Azure reads Function App settings. Never commit real values.

| Setting | Default | Description |
| --- | --- | --- |
| `FIRECRAWL_API_KEY` | — | Firecrawl bearer key. Required for Firecrawl Cloud; optional for an unauthenticated self-hosted server. |
| `FIRECRAWL_API_URL` | `https://api.firecrawl.dev` | Firecrawl base URL. `/v2` is appended unless it already ends in `/v1` or `/v2`. A self-hosted server must be reachable from the Function host. |
| `FIRECRAWL_REQUEST_TIMEOUT_SECONDS` | `30` (max 120) | Timeout for each Firecrawl API call. |
| `FIRECRAWL_POLL_INTERVAL_SECONDS` | `5` (max 60) | Delay between internal status checks. |
| `FIRECRAWL_POLL_TIMEOUT_SECONDS` | `240` (max 480) | How long the POST waits for the crawl. On timeout it returns `504` and skips downloads. **Keep it well under the caller's limit** — e.g. `75` for Power Automate's 120 s window. |
| `URL_RESOLUTION_TIMEOUT_SECONDS` | `20` (max 120) | Timeout for resolving Safe Links and tracking links. |
| `DOWNLOAD_DIR` | `downloads` | Local staging root; relative paths resolve from the project folder. Use `/tmp/downloads` in Azure and Docker. |
| `ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS` | `60` (max 600) | Timeout for each attachment GET. |
| `DEFAULT_CRAWL_LIMIT` | `1` | Page limit when a request omits `limit`. A tender and its attachments live on the starting page. |
| `MAX_CRAWL_LIMIT` | `1000` (max 10000) | Upper bound on `limit`, to cap accidental credit spend. |
| `ONEDRIVE_ENABLED` | `false` | Upload outputs to OneDrive. |
| `ONEDRIVE_CLIENT_ID` | — | Public-client app registration ID (when enabled). |
| `ONEDRIVE_REFRESH_TOKEN` | — | Delegated personal-account refresh token (when enabled). Secret. |
| `ONEDRIVE_FOLDER` | `Scraper Function` | Root folder under the signed-in OneDrive. |
| `ONEDRIVE_REQUEST_TIMEOUT_SECONDS` | `120` (max 600) | Timeout for each identity / Graph call. |

## API

Both routes use Azure Functions `function` authorization. Send the key in the `x-functions-key` header (preferred) or the `code` query parameter — not both.

### `POST /api/crawl`

Starts the crawl, polls until it finishes, processes outputs, uploads, and only then responds.

```json
{
  "url": "https://apc01.safelinks.protection.outlook.com/?url=...",
  "limit": 1
}
```

`url` may be a direct page, an Outlook Safe Link or a `connect.buy.nsw.gov.au` tracking URL. The function always asks Firecrawl for links, raw HTML and structured JSON; extra caller-supplied Firecrawl formats are kept. Raise `limit` only when the data genuinely spans several pages, because extraction runs (and is billed) on every page.

Abridged completed response:

```json
{
  "success": true,
  "status": "completed",
  "terminal": true,
  "jobId": "01ab2345-cdef-6789-abcd-ef0123456789",
  "inputUrl": "https://apc01.safelinks.protection.outlook.com/?url=...",
  "resolvedUrl": "https://buy.nsw.gov.au/prcOpportunity/...",
  "summary": {
    "name": "RFI - Enterprise Service Management (Service Desk) Solution",
    "agency": "NSW State Emergency Service",
    "tenderID": "NSWSES001090 - Request For Information",
    "category": "Information and technology",
    "closeDate": "09-Oct-2026 3:00 PM",
    "locations": "All regions",
    "type": "RFI (Open)",
    "description": "...",
    "contact": "...",
    "url": "https://buy.nsw.gov.au/prcOpportunity/..."
  },
  "attachmentCount": 2,
  "attachmentLinks": ["https://.../rfq-addenda-001.docx", "https://.../terms.pdf"],
  "downloads": {
    "status": "completed", "total": 2, "downloaded": 2, "existing": 0, "failed": 0,
    "files": [
      {"status": "downloaded", "fileName": "rfq-addenda-001.docx", "sizeBytes": 57059, "sha256": "..."}
    ]
  },
  "artifacts": {
    "status": "completed",
    "summary": {"status": "saved", "fileName": "summary.json"},
    "html": {"status": "saved", "fileName": "website.html"}
  },
  "oneDrive": {
    "status": "completed",
    "folderName": "NSWSES001090 - Request For Information",
    "folderWebUrl": "https://onedrive.live.com/...",
    "total": 4, "uploaded": 4, "failed": 0,
    "files": [{"status": "uploaded", "fileName": "summary.json", "webUrl": "https://onedrive.live.com/..."}]
  },
  "polling": {"attempts": 4, "elapsedSeconds": 19.1},
  "message": "Crawl completed and outputs were processed."
}
```

Summary fields are always present; missing values are empty strings. `summary.url` is always the resolved public URL, never a Safe Link.

**Success check.** HTTP `200` alone is not success — the crawl can complete while uploads fail. Require:

```text
status == "completed"  AND  oneDrive.status == "completed"
```

(`oneDrive.status` is `disabled` when OneDrive is off.)

**Errors** share the shape `{"success": false, "status": "error", "errorCode": "...", "error": "...", "retryable": bool}`:

| HTTP | `errorCode` | Meaning |
| --- | --- | --- |
| 400 | `INVALID_REQUEST` | Bad body, URL or parameter. |
| 4xx | `FIRECRAWL_ERROR` | Firecrawl rejected the request; `429` carries `Retry-After`. |
| 500 | `CONFIGURATION_ERROR` | A setting is missing or invalid. |
| 502 | `FIRECRAWL_ERROR` / `FIRECRAWL_UNAVAILABLE` | Firecrawl server error or unreachable. |
| 502 | `URL_RESOLUTION_ERROR` | The protected / tracking link could not be resolved. |
| 504 | `CRAWL_POLL_TIMEOUT` | Crawl not finished within `FIRECRAWL_POLL_TIMEOUT_SECONDS`; no downloads started. Includes `jobId`. |
| 500 | `INTERNAL_ERROR` | Unexpected failure (details only in logs). |

### `GET /api/crawl/{jobId}`

Diagnostics and recovery; normal callers do not need it. Returns the job status and, once completed, processes outputs like the POST. Optional query parameters: `skip` and `pageLimit` (Firecrawl result pagination) and `includeData=true` (raw Firecrawl documents — can be several MB, so never use it from Copilot Studio, which rejects responses over 500 KB).

## Storage and filenames

Local storage is only staging:

```text
<DOWNLOAD_DIR>/<Firecrawl job ID>/
├── summary.json
├── website.html
└── <attachments>
```

The durable POC destination is OneDrive:

```text
OneDrive/<ONEDRIVE_FOLDER>/<summary.tenderID>/
├── summary.json
├── website.html
└── <attachments>
```

- The folder name is the extracted `tenderID` (invalid characters become hyphens), falling back to `summary.name`, then the job ID.
- **Attachments keep their original filenames**, so each re-scrape overwrites the previous copy and only the latest version is kept. buy NSW issues a different download URL on every visit, so names are not derived from URLs. A short URL hash is appended only when two different attachments in the same crawl share a filename.
- Use the per-file `sha256` to tell whether a document's content actually changed.
- An existing OneDrive file with the same name is replaced in place, not duplicated as `name (1)`.
- Uploads use Graph simple upload: **250 MB per file** maximum.

## Personal OneDrive setup

The POC uses delegated Graph access (`Files.ReadWrite`, `offline_access`, `consumers` authority) to the OneDrive of one personal Microsoft account.

1. In Microsoft Entra admin center create an **App registration** for personal Microsoft accounts.
2. Under **Authentication**, enable **Allow public client flows**.
3. Under **API permissions**, add delegated Microsoft Graph **Files.ReadWrite**.
4. Run the login helper with the Application (client) ID:

   ```powershell
   .\.venv\Scripts\python.exe scripts\get_onedrive_refresh_token.py <CLIENT_ID>
   ```

5. Complete the device login with the target account and copy the refresh token. Do not share or commit it.
6. Set `ONEDRIVE_ENABLED=true`, `ONEDRIVE_CLIENT_ID`, `ONEDRIVE_REFRESH_TOKEN` (and optionally `ONEDRIVE_FOLDER`, `ONEDRIVE_REQUEST_TIMEOUT_SECONDS=30`) in Function App settings or `.env`, then restart.

## Power Automate and Copilot Studio

### Flow structure

```text
Get emails
└── Apply to each email                      (body.value)
    ├── Find tender anchors                  (Compose)
    └── Apply to each anchor                 outputs('Find_tender_anchors')
        ├── Extract protected URL            (Compose)
        ├── Scrape tender                    (HTTP)
        └── Check crawl and OneDrive status  (Condition)
```

Enter every formula through the **fx / Function** editor. If a Compose output literally contains `skip(...)` or `replace(...)`, it was typed as text and is not being evaluated.

**Find tender anchors**

```text
skip(split(item()?['body'], 'class="btn-cm link-btn-cm"'), 1)
```

**Extract protected URL** — `concat('&', 'amp;')` stops the designer converting the entity while editing:

```text
replace(
  first(split(first(skip(split(item(), 'href="'), 1)), '"')),
  concat('&', 'amp;'),
  '&'
)
```

Do not wrap the extracted URL in another Apply to each; it is a single string.

**Scrape tender** (HTTP action)

```text
Method:         POST
URI:            https://<FUNCTION_APP_NAME>.azurewebsites.net/api/crawl   (or your VPS URL)
Authentication: None
Headers:        Content-Type: application/json
                x-functions-key: <FUNCTION_KEY>
Body:           {"url": "@{outputs('Extract_protected_URL')}", "limit": 1}
```

- Enable **Secure Inputs** so the key is hidden in run history.
- Disable automatic retry — a retry starts another Firecrawl crawl and spends credits.

**Check crawl and OneDrive status**

```text
and(
  equals(body('Scrape_tender')?['status'], 'completed'),
  equals(body('Scrape_tender')?['oneDrive']?['status'], 'completed')
)
```

### Copilot Studio

The Standard Harness **Send HTTP request** node works without a custom connector or Power Automate Premium. Use the same URL, headers and body; set **Continue on error**, capture the status code and body, and branch on both the HTTP status and `success`. Generate the response type from the completed response above.

### Timeouts

Power Automate synchronous HTTP calls time out after **120 seconds**. Polling, downloads and uploads must all fit inside that window, so set `FIRECRAWL_POLL_TIMEOUT_SECONDS` to about `75`. Slow crawls will then return `504` rather than hang.

## Testing

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

No PowerShell script activation is needed. Tests mock all network calls; nothing contacts Firecrawl, OneDrive or buy NSW.

## Limitations and production direction

- The email parser depends on the exact `btn-cm link-btn-cm` class; a buy NSW template change breaks extraction.
- The whole job runs inside one synchronous HTTP request bounded by the caller's timeout.
- Personal OneDrive with a refresh token is a placeholder identity model, not suitable for production.

Preferred production direction:

```text
Scraper Function
    └── Azure Blob Storage (durable system of record)
            └── Queue / event
                    └── Asynchronous SharePoint publisher (organisation app auth, least-privilege site access)
```

This decouples scraping from SharePoint availability, removes the long-held HTTP request, and enables managed identity, retries and lifecycle management.

## References

- [Copilot Studio: Make HTTP requests](https://learn.microsoft.com/microsoft-copilot-studio/authoring-http-node)
- [Copilot Studio: connector responses must stay below 500 KB](https://learn.microsoft.com/troubleshoot/power-platform/copilot-studio/actions/connector-request-failure)
- [Azure Functions hosting timeouts](https://learn.microsoft.com/azure/azure-functions/functions-scale#timeout)
- [Azure Functions in custom containers](https://learn.microsoft.com/azure/azure-functions/functions-how-to-custom-container)
- [Firecrawl v2 crawl endpoint](https://docs.firecrawl.dev/api-reference/endpoint/crawl-post)
- [Microsoft identity device authorization grant](https://learn.microsoft.com/entra/identity-platform/v2-oauth2-device-code)
- [Upload small files to OneDrive](https://learn.microsoft.com/graph/api/driveitem-put-content)
