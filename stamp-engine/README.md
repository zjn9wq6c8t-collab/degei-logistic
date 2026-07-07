# DEGEI Stamp Engine

Private API for stamping transport orders.

## API

- `GET /health`
- `POST /stamp`
- Header: `X-API-Key: <STAMP_API_KEY>`

Example body:

```json
{
  "pdf_url": "https://drive.google.com/uc?export=download&id=<original_pdf_file_id>",
  "stamp_url": "https://drive.google.com/uc?export=download&id=<stamp_file_id>",
  "filename": "comanda_stampilata.pdf",
  "stamp_width": 175,
  "allow_fallback": false
}
```

If `needs_review=true`, Make must stop and notify Telegram. It must not email the client.
