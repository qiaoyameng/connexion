"""
This module implements resolution of `OpenAPI links
<https://spec.openapis.org/oas/v3.0.3#link-object>`_ declared on the responses of an
operation.

It evaluates the runtime expressions used in a link's ``parameters`` and ``requestBody``
against a concrete request/response pair and returns :class:`LinkCall` objects describing
the follow-up call, so the result can be handed off to a workflow. No HTTP request is
performed.
"""

from __future__ import annotations

import dataclasses
import typing as t
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

import yaml

from connexion.exceptions import (
    CircularLinkError,
    LinkError,
    LinkTargetNotFoundError,
    MissingLinkValueError,
)
from connexion.http_facts import METHODS


@dataclasses.dataclass
class LinkRequestContext:
    """The request side of the runtime data a link is evaluated against."""

    method: str = ""
    url: str = ""
    path_params: t.Dict[str, t.Any] = dataclasses.field(default_factory=dict)
    query_params: t.Dict[str, t.Any] = dataclasses.field(default_factory=dict)
    headers: t.Dict[str, t.Any] = dataclasses.field(default_factory=dict)
    body: t.Any = None


@dataclasses.dataclass
class LinkResponseContext:
    """The response side of the runtime data a link is evaluated against."""

    status_code: t.Optional[t.Union[int, str]] = None
    headers: t.Dict[str, t.Any] = dataclasses.field(default_factory=dict)
    body: t.Any = None


@dataclasses.dataclass
class LinkCall:
    """A description of the call a resolved link points to.

    This is a plain description of the target operation and the computed parameter
    values. It is up to the caller to execute the call.
    """

    name: str
    operation_id: t.Optional[str]
    method: str
    path: str
    parameters: t.Dict[str, t.Any] = dataclasses.field(default_factory=dict)
    request_body: t.Any = None
    server: t.Optional[dict] = None
    description: t.Optional[str] = None


def _json_pointer_get(document: t.Any, pointer: str, error: t.Type[Exception]) -> t.Any:
    """Resolve an RFC 6901 JSON Pointer against a document.

    :raises error: if the pointer is invalid or cannot be resolved.
    """
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise error(f"Invalid JSON pointer: {pointer!r}.")
    current = document
    for raw_token in pointer.split("/")[1:]:
        token = unquote(raw_token).replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                index = int(token)
            except ValueError:
                raise error(
                    f"Cannot resolve {pointer!r}: {token!r} is not a valid array index."
                )
            if not 0 <= index < len(current):
                raise error(
                    f"Cannot resolve {pointer!r}: index {index} is out of range."
                )
            current = current[index]
        elif isinstance(current, dict):
            if token not in current:
                raise error(f"Cannot resolve {pointer!r}: key {token!r} is missing.")
            current = current[token]
        else:
            raise error(f"Cannot resolve {pointer!r}: cannot descend into {current!r}.")
    return current


def _get_header(headers: t.Mapping[str, t.Any], name: str, expression: str) -> t.Any:
    """Look up a header by name, case-insensitively as mandated by RFC 9110."""
    for key, value in (headers or {}).items():
        if key.lower() == name.lower():
            return value
    raise MissingLinkValueError(
        f"Cannot evaluate {expression!r}: header {name!r} is missing."
    )


def evaluate_expression(
    expression: t.Any,
    request: LinkRequestContext,
    response: LinkResponseContext,
) -> t.Any:
    """Evaluate an OpenAPI runtime expression against a request/response pair.

    Values that do not look like a runtime expression are returned as-is (literals).

    :raises MissingLinkValueError: if the expression refers to a value that is missing.
    :raises LinkError: if the expression is not a valid runtime expression.
    """
    if not isinstance(expression, str) or not expression.startswith("$"):
        return expression

    if expression == "$url":
        return request.url
    if expression == "$method":
        return request.method
    if expression == "$statusCode":
        if response.status_code is None:
            raise MissingLinkValueError(
                "Cannot evaluate '$statusCode': no response status code was provided."
            )
        return response.status_code

    if expression.startswith("$request."):
        return _evaluate_source(expression[len("$request.") :], request, expression)
    if expression.startswith("$response."):
        return _evaluate_source(expression[len("$response.") :], response, expression)

    raise LinkError(f"Unsupported runtime expression: {expression!r}.")


