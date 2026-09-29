"""
This module contains resolvers, functions that resolves the user defined view functions
from the operations defined in the OpenAPI spec.
"""

import functools
import inspect
import logging
import typing as t

from inflection import camelize

import connexion.utils as utils
from connexion.exceptions import DeferredResolverError, ResolverError
from connexion.http_facts import METHODS

logger = logging.getLogger("connexion.resolver")

#: Method names that are resolved on registered view classes and instances.
VIEW_METHODS = METHODS | {"search"}

#: Argument names that Connexion injects at request time instead of mapping them
#: from the specification. They are excluded from signature validation.
CONTEXT_ARGUMENTS = frozenset({"context_", "user", "token_info"})


class Resolution:
    def __init__(self, function, operation_id):
        """
        Represents the result of operation resolution

        :param function: The endpoint function
        :type function: types.FunctionType
        """
        self.function = function
        self.operation_id = operation_id


class Resolver:
    def __init__(self, function_resolver: t.Callable = utils.get_function_from_name):
        """
        Standard resolver

        :param function_resolver: Function that resolves functions using an operationId
        """
        self.function_resolver = function_resolver

    def resolve(self, operation):
        """
        Default operation resolver

        :type operation: connexion.operations.AbstractOperation
        """
        operation_id = self.resolve_operation_id(operation)
        return Resolution(
            self.resolve_function_from_operation_id(operation_id), operation_id
        )

    def resolve_operation_id(self, operation):
        """
        Default operationId resolver

        :type operation: connexion.operations.AbstractOperation
        """
        operation_id = operation.operation_id
        router_controller = operation.router_controller
        if router_controller is None:
            return operation_id
        return f"{router_controller}.{operation_id}"

    def resolve_function_from_operation_id(self, operation_id):
        """
        Invokes the function_resolver

        :type operation_id: str
        """
        try:
            return self.function_resolver(operation_id)
        except ImportError as e:
            msg = f'Cannot resolve operationId "{operation_id}"! Import error was "{str(e)}"'
            raise ResolverError(msg)
        except (AttributeError, ValueError) as e:
            raise ResolverError(str(e))


def _is_view_class(handler) -> bool:
    """Whether the handler is a class with an ``as_view`` method, such as a
    Flask ``MethodView``."""
    return inspect.isclass(handler) and hasattr(handler, "as_view")


def _unwrap(function):
    """Unwrap decorators to return the original function."""
    while hasattr(function, "__wrapped__"):
        function = function.__wrapped__
    return function


