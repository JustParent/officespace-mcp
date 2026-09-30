import json
from dataclasses import replace

import httpx
import pytest

from officespace_mcp.api import API, OfficeSpaceError, Operation
from officespace_mcp.config import Settings


def test_schema_snapshot_is_complete():
    api = API(Settings())
    assert len(api.schema.query_type.fields) == 33
    assert len(api.schema.mutation_type.fields) == 54
    assert "employeeId: String!" in api.describe("CreateEmployeeInput")["definition"]


@pytest.mark.parametrize(
    "document",
    [
        'mutation { cancelBooking(id: "b1") { id } }',
        "mutation Write { ...Cancel } "
        'fragment Cancel on Mutation { cancelBooking(id: "b1") { id } }',
    ],
)
async def test_raw_mutation_gate_uses_ast_not_keyword_guessing(document):
    with pytest.raises(OfficeSpaceError, match="disabled"):
        await API(Settings()).execute(document)


def test_local_validation_rejects_unknown_fields_and_bad_variables():
    api = API(Settings())
    with pytest.raises(OfficeSpaceError, match="Cannot query field"):
        api.validate("{ employees { nonexistent } }", {})
    with pytest.raises(OfficeSpaceError, match="non-null"):
        api.validate("query($ids: [ID!]!) { booking(ids: $ids) { id } }", {})
    with pytest.raises(OfficeSpaceError, match="exactly one"):
        api.validate("query One { employeesCount } query Two { employeesCount }", {})


def test_values_are_bound_as_variables_and_never_interpolated():
    api = API(Settings())
    malicious = 'Alice") { id } } mutation { deactivateEmployees(ids: ["1"])'
    document, variables = api.compile("query", [Operation("employees", {"name": malicious}, "id")])
    assert malicious not in document
    assert malicious in variables.values()


async def test_partial_graphql_results_keep_success_and_unknown_items(office):
    def handler(_):
        return httpx.Response(
            200,
            json={
                "data": {"op0": {"id": "b1"}, "op1": None},
                "errors": [{"message": "permission denied", "path": ["op1"]}],
            },
        )

    api = API(office.settings, httpx.MockTransport(handler))
    payload = await api.mutate(
        [
            Operation("cancelBooking", {"id": "b1"}, "id"),
            Operation("cancelBooking", {"id": "b2"}, "id"),
        ]
    )
    assert not payload["ok"]
    assert [r["outcome"] for r in payload["results"]] == ["success", "unknown"]
    assert payload["results"][0]["data"] == {"id": "b1"}


async def test_domain_error_http_200_is_not_reported_as_success(office):
    def handler(_):
        return httpx.Response(
            200,
            json={
                "data": {
                    "op0": {
                        "booking": None,
                        "error": {"code": "CONFLICT", "message": "Already booked"},
                    }
                }
            },
        )

    api = API(office.settings, httpx.MockTransport(handler))
    payload = await api.mutate(
        [Operation("updateBooking", {"id": "b1"}, "booking { id } error { code message }")]
    )
    assert not payload["ok"]
    assert payload["results"][0]["outcome"] == "failed_or_partial"


async def test_empty_confirmation_is_not_reported_as_success(office):
    api = API(
        office.settings,
        httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": {"op0": {"roomBooking": None}}})
        ),
    )
    payload = await api.mutate(
        [Operation("confirmRoomBooking", {"id": "b1"}, "roomBooking { id }")]
    )
    assert not payload["ok"]
    assert payload["results"][0]["outcome"] == "unknown"


@pytest.mark.parametrize("status", [302, 401, 403, 429, 503])
async def test_no_redirects_retries_or_credential_leaks(office, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, headers={"location": "https://other.example/steal"}, text="upstream-secret"
        )

    api = API(office.settings, httpx.MockTransport(handler))
    with pytest.raises(OfficeSpaceError) as error:
        await api.execute('mutation { cancelBooking(id: "b1") { id } }')
    assert len(calls) == 1
    assert "upstream-secret" not in str(error.value)
    if status in {302, 503}:
        assert "unknown" in str(error.value)


async def test_mutation_timeout_does_not_retry(office):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret should not be reflected", request=request)

    api = API(office.settings, httpx.MockTransport(handler))
    with pytest.raises(OfficeSpaceError, match="outcome is unknown"):
        await api.execute('mutation { cancelBooking(id: "b1") { id } }')
    assert len(calls) == 1


async def test_response_size_bound(office):
    api = API(
        replace(office.settings, max_response_bytes=10),
        httpx.MockTransport(lambda _: httpx.Response(200, json={"data": {"employees": []}})),
    )
    with pytest.raises(OfficeSpaceError, match="too large"):
        await api.execute("{ employees { id } }")


async def test_stalled_pagination_is_detected(office):
    office.overrides["employeesPaginated"] = {
        "nodes": [],
        "pageInfo": {"hasNextPage": True, "endCursor": "same"},
    }
    with pytest.raises(OfficeSpaceError, match="stalled"):
        await office.api.collect("employeesPaginated", {}, "id")
    assert len(office.calls) == 2


@pytest.mark.parametrize(
    "url",
    [
        "http://tenant.test/graphql",
        "https://user:pass@tenant.test/gql",
        "https://tenant.test/graphql?key=secret",
        "file:///etc/passwd",
    ],
)
def test_invalid_configuration(url):
    with pytest.raises(ValueError):
        Settings(graphql_url=url)


def test_credentials_excluded_from_repr():
    assert "supersecret" not in repr(Settings(auth_value="supersecret", mcp_token="supersecret"))


def test_uploaded_introspection_format_can_be_loaded(tmp_path):
    from graphql import introspection_from_schema

    original = API(Settings())
    file = tmp_path / "schema.json"
    file.write_text(json.dumps({"data": introspection_from_schema(original.schema)}))
    assert len(API(Settings(schema_path=str(file))).schema.mutation_type.fields) == 54
