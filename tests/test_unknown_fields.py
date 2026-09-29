import json
import pathlib

import pytest
from connexion.exceptions import BadRequestProblem
from connexion.json_schema import strip_unknown_fields
from connexion.middleware.request_validation import RequestValidationOperation
from connexion.operations import OpenAPIOperation
from connexion.resolver import Resolver
from connexion.spec import Specification
from connexion.uri_parsing import OpenAPIURIParser
from connexion.validators import JSONRequestBodyValidator

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "unknown_fields"

CLOSED_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"name": {"type": "string"}},
}

ONEOF_SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["hunts"],
            "properties": {
                "name": {"type": "string"},
                "hunts": {"type": "boolean"},
                "lives": {"type": "integer"},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["barks"],
            "properties": {
                "name": {"type": "string"},
                "barks": {"type": "boolean"},
                "walks": {"type": "integer"},
            },
        },
    ]
}


def make_validator(schema, unknown_fields="reject"):
    return JSONRequestBodyValidator(
        schema=schema,
        required=True,
        nullable=False,
        encoding="utf-8",
        strict_validation=False,
        unknown_fields=unknown_fields,
    )


def test_strip_closed_object():
    body = {"name": "max", "legacy": True}
    removed = strip_unknown_fields(body, CLOSED_SCHEMA)
    assert body == {"name": "max"}
    assert removed == ["legacy"]


def test_strip_nested_array():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "pets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"name": {"type": "string"}},
                },
            }
        },
    }
    body = {"pets": [{"name": "rex", "legacy": 1}, {"name": "tom"}]}
    removed = strip_unknown_fields(body, schema)
    assert body == {"pets": [{"name": "rex"}, {"name": "tom"}]}
    assert removed == ["pets.0.legacy"]


def test_strip_allof():
    # allOf constraints are intersected: a field only survives if every closed
    # branch allows it. The second branch is open, so it does not restrict fields.
    schema = {
        "allOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "pets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {"name": {"type": "string"}},
                        },
                    },
                },
            },
            {
                "type": "object",
                "properties": {"name": {"minLength": 1}},
            },
        ]
    }
    body = {"name": "max", "legacy": True, "pets": [{"name": "rex", "legacy": 1}]}
    removed = strip_unknown_fields(body, schema)
    assert body == {"name": "max", "pets": [{"name": "rex"}]}
    assert set(removed) == {"legacy", "pets.0.legacy"}


def test_strip_oneof_keeps_matched_branch_fields():
    body = {"name": "tom", "hunts": True, "lives": 9}
    removed = strip_unknown_fields(body, ONEOF_SCHEMA)
    assert body == {"name": "tom", "hunts": True, "lives": 9}
    assert removed == []


def test_strip_oneof_cross_fields_not_stripped():
    # `hunts` belongs to the first branch, `barks` to the second. Neither branch
    # accepts both, so nothing is stripped to force a match. Validation should
    # reject the body instead.
    body = {"name": "tom", "hunts": True, "barks": True}
    removed = strip_unknown_fields(body, ONEOF_SCHEMA)
    assert body == {"name": "tom", "hunts": True, "barks": True}
    assert removed == []


def test_validator_reject_default():
    validator = make_validator(CLOSED_SCHEMA)
    with pytest.raises(BadRequestProblem):
        validator._validate({"name": "max", "legacy": True})
    assert validator.MUTABLE_VALIDATION is False


def test_validator_keep():
    validator = make_validator(CLOSED_SCHEMA, unknown_fields="keep")
    body = {"name": "max", "legacy": True}
    validator._validate(body)
    assert body == {"name": "max", "legacy": True}


def test_validator_strip():
    validator = make_validator(CLOSED_SCHEMA, unknown_fields="strip")
    body = {"name": "max", "legacy": True}
    validator._validate(body)
    assert body == {"name": "max"}
    assert validator.MUTABLE_VALIDATION is True


def test_validator_strip_still_validates():
    validator = make_validator(CLOSED_SCHEMA, unknown_fields="strip")
    with pytest.raises(BadRequestProblem):
        validator._validate({"name": 3, "legacy": True})


def test_validator_strip_oneof_cross_fields_rejected():
    validator = make_validator(ONEOF_SCHEMA, unknown_fields="strip")
    with pytest.raises(BadRequestProblem):
        validator._validate({"name": "tom", "hunts": True, "barks": True})


def test_validator_invalid_policy():
    with pytest.raises(ValueError):
        make_validator(CLOSED_SCHEMA, unknown_fields="drop")


def make_operation(path, spec_file="openapi.yaml"):
    spec = Specification.load(FIXTURE_DIR / spec_file)
    return OpenAPIOperation.from_spec(
        spec,
        path=path,
        method="post",
        resolver=Resolver(),
        uri_parser_class=OpenAPIURIParser,
    )


async def run_operation(operation, payload):
    """Run a payload through the request validation operation and return the body as
    the downstream application (business function) would receive it."""
    body = json.dumps(payload).encode()
    received = {}

    async def next_app(scope, receive, send):
        messages = []
        more_body = True
        while more_body:
            message = await receive()
            messages.append(message)
            more_body = message.get("more_body", False)
        received["body"] = json.loads(b"".join(m.get("body", b"") for m in messages))

    validation_operation = RequestValidationOperation(next_app, operation=operation)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "query_string": b"",
        "path_params": {},
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return messages.pop(0)

    async def send(message):
        pass

    await validation_operation(scope, receive, send)
    return received.get("body")


async def test_operation_reject():
    operation = make_operation("/reject")
    assert operation.unknown_fields_policy == "reject"

    assert await run_operation(operation, {"name": "max"}) == {"name": "max"}

    with pytest.raises(BadRequestProblem):
        await run_operation(operation, {"name": "max", "legacy": True})


async def test_operation_keep():
    operation = make_operation("/keep")
    assert operation.unknown_fields_policy == "keep"

    assert await run_operation(operation, {"name": "max", "legacy": True}) == {
        "name": "max",
        "legacy": True,
    }


async def test_operation_strip():
    operation = make_operation("/strip")
    assert operation.unknown_fields_policy == "strip"

    # The downstream application receives the same stripped data as was validated
    assert await run_operation(operation, {"name": "max", "legacy": True}) == {
        "name": "max"
    }


async def test_operation_strip_composed():
    operation = make_operation("/strip-composed")

    assert await run_operation(
        operation,
        {
            "name": "max",
            "legacy": True,
            "pets": [{"name": "rex", "legacy": 1}, {"name": "tom"}],
        },
    ) == {"name": "max", "pets": [{"name": "rex"}, {"name": "tom"}]}


async def test_operation_strip_oneof():
    operation = make_operation("/strip-oneof")

    # Fields of the matched branch are kept
    assert await run_operation(
        operation, {"name": "tom", "hunts": True, "lives": 9}
    ) == {"name": "tom", "hunts": True, "lives": 9}

    # Fields crossing both oneOf branches are not stripped to force a match
    with pytest.raises(BadRequestProblem):
        await run_operation(operation, {"name": "tom", "hunts": True, "barks": True})


def test_invalid_policy():
    with pytest.raises(ValueError):
        make_operation("/invalid", spec_file="openapi_invalid.yaml")