class DeferredResolver(Resolver):
    """
    Resolver that decouples registering an API from registering its handler functions.

    This is useful when the application is composed of plugins that are not yet loaded
    when the API specification is registered. The specification can be registered first,
    after which plugins register their handler functions, and a final call to
    :meth:`finalize` binds all pending operations at once:

    .. code-block:: python

        resolver = DeferredResolver()
        app.add_api("openapi.yaml", resolver=resolver)

        # Plugins register their handlers
        resolver.register("my_operation", my_handler)

        # Bind all operations at once
        resolver.finalize()

    Until the resolver is finalized, requests to its operations are rejected.
    ``finalize()`` validates all pending operations and reports all missing or
    conflicting bindings, as well as any mismatches between the specification
    parameters and the handler signatures, in a single error. If finalization fails,
    no operations are bound, so the application never serves a partially bound API.
    Registrations can be corrected and ``finalize()`` retried. Once finalized, the
    resolver behaves like a regular resolver and bound handlers can no longer be
    replaced.
    """

    def __init__(self, function_resolver: t.Callable = utils.get_function_from_name):
        super().__init__(function_resolver=function_resolver)
        self._registrations: t.Dict[str, t.List[t.Callable]] = {}
        self._pending: t.Dict[str, t.List[t.Tuple[t.Any, Resolution]]] = {}
        self._bindings: t.Dict[str, t.Callable] = {}
        self._view_cache: t.Dict[type, t.Callable] = {}
        self._finalized = False

    @property
    def finalized(self) -> bool:
        """Whether the resolver has been finalized."""
        return self._finalized

    @property
    def pending_operation_ids(self) -> t.List[str]:
        """The operationIds that are still waiting to be bound by ``finalize()``."""
        return list(self._pending)

    def register(
        self,
        operation_id: str,
        handler: t.Optional[t.Callable] = None,
        *,
        replace: bool = False,
    ) -> t.Optional[t.Callable]:
        """
        Register a handler for an operationId. Can be used as a decorator.

        :param operation_id: The operationId to bind the handler to, as resolved from
            the specification (including any router controller prefix).
        :param handler: The handler to bind. This can be a callable, or a class with an
            ``as_view`` method (such as a Flask ``MethodView``), in which case operations
            with operationIds of the form ``<operation_id>.<method>`` are bound to a
            shared view. Instances are supported as well, in which case such operations
            are bound to the corresponding method of the instance.
        :param replace: Whether to replace earlier registrations for the same
            operationId instead of recording a conflicting binding.

        :return: The registered handler, so this method can be used as a decorator.
        """
        if handler is None:
            # Used as a decorator
            return functools.partial(self.register, operation_id, replace=replace)

        if not callable(handler) and not any(
            hasattr(handler, method) for method in VIEW_METHODS
        ):
            raise TypeError(
                f'Handler registered for operationId "{operation_id}" must be '
                f"callable or expose view methods, got {handler!r}."
            )

        if self._finalized:
            if operation_id in self._bindings or operation_id in self._registrations:
                raise ResolverError(
                    f'Cannot register handler for operationId "{operation_id}": the '
                    "resolver has already been finalized and its bindings can no "
                    "longer be replaced."
                )
            # Allow registering handlers for APIs added after finalization.
            self._registrations[operation_id] = [handler]
            if callable(handler) and not _is_view_class(handler):
                self._bindings[operation_id] = handler
            return handler

        if replace:
            self._registrations[operation_id] = [handler]
        else:
            self._registrations.setdefault(operation_id, []).append(handler)
        return handler

    def resolve(self, operation):
        """
        Resolve the operation. Before finalization, a placeholder is returned which
        rejects requests until :meth:`finalize` binds the actual handler. After
        finalization, operations are resolved immediately.

        :type operation: connexion.operations.AbstractOperation
        """
        if self._finalized:
            return super().resolve(operation)

        operation_id = self.resolve_operation_id(operation)
        resolution = Resolution(self._placeholder(operation_id), operation_id)
        self._pending.setdefault(operation_id, []).append((operation, resolution))
        return resolution

    def resolve_function_from_operation_id(self, operation_id):
        """
        Resolve a previously registered handler for an operationId.

        :type operation_id: str
        """
        handler = self._resolve_from_registries(operation_id)
        if handler is None:
            raise ResolverError(
                f'Cannot resolve operationId "{operation_id}"! No handler has been '
                "registered for it."
            )
        return handler

    def finalize(self) -> None:
        """
        Bind all pending operations to their registered handlers at once.

        All pending operations are validated, and all missing or conflicting bindings
        and signature mismatches are reported in a single
        :class:`~connexion.exceptions.DeferredResolverError`. If finalization fails,
        no bindings are published, so the application never serves a partially bound
        API. Registrations can be corrected and ``finalize()`` retried.
        """
        if self._finalized:
            return

        errors: t.List[str] = []
        bindings: t.Dict[str, t.Callable] = {}

        conflicted = set()
        for operation_id, handlers in self._registrations.items():
            if len(self._distinct(handlers)) > 1:
                conflicted.add(operation_id)
                errors.append(
                    f'Multiple conflicting handlers registered for operationId '
                    f'"{operation_id}".'
                )

        for operation_id, pending in self._pending.items():
            if operation_id in conflicted:
                continue
            handler = self._resolve_from_registries(operation_id)
            if handler is None:
                errors.append(
                    f'No handler registered for operationId "{operation_id}".'
                )
                continue
            bindings[operation_id] = handler
            for operation, _ in pending:
                errors.extend(self._validate_operation(operation, handler))

        if errors:
            raise DeferredResolverError(
                "Could not finalize deferred resolver. The following problems were "
                "found:\n- " + "\n- ".join(errors),
                errors=errors,
            )

        # Publish all bindings at once, so a failed finalize never leaves a
        # partially bound API behind.
        self._bindings.update(bindings)
        for operation_id, pending in self._pending.items():
            for _, resolution in pending:
                resolution.function = bindings[operation_id]
        self._pending.clear()
        self._finalized = True

    def _placeholder(self, operation_id: str) -> t.Callable:
        """Build a placeholder that rejects requests until the handler is bound."""

        def deferred_handler(*args, **kwargs):
            handler = self._bindings.get(operation_id)
            if handler is None:
                raise DeferredResolverError(
                    f'Operation "{operation_id}" was called before its handler was '
                    "bound. Register all handlers and call `finalize()` before "
                    "serving requests."
                )
            return handler(*args, **kwargs)

        return deferred_handler

    def _resolve_from_registries(self, operation_id: str) -> t.Optional[t.Callable]:
        """Look up the handler for an operationId in the published bindings and
        registrations."""
        if operation_id in self._bindings:
            return self._bindings[operation_id]

        handlers = self._distinct(self._registrations.get(operation_id, ()))
        if len(handlers) == 1:
            handler = handlers[0]
            if _is_view_class(handler):
                return self._view_function(handler)
            return handler

        # Look up `<operation_id>.<method>` on a registered view class or instance.
        parent, _, method_name = operation_id.rpartition(".")
        if parent:
            parents = self._distinct(self._registrations.get(parent, ()))
            if len(parents) == 1:
                parent_handler = parents[0]
                if _is_view_class(parent_handler):
                    if hasattr(parent_handler, method_name):
                        return self._view_function(parent_handler)
                elif hasattr(parent_handler, method_name):
                    return getattr(parent_handler, method_name)
        return None

    def _view_function(self, view_class: type) -> t.Callable:
        """Return a shared view function for a view class, such as a Flask
        ``MethodView``."""
        if view_class not in self._view_cache:
            self._view_cache[view_class] = view_class.as_view(view_class.__name__)
        return self._view_cache[view_class]

    @staticmethod
    def _distinct(handlers: t.Iterable[t.Callable]) -> t.List[t.Callable]:
        """Return the distinct handlers, preserving registration order."""
        distinct = []
        for handler in handlers:
            if not any(handler is seen for seen in distinct):
                distinct.append(handler)
        return distinct

    def _validate_operation(self, operation, handler: t.Callable) -> t.List[str]:
        """Validate that the handler signature matches the parameter mapping of the
        operation."""
        from connexion.decorators.parameter import sanitized

        try:
            signature = inspect.signature(_unwrap(handler))
        except (TypeError, ValueError):
            # Some callables (e.g. some builtins) have no introspectable signature.
            return []

        parameters = signature.parameters
        accepted = {
            name
            for name, parameter in parameters.items()
            if parameter.kind
            not in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)
        }
        has_kwargs = any(
            parameter.kind == parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        positional_only = {
            name
            for name, parameter in parameters.items()
            if parameter.kind == parameter.POSITIONAL_ONLY
        }
        required = {
            name
            for name, parameter in parameters.items()
            if parameter.default is parameter.empty
            and parameter.kind
            in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY)
        }

        expected = set()
        for parameter in operation.parameters:
            name = sanitized(parameter.get("name"))
            if name:
                expected.add(name)
        if operation.is_request_body_defined:
            try:
                body_name = operation.body_name(None)
            except Exception:  # pragma: no cover
                body_name = "body"
            name = sanitized(body_name)
            if name:
                expected.add(name)

        operation_id = operation.operation_id
        handler_name = getattr(handler, "__qualname__", repr(handler))
        errors = []

        for name in sorted(positional_only):
            errors.append(
                f'Operation "{operation_id}": argument "{name}" of handler '
                f'"{handler_name}" is positional-only, but Connexion passes '
                "arguments by keyword."
            )
        if not has_kwargs:
            for name in sorted(expected - accepted):
                errors.append(
                    f'Operation "{operation_id}": parameter "{name}" is defined in '
                    f'the specification, but not accepted by handler '
                    f'"{handler_name}".'
                )
        for name in sorted(required - expected - CONTEXT_ARGUMENTS):
            errors.append(
                f'Operation "{operation_id}": required argument "{name}" of handler '
                f'"{handler_name}" is not provided by the specification.'
            )
        return errors


