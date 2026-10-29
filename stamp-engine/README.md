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

## Placement safeguards

- Carrier signature/confirmation cells take precedence over recipient headers and contractual mentions.
- Repeated carrier cells are stamped on every matching page, not on unrelated pages.
- Without a dedicated cell, fallback requires free bottom-right space on every page; an unsafe or partial fallback returns review instead.
- Stamps remain upright, including rotated source pages. Source-bound manual placements remain supported.
- Run `python -m unittest discover -s . -p 'test_*.py'` before deployment.
