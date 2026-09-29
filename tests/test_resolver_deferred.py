import pytest
from connexion import AsyncApp, FlaskApp
from connexion.exceptions import DeferredResolverError, ResolverError
from connexion.operations import OpenAPIOperation
from connexion.resolver import DeferredResolver

from fakeapi.example_method_view import PetsView
from fakeapi.hello import post_greeting

COMPONENTS = {"parameters": {"myparam": {"in": "path", "schema": {"type": "integer"}}}}

APP_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Deferred", "version": "1.0"},
    "paths": {
        "/greeting/{name}": {
            "post": {
                "operationId": "fakeapi.hello.post_greeting",
                "parameters": [
                    {
                        "name": "name",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "greeting response",
                        "content": {
                            "application/json": {"schema": {"type": "object"}}
                        },
                    }
                },
            }
        }
    },
}


def make_operation(resolver, operation_id, path="/endpoint", method="GET", **operation_kwargs):
    operation = {"operationId": operation_id, "responses": {"200": {"description": "ok"}}}
    operation.update(operation_kwargs)
    return OpenAPIOperation(
        method=method,
        path=path,
        path_parameters=[],
        operation=operation,
        components=COMPONENTS,
        resolver=resolver,
    )


def greeting_handler(name):
    return {"greeting": f"Hello {name}"}


def optional_greeting_handler(name="world"):
    return {"greeting": f"Hello {name}"}


def test_placeholder_rejects_calls_before_finalize():
    resolver = DeferredResolver()
    operation = make_operation(resolver, "greeting")

    assert operation.operation_id == "greeting"
    assert resolver.pending_operation_ids == ["greeting"]
    with pytest.raises(DeferredResolverError):
        operation.function()


def test_finalize_binds_registered_handler():
    resolver = DeferredResolver()
    operation = make_operation(resolver, "greeting")

    resolver.register("greeting", optional_greeting_handler)
    resolver.finalize()

    assert resolver.finalized
    assert resolver.pending_operation_ids == []
    assert operation.function is optional_greeting_handler
    assert operation.function(name="David") == {"greeting": "Hello David"}


def test_finalize_is_idempotent():
    resolver = DeferredResolver()
    make_operation(resolver, "greeting")
    resolver.register("greeting", optional_greeting_handler)
    resolver.finalize()
    resolver.finalize()


def test_finalize_reports_all_missing_handlers_at_once():
    resolver = DeferredResolver()
    make_operation(resolver, "greeting")
    make_operation(resolver, "farewell")

    with pytest.raises(DeferredResolverError) as exc_info:
        resolver.finalize()

    errors = exc_info.value.errors
    assert any('operationId "greeting"' in error for error in errors)
    assert any('operationId "farewell"' in error for error in errors)

    # A failed finalize allows completing the registrations and retrying.
    resolver.register("greeting", optional_greeting_handler)
    resolver.register("farewell", optional_greeting_handler)
    resolver.finalize()
    assert resolver.finalized


def test_failed_finalize_leaves_no_partial_bindings():
    resolver = DeferredResolver()
    bound_operation = make_operation(resolver, "greeting")
    missing_operation = make_operation(resolver, "farewell")

    resolver.register("greeting", optional_greeting_handler)
    with pytest.raises(DeferredResolverError):
        resolver.finalize()

    # No half-bound routes: even the resolvable operation keeps rejecting calls.
    assert not resolver.finalized
    with pytest.raises(DeferredResolverError):
        bound_operation.function()
    with pytest.raises(DeferredResolverError):
        missing_operation.function()

    resolver.register("farewell", optional_greeting_handler)
    resolver.finalize()
    assert bound_operation.function is optional_greeting_handler


def test_finalize_reports_conflicting_handlers():
    resolver = DeferredResolver()
    make_operation(resolver, "greeting")

    def other_handler(name="world"):
        return {"greeting": f"Hi {name}"}

    resolver.register("greeting", optional_greeting_handler)
    resolver.register("greeting", other_handler)

    with pytest.raises(DeferredResolverError) as exc_info:
        resolver.finalize()
    assert any("conflicting" in error for error in exc_info.value.errors)

    # Registering the same handler twice is idempotent, not a conflict.
    resolver = DeferredResolver()
    make_operation(resolver, "greeting")
    resolver.register("greeting", optional_greeting_handler)
    resolver.register("greeting", optional_greeting_handler)
    resolver.finalize()
    assert resolver.finalized


def test_conflicting_registration_can_be_replaced_before_finalize():
    resolver = DeferredResolver()
    operation = make_operation(resolver, "greeting")

    def other_handler(name="world"):
        return {"greeting": f"Hi {name}"}

    resolver.register("greeting", optional_greeting_handler)
    resolver.register("greeting", other_handler)
    with pytest.raises(DeferredResolverError):
        resolver.finalize()

    resolver.register("greeting", other_handler, replace=True)
    resolver.finalize()
    assert operation.function is other_handler


def test_finalize_validates_handler_signature():
    parameters = [
        {"name": "name", "in": "path", "required": True, "schema": {"type": "string"}}
    ]

    # Parameter defined in the specification but not accepted by the handler.
    resolver = DeferredResolver()
    make_operation(resolver, "greeting", parameters=parameters)
    resolver.register("greeting", lambda: None)
    with pytest.raises(DeferredResolverError) as exc_info:
        resolver.finalize()
    assert any('"name"' in error for error in exc_info.value.errors)

    # Fixing the registration allows retrying.
    resolver.register("greeting", greeting_handler, replace=True)
    resolver.finalize()
    assert resolver.finalized

    # Required handler argument not provided by the specification.
    resolver = DeferredResolver()
    make_operation(resolver, "greeting")
    resolver.register("greeting", greeting_handler)
    with pytest.raises(DeferredResolverError) as exc_info:
        resolver.finalize()
    assert any('"name"' in error for error in exc_info.value.errors)

    # Handlers with **kwargs accept any specification parameter.
    resolver = DeferredResolver()
    make_operation(resolver, "greeting", parameters=parameters)
    resolver.register("greeting", lambda **kwargs: kwargs)
    resolver.finalize()
    assert resolver.finalized


