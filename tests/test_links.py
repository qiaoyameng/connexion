import pytest
import yaml

from connexion.exceptions import (
    CircularLinkError,
    LinkError,
    LinkTargetNotFoundError,
    MissingLinkValueError,
)
from connexion.links import LinkResolver
from connexion.spec import Specification

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "links", "version": "1.0"},
    "paths": {
        "/users": {
            "get": {
                "operationId": "listUsers",
                "responses": {
                    "200": {
                        "description": "ok",
                        "links": {
                            "getUserById": {
                                "operationId": "getUser",
                                "parameters": {
                                    "userId": "$response.body#/users/0/id"
                                },
                            },
                            "getUserByHeader": {
                                "operationId": "getUser",
                                "parameters": {"userId": "$response.header.X-User-Id"},
                            },
                        },
                    }
                },
            }
        },
        "/users/{userId}": {
            "get": {
                "operationId": "getUser",
                "parameters": [
                    {
                        "name": "userId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    }
                ],
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


def test_nested_array_id_binding():
    """An ID nested in an array in the response body is bound to another operation."""
    resolver = LinkResolver(SPEC)
    calls = resolver.resolve(
        "listUsers",
        200,
        response={
            "headers": {"X-User-Id": "42"},
            "body": {"users": [{"id": 42}, {"id": 43}]},
        },
    )
    call = next(call for call in calls if call.name == "getUserById")
    assert call.operation_id == "getUser"
    assert call.method == "get"
    assert call.path == "/users/{userId}"
    assert call.parameters == {"userId": 42}


@pytest.mark.parametrize(
    "headers", [{"x-user-id": "7"}, {"X-USER-ID": "7"}, {"X-User-Id": "7"}]
)
def test_response_header_case_insensitive(headers):
    """Header lookup in runtime expressions is case-insensitive."""
    resolver = LinkResolver(SPEC)
    calls = resolver.resolve(
        "listUsers",
        200,
        response={"headers": headers, "body": {"users": [{"id": 1}]}},
    )
    call = next(call for call in calls if call.name == "getUserByHeader")
    assert call.parameters == {"userId": "7"}


def test_request_header_case_insensitive():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "links", "version": "1.0"},
        "paths": {
            "/users": {
                "get": {
                    "operationId": "listUsers",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "links": {
                                "getUser": {
                                    "operationId": "getUser",
                                    "parameters": {
                                        "userId": "$request.header.x-user-id"
                                    },
                                }
                            },
                        }
                    },
                }
            },
            "/users/{userId}": {
                "get": {
                    "operationId": "getUser",
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    }
    resolver = LinkResolver(spec)
    calls = resolver.resolve(
        "listUsers", 200, request={"headers": {"X-USER-ID": "9"}}
    )
    assert calls[0].parameters == {"userId": "9"}


def test_request_expressions():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "links", "version": "1.0"},
        "paths": {
            "/users/{userId}": {
                "get": {
                    "operationId": "getUser",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "links": {
                                "createThing": {
                                    "operationId": "createThing",
                                    "parameters": {
                                        "userId": "$request.path.userId",
                                        "verbose": "$request.query.verbose",
                                        "method": "$method",
                                        "url": "$url",
                                        "status": "$statusCode",
                                        "constant": "fixed",
                                    },
                                    "requestBody": "$request.body#/payload",
                                }
                            },
                        }
                    },
                }
            },
            "/things": {
                "post": {
                    "operationId": "createThing",
                    "responses": {"201": {"description": "ok"}},
                }
            },
        },
    }
    resolver = LinkResolver(spec)
    calls = resolver.resolve(
        "getUser",
        200,
        request={
            "method": "GET",
            "url": "https://api.example.com/users/5?verbose=true",
            "path_params": {"userId": "5"},
            "query_params": {"verbose": "true"},
            "body": {"payload": {"name": "thing"}},
        },
    )
    call = calls[0]
    assert call.method == "post"
    assert call.path == "/things"
    assert call.parameters == {
        "userId": "5",
        "verbose": "true",
        "method": "GET",
        "url": "https://api.example.com/users/5?verbose=true",
        "status": 200,
        "constant": "fixed",
    }
    assert call.request_body == {"name": "thing"}


def test_missing_value_raises_distinct_error():
    """A missing value raises MissingLinkValueError instead of becoming 'None'."""
    resolver = LinkResolver(SPEC)
    with pytest.raises(MissingLinkValueError) as exc_info:
        resolver.resolve("listUsers", 200, response={"body": {"users": []}})
    assert "None" not in str(exc_info.value)

    with pytest.raises(MissingLinkValueError):
        resolver.resolve("listUsers", 200, response={"headers": {}})


