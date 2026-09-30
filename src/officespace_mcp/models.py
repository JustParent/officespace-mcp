"""Inputs shared by the admin workflow tools."""

from datetime import date
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Kind = Literal["desk", "room"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Space(Input):
    reference: str = Field(description="Exact desk/room label, or id:<OfficeSpace ID>.")
    site: str | None = Field(default=None, description="Exact site name, or id:<ID>.")
    floor: str | None = Field(default=None, description="Exact floor label, or id:<ID>.")


class Booking(Input):
    kind: Kind
    space: Space
    person: str = Field(description="Employee email, exact full name, or id:<employee ID>.")
    start: AwareDatetime
    end: AwareDatetime
    title: str | None = Field(default=None, description="Required for room reservations.")
    guest_emails: list[str] = Field(default_factory=list)
    note: str | None = None

    @model_validator(mode="after")
    def check(self):
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.kind == "room" and not self.title:
            raise ValueError("Room bookings require a title")
        if self.kind == "desk" and (self.title or self.guest_emails):
            raise ValueError("title and guest_emails apply only to room bookings")
        return self


class EmployeeChange(Input):
    person: str | None = Field(default=None, description="Required for update/deactivate.")
    values: dict[str, Any] = Field(
        default_factory=dict,
        description="GraphQL employee fields, e.g. department, title, team, firstName, lastName. "
        "Create requires employeeId (external ID). Omitted fields remain unchanged; null clears.",
    )


class Move(Input):
    person: str
    destination: Space | None = Field(default=None, description="Omit to vacate the source seat.")
    source: Space | None = Field(
        default=None, description="Defaults to the employee's unique current seat."
    )
    move_date: date
    description: str | None = None


class RequestChange(Input):
    id: str | None = None
    request_type: str | None = Field(default=None, description="Exact type name or id:<ID>.")
    subject: str | None = None
    requestor: str | None = Field(default=None, description="Employee email, name or id:<ID>.")
    site: str | None = None
    status: (
        Literal[
            "ACCEPTED", "CLOSED", "CREATED", "DELEGATED", "OPENED", "MERGED", "REJECTED", "SOLVED"
        ]
        | None
    ) = None
    assignee: str | None = Field(default=None, description="User email, name or id:<user ID>.")
    comment: str | None = None
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional schema-defined createRequest/updateRequestStatus arguments. "
        "For example customFieldValues, dueAt, floorId, locationId or masterRequestId.",
    )
