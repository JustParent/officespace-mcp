import json
from copy import deepcopy

import httpx
import pytest
from graphql import get_argument_values, get_variable_values, parse

from officespace_mcp.api import API
from officespace_mcp.config import Settings


def connection(rows, next_cursor=None):
    return {"nodes": rows, "pageInfo": {"hasNextPage": bool(next_cursor), "endCursor": next_cursor}}


class Office:
    """Mock OfficeSpace at the HTTP boundary; real schema validation stays in the client."""

    def __init__(self):
        self.settings = Settings(
            graphql_url="https://tenant.example/graphql",
            auth_value="upstream-secret",
            enable_mutations=True,
        )
        self.api = API(self.settings, httpx.MockTransport(self.handle))
        self.calls, self.http_requests, self.overrides = [], [], {}
        self.sites = [{"id": "site1", "name": "London", "timeZone": "Europe/London"}]
        self.floors = [{"id": "floor1", "label": "Ground", "site": self.sites[0]}]
        self.seats = [
            {
                "id": "desk1",
                "label": "A12",
                "bookable": True,
                "isInactive": False,
                "assets": [{"id": "asset1", "name": "Monitor"}],
                "floor": self.floors[0],
            },
            {
                "id": "desk2",
                "label": "A13",
                "bookable": True,
                "isInactive": False,
                "assets": [],
                "floor": self.floors[0],
            },
        ]
        self.people = [
            {
                "id": "employee1",
                "externalId": "hr1",
                "fullName": "Alice Jones",
                "email": "alice@example.com",
                "active": True,
                "department": "Finance",
                "team": "Accounts",
                "seats": [self.seats[0]],
            },
            {
                "id": "employee2",
                "externalId": "hr2",
                "fullName": "Bob Jones",
                "email": "bob@example.com",
                "active": True,
                "department": "Engineering",
                "team": "Product",
                "seats": [self.seats[1]],
            },
        ]
        self.rooms = [
            {
                "id": "room1",
                "label": "Orion",
                "capacity": 6,
                "bookingCapacity": 6,
                "attributes": [],
                "floor": self.floors[0],
            }
        ]
        self.users = [{"id": "user1", "fullName": "Alice Jones", "email": "alice@example.com"}]
        self.desk_bookings = [self.desk_booking("b1", self.people[0], self.seats[0])]
        self.room_bookings = []

    @staticmethod
    def desk_booking(id, person, seat):
        return {
            "id": id,
            "employee": person,
            "seat": seat,
            "checkInTime": "2026-10-01T08:00:00Z",
            "checkOutScheduled": "2026-10-01T16:00:00Z",
            "checkOutTime": None,
            "isCanceled": False,
            "isActive": False,
        }

    @property
    def writes(self):
        return [call for call in self.calls if call[0] == "mutation"]

    def handle(self, request):
        self.http_requests.append(request)
        assert str(request.url) == self.settings.graphql_url
        assert request.headers["apikey"] == "upstream-secret"
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        assert "upstream-secret" not in body["query"]
        ast = parse(body["query"])
        operation = ast.definitions[0]
        root = (
            self.api.schema.query_type
            if operation.operation.value == "query"
            else (self.api.schema.mutation_type)
        )
        data = {}
        variables = get_variable_values(
            self.api.schema, operation.variable_definitions or [], body["variables"]
        )
        for selection in operation.selection_set.selections:
            name = selection.name.value
            args = get_argument_values(root.fields[name], selection, variables)
            self.calls.append((operation.operation.value, name, args))
            if name in self.overrides:
                override = self.overrides[name]
                value = override(args) if callable(override) else deepcopy(override)
            else:
                value = self.resolve(name, args)
            data[selection.alias.value if selection.alias else name] = value
        return httpx.Response(200, json={"data": data})

    def resolve(self, name, args):
        tables = {
            "employees": self.people,
            "sites": self.sites,
            "floors": self.floors,
            "seats": self.seats,
            "rooms": self.rooms,
            "users": self.users,
            "booking": self.desk_bookings,
        }
        if name in tables:
            rows = deepcopy(tables[name])
            for arg, key in [("ids", "id"), ("emails", "email")]:
                if arg in args:
                    rows = [r for r in rows if r[key] in args[arg]]
            for arg, key in [
                ("name", "fullName" if name == "employees" else "name"),
                ("label", "label"),
                ("query", "fullName"),
            ]:
                if arg in args:
                    rows = [r for r in rows if args[arg].casefold() in r[key].casefold()]
            if "floorIds" in args:
                rows = [r for r in rows if r["floor"]["id"] in args["floorIds"]]
            return rows
        if name == "employeesPaginated":
            return connection(self.people)
        if name == "bookings":
            return connection(self.desk_bookings)
        if name == "roomBookings":
            return connection(self.room_bookings)
        if name == "requestTypes":
            return [{"id": "type1", "name": "Maintenance"}]
        if name == "requestsPaginated":
            return connection([])
        if name == "createBooking":
            return {"booking": {"id": "created"}, "seat": {"id": args["seatId"]}, "error": None}
        if name == "bookRoom":
            return {"roomBooking": {"id": "meeting"}, "errors": []}
        if name in {"cancelBooking", "endBooking"}:
            return {"id": args["id"]}
        if name == "updateBooking":
            return {"booking": {"id": args["id"]}, "error": None}
        if name == "createMoves":
            return {"moves": [{"id": "move1"}], "errors": [], "failedCount": 0}
        if name in self.api.schema.mutation_type.fields:
            return {"errors": []}
        raise AssertionError(f"No mock configured for {name}")


@pytest.fixture
def office():
    return Office()
