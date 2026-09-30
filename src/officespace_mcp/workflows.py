"""Admin tasks: resolve human references, paginate and submit validated batches."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from graphql import get_named_type, is_leaf_type

from .api import API, OfficeSpaceError, Operation
from .models import Booking, EmployeeChange, Kind, Move, RequestChange, Space

LOCATION = "id label site { id name timeZone }"
PERSON = (
    "id externalId fullName email active department team title assignedSiteName "
    "seats(excludeVisitors: true) { id label floor { " + LOCATION + " } }"
)
DESK = "id label bookable isInactive assets { id name } floor { " + LOCATION + " }"
ROOM = "id label capacity bookingCapacity attributes { name value } floor { " + LOCATION + " }"
DESK_BOOKING = (
    "id checkInTime checkOutScheduled checkOutTime isCanceled isActive "
    "employee { id externalId fullName email } seat { id label floor { " + LOCATION + " } }"
)
ROOM_BOOKING = "id title startTime endTime confirmedAt owner { id fullName email }"
MOVE = (
    "id moveDate completedDate description hasConflict conflictMessage fromId toId "
    "employee { id fullName email }"
)
REQUEST = (
    "id subject status createdAt dueAt requestType { id name } "
    "requestor { id fullName email } assignee { id fullName email } site { id name }"
)
PROJECTIONS = {
    "Employee": PERSON,
    "SeatOpenBooking": DESK_BOOKING,
    "RoomBooking": ROOM_BOOKING,
    "MoveEmployee": MOVE,
    "Request": REQUEST,
    "Seat": "id label",
    "Room": "id label",
    "Error": "attribute errors label",
    "SeatBookingError": "code fieldName message conflictingBookings",
}


def identify(items: list[dict], reference: str, keys: tuple[str, ...]) -> dict:
    by_id = reference.startswith("id:")
    wanted = reference[3:] if by_id else reference
    keys = ("id",) if by_id else keys
    matches = [
        item
        for item in items
        if any(str(item.get(k) or "").casefold() == wanted.casefold() for k in keys)
    ]
    if len(matches) != 1:
        candidates = [
            {
                k: row.get(k)
                for k in ("id", "fullName", "email", "name", "label", "floor")
                if k in row
            }
            for row in (matches or items)[:10]
        ]
        raise OfficeSpaceError(
            f"Expected one exact match for {reference!r}; found {len(matches)}. "
            f"Use id:<ID> or narrow the site/floor. Candidates: {candidates}"
        )
    return matches[0]


def bounded(items: list, limit: int):
    return {"items": items[:limit], "complete": len(items) <= limit, "matching_count": len(items)}


def time_window(start: datetime | None, end: datetime | None):
    if not start or not end or not start.tzinfo or not end.tzinfo or end <= start:
        raise OfficeSpaceError("Provide start and end with UTC offsets, with end after start.")


def local_booking_times(start: datetime, end: datetime, zone: str) -> dict:
    time_window(start, end)
    if start.second or end.second or start.microsecond or end.microsecond:
        raise OfficeSpaceError("Desk bookings accept whole minutes only.")
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise OfficeSpaceError(f"Unrecognised site time zone {zone!r}.") from None
    start, end = start.astimezone(tz), end.astimezone(tz)
    for value in (start, end):
        if value.replace(fold=0).utcoffset() != value.replace(fold=1).utcoffset():
            raise OfficeSpaceError("Desk API local times cannot disambiguate a DST clock change.")
    return {
        "checkInDate": start.strftime("%Y-%m-%d"),
        "checkInTime": start.strftime("%H:%M"),
        "checkOutDate": end.strftime("%Y-%m-%d"),
        "checkOutTime": end.strftime("%H:%M"),
    }


class Workflows:
    def __init__(self, api: API):
        self.api = api
        # A Workflows instance lasts one MCP call, so lookups cannot go stale between calls.
        self.people_cache, self.user_cache, self.site_cache = {}, {}, {}
        self.floor_cache, self.space_cache = {}, {}

    def mutation(self, name: str, arguments: dict) -> Operation:
        result_type = get_named_type(self.api.schema.mutation_type.fields[name].type)
        if result_type.name in PROJECTIONS:
            selection = PROJECTIONS[result_type.name]
        else:
            fields = []
            for key, value in result_type.fields.items():
                nested = get_named_type(value.type)
                if value.args:
                    continue
                if is_leaf_type(nested):
                    fields.append(key)
                elif nested.name in PROJECTIONS:
                    fields.append(key + " { " + PROJECTIONS[nested.name] + " }")
            selection = " ".join(fields) or "__typename"
        return Operation(name, arguments, selection)

    async def person(self, ref: str) -> dict:
        if ref in self.people_cache:
            return self.people_cache[ref]
        args = (
            {"ids": [ref[3:]]}
            if ref.startswith("id:")
            else ({"emails": [ref]} if "@" in ref else {"name": ref})
        )
        rows = await self.api.query("employees", args, PERSON)
        self.people_cache[ref] = identify(rows, ref, ("email", "fullName"))
        return self.people_cache[ref]

    async def user(self, ref: str) -> dict:
        if ref in self.user_cache:
            return self.user_cache[ref]
        args = (
            {"ids": [ref[3:]]}
            if ref.startswith("id:")
            else ({"emails": [ref]} if "@" in ref else {"query": ref})
        )
        rows = await self.api.query("users", args, "id fullName email")
        self.user_cache[ref] = identify(rows, ref, ("email", "fullName"))
        return self.user_cache[ref]

    async def site(self, ref: str) -> dict:
        if ref in self.site_cache:
            return self.site_cache[ref]
        args = {"ids": [ref[3:]]} if ref.startswith("id:") else {"name": ref}
        self.site_cache[ref] = identify(
            await self.api.query("sites", args, "id name timeZone"), ref, ("name",)
        )
        return self.site_cache[ref]

    async def floor_ids(self, site: str | None, floor: str | None) -> list[str] | None:
        key = (site, floor)
        if key in self.floor_cache:
            return self.floor_cache[key]
        if not site and not floor:
            return None
        args = {"siteIds": [(await self.site(site))["id"]]} if site else {}
        if floor and floor.startswith("id:"):
            args["ids"] = [floor[3:]]
        rows = await self.api.query("floors", args, LOCATION)
        if floor:
            rows = [identify(rows, floor, ("label",))]
        self.floor_cache[key] = [row["id"] for row in rows]
        return self.floor_cache[key]

    async def space(self, kind: Kind, ref: Space) -> dict:
        key = (kind, ref.model_dump_json())
        if key in self.space_cache:
            return self.space_cache[key]
        floors = await self.floor_ids(ref.site, ref.floor)
        if floors == []:
            raise OfficeSpaceError("No floors match this site.")
        args = {"floorIds": floors} if floors is not None else {}
        args.update(
            {"ids": [ref.reference[3:]]}
            if ref.reference.startswith("id:")
            else {"label": ref.reference}
        )
        rows = await self.api.query(
            "seats" if kind == "desk" else "rooms", args, DESK if kind == "desk" else ROOM
        )
        self.space_cache[key] = identify(rows, ref.reference, ("label",))
        return self.space_cache[key]

    async def find_people(
        self,
        search: str | None,
        department: str | None,
        team: str | None,
        active: bool | None,
        limit: int,
    ):
        args = {"active": active} if active is not None else {}
        if search:
            if search.startswith("id:"):
                args["ids"] = [search[3:]]
            elif "@" in search:
                args["emails"] = [search]
            else:
                args["name"] = search
        if search and "name" in args:
            rows = await self.api.query("employees", args, PERSON)
            complete = True
        else:
            page = await self.api.collect("employeesPaginated", args, PERSON)
            rows, complete = page["items"], page["complete"]
        rows = [
            r
            for r in rows
            if r
            and (not department or (r.get("department") or "").casefold() == department.casefold())
            and (not team or (r.get("team") or "").casefold() == team.casefold())
        ]
        result = bounded(rows, limit)
        result["complete"] &= complete
        return result

    async def overview(self, site: str | None):
        args = {"ids": [(await self.site(site))["id"]]} if site else {}
        rows = await self.api.query(
            "sites",
            args,
            "id name timeZone capacity occupiedSeatsCount vacantSeatsCount openBookableSeatsCount "
            "floors { id label online managed maxCapacity }",
        )
        return {"sites": rows, "basis": "Current seat allocation and capacity, not attendance."}

    async def find_spaces(
        self,
        kind: Kind,
        site: str | None,
        floor: str | None,
        search: str | None,
        minimum_capacity: int | None,
        assets: list[str] | None,
        start: datetime | None,
        end: datetime | None,
        limit: int,
    ):
        if start or end:
            time_window(start, end)
        if minimum_capacity is not None and kind != "room":
            raise OfficeSpaceError("minimum_capacity applies to rooms.")
        if assets and kind != "desk":
            raise OfficeSpaceError(
                "assets applies to desks; room attributes are returned separately."
            )
        floors = await self.floor_ids(site, floor)
        if floors == []:
            return {"items": [], "complete": True}
        args = {"bookableOnly": True, **({"floorIds": floors} if floors is not None else {})}
        if kind == "desk" and start:
            args["availableDatetime"] = start.isoformat()
        rows = await self.api.query(
            "seats" if kind == "desk" else "rooms", args, DESK if kind == "desk" else ROOM
        )
        rows = [
            r
            for r in rows
            if (not search or search.casefold() in r["label"].casefold())
            and (
                minimum_capacity is None
                or (
                    r.get("bookingCapacity")
                    if r.get("bookingCapacity") is not None
                    else r.get("capacity") or 0
                )
                >= minimum_capacity
            )
            and (
                not assets
                or {a.casefold() for a in assets}
                <= {a["name"].casefold() for a in r.get("assets", [])}
            )
            and not r.get("isInactive", False)
        ]
        complete = len(rows) <= limit
        rows = rows[:limit]
        if start:
            semaphore = asyncio.Semaphore(5)

            async def check(row):
                async with semaphore:
                    page = await self.bookings(
                        kind, start, end, None, Space(reference="id:" + row["id"]), None
                    )
                    if not page["complete"]:
                        raise OfficeSpaceError(
                            "Availability scan incomplete; narrow the time range."
                        )
                    return not any(not b.get("isCanceled", False) for b in page["items"])

            free = await asyncio.gather(*(check(row) for row in rows))
            rows = [row for row, available in zip(rows, free, strict=True) if available]
        return {
            "items": rows,
            "complete": complete,
            "availability": "No conflicting reservations in the requested interval."
            if start
            else "Bookable inventory; no time interval checked.",
            "booking_policy": "OfficeSpace checks permissions, schedules and races when booking.",
        }

    async def bookings(
        self,
        kind: Kind,
        start: datetime,
        end: datetime,
        person: str | None,
        space: Space | None,
        site: str | None,
    ):
        time_window(start, end)
        who = await self.person(person) if person else None
        place = await self.space(kind, space) if space else None
        if kind == "desk":
            args = {"limitingPeriodStart": start.isoformat(), "limitingPeriodEnd": end.isoformat()}
            if who:
                args["clientEmployeeIds"] = [who["externalId"]]
            if place:
                args["seatIds"] = [place["id"]]
            if site:
                args["siteIds"] = [(await self.site(site))["id"]]
            result = await self.api.collect("bookings", args, DESK_BOOKING)
            result["items"] = [
                r
                for r in result["items"]
                if r
                and (not who or r["employee"]["id"] == who["id"])
                and (not place or r["seat"]["id"] == place["id"])
                and datetime.fromisoformat(r["checkInTime"]) < end
                and datetime.fromisoformat(r["checkOutTime"] or r["checkOutScheduled"]) > start
            ]
        else:
            if site and not place:
                floors = await self.floor_ids(site, None)
                rooms = (
                    await self.api.query("rooms", {"floorIds": floors}, "id label")
                    if floors
                    else []
                )
                if len(rooms) > 50:
                    raise OfficeSpaceError("Site has over 50 rooms; select a specific room.")
                pages = []
                for room in rooms:
                    pages.append(
                        await self.bookings(
                            kind, start, end, None, Space(reference="id:" + room["id"]), None
                        )
                    )
                items = [item for page in pages for item in page["items"]]
                result = bounded(items, self.api.settings.max_records)
                result["complete"] &= all(page["complete"] for page in pages)
            else:
                args = {"startTime": start.isoformat(), "endTime": end.isoformat()}
                if place:
                    args["roomId"] = place["id"]
                    if site and place["floor"]["site"]["id"] != (await self.site(site))["id"]:
                        raise OfficeSpaceError("Room does not belong to the specified site.")
                result = await self.api.collect("roomBookings", args, ROOM_BOOKING)
                result["items"] = [
                    dict(r, room=place)
                    for r in result["items"]
                    if datetime.fromisoformat(r["startTime"]) < end
                    and datetime.fromisoformat(r["endTime"]) > start
                ]
            if who:
                if not who.get("email"):
                    raise OfficeSpaceError("Employee has no email; cannot match room organizer.")
                result["items"] = [
                    r
                    for r in result["items"]
                    if (r.get("owner") or {}).get("email", "").casefold() == who["email"].casefold()
                ]
        result["count"] = len(result["items"])
        result["kind"] = kind
        return result

    async def book(self, bookings: list[Booking]):
        self.api.require_writes()
        operations, seen = [], set()
        for item in bookings:
            who = await self.person(item.person)
            place = await self.space(item.kind, item.space)
            key = (item.kind, place["id"], who["id"], item.start, item.end)
            if key in seen:
                raise OfficeSpaceError("Duplicate reservation in this batch; no writes sent.")
            seen.add(key)
            if not who["active"]:
                raise OfficeSpaceError(f"Employee {item.person!r} is inactive.")
            if item.kind == "desk":
                if not place["bookable"] or place["isInactive"]:
                    raise OfficeSpaceError(f"Desk {place['label']!r} is not bookable.")
                args = {
                    "employeeId": who["id"],
                    "seatId": place["id"],
                    **local_booking_times(item.start, item.end, place["floor"]["site"]["timeZone"]),
                }
                if item.note is not None:
                    args["note"] = item.note
                name = "createBooking"
            else:
                args = {
                    "roomId": place["id"],
                    "organizerId": who["id"],
                    "title": item.title,
                    "startTime": item.start.isoformat(),
                    "endTime": item.end.isoformat(),
                    "guestEmails": item.guest_emails,
                }
                if item.note is not None:
                    args["description"] = item.note
                name = "bookRoom"
            operations.append(self.mutation(name, args))
        return await self.api.mutate(operations)

    async def change_bookings(
        self,
        kind: Kind,
        action: str,
        ids: list[str] | None,
        person: str | None,
        space: Space | None,
        start: datetime | None,
        end: datetime | None,
        new_start: datetime | None,
        new_end: datetime | None,
    ):
        self.api.require_writes()
        if ids and any(x is not None for x in (person, space, start, end)):
            raise OfficeSpaceError("Supply IDs or a person/space and interval, not both.")
        if action == "reschedule":
            time_window(new_start, new_end)
        elif new_start or new_end:
            raise OfficeSpaceError("new_start/new_end apply only to reschedule.")
        if not ids:
            if not person and not space:
                raise OfficeSpaceError("Supply IDs or a person/space and a bounded interval.")
            page = await self.bookings(kind, start, end, person, space, None)
            if not page["complete"]:
                raise OfficeSpaceError("Matching bookings exceed the limit; narrow the selection.")
            ids = [r["id"] for r in page["items"] if not r.get("isCanceled", False)]
        if len(ids) > 50 or len(ids) != len(set(ids)):
            raise OfficeSpaceError("Supply at most 50 unique booking IDs.")
        if not ids:
            return {"ok": True, "results": [], "matched": 0}
        desks = {}
        if kind == "desk" and action == "reschedule":
            rows = await self.api.query("booking", {"ids": ids}, DESK_BOOKING)
            desks = {row["id"]: row for row in rows}
            if set(ids) != set(desks):
                raise OfficeSpaceError("Some desk bookings could not be resolved; no updates sent.")
        names = {
            ("desk", "cancel"): "cancelBooking",
            ("desk", "reschedule"): "updateBooking",
            ("desk", "confirm"): "confirmSeatOpenBooking",
            ("desk", "end"): "endBooking",
            ("room", "cancel"): "deleteRoomBooking",
            ("room", "reschedule"): "updateRoomBooking",
            ("room", "confirm"): "confirmRoomBooking",
            ("room", "end"): "updateRoomBooking",
        }
        operations = []
        for booking_id in ids:
            args = {"id": booking_id}
            if action == "reschedule":
                if kind == "desk":
                    args.update(
                        local_booking_times(
                            new_start,
                            new_end,
                            desks[booking_id]["seat"]["floor"]["site"]["timeZone"],
                        )
                    )
                else:
                    args.update(startTime=new_start.isoformat(), endTime=new_end.isoformat())
            if action == "end" and kind == "room":
                from datetime import UTC

                args["endTime"] = datetime.now(UTC).isoformat()
            operations.append(self.mutation(names[kind, action], args))
        return await self.api.mutate(operations)

    async def employees(self, action: str, changes: list[EmployeeChange]):
        self.api.require_writes()
        operations, ids = [], []
        for change in changes:
            if action == "create":
                if change.person is not None:
                    raise OfficeSpaceError("Create uses values.employeeId; omit person.")
                operations.append(self.mutation("createEmployee", {"employee": change.values}))
            else:
                if not change.person:
                    raise OfficeSpaceError("Update/deactivate requires person.")
                who = await self.person(change.person)
                if who["id"] in ids:
                    raise OfficeSpaceError("The same employee appears more than once.")
                ids.append(who["id"])
                if "id" in change.values:
                    raise OfficeSpaceError("Use person to choose the employee, not values.id.")
                if action == "update":
                    if not change.values:
                        raise OfficeSpaceError("Update requires at least one value.")
                    operations.append(
                        self.mutation(
                            "updateEmployee",
                            {
                                "input": {**change.values, "id": who["id"]},
                            },
                        )
                    )
                elif change.values:
                    raise OfficeSpaceError("Deactivate does not accept values.")
        if action == "deactivate":
            operations = [self.mutation("deactivateEmployees", {"ids": ids})]
        return await self.api.mutate(operations)

    async def moves(
        self, action: str, plans: list[Move] | None, ids: list[str] | None, comment: str | None
    ):
        self.api.require_writes()
        if action == "schedule":
            if ids or not plans or comment:
                raise OfficeSpaceError("Schedule requires plans only; put notes in description.")
            inputs = []
            for plan in plans:
                who = await self.person(plan.person)
                if any(row["employee"]["id"] == who["id"] for row in inputs):
                    raise OfficeSpaceError("Schedule each employee only once in a batch.")
                if plan.source:
                    source = await self.space("desk", plan.source)
                    if source["id"] not in {s["id"] for s in who["seats"]}:
                        raise OfficeSpaceError("The source is not one of the employee's seats.")
                elif len(who["seats"]) > 1:
                    raise OfficeSpaceError(f"{plan.person} has multiple seats; specify source.")
                else:
                    source = next(iter(who["seats"]), None)
                target = await self.space("desk", plan.destination) if plan.destination else None
                if not source and not target:
                    raise OfficeSpaceError("Cannot vacate an employee with no assigned seat.")
                entry = {
                    "employee": {"id": who["id"]},
                    "moveDate": plan.move_date.isoformat(),
                    "to": {"id": target["id"] if target else "-1"},
                }
                if source:
                    entry["from"] = {"id": source["id"]}
                if plan.description is not None:
                    entry["description"] = plan.description
                inputs.append(entry)
            operations = [self.mutation("createMoves", {"input": inputs})]
        else:
            if plans or not ids or len(ids) != len(set(ids)):
                raise OfficeSpaceError("Complete/cancel requires unique IDs and no plans.")
            args = {"ids": ids}
            if comment is not None:
                if action != "cancel":
                    raise OfficeSpaceError("Only cancellation accepts comment.")
                args["comment"] = comment
            operations = [
                self.mutation("cancelMoves" if action == "cancel" else "completeMoves", args)
            ]
        return await self.api.mutate(operations)

    async def requests(
        self, action: str, changes: list[RequestChange] | None, status: str | None, site: str | None
    ):
        if action == "list":
            if changes:
                raise OfficeSpaceError("List does not accept changes.")
            args = {"status": status} if status else {}
            page = await self.api.collect("requestsPaginated", args, REQUEST)
            if site:
                site_id = (await self.site(site))["id"]
                page["items"] = [
                    r for r in page["items"] if (r.get("site") or {}).get("id") == site_id
                ]
            return page
        self.api.require_writes()
        if status or site or not changes:
            raise OfficeSpaceError(
                "Create/status requires changes; put status/site inside each change."
            )
        operations = []
        for change in changes:
            if action == "create":
                if not all((change.request_type, change.subject, change.requestor)):
                    raise OfficeSpaceError("Create requires request_type, subject and requestor.")
                if change.id or change.status or change.assignee or change.comment:
                    raise OfficeSpaceError("Create does not accept id/status/assignee/comment.")
                who = await self.person(change.requestor)
                if not who.get("email"):
                    raise OfficeSpaceError("The requestor needs an email address.")
                types = await self.api.query("requestTypes", {}, "id name")
                request_type = identify(types, change.request_type, ("name",))
                args = {
                    "requestTypeId": request_type["id"],
                    "subject": change.subject,
                    "requestor": who["fullName"],
                    "requestorEmail": who["email"],
                }
                if change.site:
                    args["siteId"] = (await self.site(change.site))["id"]
                name = "createRequest"
            else:
                if not change.id or not change.status:
                    raise OfficeSpaceError("Status change requires id and status.")
                if change.request_type or change.subject or change.requestor or change.site:
                    raise OfficeSpaceError("Status changes do not accept create fields.")
                args = {"id": change.id, "status": change.status}
                if change.assignee:
                    args["toUserId"] = (await self.user(change.assignee))["id"]
                if change.comment is not None:
                    args["comment"] = change.comment
                name = "updateRequestStatus"
            if set(args).intersection(change.extra):
                raise OfficeSpaceError("extra must not override resolved fields.")
            args.update(change.extra)
            operations.append(self.mutation(name, args))
        return await self.api.mutate(operations)
