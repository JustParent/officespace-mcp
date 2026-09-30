# OfficeSpace MCP

Admin workflows for OfficeSpace, with a schema-validated GraphQL escape hatch. Uses the official
Python MCP SDK. Supports stdio and Streamable HTTP. One process connects to one OfficeSpace tenant.

## Admin workflows

Each row is **one MCP call**, including internal identity lookups, pagination and bulk operations.
Ambiguous names return candidates before any changes are sent. Use `id:<ID>` to disambiguate.

| Admin request | Tool | What it handles internally |
| --- | --- | --- |
| Show London office capacity and free desks | `workplace_overview` | Site lookup, floors, allocated/vacant/bookable seat counts |
| Where does Alice sit? Who is in Finance? | `find_people` | Employee search, department/team filters, assigned desks and locations |
| Find a six-person room free from 10–11 | `find_spaces` | Site/floor lookup, capacity, reservation conflicts; returns candidate rooms |
| Who has booked these desks tomorrow? | `get_bookings` | Time-window filters, people/space lookup and cursor pagination |
| Book these four desks for these four people | `book_spaces` | All identity lookups, site time zones, one validated mutation batch |
| Cancel Alice's desk bookings tomorrow | `change_bookings` | Resolves Alice, loads matching bookings and cancels them in one call |
| Move these reservations to Friday | `change_bookings` | Batch reschedule, timezone conversion, per-reservation results |
| Add starters, change departments, deactivate leavers | `manage_employees` | Up to 50 employee changes with schema validation |
| Move the team to these desks next Monday | `manage_moves` | Employee/current-seat/target-seat resolution, one bulk move submission |
| Report a broken desk, assign a request, close several tickets | `manage_requests` | Request type/requestor/assignee lookups, bulk submissions/status changes |

The nine workflow tools are complemented by `inspect_schema` and `graphql` for the remaining API:
leases, neighborhoods, assets, presence, move reports and other operations. The bundled snapshot has
33 root queries and 54 mutations. No handwritten endpoint inventory is needed for advanced access.

An open-ended request such as “find a suitable room, then book the one I choose” needs a search and a
booking call. A request naming the room and organizer can go straight to `book_spaces`. Bulk calls
avoid repeated model/tool round trips; they may involve multiple upstream HTTP requests.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked
cp .env.example .env
# Fill in .env, then:
uv run --env-file .env officespace-mcp
```

Configuration:

| Variable | Meaning |
| --- | --- |
| `OFFICESPACE_GRAPHQL_URL` | Exact HTTPS GraphQL POST endpoint for the tenant |
| `OFFICESPACE_AUTH_HEADER` | Authentication header name; defaults to `Authorization` |
| `OFFICESPACE_AUTH_VALUE` | Complete header value, including any required prefix |
| `OFFICESPACE_ENABLE_MUTATIONS` | `true` enables writes; defaults to `false` |
| `OFFICESPACE_SCHEMA_PATH` | Optional path to a replacement SDL or introspection JSON schema |
| `MCP_BEARER_TOKEN` | Required for HTTP clients; a separate server access token |
| `MCP_ALLOWED_HOSTS` | HTTP Host allowlist, comma-separated; add the public reverse-proxy hostname |

The uploaded schema describes operations and types, **not the endpoint or authentication scheme**.
Get those from your tenant's GraphiQL setup at
`https://<tenant>.officespacesoftware.com/api/base/graphiql` or from OfficeSpace support. Copy the POST
endpoint and authentication header for an API-key request; do not use a browser session cookie.
The documentation page URL is not assumed to be the API endpoint. Legacy REST authentication is not
assumed to apply to GraphQL. Credentials stay in process configuration, never tool arguments.

Without credentials the schema tools still work; API tools return a configuration error. This
implementation has contract tests against the supplied schema and mocked upstream responses; it
has not yet been verified against a live tenant.

### MCP client configuration (stdio)

```json
{
  "mcpServers": {
    "officespace": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/officespace-mcp", "run", "--locked", "--env-file", ".env", "officespace-mcp"]
    }
  }
}
```

### Streamable HTTP

Set `MCP_BEARER_TOKEN` to a strong random token, set `MCP_ALLOWED_HOSTS` to the host clients use, and
run behind an HTTPS reverse proxy:

