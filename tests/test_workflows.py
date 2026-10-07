from copy import deepcopy
from dataclasses import replace
from datetime import datetime

import pytest
from conftest import connection
from mcp import Client

from officespace_mcp.api import API, OfficeSpaceError
from officespace_mcp.config import Settings
from officespace_mcp.models import Booking, EmployeeChange, Move, RequestChange, Space
from officespace_mcp.server import create_server
from officespace_mcp.workflows import Workflows, local_booking_times

START = datetime.fromisoformat("2026-10-01T08:00:00Z")
END = datetime.fromisoformat("2026-10-01T16:00:00Z")


def booking(**overrides):
    return Booking.model_validate(
        {
            "kind": "desk",
            "space": {"reference": "A12", "site": "London"},
            "person": "alice@example.com",
            "start": START,
            "end": END,
            **overrides,
        }
    )


async def test_team_booking_is_one_mcp_call_and_one_write_batch(office):
    server = create_server(office.settings, office.api.transport)
    async with Client(server) as client:
        response = await client.call_tool(
            "book_spaces",
            {
                "bookings": [
                    booking().model_dump(mode="json"),
                    booking(
                        space={"reference": "A13", "site": "London"}, person="bob@example.com"
                    ).model_dump(mode="json"),
                ]
            },
        )
    assert not response.is_error
    assert len(office.writes) == 2
    writes = [r for r in office.http_requests if b"mutation OfficeSpace" in r.content]
    assert len(writes) == 1
    assert [args["checkInTime"] for _, _, args in office.writes] == ["09:00", "09:00"]
    assert [args["employeeId"] for _, _, args in office.writes] == ["employee1", "employee2"]
    assert len([c for c in office.calls if c[1] == "sites"]) == 1  # request-scoped lookup cache


async def test_room_booking_uses_employee_not_user_id(office):
    result = await Workflows(office.api).book(
        [booking(kind="room", space={"reference": "Orion"}, title="Planning")]
    )
    assert result["ok"]
    assert office.writes[0][1] == "bookRoom"
    assert office.writes[0][2]["organizerId"] == "employee1"
    assert office.writes[0][2]["startTime"] == START.isoformat()


async def test_ambiguous_second_person_prevents_entire_batch(office):
    office.people.append(dict(office.people[1], id="employee3"))
    with pytest.raises(OfficeSpaceError, match="found 2"):
        await Workflows(office.api).book(
            [booking(), booking(person="Bob Jones", space={"reference": "A13"})]
        )
    assert not office.writes


async def test_duplicate_desk_labels_require_scope(office):
    office.seats.append(dict(office.seats[0], id="desk3"))
    with pytest.raises(OfficeSpaceError, match="Candidates"):
        await Workflows(office.api).book([booking()])
    assert not office.writes


async def test_duplicate_bookings_are_rejected_before_writes(office):
    with pytest.raises(OfficeSpaceError, match="Duplicate reservation"):
        await Workflows(office.api).book([booking(), booking()])
    assert not office.writes


async def test_invalid_employee_field_in_last_item_prevents_all_writes(office):
    with pytest.raises(OfficeSpaceError, match="notDefined"):
        await Workflows(office.api).employees(
            "update",
            [
                EmployeeChange(person="alice@example.com", values={"department": "Legal"}),
                EmployeeChange(person="bob@example.com", values={"notDefined": "bad"}),
            ],
        )
    assert not office.writes


async def test_update_and_deactivate_employee_contracts(office):
    workflow = Workflows(office.api)
    result = await workflow.employees(
        "update",
        [EmployeeChange(person="alice@example.com", values={"department": "Legal", "title": None})],
    )
    assert result["ok"]
    assert office.writes[-1][2]["input"] == {
        "id": "employee1",
        "department": "Legal",
        "title": None,
    }
    await workflow.employees("deactivate", [EmployeeChange(person="alice@example.com")])
    assert office.writes[-1][2] == {"ids": ["employee1"]}


