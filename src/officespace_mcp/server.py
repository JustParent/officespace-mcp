"""MCP entry point: admin workflows plus schema-backed access to the remaining API."""

import argparse
import hmac
import json
import os
from collections.abc import Awaitable
from typing import Annotated, Literal

import httpx
import uvicorn
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import AwareDatetime, Field
from starlette.responses import JSONResponse

from . import __version__
from .api import API, OfficeSpaceError
from .config import Settings
from .models import Booking, EmployeeChange, Kind, Move, RequestChange, Space
from .workflows import Workflows

Limit = Annotated[int, Field(ge=1, le=1000)]
BatchBookings = Annotated[list[Booking], Field(min_length=1, max_length=50)]
BatchEmployees = Annotated[list[EmployeeChange], Field(min_length=1, max_length=50)]
BatchMoves = Annotated[list[Move], Field(min_length=1, max_length=50)]
BatchRequests = Annotated[list[RequestChange], Field(min_length=1, max_length=50)]
IDs = Annotated[list[str], Field(min_length=1, max_length=50)]
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)


def result(payload: dict, error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structuredContent=payload,
        isError=error or payload.get("ok") is False or bool(payload.get("errors")),
    )


async def respond(call: Awaitable[dict]) -> CallToolResult:
    try:
        return result(await call)
    except OfficeSpaceError as e:
        return result({"ok": False, "error": str(e)}, error=True)


