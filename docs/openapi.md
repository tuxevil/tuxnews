# API Contract

The versioned contract is `backend/openapi.json`. Regenerate it from the
application with:

```bash
python3 backend/scripts/export_openapi.py
cd frontend && npm run generate:api
```

The second command writes `frontend/src/generated/openapi.ts` with
`openapi-typescript`; `frontend/src/api.ts` aliases its response types instead
of maintaining a second schema copy. `test_openapi_contract.py` compares the
snapshot to FastAPI's runtime document, checks the public route surface and
requires authentication metadata on private operations.

Authentication uses bearer access tokens and the existing HttpOnly refresh
cookie. Content responses retain `security_context`, signed feed cursors, and
provenance fields. Examples and generated artifacts contain no credentials or
external instructions.