async def test_create_employee_contract(office):
    await Workflows(office.api).employees(
        "create",
        [
            EmployeeChange(
                values={
                    "employeeId": "HR123",
                    "firstName": "Cathy",
                    "lastName": "Smith",
                    "department": "Sales",
                }
            )
        ],
    )
    assert office.writes[-1][2]["employee"]["employeeId"] == "HR123"


async def test_cancel_by_person_follows_pages_before_writes(office):
    first = office.desk_bookings[0]
    second = dict(first, id="b2")
    office.overrides["bookings"] = lambda args: (
        connection([first], "page2") if not args.get("after") else connection([second])
    )
    result = await Workflows(office.api).change_bookings(
        "desk", "cancel", None, "alice@example.com", None, START, END, None, None
    )
    assert result["ok"]
    assert [args["id"] for _, _, args in office.writes] == ["b1", "b2"]
    assert [c[0] for c in office.calls][-2:] == ["mutation", "mutation"]
    assert len([c for c in office.calls if c[1] == "bookings"]) == 2


async def test_truncated_selection_prevents_cancellation(office):
    api = API(
        Settings(
            graphql_url=office.settings.graphql_url,
            auth_value=office.settings.auth_value,
            enable_mutations=True,
            max_records=1,
        ),
        office.api.transport,
    )
    office.overrides["bookings"] = connection(office.desk_bookings, "more")
    with pytest.raises(OfficeSpaceError, match="exceed the limit"):
        await Workflows(api).change_bookings(
            "desk", "cancel", None, "alice@example.com", None, START, END, None, None
        )
    assert not office.writes


async def test_person_selection_does_not_cancel_another_employee(office):
    office.desk_bookings.append(office.desk_booking("b2", office.people[1], office.seats[1]))
    await Workflows(office.api).change_bookings(
        "desk", "cancel", None, "alice@example.com", None, START, END, None, None
    )
    assert [args["id"] for _, _, args in office.writes] == ["b1"]


@pytest.mark.parametrize("kind", ["desk", "room"])
@pytest.mark.parametrize("action", ["cancel", "reschedule", "confirm", "end"])
async def test_all_booking_change_contracts(office, kind, action):
    start, end = (START, END) if action == "reschedule" else (None, None)
    result = await Workflows(office.api).change_bookings(
        kind, action, ["b1"], None, None, None, None, start, end
    )
    assert result["ok"]


async def test_room_availability_checks_whole_interval_not_just_start(office):
    office.rooms.append(dict(office.rooms[0], id="room2", label="Vega"))
    busy = {
        "id": "meeting1",
        "startTime": "2026-10-01T09:00:00Z",
        "endTime": "2026-10-01T10:00:00Z",
        "owner": office.users[0],
    }
    office.overrides["roomBookings"] = lambda args: connection(
        [busy] if args.get("roomId") == "room1" else []
    )
    result = await Workflows(office.api).find_spaces(
        "room", "London", None, None, 6, None, START, END, 25
    )
    assert [r["label"] for r in result["items"]] == ["Vega"]


async def test_desks_assets_and_no_conflict_contract(office):
    office.desk_bookings.clear()
    result = await Workflows(office.api).find_spaces(
        "desk", None, None, None, None, ["monitor"], START, END, 25
    )
    assert [r["label"] for r in result["items"]] == ["A12"]
    assert result["complete"]


async def test_room_site_report_attaches_room_identity(office):
    office.room_bookings = [
        {
            "id": "meeting1",
            "startTime": START.isoformat(),
            "endTime": END.isoformat(),
            "owner": office.users[0],
        }
    ]
    result = await Workflows(office.api).bookings(
        "room", START, END, "alice@example.com", None, "London"
    )
    assert result["items"][0]["room"]["label"] == "Orion"