def create_server(
    settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> MCPServer:
    settings = settings or Settings.from_env()
    api = API(settings, transport)
    server = MCPServer(
        "OfficeSpace",
        version=__version__,
        instructions="Use workflow tools for common admin tasks. Names/emails are resolved inside "
        "each call; use id:<ID> when ambiguous. Send bulk items together. complete=false means "
        "results are partial. Batches are not transactions; inspect each outcome and never replay "
        "uncertain mutations. Seat capacity/allocation is not actual attendance. For other tasks, "
        "inspect_schema describes the full API for graphql. This process uses one tenant's "
        "configured API credential. Writes enabled: " + str(settings.enable_mutations).lower(),
    )

    @server.tool(annotations=READ)
    async def workplace_overview(site: str | None = None) -> CallToolResult:
        """Sites, floors, time zones, capacity, allocated/vacant and bookable desk counts.

        site is an exact name or id:<ID>. Counts describe current seat allocation, not attendance.
        """
        return await respond(Workflows(api).overview(site))

    @server.tool(annotations=READ)
    async def find_people(
        search: str | None = None,
        department: str | None = None,
        team: str | None = None,
        active: bool | None = True,
        limit: Limit = 100,
    ) -> CallToolResult:
        """Find employees and their assigned desks, floors and sites in one call.

        search accepts a partial name, exact email, or id:<employee ID>. Department/team use
        exact names. Set active=null to include inactive people. Use get_bookings for reservations.
        """
        return await respond(Workflows(api).find_people(search, department, team, active, limit))

    @server.tool(annotations=READ)
    async def find_spaces(
        kind: Kind,
        site: str | None = None,
        floor: str | None = None,
        search: str | None = None,
        minimum_capacity: Annotated[int, Field(ge=1)] | None = None,
        assets: list[str] | None = None,
        start: AwareDatetime | None = None,
        end: AwareDatetime | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> CallToolResult:
        """Find bookable desks/rooms with location, capacity and features.

        Optionally check a start/end interval internally and exclude conflicting reservations.
        Times need UTC offsets. OfficeSpace still enforces booking policies when reserving.
        site/floor accept exact names or id:<ID>. search is a label substring. minimum_capacity
        applies to rooms; assets requires all listed desk asset names. complete=false means
        more candidates exist: narrow the search, don't conclude that no space is available.
        """
        return await respond(
            Workflows(api).find_spaces(
                kind, site, floor, search, minimum_capacity, assets, start, end, limit
            )
        )

    @server.tool(annotations=READ)
    async def get_bookings(
        kind: Kind,
        start: AwareDatetime,
        end: AwareDatetime,
        person: str | None = None,
        space: Space | None = None,
        site: str | None = None,
    ) -> CallToolResult:
        """Bookings overlapping a time interval, with internal name lookups and pagination.

        person is email, exact name or id:<employee ID>; for rooms this filters the organizer,
        not guests. Without a room filter, the upstream room API does not provide room identity.
        A room report scoped to a site queries its rooms internally (maximum 50).
        complete=false means the report reached the 1,000-record bound; narrow its filters.
        """
        return await respond(Workflows(api).bookings(kind, start, end, person, space, site))

    @server.tool(annotations=WRITE)
    async def book_spaces(bookings: BatchBookings) -> CallToolResult:
        """Book named desks or rooms on behalf of employees, including team batches.

        Resolves every person and space before submitting any writes. Desk times are converted
        to each site's time zone. Room organizerId is an EMPLOYEE ID. Notifications and conflict
        rules stay with OfficeSpace. One call can contain up to 50 reservations. Inspect every
        result: a batch can partially succeed and must not be blindly retried.
        """
        return await respond(Workflows(api).book(bookings))

    @server.tool(annotations=WRITE)
    async def change_bookings(
        kind: Kind,
        action: Literal["cancel", "reschedule", "confirm", "end"],
        ids: IDs | None = None,
        person: str | None = None,
        space: Space | None = None,
        start: AwareDatetime | None = None,
        end: AwareDatetime | None = None,
        new_start: AwareDatetime | None = None,
        new_end: AwareDatetime | None = None,
    ) -> CallToolResult:
        """Cancel, reschedule, confirm or end up to 50 reservations in one call.

        Supply IDs OR a person/space plus start/end to resolve matching reservations internally.
        Reschedule requires new_start/new_end; these apply to every selected booking. Cancel
        future room reservations; use end for a room meeting already in progress. This does not
        cancel an entire recurrence series, only the selected reservation IDs.
        """
        return await respond(
            Workflows(api).change_bookings(
                kind, action, ids, person, space, start, end, new_start, new_end
            )
        )

    @server.tool(annotations=WRITE)
    async def manage_employees(
        action: Literal["create", "update", "deactivate"], changes: BatchEmployees
    ) -> CallToolResult:
        """Create, update or deactivate up to 50 employee records.

        Resolve people by email/exact name/id:<ID>. values uses OfficeSpace field names, e.g.
        department, title, team, email, startDate; inspect_schema exposes CreateEmployeeInput and
        UpdateEmployeeInput for other fields. Create needs values.employeeId (external ID).
        Deactivate changes the employee record only: it does not cancel bookings or vacate seats.
        All local input validation and identity lookups finish before writes are sent.
        """
        return await respond(Workflows(api).employees(action, changes))

    @server.tool(annotations=WRITE)
    async def manage_moves(
        action: Literal["schedule", "complete", "cancel"],
        plans: BatchMoves | None = None,
        ids: IDs | None = None,
        comment: str | None = None,
    ) -> CallToolResult:
        """Schedule a team move, complete moves or cancel moves in one call.

        Schedule uses plans with person, destination and move_date. Source defaults to the
        employee's unique current seat; omit destination to vacate. Complete/cancel use IDs.
        Scheduling does not immediately complete a move. No force-vacate or conflict override.
        """
        return await respond(Workflows(api).moves(action, plans, ids, comment))

    @server.tool(annotations=WRITE)
    async def manage_requests(
        action: Literal["list", "create", "status"],
        changes: BatchRequests | None = None,
        status: str | None = None,
        site: str | None = None,
    ) -> CallToolResult:
        """List facilities requests, submit requests or update/assign/close requests in bulk.

        List accepts top-level status/site and works with writes disabled. Create resolves the
        request type and requestor; each change needs request_type, subject, requestor. Status
        changes need id/status and may include assignee and comment. extra accepts schema-defined
        arguments such as customFieldValues or masterRequestId. File uploads are not supported.
        """
        return await respond(Workflows(api).requests(action, changes, status, site))

    @server.tool(annotations=READ)
    async def inspect_schema(search: str = "", type_name: str | None = None) -> CallToolResult:
        """Discover API operations by name/description, or inspect a GraphQL type definition.

        Use for features outside the common workflows, including leases, assets, neighborhoods,
        presence and move reporting. Type definitions include fields, arguments, enums and docs.
        """
        try:
            return result(api.describe(type_name) if type_name else api.operations(search))
        except OfficeSpaceError as e:
            return result({"ok": False, "error": str(e)}, error=True)

    @server.tool(annotations=WRITE if settings.enable_mutations else READ)
    async def graphql(document: str, variables: dict | None = None) -> CallToolResult:
        """Execute one schema-validated GraphQL query or mutation for advanced admin tasks.

        Discover fields with inspect_schema. Use variables for values. The same write gate
        applies as for workflow tools. Partial data and GraphQL errors are preserved. No automatic
        retries. For mutations request payload error/errors fields and inspect them for failures.
        JSON transport only: multipart Upload inputs and subscriptions are not supported.
        """
        return await respond(api.execute(document, variables))

    @server.resource("officespace://schema", mime_type="text/plain")
    def schema() -> str:
        """The schema snapshot used for local validation."""
        from graphql import print_schema

        return print_schema(api.schema)

    return server


class BearerAuth:
    def __init__(self, app, token: str):
        self.app, self.expected = app, ("Bearer " + token).encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            values = [v for k, v in scope["headers"] if k.lower() == b"authorization"]
            if len(values) != 1 or not hmac.compare_digest(values[0], self.expected):
                response = JSONResponse(
                    {"error": "Unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def http_app(server: MCPServer, settings: Settings, allowed_hosts: list[str] | None = None):
    if not settings.mcp_token:
        raise ValueError("HTTP transport requires MCP_BEARER_TOKEN.")
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=allowed_hosts
            or ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"],
        ),
    )
    return BearerAuth(app, settings.mcp_token)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--export-schema", metavar="FILE", help="Convert introspection JSON to SDL")
    args = parser.parse_args()
    if args.export_schema:
        from graphql import print_schema

        api = API(Settings(schema_path=args.export_schema))
        print(print_schema(api.schema))
        return
    try:
        settings = Settings.from_env()
        server = create_server(settings)
        if args.transport == "stdio":
            server.run()
        else:
            hosts = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
            uvicorn.run(
                http_app(server, settings, hosts), host=args.host, port=args.port, access_log=False
            )
    except (ValueError, OSError) as e:
        parser.error(str(e))


if __name__ == "__main__":
    main()
