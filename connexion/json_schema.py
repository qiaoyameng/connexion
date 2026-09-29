"""
Module containing all code related to json schema validation.
"""

import contextlib
import io
import json
import os
import typing as t
import urllib.parse
import urllib.request
from collections.abc import Mapping
from copy import deepcopy

import requests
import yaml
from jsonschema import Draft4Validator
from jsonschema.exceptions import ValidationError
from jsonschema.validators import extend
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT4

from .utils import deep_get


class ExtendedSafeLoader(yaml.SafeLoader):
    """Extends the yaml SafeLoader to coerce all keys to string so the result is valid json."""

    def __init__(self, stream):
        self.original_construct_mapping = self.construct_mapping
        self.construct_mapping = self.extended_construct_mapping
        super().__init__(stream)

    def extended_construct_mapping(self, node, deep=False):
        data = self.original_construct_mapping(node, deep)
        return {str(key): data[key] for key in data}


class FileHandler:
    """Handler to resolve file refs."""

    def __call__(self, uri):
        filepath = self._uri_to_path(uri)
        with open(filepath) as fh:
            return yaml.load(fh, ExtendedSafeLoader)

    @staticmethod
    def _uri_to_path(uri):
        parsed = urllib.parse.urlparse(uri)
        host = "{0}{0}{mnt}{0}".format(os.path.sep, mnt=parsed.netloc)
        return os.path.abspath(
            os.path.join(host, urllib.request.url2pathname(parsed.path))
        )


class URLHandler:
    """Handler to resolve url refs."""

    def __call__(self, uri):
        response = requests.get(uri)
        response.raise_for_status()

        data = io.StringIO(response.text)
        with contextlib.closing(data) as fh:
            return yaml.load(fh, ExtendedSafeLoader)


def resource_from_spec(spec: t.Dict[str, t.Any]) -> Resource:
    """Create a `referencing.Resource` from a schema specification."""
    return Resource.from_contents(spec, default_specification=DRAFT4)


def retrieve(uri: str) -> Resource:
    """Retrieve a resource from a URI.

    This function is passed to the `referencing.Registry`,
    which calls it whenever a URI not present in the registry is accessed."""
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme in ("http", "https"):
        content = URLHandler()(uri)
    elif parsed.scheme in ("file", ""):
        content = FileHandler()(uri)
    else:  # pragma: no cover
        # Default branch from jsonschema.RefResolver.resolve_remote()
        # for backwards compatibility.
        with urllib.request.urlopen(uri) as url:
            content = json.loads(url.read().decode("utf-8"))
    return resource_from_spec(content)


def resolve_refs(spec, store=None, base_uri=""):
    """
    Resolve JSON references like {"$ref": <some URI>} in a spec.
    Optionally takes a store, which is a mapping from reference URLs to a
    dereferenced objects. Prepopulating the store can avoid network calls.
    """
    spec = deepcopy(spec)
    store = store or {}
    registry = Registry(retrieve=retrieve).with_resources(
        (
            (base_uri, resource_from_spec(spec)),
            *((key, resource_from_spec(value)) for key, value in store.items()),
        )
    )

    def _do_resolve(node, resolver):
        if isinstance(node, Mapping) and "$ref" in node:
            path = node["$ref"][2:].split("/")
            try:
                # resolve known references
                retrieved = deep_get(spec, path)
                node.update(retrieved)
                if isinstance(retrieved, Mapping) and "$ref" in retrieved:
                    node = _do_resolve(node, resolver)
                node.pop("$ref", None)
                return node
            except KeyError:
                # resolve external references
                resolved = resolver.lookup(node["$ref"])
                return _do_resolve(resolved.contents, resolved.resolver)
        elif isinstance(node, Mapping):
            for k, v in node.items():
                node[k] = _do_resolve(v, resolver)
        elif isinstance(node, (list, tuple)):
            for i, _ in enumerate(node):
                node[i] = _do_resolve(node[i], resolver)
        return node

    res = _do_resolve(spec, registry.resolver(base_uri))
    return res


def format_error_with_path(exception: ValidationError) -> str:
    """Format a `ValidationError` with path to error."""
    error_path = ".".join(str(item) for item in exception.path)
    error_path_msg = f" - '{error_path}'" if error_path else ""
    return error_path_msg


def allow_nullable(validation_fn: t.Callable) -> t.Callable:
    """Extend an existing validation function, so it allows nullable values to be null."""

    def nullable_validation_fn(validator, to_validate, instance, schema):
        if instance is None and (
            schema.get("x-nullable") is True or schema.get("nullable")
        ):
            return

        yield from validation_fn(validator, to_validate, instance, schema)

    return nullable_validation_fn


def validate_writeOnly(validator, wo, instance, schema):
    yield ValidationError("Property is write-only")


NullableTypeValidator = allow_nullable(Draft4Validator.VALIDATORS["type"])
NullableEnumValidator = allow_nullable(Draft4Validator.VALIDATORS["enum"])

Draft4RequestValidator = extend(
    Draft4Validator,
    {
        "type": NullableTypeValidator,
        "enum": NullableEnumValidator,
    },
)

Draft4ResponseValidator = extend(
    Draft4Validator,
    {
        "type": NullableTypeValidator,
        "enum": NullableEnumValidator,
        "writeOnly": validate_writeOnly,
        "x-writeOnly": validate_writeOnly,
    },
)


UNKNOWN_FIELDS_POLICIES = ("reject", "strip", "keep")
"""Available policies for the operation-level ``x-unknown-fields`` extension."""