async def test_schedule_move_resolves_both_seats_and_never_forces(office):
    result = await Workflows(office.api).moves(
        "schedule",
        [
            Move(
                person="alice@example.com",
                destination=Space(reference="A13"),
                move_date="2026-10-05",
            )
        ],
        None,
        None,
    )
    assert result["ok"]
    args = office.writes[-1][2]
    assert args == {
        "input": [
            {
                "employee": {"id": "employee1"},
                "from": {"id": "desk1"},
                "to": {"id": "desk2"},
                "moveDate": "2026-10-05",
            }
        ],
        "forceVacateMove": False,
    }


async def test_vacate_uses_documented_minus_one_sentinel(office):
    await Workflows(office.api).moves(
        "schedule", [Move(person="alice@example.com", move_date="2026-10-05")], None, None
    )
    assert office.writes[-1][2]["input"][0]["to"] == {"id": "-1"}


@pytest.mark.parametrize("action", ["cancel", "complete"])
async def test_move_status_contracts(office, action):
    assert (await Workflows(office.api).moves(action, None, ["move1"], None))["ok"]


async def test_create_and_assign_facilities_request(office):
    workflow = Workflows(office.api)
    await workflow.requests(
        "create",
        [
            RequestChange(
                request_type="Maintenance",
                subject="Broken desk",
                requestor="alice@example.com",
                site="London",
            )
        ],
        None,
        None,
    )
    assert office.writes[-1][2] == {
        "requestTypeId": "type1",
        "subject": "Broken desk",
        "requestor": "Alice Jones",
        "requestorEmail": "alice@example.com",
        "siteId": "site1",
        "skipCustomFields": False,
    }
    await workflow.requests(
        "status",
        [
            RequestChange(
                id="req1",
                status="DELEGATED",
                assignee="alice@example.com",
                comment="Please investigate",
            )
        ],
        None,
        None,
    )
    assert office.writes[-1][2]["toUserId"] == "user1"


async def test_list_requests_and_directory_overview_contracts(office):
    workflow = Workflows(office.api)
    assert (await workflow.requests("list", None, "OPENED", None))["complete"]
    assert (await workflow.overview("London"))["sites"][0]["id"] == "site1"
    found = await workflow.find_people(None, "Finance", None, True, 100)
    assert [p["id"] for p in found["items"]] == ["employee1"]


async def test_no_stale_lookup_cache_across_mcp_calls(office):
    server = create_server(office.settings, office.api.transport)
    async with Client(server) as client:
        payload = {
            "action": "update",
            "changes": [{"person": "alice@example.com", "values": {"department": "Sales"}}],
        }
        assert not (await client.call_tool("manage_employees", payload)).is_error
        office.people.append(deepcopy(office.people[0]))
        assert (await client.call_tool("manage_employees", payload)).is_error
    assert len(office.writes) == 1


@pytest.mark.parametrize(
    "instant, expected", [("2026-10-01T08:00:00Z", "09:00"), ("2026-12-01T08:00:00Z", "08:00")]
)
def test_desk_timezone_handles_seasonal_offset(instant, expected):
    start = datetime.fromisoformat(instant)
    from datetime import timedelta

    assert (
        local_booking_times(start, start + timedelta(hours=1), "Europe/London")["checkInTime"]
        == expected
    )


def test_dst_ambiguity_is_rejected():
    with pytest.raises(OfficeSpaceError, match="DST"):
        local_booking_times(
            datetime.fromisoformat("2026-10-25T00:30:00Z"),
            datetime.fromisoformat("2026-10-25T03:30:00Z"),
            "Europe/London",
        )


BOOK_ARGS = {"bookings": [booking().model_dump(mode="json")]}


async def validate(office, tool, arguments, settings=None):
    server = create_server(settings or office.settings, office.api.transport)
    async with Client(server) as client:
        return await client.call_tool("validate_request", {"tool": tool, "arguments": arguments})


