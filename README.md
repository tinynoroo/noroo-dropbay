# noroo-dropbay

A self-hosted file drop for you and your AI.

You upload from the browser. Your AI assistant uploads over MCP. Both land in the same folder on your server, under their original filenames. The interface is in Korean.

## Three ways in

| Path | Who uses it | How it works |
|---|---|---|
| Web page | You | Log in with a password and drop files. Uploads are chunked and resumable |
| MCP `save_image` | An AI assistant | Sends an image as base64 in the tool call |
| MCP `request_upload` | An AI assistant | Gets a one-time upload URL, then sends the file to it with a plain HTTP `PUT` (for example `curl -T`) |

`request_upload` exists because base64 in a tool argument runs into the model's output limit for anything large. The server hands out a URL instead and the file travels over ordinary HTTP. If the assistant cannot make HTTP requests itself, it can pass the URL to you to open in a browser.

## Details

- **Resumable uploads.** Progress lives on disk, so an upload survives a dropped connection, a page reload, or a server restart. Picking the same file again continues from where it stopped.
- **One-time URLs.** A `request_upload` URL expires after 10 minutes and works once. If the transfer fails midway, the URL can be reused until it expires.
- **Original filenames.** Finished files are stored as-is under `data/blobs`, so you can open them directly on the server.
- **No database.** Files and a `meta.json` index, nothing else.

## Access control

Everything is closed except `/healthz`.

- **Web:** a password (`UPLOAD_PASSWORD`). Logging in sets a signed cookie that lasts 30 days.
- **MCP and API:** a token (`UPLOAD_TOKEN`). Connector UIs differ in what they let you enter, so the token is accepted in any of these forms:
  - `Authorization: Bearer <token>`
  - one of `X-API-Key`, `Api-Key`, `X-Auth-Token`, `X-Access-Token`, `X-Token`, `X-Upload-Token`
  - in the URL, as `/mcp/<token>`, for clients that cannot send headers at all
- The MCP handshake (`initialize`, `tools/list`) is open without a token, because clients read an unauthenticated 401 as "this server uses OAuth". Only tool names and descriptions are visible that way. Every `tools/call` requires the token.

## Running it

```
UPLOAD_TOKEN=<long-random-string> UPLOAD_PASSWORD=<password> docker compose up --build
```

The compose file binds to `127.0.0.1:9002` and joins an external Docker network named `dbnet`, because it was written for a server managed with [noroo-deploy](https://github.com/tinynoroo/noroo-deploy). To run it on its own, create the network first (`docker network create dbnet`) or remove the `networks` lines.

Put it behind an HTTPS reverse proxy. The server trusts `X-Forwarded-Proto` and `X-Forwarded-Host` so that the upload URLs it issues come out right.

Then point your MCP client at `https://<your-host>/mcp`.

| Variable | Meaning |
|---|---|
| `UPLOAD_TOKEN` | Token for MCP and API access |
| `UPLOAD_PASSWORD` | Password for the web page. Web login is disabled when it is empty |
| `DATA_DIR` | Where files are stored (default `/app/data`) |

## Stack

Python 3.12 and Flask, in a single file (`app/server.py`), plus two static HTML pages.
