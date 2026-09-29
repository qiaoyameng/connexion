"""
This module contains resolvers, functions that resolves the user defined view functions
from the operations defined in the OpenAPI spec.
"""

import inspect
import logging
import typing as t

from inflection import camelize

import connexion.utils as utils
from connexion.exceptions import (
    DeferredResolutionError,
    ProblemException,
    ResolverError,
)

logger = logging.getLogger("connexion.resolver")


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


class _DeferredResolution:
    """
    Holds the placeholder registered with the framework in place of the actual view
    function. The placeholder is bound to the actual function when the
    :class:`DeferredResolver` is finalized. Until then, it raises a 503 ProblemException
    when called, so no business logic is served.
    """

    __slots__ = ("operation_id", "function", "placeholder")

    def __init__(self, operation_id: t.Optional[str]):
        self.operation_id = operation_id
        self.function: t.Optional[t.Callable] = None

        async def placeholder(*args, **kwargs):
            function = self.function
            if function is None:
                raise ProblemException(
                    status=503,
                    title="Service unavailable",
                    detail=(
                        f'Operation "{self.operation_id}" is not available yet. The '
                        "application registered its APIs with a DeferredResolver that "
                        "has not been finalized."
                    ),
                )
            result = function(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

        # Flask requires view function names without dots.
        name = f"deferred_{operation_id}".replace(".", "_")
        setattr(placeholder, "__name__", name)
        setattr(placeholder, "__qualname__", name)
        self.placeholder = placeholder

    def bind(self, function: t.Callable) -> None:
        self.function = function
        # Expose the bound function so introspection (signatures, coroutine detection)
        # sees the actual view function once bound.
        setattr(self.placeholder, "__wrapped__", function)

    def __repr__(self) -> str:
        state = f"bound to {self.function!r}" if self.function else "unbound"
        return f"<_DeferredResolution {self.operation_id!r} ({state})>"


class DeferredResolver(Resolver):
    """
    Resolver that defers binding of view functions to an explicit :meth:`finalize` call.

    This supports plugin startup orders in which the specifications are loaded before all
    view functions are available:

    1. APIs are registered as usual via ``add_api(..., resolver=DeferredResolver())``. No
       view functions are needed yet. Operations registered before finalization are
       bound to placeholders, so no business logic is served (requests receive a 503
       response).
    2. Plugins register their view functions per operationId via :meth:`register_function`.
    3. :meth:`finalize` binds all operations at once. It raises a
       :class:`connexion.exceptions.DeferredResolutionError` listing all missing and
       duplicate bindings, as well as any signature mismatches between the registered
       functions and the parameter mapping of their operations. If finalization fails,
       no operation is bound, so a half-configured set of routes is never exposed.
       Missing or conflicting registrations can be fixed, after which :meth:`finalize`
       can be retried.

    Note that connexion registers operations when its middleware stack is built, which
    happens at the latest when the first request is received. :meth:`finalize` validates
    all operations known at that point, and can be called again if more operations were
    registered in the meantime.

    After successful finalization, functions bound to an operationId can no longer be
    replaced, and unknown operationIds raise a
    :class:`connexion.exceptions.ResolverError`, like with the standard
    :class:`Resolver`.
    """

    def __init__(self, resolver: t.Optional[Resolver] = None):
        """
        :param resolver: Resolver to delegate operationId resolution to. Defaults to the
            standard :class:`Resolver`. Pass e.g. a :class:`RestyResolver` or
            :class:`MethodViewResolver` to use their operationId semantics.
        """
        super().__init__()
        self.resolver = resolver or Resolver()
        self._pending: t.Dict[
            t.Optional[str], t.List[t.Tuple[t.Any, _DeferredResolution]]
        ] = {}
        self._registrations: t.Dict[str, t.List[t.Callable]] = {}
        self._functions: t.Dict[str, t.Callable] = {}
        self._finalized = False

    @property
    def finalized(self) -> bool:
        """Whether the resolver has been successfully finalized at least once."""
        return self._finalized

    @property
    def pending_operation_ids(self) -> t.List[t.Optional[str]]:
        """Operation ids of operations still waiting to be bound by :meth:`finalize`."""
        return list(self._pending)

    def resolve_operation_id(self, operation):
        return self.resolver.resolve_operation_id(operation)

    def resolve(self, operation):
        operation_id = self.resolve_operation_id(operation)
        try:
            function = self._functions[operation_id]
        except KeyError:
            if self._finalized:
                raise ResolverError(
                    f'Cannot resolve operationId "{operation_id}"! No function was '
                    "registered for it."
                )
        else:
            return Resolution(function, operation_id)

        deferred = _DeferredResolution(operation_id)
        self._pending.setdefault(operation_id, []).append((operation, deferred))
        return Resolution(deferred.placeholder, operation_id)

    def register_function(self, operation_id: str, function: t.Callable) -> None:
        """
        Register a view function for an operationId. Called by plugins to provide the
        business logic for operations that were registered before they were available.

        Registering multiple different functions for the same operationId is reported
        as an error by :meth:`finalize`. After finalization, functions are bound
        immediately, and functions already bound to an operationId can no longer be
        replaced.
        """
        if operation_id in self._functions:
            raise DeferredResolutionError(
                f'Cannot replace the function already bound to operationId '
                f'"{operation_id}".'
            )
        if self._finalized:
            self._functions[operation_id] = function
            for _, deferred in self._pending.pop(operation_id, []):
                deferred.bind(function)
            return
        functions = self._registrations.setdefault(operation_id, [])
        if function not in functions:
            functions.append(function)

    def unregister_function(
        self, operation_id: str, function: t.Optional[t.Callable] = None
    ) -> None:
        """
        Remove registrations for an operationId, e.g. to fix duplicate registrations
        reported by a failed :meth:`finalize`. If ``function`` is provided, only that
        function is removed, otherwise all registrations for the operationId are removed.
        """
        if operation_id in self._functions:
            raise DeferredResolutionError(
                f'Cannot unregister the function already bound to operationId '
                f'"{operation_id}".'
            )
        functions = self._registrations.get(operation_id)
        if not functions:
            return
        if function is None:
            functions.clear()
        else:
            while function in functions:
                functions.remove(function)

    def finalize(self) -> None:
        """
        Bind all known operations to their registered view functions at once.

        Raises a :class:`connexion.exceptions.DeferredResolutionError` listing all
        missing and duplicate bindings and all signature mismatches. If any error is
        found, no operation is bound, and ``finalize`` can be retried after fixing the
        registrations.
        """
        errors = []

        for operation_id, functions in self._registrations.items():
            if len(functions) > 1:
                names = ", ".join(_qualname(f) for f in functions)
                errors.append(
                    f'Multiple functions registered for operationId "{operation_id}": '
                    f"{names}."
                )

        for operation_id, pending in self._pending.items():
            functions = self._registrations.get(operation_id, [])
            if not functions:
                errors.append(f'No function registered for operationId "{operation_id}".')
                continue
            if len(functions) == 1:
                function = functions[0]
                for operation, _ in pending:
                    try:
                        self._validate_arguments(operation, operation_id, function)
                    except DeferredResolutionError as e:
                        errors.append(str(e))

        if errors:
            raise DeferredResolutionError(
                "Could not finalize deferred resolution:\n- "
                + "\n- ".join(dict.fromkeys(errors))
            )

        # Commit the registrations and bind all pending operations.
        for operation_id, functions in self._registrations.items():
            self._functions[operation_id] = functions[0]
        self._registrations.clear()
        for operation_id, pending in self._pending.items():
            function = self._functions[operation_id]
            for _, deferred in pending:
                deferred.bind(function)
        self._pending.clear()
        self._finalized = True

    def _validate_arguments(
        self, operation, operation_id: t.Optional[str], function: t.Callable
    ) -> None:
        """
        Validate that the arguments required by the function are provided by the
        parameter mapping of the operation.
        """
        from connexion.decorators.parameter import unwrap_decorators

        try:
            signature = inspect.signature(unwrap_decorators(function))
        except (TypeError, ValueError):
            # Cannot inspect the signature, so we cannot validate it.
            return
        parameters = list(signature.parameters.values())
        if any(p.kind == p.VAR_KEYWORD for p in parameters):
            return
        provided = self._provided_arguments(operation)
        missing = [
            p.name
            for p in parameters
            if p.default is p.empty
            and p.kind
            in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
            and p.name not in ("self", "cls")
            and p.name not in provided
        ]
        if missing:
            raise DeferredResolutionError(
                f'Function "{_qualname(function)}" registered for operationId '
                f'"{operation_id}" requires argument(s) {", ".join(missing)} which '
                f"are not provided by the parameters of "
                f'"{operation.method.upper()} {operation.path}".'
            )

    @staticmethod
    def _provided_arguments(operation) -> t.Set[str]:
        """
        Compute the argument names connexion can pass to the view function of the
        operation, taking parameter sanitization into account.
        """
        from connexion.decorators.parameter import CONTEXT_NAME, pythonic, sanitized

        names = {CONTEXT_NAME}
        for parameter in operation.parameters:
            if parameter.get("in") in ("path", "query", "formData", "body"):
                names.add(parameter["name"])
        if operation.is_request_body_defined:
            try:
                names.add(operation.body_name(None))
            except Exception:  # pragma: no cover - defensive
                names.add("body")

        provided = set()
        for name in names:
            provided.update((name, sanitized(name), pythonic(name)))
        return provided


def _qualname(function: t.Callable) -> str:
    return getattr(function, "__qualname__", None) or getattr(
        function, "__name__", repr(function)
    )


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