async def test_validate_request_previews_resolved_booking_without_writing(office):
    response = await validate(office, "book_spaces", BOOK_ARGS)
    assert not response.is_error
    payload = response.structured_content
    assert payload["ok"] and payload["dry_run"] and payload["writes_sent"] is False
    (item,) = payload["would_submit"]
    assert item["operation"] == "createBooking"
    assert item["arguments"]["employeeId"] == "employee1"
    assert item["summary"] == {
        "person": "Alice Jones",
        "email": "alice@example.com",
        "space": "A12",
        "floor": "Ground",
        "site": "London",
        "start": "2026-10-01 09:00",
        "end": "2026-10-01 17:00",
        "time_zone": "Europe/London",
    }
    assert not office.writes


async def test_validate_request_arguments_match_what_the_real_write_sends(office):
    preview = (await validate(office, "book_spaces", BOOK_ARGS)).structured_content
    async with Client(create_server(office.settings, office.api.transport)) as client:
        assert not (await client.call_tool("book_spaces", BOOK_ARGS)).is_error
    assert [(i["operation"], i["arguments"]) for i in preview["would_submit"]] == [
        (name, args) for _, name, args in office.writes
    ]


async def test_validate_request_surfaces_lookup_failures_before_any_write(office):
    office.seats.append(dict(office.seats[0], id="desk3"))
    response = await validate(office, "book_spaces", BOOK_ARGS)
    assert response.is_error
    assert "Candidates" in response.structured_content["error"]
    assert not office.writes


async def test_validate_request_reports_bad_argument_shape_without_calling_upstream(office):
    bad = {"bookings": [{k: v for k, v in BOOK_ARGS["bookings"][0].items() if k != "person"}]}
    response = await validate(office, "book_spaces", bad)
    assert response.is_error
    assert "bookings.0.person" in response.structured_content["error"]
    assert not office.calls


async def test_validate_request_rejects_unknown_arguments(office):
    response = await validate(office, "book_spaces", {**BOOK_ARGS, "dry_run": True})
    assert response.is_error
    assert "dry_run" in response.structured_content["error"]
    assert not office.calls


async def test_validate_request_catches_invalid_graphql_fields_in_workflow_values(office):
    response = await validate(
        office,
        "manage_employees",
        {
            "action": "update",
            "changes": [{"person": "alice@example.com", "values": {"notDefined": "bad"}}],
        },
    )
    assert response.is_error
    assert "notDefined" in response.structured_content["error"]
    assert not office.writes


async def test_validate_request_checks_raw_graphql_locally_without_network(office):
    good = 'mutation { cancelBooking(id: "b1") { id } }'
    ok = await validate(office, "graphql", {"document": good})
    assert not ok.is_error and ok.structured_content["dry_run"]
    bad = await validate(office, "graphql", {"document": "mutation { noSuchMutation { id } }"})
    assert bad.is_error
    assert not office.calls


async def test_validate_request_respects_the_write_gate(office):
    response = await validate(
        office, "book_spaces", BOOK_ARGS, replace(office.settings, enable_mutations=False)
    )
    assert response.is_error
    assert "disabled" in response.structured_content["error"]
    assert not office.calls


async def test_validate_request_rejects_tools_it_cannot_dry_run(office):
    response = await validate(office, "find_people", {})
    assert response.is_error
    assert "'book_spaces'" in response.content[0].text  # the error lists what it can validate
    assert not office.calls


async def test_validating_does_not_turn_later_real_calls_into_dry_runs(office):
    async with Client(create_server(office.settings, office.api.transport)) as client:
        await client.call_tool("validate_request", {"tool": "book_spaces", "arguments": BOOK_ARGS})
        assert not office.writes
        response = await client.call_tool("book_spaces", BOOK_ARGS)
    assert not response.is_error
    assert "dry_run" not in response.structured_content
    assert len(office.writes) == 1
