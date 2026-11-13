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
- Split carrier / signature / stamp captions in one column form a dedicated cell, including on an upper final page.
- Without a dedicated cell, fallback stamps every page at bottom right. It first searches clear space, then minimizes text/visual overlap in that corner when no clear space exists (owner policy, 13.11.2026).
- The low-level candidate helper keeps overlap disabled by default; automatic all-page fallback opts in. Invalid PDFs and manual coordinates still fail validation.
- Stamps remain upright, including rotated source pages. Source-bound manual placements remain supported.
- Run `python -m unittest discover -s . -p 'test_*.py'` before deployment.