class RelativeResolver(Resolver):
    """
    Resolves endpoint functions relative to a given root path or module.
    """

    def __init__(self, root_path, function_resolver=utils.get_function_from_name):
        """
        :param root_path: The root path relative to which an operationId is resolved.
            Can also be a module. Has the same effect as setting
            `x-swagger-router-controller` or `x-openapi-router-controller` equal to
            `root_path` for every operation individually.
        :type root_path: typing.Union[str, types.ModuleType]
        :param function_resolver: Function that resolves functions using an operationId
        :type function_resolver: types.FunctionType
        """
        super().__init__(function_resolver=function_resolver)
        if inspect.ismodule(root_path):
            self.root_path = root_path.__name__
        else:
            self.root_path = root_path

    def resolve_operation_id(self, operation):
        """Resolves the operationId relative to the root path, unless
        x-swagger-router-controller or x-openapi-router-controller is specified.

        :param operation: The operation to resolve
        :type operation: connexion.operations.AbstractOperation
        """
        operation_id = operation.operation_id
        router_controller = operation.router_controller
        if router_controller is None:
            return f"{self.root_path}.{operation_id}"
        return f"{router_controller}.{operation_id}"


class RestyResolver(Resolver):
    """
    Resolves endpoint functions using REST semantics (unless overridden by specifying operationId)
    """

    def __init__(
        self, default_module_name: str, *, collection_endpoint_name: str = "search"
    ):
        """
        :param default_module_name: Default module name for operations
        :param collection_endpoint_name: Name of function to resolve collection endpoints to
        """
        super().__init__()
        self.default_module_name = default_module_name
        self.collection_endpoint_name = collection_endpoint_name

    def resolve_operation_id(self, operation):
        """
        Resolves the operationId using REST semantics unless explicitly configured in the spec

        :type operation: connexion.operations.AbstractOperation
        """
        if operation.operation_id:
            return super().resolve_operation_id(operation)

        return self.resolve_operation_id_using_rest_semantics(operation)

    def resolve_operation_id_using_rest_semantics(self, operation):
        """
        Resolves the operationId using REST semantics

        :type operation: connexion.operations.AbstractOperation
        """

        # Split the path into components delimited by '/'
        path_components = [c for c in operation.path.split("/") if len(c)]

        def is_var(component):
            """True if the path component is a var. eg, '{id}'"""
            return (component[0] == "{") and (component[-1] == "}")

        resource_name = ".".join([c for c in path_components if not is_var(c)]).replace(
            "-", "_"
        )

        def get_controller_name():
            x_router_controller = operation.router_controller

            name = self.default_module_name

            if x_router_controller:
                name = x_router_controller

            elif resource_name:
                name += "." + resource_name

            return name

        def get_function_name():
            method = operation.method

            is_collection_endpoint = (
                method.lower() == "get"
                and len(resource_name)
                and not is_var(path_components[-1])
            )

            return (
                self.collection_endpoint_name
                if is_collection_endpoint
                else method.lower()
            )

        return f"{get_controller_name()}.{get_function_name()}"