def allow_unknown_fields(validation_fn: t.Callable) -> t.Callable:
    """Extend the ``additionalProperties`` validation function to ignore
    ``additionalProperties: false``, effectively allowing unknown fields on objects
    declared as closed."""

    def ignore_closed_validation_fn(validator, additional_properties, instance, schema):
        if additional_properties is False:
            return

        yield from validation_fn(validator, additional_properties, instance, schema)

    return ignore_closed_validation_fn


Draft4KeepUnknownFieldsValidator = extend(
    Draft4RequestValidator,
    {
        "additionalProperties": allow_unknown_fields(
            Draft4Validator.VALIDATORS["additionalProperties"]
        )
    },
)
"""Request validator that allows unknown fields, even on objects declared as closed
via ``additionalProperties: false``."""


def _is_valid_ignoring_unknown_fields(schema: dict, instance: t.Any) -> bool:
    """Check whether an instance would validate against a schema if unknown fields on
    closed objects were allowed."""
    return Draft4KeepUnknownFieldsValidator(schema).is_valid(instance)


def _allowed_keys(schema: dict, instance: dict) -> t.Optional[set]:
    """Compute the set of property names allowed for ``instance`` under ``schema``.

    Returns ``None`` if the schema does not restrict property names. Constraints from
    ``allOf`` are intersected, while constraints from ``oneOf``/``anyOf`` are unioned
    over the branches the instance can still match when unknown fields are ignored.
    If no branch can match, the instance is left untouched so validation can report
    the error instead of stripping fields that are valid in another branch.
    """
    allowed = None

    def restrict(names: t.Optional[set]) -> None:
        nonlocal allowed
        if names is None:
            return
        allowed = names if allowed is None else allowed & names

    if (
        schema.get("additionalProperties", True) is False
        and "patternProperties" not in schema
    ):
        restrict(set(schema.get("properties", {})))

    for subschema in schema.get("allOf", []):
        restrict(_allowed_keys(subschema, instance))

    for keyword in ("oneOf", "anyOf"):
        branches = schema.get(keyword)
        if not branches:
            continue
        union = None
        for branch in branches:
            if not _is_valid_ignoring_unknown_fields(branch, instance):
                continue
            branch_keys = _allowed_keys(branch, instance)
            if branch_keys is None:
                # A matching branch does not restrict property names.
                union = None
                break
            union = branch_keys if union is None else union | branch_keys
        restrict(union)

    return allowed


def _property_subschemas(schema: dict, instance: dict, key: str) -> t.List[dict]:
    """Collect the subschemas describing property ``key`` from the schema itself, its
    ``allOf`` subschemas and the ``oneOf``/``anyOf`` branches the instance can still
    match when unknown fields are ignored."""
    subschemas = []

    properties = schema.get("properties", {})
    if key in properties:
        subschemas.append(properties[key])
    else:
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            subschemas.append(additional)

    for subschema in schema.get("allOf", []):
        subschemas.extend(_property_subschemas(subschema, instance, key))

    for keyword in ("oneOf", "anyOf"):
        for branch in schema.get(keyword, []):
            if _is_valid_ignoring_unknown_fields(branch, instance):
                subschemas.extend(_property_subschemas(branch, instance, key))

    return subschemas


def _item_subschemas(schema: dict, instance: list) -> t.List[dict]:
    """Collect the subschemas describing the items of an array, analogous to
    :func:`_property_subschemas`."""
    subschemas = []

    items = schema.get("items")
    if isinstance(items, dict):
        subschemas.append(items)

    for subschema in schema.get("allOf", []):
        subschemas.extend(_item_subschemas(subschema, instance))

    for keyword in ("oneOf", "anyOf"):
        for branch in schema.get(keyword, []):
            if _is_valid_ignoring_unknown_fields(branch, instance):
                subschemas.extend(_item_subschemas(branch, instance))

    return subschemas


def _strip(instance: t.Any, schema: dict, path: tuple, removed: list) -> None:
    if isinstance(instance, dict):
        allowed = _allowed_keys(schema, instance)
        if allowed is not None:
            for key in list(instance):
                if key not in allowed:
                    del instance[key]
                    removed.append(".".join(path + (key,)))

        for key, value in instance.items():
            subschemas = _property_subschemas(schema, instance, key)
            if len(subschemas) == 1:
                _strip(value, subschemas[0], path + (key,), removed)
            elif subschemas:
                _strip(value, {"anyOf": subschemas}, path + (key,), removed)

    elif isinstance(instance, list):
        items = schema.get("items")
        if isinstance(items, list):
            # Tuple validation
            for i, (item, subschema) in enumerate(zip(instance, items)):
                _strip(item, subschema, path + (str(i),), removed)
        else:
            subschemas = _item_subschemas(schema, instance)
            for i, item in enumerate(instance):
                if len(subschemas) == 1:
                    _strip(item, subschemas[0], path + (str(i),), removed)
                elif subschemas:
                    _strip(item, {"anyOf": subschemas}, path + (str(i),), removed)


def strip_unknown_fields(instance: t.Any, schema: dict) -> t.List[str]:
    """Strip unknown fields from objects declared as closed (``additionalProperties:
    false``) in the schema. The instance is updated in place.

    ``allOf`` constraints are merged, ``oneOf``/``anyOf`` constraints are applied based
    on the branches the instance can still match, and objects nested in arrays are
    handled. Fields that are valid in any branch the instance can still match are never
    stripped to force validation to pass.

    :return: The dotted paths of the removed fields.
    """
    removed: t.List[str] = []
    _strip(instance, schema, (), removed)
    return removed