def test_finalize_validates_positional_only_arguments():
    resolver = DeferredResolver()
    make_operation(resolver, "greeting")

    def positional_only_handler(name, /):
        return name

    resolver.register("greeting", positional_only_handler)
    with pytest.raises(DeferredResolverError) as exc_info:
        resolver.finalize()
    assert any("positional-only" in error for error in exc_info.value.errors)


def test_shared_handler_across_operations():
    """Multiple operations (e.g. from multiple specifications) can reference the
    same handler."""
    resolver = DeferredResolver()
    parameters = [
        {"name": "name", "in": "path", "required": True, "schema": {"type": "string"}}
    ]
    operation_one = make_operation(
        resolver, "fakeapi.hello.post_greeting", path="/one/{name}", parameters=parameters
    )
    operation_two = make_operation(
        resolver, "fakeapi.hello.post_greeting", path="/two/{name}", parameters=parameters
    )

    resolver.register("fakeapi.hello.post_greeting", post_greeting)
    resolver.finalize()

    assert operation_one.function is post_greeting
    assert operation_two.function is post_greeting


def test_method_view_class_binding():
    resolver = DeferredResolver()
    get_operation = make_operation(resolver, "fakeapi.PetsView.get")
    post_operation = make_operation(resolver, "fakeapi.PetsView.post")

    resolver.register("fakeapi.PetsView", PetsView)
    resolver.finalize()

    # All methods of the view are bound to a single shared view function.
    assert get_operation.function is post_operation.function
    assert get_operation.function.view_class is PetsView


def test_method_view_instance_binding():
    resolver = DeferredResolver()
    get_operation = make_operation(resolver, "fakeapi.PetsView.get")
    search_operation = make_operation(resolver, "fakeapi.PetsView.search")

    resolver.register("fakeapi.PetsView", PetsView())
    resolver.finalize()

    assert get_operation.function() == [{"name": "get"}]
    assert search_operation.function() == [{"name": "search"}]


def test_method_view_missing_method():
    resolver = DeferredResolver()
    make_operation(resolver, "fakeapi.PetsView.delete")

    resolver.register("fakeapi.PetsView", PetsView)
    with pytest.raises(DeferredResolverError) as exc_info:
        resolver.finalize()
    assert any('operationId "fakeapi.PetsView.delete"' in error for error in exc_info.value.errors)


def test_register_after_finalize_is_rejected():
    resolver = DeferredResolver()
    make_operation(resolver, "greeting")
    resolver.register("greeting", optional_greeting_handler)
    resolver.finalize()

    # Bound handlers can no longer be replaced.
    with pytest.raises(ResolverError):
        resolver.register("greeting", optional_greeting_handler)
    with pytest.raises(ResolverError):
        resolver.register("greeting", lambda name: None, replace=True)


def test_register_view_bound_operation_after_finalize_is_rejected():
    resolver = DeferredResolver()
    make_operation(resolver, "fakeapi.PetsView.get")
    resolver.register("fakeapi.PetsView", PetsView)
    resolver.finalize()

    with pytest.raises(ResolverError):
        resolver.register("fakeapi.PetsView.get", lambda: None)
    with pytest.raises(ResolverError):
        resolver.register("fakeapi.PetsView", PetsView)


def test_resolve_after_finalize_is_immediate():
    resolver = DeferredResolver()
    make_operation(resolver, "greeting")
    resolver.register("greeting", optional_greeting_handler)
    resolver.finalize()

    # New operations are resolved immediately after finalization.
    operation = make_operation(resolver, "greeting")
    assert operation.function is optional_greeting_handler

    # Unknown operationIds raise immediately, like with the standard resolvers.
    with pytest.raises(ResolverError):
        make_operation(resolver, "ohai.I.do.not.exist")

    # Handlers for APIs added after finalization can still be registered.
    resolver.register("farewell", optional_greeting_handler)
    operation = make_operation(resolver, "farewell")
    assert operation.function is optional_greeting_handler


def test_register_as_decorator():
    resolver = DeferredResolver()
    operation = make_operation(resolver, "greeting")

    @resolver.register("greeting")
    def decorated_handler(name="world"):
        return {"greeting": f"Hello {name}"}

    resolver.finalize()
    assert operation.function is decorated_handler


def test_register_non_callable_is_rejected():
    resolver = DeferredResolver()
    with pytest.raises(TypeError):
        resolver.register("greeting", "not.a.function")


@pytest.mark.parametrize("app_class", [FlaskApp, AsyncApp])
def test_app_rejects_requests_before_finalize(app_class):
    resolver = DeferredResolver()
    app = app_class(__name__)
    app.add_api(APP_SPEC, resolver=resolver)

    # Build the middleware stack (normally done lazily on the first request) so
    # the API operations are registered on the wrapped application.
    app.middleware._build_middleware_stack()

    if app_class is FlaskApp:
        view = next(
            view
            for name, view in app._middleware_app.app.view_functions.items()
            if "post_greeting" in name
        )
    else:
        api = app._middleware_app.apis[""][0]
        view = api.operations["fakeapi.hello.post_greeting"]

    # Business requests are not served before finalization.
    with pytest.raises(DeferredResolverError):
        view._fn()

    resolver.register("fakeapi.hello.post_greeting", post_greeting)
    resolver.finalize()

    assert view._fn(name="David") == {"greeting": "Hello David"}