class MethodResolverBase(RestyResolver):
    """
    Resolves endpoint functions based on Flask's MethodView semantics, e.g.

    .. code-block:: yaml

        paths:
            /foo_bar:
                get:
                    # Implied function call: api.FooBarView().get

    .. code-block:: python

        class FooBarView(MethodView):
            def get(self):
                return ...
            def post(self):
                return ...

    """

    _class_arguments_type = t.Dict[
        str, t.Dict[str, t.Union[t.Iterable, t.Dict[str, t.Any]]]
    ]

    def __init__(self, *args, class_arguments: _class_arguments_type = None, **kwargs):
        """
        :param args: Arguments passed to :class:`~RestyResolver`
        :param class_arguments: Arguments to instantiate the View Class in the format below
        :param kwargs: Keywords arguments passed to :class:`~RestyResolver`

        .. code-block:: python

            {
              "ViewName": {
                "args": (positional arguments,)
                "kwargs": {
                  "keyword": "argument"
                }
              }
            }
        """
        self.class_arguments = class_arguments or {}
        super(MethodResolverBase, self).__init__(*args, **kwargs)
        self.initialized_views: list = []

    def resolve_operation_id(self, operation):
        """
        Resolves the operationId using REST semantics unless explicitly configured in the spec
        Once resolved with REST semantics the view_name is capitalised and has 'View' added
        to it so it now matches the Class names of the MethodView

        :type operation: connexion.operations.AbstractOperation
        """
        if operation.operation_id:
            # If operation_id is defined then use the higher level API to resolve
            return RestyResolver.resolve_operation_id(self, operation)

        # Use RestyResolver to get operation_id for us (follow their naming conventions/structure)
        operation_id = self.resolve_operation_id_using_rest_semantics(operation)
        module_name, view_base, meth_name = operation_id.rsplit(".", 2)
        view_name = camelize(view_base) + "View"

        return f"{module_name}.{view_name}.{meth_name}"

    def resolve_function_from_operation_id(self, operation_id):
        """
        Invokes the function_resolver

        :type operation_id: str
        """

        try:
            module_name, view_name, meth_name = operation_id.rsplit(".", 2)
            if operation_id and not view_name.endswith("View"):
                # If operation_id is not a view then assume it is a standard function
                return self.function_resolver(operation_id)

            mod = __import__(module_name, fromlist=[view_name])
            view_cls = getattr(mod, view_name)
            # find the view and return it
            return self.resolve_method_from_class(view_name, meth_name, view_cls)

        except ImportError as e:
            msg = 'Cannot resolve operationId "{}"! Import error was "{}"'.format(
                operation_id, str(e)
            )
            raise ResolverError(msg)
        except (AttributeError, ValueError) as e:
            raise ResolverError(str(e))

    def resolve_method_from_class(self, view_name, meth_name, view_cls):
        """
        Returns the view function for the given view class.
        """
        raise NotImplementedError()


