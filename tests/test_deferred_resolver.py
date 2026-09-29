import types

import pytest
from connexion import AsyncApp, FlaskApp
from connexion.exceptions import DeferredResolutionError, ResolverError
from connexion.resolver import (
    DeferredResolver,
    MethodResolver,
    MethodViewResolver,
    Resolver,
)

from fakeapi.example_method_view import PetsView


def greeting_spec(**kwargs):
    """A minimal spec with a single operation, parametrized via kwargs."""
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "deferred", "version": "1.0"},
        "servers": [{"url": "/v1.0"}],
        "paths": {
            "/greeting/{name}": {
                "get": {
                    "operationId": "fakeapi.hello.get_bye",
                    "parameters": [
                        {
                            "name": "name",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }
    spec.update(kwargs)
    return spec


def get_bye(name):
    return {"greeting": f"Bye {name}"}


def test_register_api_before_functions(app_class):
    """APIs can be registered before the view functions are available. Requests are
    not served until the resolver is finalized."""
    resolver = DeferredResolver()
    app = app_class(__name__)
    app.add_api(greeting_spec(), resolver=resolver)

    app_client = app.test_client()
    response = app_client.get("/v1.0/greeting/robbe")
    assert response.status_code == 503

    with pytest.raises(DeferredResolutionError) as exc_info:
        resolver.finalize()
    assert "fakeapi.hello.get_bye" in str(exc_info.value)

    # A failed finalize does not expose half-configured routes.
    response = app_client.get("/v1.0/greeting/robbe")
    assert response.status_code == 503

    resolver.register_function("fakeapi.hello.get_bye", get_bye)
    resolver.finalize()
    assert resolver.finalized

    response = app_client.get("/v1.0/greeting/robbe")
    assert response.status_code == 200
    assert response.json() == {"greeting": "Bye robbe"}


def test_finalize_reports_all_errors_at_once(app_class):
    """Missing, duplicate and mismatched bindings are all reported in one go."""
    spec = greeting_spec()
    spec["paths"]["/greeting/{name}"]["post"] = {
        "operationId": "fakeapi.hello.post_greeting",
        "parameters": [
            {
                "name": "name",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
        ],
        "responses": {"200": {"description": "OK"}},
    }
    spec["paths"]["/farewell"] = {
        "get": {
            "operationId": "fakeapi.hello.get_farewell",
            "responses": {"200": {"description": "OK"}},
        }
    }

    resolver = DeferredResolver()
    app = app_class(__name__)
    app.add_api(spec, resolver=resolver)

    def duplicate_one(name):
        return name

    def duplicate_two(name):
        return name

    def mismatched(unknown_argument):
        return unknown_argument

    # fakeapi.hello.get_bye: no registration (missing)
    # fakeapi.hello.post_greeting: two different functions (duplicate)
    # fakeapi.hello.get_farewell: signature does not match the parameter mapping
    resolver.register_function("fakeapi.hello.post_greeting", duplicate_one)
    resolver.register_function("fakeapi.hello.post_greeting", duplicate_two)
    resolver.register_function("fakeapi.hello.get_farewell", mismatched)

    with pytest.raises(DeferredResolutionError) as exc_info:
        resolver.finalize()

    message = str(exc_info.value)
    assert 'No function registered for operationId "fakeapi.hello.get_bye"' in message
    assert (
        'Multiple functions registered for operationId "fakeapi.hello.post_greeting"'
        in message
    )
    assert "duplicate_one" in message
    assert "duplicate_two" in message
    assert "fakeapi.hello.get_farewell" in message
    assert "unknown_argument" in message

    # Nothing was bound: no half-configured routes are exposed.
    app_client = app.test_client()
    assert app_client.get("/v1.0/greeting/robbe").status_code == 503
    assert app_client.post("/v1.0/greeting/robbe").status_code == 503
    assert app_client.get("/v1.0/farewell").status_code == 503

    # Fix the registrations and retry.
    resolver.register_function("fakeapi.hello.get_bye", get_bye)
    resolver.unregister_function("fakeapi.hello.post_greeting", duplicate_two)
    resolver.unregister_function("fakeapi.hello.get_farewell")
    resolver.register_function("fakeapi.hello.get_farewell", lambda: "farewell")

    resolver.finalize()

    assert app_client.get("/v1.0/greeting/robbe").status_code == 200
    assert app_client.post("/v1.0/greeting/robbe").status_code == 200
    assert app_client.get("/v1.0/farewell").status_code == 200


def test_shared_function_across_specs(app_class):
    """Multiple specs can reference the same handler function via the same
    operationId. A single registration binds them all."""
    resolver = DeferredResolver()
    app = app_class(__name__)
    app.add_api(greeting_spec(), base_path="/v1", resolver=resolver)
    app.add_api(greeting_spec(), base_path="/v2", resolver=resolver)

    resolver.register_function("fakeapi.hello.get_bye", get_bye)
    resolver.finalize()

    app_client = app.test_client()
    assert app_client.get("/v1/greeting/robbe").json() == {"greeting": "Bye robbe"}
    assert app_client.get("/v2/greeting/robbe").json() == {"greeting": "Bye robbe"}


def test_method_view_binding(method_view_resolver):
    """View functions of (Method)View classes can be registered explicitly."""
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "deferred", "version": "1.0"},
        "servers": [{"url": "/v1.0"}],
        "paths": {
            "/pets": {
                "post": {"responses": {"201": {"description": "OK"}}},
            },
            "/pets/{petId}": {
                "get": {
                    "parameters": [
                        {
                            "name": "petId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {"200": {"description": "OK"}},
                },
            },
        },
    }

    resolver = DeferredResolver(
        resolver=method_view_resolver("fakeapi.example_method_view")
    )
    app = FlaskApp(__name__)
    app.add_api(spec, resolver=resolver)

    app_client = app.test_client()

    # Operations are registered when the middleware stack is built on first request.
    # They are not served before finalization.
    assert app_client.get("/v1.0/pets/1").status_code == 503

    get_operation_id = "fakeapi.example_method_view.PetsView.get"
    post_operation_id = "fakeapi.example_method_view.PetsView.post"
    assert set(resolver.pending_operation_ids) == {get_operation_id, post_operation_id}

    if method_view_resolver is MethodViewResolver:
        # A single as_view function dispatches all methods of the view class.
        view = PetsView.as_view("PetsView")
        resolver.register_function(get_operation_id, view)
        resolver.register_function(post_operation_id, view)
    else:
        view = PetsView()
        resolver.register_function(get_operation_id, view.get)
        resolver.register_function(post_operation_id, view.post)

    resolver.finalize()

    response = app_client.get("/v1.0/pets/1")
    assert response.status_code == 200
    assert response.json() == {"name": "get", "petId": 1}
    response = app_client.post("/v1.0/pets")
    assert response.status_code == 201
    assert response.json() == {"name": "post"}


def test_cannot_replace_bound_function_after_finalize(app_class):
    """After a successful finalize, bound functions can no longer be replaced."""
    resolver = DeferredResolver()
    app = app_class(__name__)
    app.add_api(greeting_spec(), resolver=resolver)

    resolver.register_function("fakeapi.hello.get_bye", get_bye)
    resolver.finalize()

    def sneaky_replacement(name):
        return {"greeting": "oops"}

    with pytest.raises(DeferredResolutionError):
        resolver.register_function("fakeapi.hello.get_bye", sneaky_replacement)
    with pytest.raises(DeferredResolutionError):
        resolver.unregister_function("fakeapi.hello.get_bye")

    app_client = app.test_client()
    assert app_client.get("/v1.0/greeting/robbe").json() == {"greeting": "Bye robbe"}


def test_finalize_before_first_request(app_class):
    """Functions can be registered and finalized before the middleware stack is built.
    They are bound once operations are registered on first request."""
    resolver = DeferredResolver()
    resolver.register_function("fakeapi.hello.get_bye", get_bye)
    resolver.finalize()

    app = app_class(__name__)
    app.add_api(greeting_spec(), resolver=resolver)

    app_client = app.test_client()
    response = app_client.get("/v1.0/greeting/robbe")
    assert response.status_code == 200
    assert response.json() == {"greeting": "Bye robbe"}


def test_unknown_operation_id_after_finalize():
    """After finalization, unknown operationIds raise immediately, like with the
    standard Resolver."""
    resolver = DeferredResolver()
    resolver.finalize()

    operation = types.SimpleNamespace(
        operation_id="fakeapi.hello.unknown", router_controller=None
    )
    with pytest.raises(ResolverError):
        resolver.resolve(operation)

    # Registering a new function after finalization binds it immediately.
    resolver.register_function("fakeapi.hello.get_bye", get_bye)
    operation = types.SimpleNamespace(
        operation_id="fakeapi.hello.get_bye", router_controller=None
    )
    assert resolver.resolve(operation).function is get_bye


def test_standard_resolver_unaffected(app_class):
    """The existing immediate resolution mode keeps working."""
    app = app_class(__name__)
    app.add_api(greeting_spec(), resolver=Resolver())

    app_client = app.test_client()
    response = app_client.get("/v1.0/greeting/robbe")
    assert response.status_code == 200


def test_async_view_function():
    """Async view functions are supported once bound."""

    async def async_get_bye(name):
        return {"greeting": f"Bye {name}"}

    resolver = DeferredResolver()
    app = AsyncApp(__name__)
    app.add_api(greeting_spec(), resolver=resolver)
    resolver.register_function("fakeapi.hello.get_bye", async_get_bye)
    resolver.finalize()

    app_client = app.test_client()
    response = app_client.get("/v1.0/greeting/robbe")
    assert response.status_code == 200
    assert response.json() == {"greeting": "Bye robbe"}
