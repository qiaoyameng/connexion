OpenAPI Links
=============

Connexion can resolve the `links`_ declared on the responses of an operation, so the
result can be handed off to a follow-up workflow. Resolving a link evaluates the
`runtime expressions`_ in its ``parameters`` and ``requestBody`` against a concrete
request/response pair and returns a description of the follow-up call. No HTTP request
is performed.

.. code-block:: python

    from connexion.links import LinkResolver

    resolver = LinkResolver(spec)  # a spec dict or Specification instance
    calls = resolver.resolve(
        "listUsers",
        200,
        request={"method": "GET", "url": "https://api.example.com/users"},
        response={
            "headers": {"X-User-Id": "42"},
            "body": {"users": [{"id": 42}]},
        },
    )
    for call in calls:
        print(call.method, call.path, call.parameters, call.request_body)

The resolver supports ``$request`` and ``$response`` expressions for ``path``,
``query``, ``header`` and ``body`` sources, including JSON Pointers such as
``$response.body#/users/0/id``. Header names are matched case-insensitively. A
relative ``operationRef`` is resolved against the location of the specification
document.

Errors are raised as distinct exception types:

- ``MissingLinkValueError``: a runtime expression refers to a value that is missing
  from the request or response. Missing values are never coerced to the string
  ``"None"``.
- ``LinkTargetNotFoundError``: the operation targeted by ``operationId`` or
  ``operationRef`` does not exist.
- ``CircularLinkError``: resolving the link led to a circular ``$ref`` chain.

.. _links: https://spec.openapis.org/oas/v3.0.3#link-object
.. _runtime expressions: https://spec.openapis.org/oas/v3.0.3#runtime-expressions