```sh
uv run --env-file .env officespace-mcp --transport streamable-http --host 0.0.0.0 --port 8000
```

Clients connect to `/mcp` with `Authorization: Bearer <MCP_BEARER_TOKEN>`. This is static token auth,
not an OAuth authorization server. All clients share the configured OfficeSpace credential's
permissions. Run a separate instance/credential per tenant or permission boundary. Origin/Host
checks stay enabled. The server refuses to start HTTP without its access token.

## Examples

Book named desks for a team with one `book_spaces` call:

```json
{
  "bookings": [
    {"kind": "desk", "space": {"reference": "A12", "site": "London"}, "person": "alice@example.com", "start": "2026-10-01T09:00:00+01:00", "end": "2026-10-01T17:00:00+01:00"},
    {"kind": "desk", "space": {"reference": "A13", "site": "London"}, "person": "bob@example.com", "start": "2026-10-01T09:00:00+01:00", "end": "2026-10-01T17:00:00+01:00"}
  ]
}
```

Cancel Alice's reservations with one `change_bookings` call:

```json
{"kind": "desk", "action": "cancel", "person": "alice@example.com", "start": "2026-10-01T00:00:00+01:00", "end": "2026-10-02T00:00:00+01:00"}
```

Update a person's department with `manage_employees`:

```json
{"action": "update", "changes": [{"person": "alice@example.com", "values": {"department": "Finance", "title": "Finance Manager"}}]}
```

For advanced tasks, call `inspect_schema` with `search` (operation discovery) or `type_name` (fields,
input types and enums), then pass one GraphQL operation plus variables to `graphql`. Raw mutations
are subject to the same write gate. Ask for payload `error`/`errors` fields and inspect their contents:
GraphQL can return HTTP 200 while an operation fails. The full SDL is also an MCP resource at
`officespace://schema`.

## Behaviour and limits

- All workflow identities and local GraphQL inputs are validated before a write batch is submitted.
  OfficeSpace enforces authorization and business rules. Mutations do not bypass notifications,
  force-vacate conflicting bookings or override move conflicts.
- Batches are **not atomic**. Some items may succeed even if others fail. Per-item outcomes and
  upstream errors are returned. The server never retries requests automatically. If a mutation
  times out, its outcome is unknown; check the affected records before retrying.
- Times need explicit offsets. Desk bookings are converted to the site's IANA time zone and use
  minute precision. Ambiguous desk wall times during a DST clock change are rejected.
- Availability is a reservation-conflict check. OfficeSpace may still reject a booking because of
  schedules, permissions, confirmation rules or a concurrent reservation. A search is not a hold.
- Cursor-based reads stop at 1,000 records and return `complete: false`. Filtered writes refuse an
  incomplete booking selection. Narrow filters if results are incomplete. Space searches examine
  up to `limit` candidates and disclose whether more exist.
- Capacity/vacancy counts are **seat allocation**, not measured attendance or historical utilization.
- Room `person` filters match the organizer, not guests. The root room-booking response has no room
  reference: use a room or site filter when you need room identity. Site room reports support up to
  50 rooms. Cancellation changes selected bookings, not an entire recurrence series.
- Employee deactivation does not automatically cancel bookings or vacate seats. Moves are scheduled
  first and completed explicitly. No implicit offboarding cascade.
- JSON GraphQL is supported; multipart file uploads are not. Deployed schema/plan/permissions may
  differ from the snapshot. Use `OFFICESPACE_SCHEMA_PATH` to supply another export.

## Development

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
```

CI runs lint, tests and a wheel build on Python 3.12 and 3.13. Tests include real in-memory and stdio
MCP clients, the HTTP authentication boundary, request construction against the full schema,
pagination, ambiguity, partial failures and the write gate. No live credentials are needed.

The SDL was generated from the introspection export supplied on 2026-09-30. To replace it:

```sh
uv run officespace-mcp --export-schema /path/to/introspection.json > src/officespace_mcp/schema.graphql
```

Public references:

- [OfficeSpace API guide](https://support.officespacesoftware.com/articles/en_US/Knowledge/Using-the-OfficeSpace-API-HC)
- [Official Python examples (legacy REST)](https://github.com/officespacesoftware/api_client_python)
- [Official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