def _evaluate_source(
    source: str,
    context: t.Union[LinkRequestContext, LinkResponseContext],
    expression: str,
) -> t.Any:
    location, _, name = source.partition(".")
    if location == "header":
        if not name:
            raise LinkError(f"Invalid runtime expression: {expression!r}.")
        return _get_header(context.headers, name, expression)
    if location == "query":
        if not isinstance(context, LinkRequestContext):
            raise LinkError(f"Invalid runtime expression: {expression!r}.")
        if name not in context.query_params:
            raise MissingLinkValueError(
                f"Cannot evaluate {expression!r}: query parameter {name!r} is missing."
            )
        return context.query_params[name]
    if location == "path":
        if not isinstance(context, LinkRequestContext):
            raise LinkError(f"Invalid runtime expression: {expression!r}.")
        if name not in context.path_params:
            raise MissingLinkValueError(
                f"Cannot evaluate {expression!r}: path parameter {name!r} is missing."
            )
        return context.path_params[name]
    if source == "body" or source.startswith("body#"):
        pointer = source[len("body#") :] if source.startswith("body#") else ""
        return _json_pointer_get(context.body, pointer, MissingLinkValueError)
    raise LinkError(f"Invalid runtime expression: {expression!r}.")


class LinkResolver:
    """Resolves the links declared on the responses of operations in an OpenAPI spec.

    :param spec: an OpenAPI spec, either as a raw dict or as a
        :class:`~connexion.spec.Specification` instance.
    :param base_uri: the base URI relative ``operationRef`` and ``$ref`` values are
        resolved against. Defaults to the base URI of the specification, which is the
        location of the specification file when it was loaded from disk.
    """

    def __init__(self, spec: t.Any, base_uri: t.Optional[str] = None):
        from connexion.spec import Specification

        if isinstance(spec, Specification):
            if base_uri is None:
                base_uri = spec._base_uri
            spec = spec.raw
        self._spec = spec
        self._base_uri = base_uri or ""
        self._document_cache: t.Dict[str, t.Any] = {}

    def resolve(
        self,
        operation_id: str,
        status_code: t.Union[int, str],
        *,
        request: t.Union[LinkRequestContext, dict, None] = None,
        response: t.Union[LinkResponseContext, dict, None] = None,
    ) -> t.List[LinkCall]:
        """Resolve the links declared on a response of an operation.

        :param operation_id: the operationId of the operation the response belongs to.
        :param status_code: the status code of the response to resolve links for.
        :param request: the request that triggered the response, as a
            :class:`LinkRequestContext` or an equivalent dict.
        :param response: the received response, as a :class:`LinkResponseContext` or an
            equivalent dict.
        :return: a list of :class:`LinkCall` descriptions, one per declared link.
        """
        request_context = self._coerce_request(request)
        response_context = self._coerce_response(response, status_code)
        links = self.get_links(operation_id, status_code)
        return [
            self._build_call(name, link, request_context, response_context)
            for name, link in links.items()
        ]

    def get_links(
        self, operation_id: str, status_code: t.Union[int, str]
    ) -> t.Dict[str, dict]:
        """Return the link objects declared on a response of an operation, with any
        ``$ref`` chains resolved."""
        found = self._find_operation(self._spec, operation_id)
        if found is None:
            raise LinkTargetNotFoundError(
                f"No operation with operationId {operation_id!r} exists in the spec."
            )
        _, _, operation = found
        responses = self._resolve_refs(operation.get("responses") or {})
        response = responses.get(str(status_code))
        if response is None:
            response = responses.get("default")
        if response is None:
            return {}
        response = self._resolve_refs(response)
        return {
            name: self._resolve_refs(link)
            for name, link in (response.get("links") or {}).items()
        }

    @staticmethod
    def _coerce_request(
        request: t.Union[LinkRequestContext, dict, None]
    ) -> LinkRequestContext:
        if request is None:
            return LinkRequestContext()
        if isinstance(request, dict):
            return LinkRequestContext(**request)
        return request

    @staticmethod
    def _coerce_response(
        response: t.Union[LinkResponseContext, dict, None],
        status_code: t.Union[int, str],
    ) -> LinkResponseContext:
        if response is None:
            return LinkResponseContext(status_code=status_code)
        if isinstance(response, dict):
            context = LinkResponseContext(**response)
        else:
            context = response
        if context.status_code is None:
            context.status_code = status_code
        return context

    def _build_call(
        self,
        name: str,
        link: dict,
        request: LinkRequestContext,
        response: LinkResponseContext,
    ) -> LinkCall:
        has_operation_id = "operationId" in link
        has_operation_ref = "operationRef" in link
        if has_operation_id and has_operation_ref:
            raise LinkError(f"Link {name!r} defines both operationId and operationRef.")
        if has_operation_id:
            target_id = link["operationId"]
            found = self._find_operation(self._spec, target_id)
            if found is None:
                raise LinkTargetNotFoundError(
                    f"Link {name!r} targets operationId {target_id!r}, "
                    "which does not exist in the spec."
                )
            path, method, _ = found
        elif has_operation_ref:
            target_id, path, method = self._resolve_operation_ref(
                name, link["operationRef"]
            )
        else:
            raise LinkError(
                f"Link {name!r} defines neither operationId nor operationRef."
            )

        parameters = {
            parameter: evaluate_expression(expression, request, response)
            for parameter, expression in (link.get("parameters") or {}).items()
        }
        request_body = None
        if "requestBody" in link:
            request_body = evaluate_expression(link["requestBody"], request, response)

        return LinkCall(
            name=name,
            operation_id=target_id,
            method=method,
            path=path,
            parameters=parameters,
            request_body=request_body,
            server=link.get("server"),
            description=link.get("description"),
        )

    def _resolve_operation_ref(
        self, name: str, operation_ref: str
    ) -> t.Tuple[t.Optional[str], str, str]:
        """Resolve an operationRef to the targeted operation.

        Relative references are resolved against the location of the specification
        document, as required by the OpenAPI specification.
        """
        document_uri, fragment = urldefrag(operation_ref)
        if document_uri:
            document = self._load_document(urljoin(self._base_uri, document_uri))
        else:
            document = self._spec
        operation = _json_pointer_get(document, fragment, LinkTargetNotFoundError)
        if not isinstance(operation, dict):
            raise LinkTargetNotFoundError(
                f"Link {name!r} operationRef {operation_ref!r} does not point to "
                "an operation object."
            )

        tokens = [
            token.replace("~1", "/").replace("~0", "~")
            for token in fragment.split("/")[1:]
        ]
        if len(tokens) == 3 and tokens[0] == "paths":
            _, path, method = tokens
        else:
            found = self._find_operation_object(document, operation)
            if found is None:
                raise LinkTargetNotFoundError(
                    f"Link {name!r} operationRef {operation_ref!r} does not point to "
                    "an operation in the paths of the target document."
                )
            path, method, _ = found
        return operation.get("operationId"), path, method.lower()

    @staticmethod
    def _find_operation(
        spec: dict, operation_id: str
    ) -> t.Optional[t.Tuple[str, str, dict]]:
        for path, path_item in (spec.get("paths") or {}).items():
            if not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method.lower() not in METHODS:
                    continue
                if (
                    isinstance(operation, dict)
                    and operation.get("operationId") == operation_id
                ):
                    return path, method.lower(), operation
        return None

    @staticmethod
    def _find_operation_object(
        spec: dict, target: dict
    ) -> t.Optional[t.Tuple[str, str, dict]]:
        for path, path_item in (spec.get("paths") or {}).items():
            if not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method.lower() not in METHODS:
                    continue
                if operation is target:
                    return path, method.lower(), operation
        return None

    def _resolve_refs(self, node: t.Any, _seen: t.Tuple[str, ...] = ()) -> t.Any:
        """Resolve a chain of ``$ref`` values, detecting circular references."""
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if ref in _seen:
                raise CircularLinkError(
                    f"Circular reference detected while resolving {ref!r}."
                )
            _seen = (*_seen, ref)
            document_uri, fragment = urldefrag(ref)
            if document_uri:
                document = self._load_document(urljoin(self._base_uri, document_uri))
            else:
                document = self._spec
            resolved = _json_pointer_get(document, fragment, LinkTargetNotFoundError)
            if isinstance(resolved, dict) and isinstance(node, dict):
                merged = {k: v for k, v in node.items() if k != "$ref"}
                merged.update(resolved)
                node = merged
            else:
                node = resolved
        return node

    def _load_document(self, uri: str) -> t.Any:
        """Load an external document, caching the result."""
        if uri in self._document_cache:
            return self._document_cache[uri]
        parsed = urlsplit(uri)
        if parsed.scheme in ("http", "https"):
            from connexion.json_schema import URLHandler

            document = URLHandler()(uri)
        else:
            path = unquote(parsed.path) if parsed.scheme == "file" else uri
            with open(path) as fh:
                document = yaml.safe_load(fh)
        self._document_cache[uri] = document
        return document
