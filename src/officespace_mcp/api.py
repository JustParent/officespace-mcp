"""Schema validation, variable binding and a bounded, non-retrying HTTP client."""

import json
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import httpx
from graphql import (
    GraphQLError,
    OperationDefinitionNode,
    OperationType,
    build_client_schema,
    build_schema,
    get_variable_values,
    parse,
    print_type,
    validate,
    validate_schema,
)

from .config import Settings


class OfficeSpaceError(ValueError):
    """An actionable error that can be returned to an MCP client."""


@dataclass
class Operation:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    fields: str = ""
    summary: dict[str, Any] = field(default_factory=dict)


class API:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport
        source = (
            Path(settings.schema_path).read_text()
            if settings.schema_path
            else files("officespace_mcp").joinpath("schema.graphql").read_text()
        )
        if source.lstrip().startswith("{"):
            payload = json.loads(source)
            self.schema = build_client_schema(payload.get("data", payload))
        else:
            self.schema = build_schema(source)
        errors = validate_schema(self.schema)
        if errors:
            raise OfficeSpaceError("Invalid schema: " + "; ".join(e.message for e in errors))
        upload = self.schema.get_type("Upload")
        if upload:

            def reject_upload(*_):
                raise GraphQLError(
                    "Multipart file uploads are not supported by this JSON transport."
                )

            upload.parse_value = reject_upload
            upload.parse_literal = reject_upload

    def require_writes(self):
        if not self.settings.enable_mutations:
            raise OfficeSpaceError("Writes are disabled. Set OFFICESPACE_ENABLE_MUTATIONS=true.")

    def describe(self, name: str) -> dict:
        item = self.schema.get_type(name)
        if not item:
            raise OfficeSpaceError(f"Unknown type: {name}")
        return {"name": name, "definition": print_type(item)}

    def operations(self, search: str = "") -> dict:
        result = []
        for kind, root in [
            ("query", self.schema.query_type),
            ("mutation", self.schema.mutation_type),
        ]:
            for name, f in root.fields.items():
                if search.casefold() not in (name + " " + (f.description or "")).casefold():
                    continue
                result.append(
                    {
                        "kind": kind,
                        "name": name,
                        "description": f.description,
                        "arguments": {k: str(a.type) for k, a in f.args.items()},
                        "returns": str(f.type),
                    }
                )
        return {"mutations_enabled": self.settings.enable_mutations, "operations": result}

    def compile(self, kind: Literal["query", "mutation"], operations: list[Operation]):
        if not 1 <= len(operations) <= 50:
            raise OfficeSpaceError("Use between 1 and 50 operations per batch.")
        root = self.schema.query_type if kind == "query" else self.schema.mutation_type
        definitions, selections, variables = [], [], {}
        for i, operation in enumerate(operations):
            f = root.fields.get(operation.name)
            if f is None:
                raise OfficeSpaceError(f"Unknown {kind}: {operation.name}")
            args = []
            for name, value in operation.arguments.items():
                if name not in f.args:
                    raise OfficeSpaceError(f"Unknown argument {operation.name}.{name}")
                variable = f"v{i}_{name}"
                definitions.append(f"${variable}: {f.args[name].type}")
                args.append(f"{name}: ${variable}")
                variables[variable] = value
            suffix = "(" + ", ".join(args) + ")" if args else ""
            fields = " { " + operation.fields + " }" if operation.fields else ""
            selections.append(f"op{i}: {operation.name}{suffix}{fields}")
        defs = "(" + ", ".join(definitions) + ")" if definitions else ""
        document = f"{kind} OfficeSpace{defs} {{ " + " ".join(selections) + " }"
        self.validate(document, variables, kind)
        return document, variables

    def validate(self, document: str, variables: dict, expected: str | None = None):
        if len(document) > 64_000:
            raise OfficeSpaceError("GraphQL document exceeds 64,000 characters.")
        try:
            ast = parse(document, max_tokens=10_000)
        except GraphQLError as e:
            raise OfficeSpaceError(e.message) from None
        operations = [d for d in ast.definitions if isinstance(d, OperationDefinitionNode)]
        if len(operations) != 1:
            raise OfficeSpaceError("Send exactly one GraphQL operation per call.")
        operation = operations[0]
        kind = operation.operation.value
        if operation.operation == OperationType.SUBSCRIPTION:
            raise OfficeSpaceError("Subscriptions are not supported.")
        if expected and expected != kind:
            raise OfficeSpaceError(f"This call requires a {expected} operation.")
        if kind == "mutation":
            self.require_writes()
        errors = validate(self.schema, ast, max_errors=10)
        if not errors:
            values = get_variable_values(
                self.schema, operation.variable_definitions or [], variables, max_errors=10
            )
            if isinstance(values, list):
                errors = values
        if errors:
            raise OfficeSpaceError("; ".join(e.message for e in errors))
        return kind

    async def execute(self, document: str, variables: dict | None = None) -> dict:
        variables = variables or {}
        kind = self.validate(document, variables)
        if not self.settings.graphql_url or not self.settings.auth_value:
            raise OfficeSpaceError("Configure OFFICESPACE_GRAPHQL_URL and OFFICESPACE_AUTH_VALUE.")
        headers = {
            "Accept": "application/json",
            "apikey": self.settings.auth_value,
        }
        uncertain = (
            " Mutation outcome is unknown; check OfficeSpace before retrying."
            if kind == "mutation"
            else ""
        )
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.settings.timeout_seconds,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "POST",
                    self.settings.graphql_url,
                    headers=headers,
                    json={"query": document, "variables": variables},
                ) as response:
                    if response.status_code in {401, 403}:
                        raise OfficeSpaceError(
                            "OfficeSpace rejected the credentials or operation permissions."
                        )
                    if response.status_code == 429:
                        raise OfficeSpaceError("OfficeSpace rate limit reached; no retry was made.")
                    if not 200 <= response.status_code < 300:
                        raise OfficeSpaceError(
                            f"OfficeSpace HTTP {response.status_code}." + uncertain
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.settings.max_response_bytes:
                            raise OfficeSpaceError(
                                "Response too large; narrow the query." + uncertain
                            )
        except httpx.RequestError:
            raise OfficeSpaceError("OfficeSpace request failed or timed out." + uncertain) from None
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise OfficeSpaceError("OfficeSpace returned invalid JSON." + uncertain) from None
        if not isinstance(payload, dict) or not {"data", "errors"}.intersection(payload):
            raise OfficeSpaceError("OfficeSpace did not return a GraphQL response." + uncertain)
        return payload

    async def query(self, name: str, arguments: dict, fields: str):
        document, variables = self.compile("query", [Operation(name, arguments, fields)])
        payload = await self.execute(document, variables)
        if payload.get("errors"):
            messages = [e.get("message", "GraphQL error") for e in payload["errors"]]
            raise OfficeSpaceError("OfficeSpace lookup failed: " + "; ".join(messages))
        if not isinstance(payload.get("data"), dict) or payload["data"].get("op0") is None:
            raise OfficeSpaceError("OfficeSpace returned no data for the lookup.")
        return payload["data"]["op0"]

    async def collect(self, name: str, arguments: dict, fields: str) -> dict:
        """Follow cursors internally; never silently present a partial list as complete."""
        rows, after, seen = [], None, set()
        for _ in range(100):
            page = await self.query(
                name,
                {
                    **arguments,
                    "first": min(100, self.settings.max_records - len(rows)),
                    **({"after": after} if after else {}),
                },
                "nodes { " + fields + " } pageInfo { hasNextPage endCursor }",
            )
            rows.extend(page["nodes"])
            info = page["pageInfo"]
            if not info["hasNextPage"]:
                return {"items": rows, "complete": True}
            after = info.get("endCursor")
            if not after or after in seen:
                raise OfficeSpaceError("OfficeSpace pagination stalled; narrow the query.")
            seen.add(after)
            if len(rows) >= self.settings.max_records:
                break
        return {"items": rows, "complete": False, "next_cursor": after}

    async def mutate(self, operations: list[Operation]) -> dict:
        document, variables = self.compile("mutation", operations)
        payload = await self.execute(document, variables)
        data = payload.get("data") or {}
        errors = payload.get("errors") or []
        results = []
        for i, operation in enumerate(operations):
            alias = f"op{i}"
            value = data.get(alias)
            specific = [e for e in errors if not e.get("path") or e["path"][0] == alias]
            domain = []
            if isinstance(value, dict):
                domain = value.get("errors") or ([value["error"]] if value.get("error") else [])
            missing_result = isinstance(value, dict) and any(
                key in value and value[key] is None
                for key in ("booking", "roomBooking", "employee", "request", "moves")
            )
            if specific or value is None:
                outcome = "unknown"
            elif domain or (isinstance(value, dict) and value.get("failedCount", 0)):
                outcome = "failed_or_partial"
            elif missing_result:
                outcome = "unknown"
            else:
                outcome = "success"
            results.append(
                {
                    "index": i,
                    "operation": operation.name,
                    "outcome": outcome,
                    "data": value,
                    "errors": specific or domain,
                }
            )
        return {
            "ok": all(r["outcome"] == "success" for r in results),
            "results": results,
            "atomic": False,
            "retry_guidance": "Do not replay a batch; check uncertain items in OfficeSpace first.",
        }