class MethodResolver(MethodResolverBase):
    """
    A generic method resolver that instantiates a class and extracts the method
    from it, based on the operation id.
    """

    def resolve_method_from_class(self, view_name, meth_name, view_cls):
        view = None
        for v in self.initialized_views:
            if v.__class__ == view_cls:
                view = v
                break
        if view is None:
            # get the args and kwargs for this view
            cls_arguments = self.class_arguments.get(view_name, {})
            cls_args = cls_arguments.get("args", ())
            cls_kwargs = cls_arguments.get("kwargs", {})
            # instantiate the class with the args and kwargs
            view = view_cls(*cls_args, **cls_kwargs)
            self.initialized_views.append(view)
        # get the method if the class
        func = getattr(view, meth_name)
        # Return the method function of the class
        return func


class MethodViewResolver(MethodResolverBase):
    """
    A specialized method resolver that works with flask's method views.
    It resolves the method by calling as_view on the class.
    """

    def __init__(self, *args, **kwargs):
        if "collection_endpoint_name" in kwargs:
            del kwargs["collection_endpoint_name"]
            # Dispatch of request is done by Flask
            logger.warning(
                "collection_endpoint_name is ignored by the MethodViewResolver. "
                "Requests to a collection endpoint will be routed to .get()"
            )
        super().__init__(*args, **kwargs)

    def resolve_method_from_class(self, view_name, meth_name, view_cls):
        view = None
        for v in self.initialized_views:
            # views returned by <class>.as_view
            # have the origin class attached as .view_class
            if v.view_class == view_cls:
                view = v
                break
        if view is None:
            # get the args and kwargs for this view
            cls_arguments = self.class_arguments.get(view_name, {})
            cls_args = cls_arguments.get("args", ())
            cls_kwargs = cls_arguments.get("kwargs", {})
            # call as_view to get a view function
            # that is decorated with the classes
            # decorator list, if any
            view = view_cls.as_view(view_name, *cls_args, **cls_kwargs)
            # add the view to the list of initialized views
            # in order to call as_view only once
            self.initialized_views.append(view)
        # return the class as view function
        # for each operation so that requests
        # are dispatched with <class>.dispatch_request,
        # when calling the view function
        return view