def test_missing_target_raises_distinct_error():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "links", "version": "1.0"},
        "paths": {
            "/users": {
                "get": {
                    "operationId": "listUsers",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "links": {
                                "broken": {
                                    "operationId": "doesNotExist",
                                    "parameters": {},
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    resolver = LinkResolver(spec)
    with pytest.raises(LinkTargetNotFoundError):
        resolver.resolve("listUsers", 200)

    with pytest.raises(LinkTargetNotFoundError):
        resolver.resolve("unknownOperation", 200)


def test_circular_link_ref_raises_distinct_error():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "links", "version": "1.0"},
        "paths": {
            "/users": {
                "get": {
                    "operationId": "listUsers",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "links": {"loop": {"$ref": "#/components/links/linkA"}},
                        }
                    },
                }
            }
        },
        "components": {
            "links": {
                "linkA": {"$ref": "#/components/links/linkB"},
                "linkB": {"$ref": "#/components/links/linkA"},
            }
        },
    }
    resolver = LinkResolver(spec)
    with pytest.raises(CircularLinkError):
        resolver.resolve("listUsers", 200)


def test_link_ref_to_components():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "links", "version": "1.0"},
        "paths": {
            "/users": {
                "get": {
                    "operationId": "listUsers",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "links": {
                                "getUserById": {"$ref": "#/components/links/getUser"}
                            },
                        }
                    },
                }
            },
            "/users/{userId}": {
                "get": {
                    "operationId": "getUser",
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
        "components": {
            "links": {
                "getUser": {
                    "operationId": "getUser",
                    "parameters": {"userId": "$response.body#/users/1/id"},
                }
            }
        },
    }
    resolver = LinkResolver(spec)
    calls = resolver.resolve(
        "listUsers",
        200,
        response={"body": {"users": [{"id": 1}, {"id": 2}]}},
    )
    assert calls[0].parameters == {"userId": 2}


def test_relative_operation_ref_resolved_against_spec_location(tmp_path):
    external = {
        "openapi": "3.0.0",
        "info": {"title": "external", "version": "1.0"},
        "paths": {
            "/things/{thingId}": {
                "get": {
                    "operationId": "getThing",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    (tmp_path / "external.yaml").write_text(yaml.safe_dump(external))

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "links", "version": "1.0"},
        "paths": {
            "/users": {
                "get": {
                    "operationId": "listUsers",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "links": {
                                "getThing": {
                                    "operationRef": "./external.yaml#/paths/~1things~1{thingId}/get",
                                    "parameters": {
                                        "thingId": "$response.body#/things/0/id"
                                    },
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    resolver = LinkResolver(spec, base_uri=f"{tmp_path}/")
    calls = resolver.resolve(
        "listUsers", 200, response={"body": {"things": [{"id": "abc"}]}}
    )
    call = calls[0]
    assert call.operation_id == "getThing"
    assert call.method == "get"
    assert call.path == "/things/{thingId}"
    assert call.parameters == {"thingId": "abc"}


def test_operation_ref_missing_target():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "links", "version": "1.0"},
        "paths": {
            "/users": {
                "get": {
                    "operationId": "listUsers",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "links": {
                                "broken": {
                                    "operationRef": "#/paths/~1missing/get",
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    resolver = LinkResolver(spec)
    with pytest.raises(LinkTargetNotFoundError):
        resolver.resolve("listUsers", 200)


def test_invalid_expression_raises_link_error():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "links", "version": "1.0"},
        "paths": {
            "/users": {
                "get": {
                    "operationId": "listUsers",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "links": {
                                "invalid": {
                                    "operationId": "listUsers",
                                    "parameters": {"x": "$response.cookie.session"},
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    resolver = LinkResolver(spec)
    with pytest.raises(LinkError):
        resolver.resolve("listUsers", 200)


def test_resolver_accepts_specification_instance():
    specification = Specification.from_dict(SPEC)
    resolver = LinkResolver(specification)
    calls = resolver.resolve(
        "listUsers",
        200,
        response={"headers": {"X-User-Id": "42"}, "body": {"users": [{"id": 42}]}},
    )
    call = next(call for call in calls if call.name == "getUserById")
    assert call.parameters == {"userId": 42}


def test_response_without_links_returns_empty_list():
    resolver = LinkResolver(SPEC)
    assert resolver.resolve("getUser", 200) == []
