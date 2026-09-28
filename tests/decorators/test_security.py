import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
import requests
from connexion.exceptions import (
    BadRequestProblem,
    ConnexionException,
    OAuthProblem,
    OAuthResponseProblem,
    OAuthScopeProblem,
)
from connexion.lifecycle import ConnexionRequest
from connexion.security import (
    NO_VALUE,
    ApiKeySecurityHandler,
    BasicSecurityHandler,
    OAuthSecurityHandler,
    SecurityHandlerFactory,
)


def test_get_tokeninfo_url(monkeypatch):
    security_handler = OAuthSecurityHandler()
    security_handler.get_token_info_remote = MagicMock(
        return_value="get_token_info_remote_result"
    )
    env = {}
    monkeypatch.setattr("os.environ", env)
    logger = MagicMock()
    monkeypatch.setattr("connexion.security.logger", logger)

    security_def = {}
    assert security_handler.get_tokeninfo_func(security_def) is None
    logger.warn.assert_not_called()

    env["TOKENINFO_URL"] = "issue-146"
    assert (
        security_handler.get_tokeninfo_func(security_def)
        == "get_token_info_remote_result"
    )
    security_handler.get_token_info_remote.assert_called_with("issue-146")
    logger.warn.assert_not_called()
    logger.warn.reset_mock()

    security_def = {"x-tokenInfoUrl": "bar"}
    assert (
        security_handler.get_tokeninfo_func(security_def)
        == "get_token_info_remote_result"
    )
    security_handler.get_token_info_remote.assert_called_with("bar")
    logger.warn.assert_not_called()


def test_verify_oauth_missing_auth_header():
    def somefunc(token):
        return None

    security_handler = OAuthSecurityHandler()
    wrapped_func = security_handler._get_verify_func(
        somefunc, security_handler.validate_scope, ["admin"]
    )

    request = ConnexionRequest(scope={"type": "http", "headers": []})

    assert wrapped_func(request) is NO_VALUE


async def test_verify_oauth_scopes_remote(monkeypatch):
    tokeninfo = dict(uid="foo", scope="scope1 scope2")

    async def get_tokeninfo_response(*args, **kwargs):
        tokeninfo_response = requests.Response()
        tokeninfo_response.status_code = requests.codes.ok
        tokeninfo_response._content = json.dumps(tokeninfo).encode()
        return tokeninfo_response

    security_handler = OAuthSecurityHandler()
    token_info_func = security_handler.get_tokeninfo_func(
        {"x-tokenInfoUrl": "https://example.org/tokeninfo"}
    )
    wrapped_func = security_handler._get_verify_func(
        token_info_func, security_handler.validate_scope, ["admin"]
    )

    request = ConnexionRequest(
        scope={"type": "http", "headers": [[b"authorization", b"Bearer 123"]]}
    )

    client = MagicMock()
    client.get = get_tokeninfo_response
    monkeypatch.setattr(OAuthSecurityHandler, "client", client)

    with pytest.raises(OAuthScopeProblem) as exc_info:
        await wrapped_func(request)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail.startswith(
        "Provided token does not have the required scope"
    )

    tokeninfo["scope"] += " admin"
    assert await wrapped_func(request) is not None

    tokeninfo["scope"] = ["foo", "bar"]
    with pytest.raises(OAuthScopeProblem) as exc_info:
        await wrapped_func(request)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail.startswith(
        "Provided token does not have the required scope"
    )

    tokeninfo["scope"].append("admin")
    assert await wrapped_func(request) is not None


async def test_verify_oauth_invalid_local_token_response_none():
    def somefunc(token):
        return None

    security_handler = OAuthSecurityHandler()
    wrapped_func = security_handler._get_verify_func(
        somefunc, security_handler.validate_scope, ["admin"]
    )

    request = ConnexionRequest(
        scope={"type": "http", "headers": [[b"authorization", b"Bearer 123"]]}
    )

    with pytest.raises(OAuthResponseProblem):
        await wrapped_func(request)


async def test_verify_oauth_scopes_local():
    tokeninfo = dict(uid="foo", scope="scope1 scope2")

    def token_info(token):
        return tokeninfo

    security_handler = OAuthSecurityHandler()
    wrapped_func = security_handler._get_verify_func(
        token_info, security_handler.validate_scope, ["admin"]
    )

    request = ConnexionRequest(
        scope={"type": "http", "headers": [[b"authorization", b"Bearer 123"]]}
    )

    with pytest.raises(OAuthScopeProblem) as exc_info:
        await wrapped_func(request)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail.startswith(
        "Provided token does not have the required scope"
    )

    tokeninfo["scope"] += " admin"
    assert await wrapped_func(request) is not None

    tokeninfo["scope"] = ["foo", "bar"]
    with pytest.raises(OAuthScopeProblem) as exc_info:
        await wrapped_func(request)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail.startswith(
        "Provided token does not have the required scope"
    )

    tokeninfo["scope"].append("admin")
    assert await wrapped_func(request) is not None


def test_verify_basic_missing_auth_header():
    def somefunc(username, password, required_scopes=None):
        return None

    security_handler = BasicSecurityHandler()
    wrapped_func = security_handler._get_verify_func(somefunc)

    request = ConnexionRequest(
        scope={"type": "http", "headers": [[b"authorization", b"Bearer 123"]]}
    )

    assert wrapped_func(request) is NO_VALUE


async def test_verify_basic():
    def basic_info(username, password, required_scopes=None):
        if username == "foo" and password == "bar":
            return {"sub": "foo"}
        return None

    security_handler = BasicSecurityHandler()
    wrapped_func = security_handler._get_verify_func(basic_info)

    request = ConnexionRequest(
        scope={"type": "http", "headers": [[b"authorization", b"Basic Zm9vOmJhcg=="]]}
    )

    assert await wrapped_func(request) is not None


async def test_verify_apikey_query():
    def apikey_info(apikey, required_scopes=None):
        if apikey == "foobar":
            return {"sub": "foo"}
        return None

    security_handler_factory = ApiKeySecurityHandler()
    wrapped_func = security_handler_factory._get_verify_func(
        apikey_info, "query", "auth", None
    )

    request = ConnexionRequest(scope={"type": "http", "query_string": b"auth=foobar"})

    assert await wrapped_func(request) is not None


async def test_verify_apikey_header():
    def apikey_info(apikey, required_scopes=None):
        if apikey == "foobar":
            return {"sub": "foo"}
        return None

    security_handler_factory = ApiKeySecurityHandler()
    wrapped_func = security_handler_factory._get_verify_func(
        apikey_info, "header", "X-Auth", None
    )

    request = ConnexionRequest(
        scope={"type": "http", "headers": [[b"x-auth", b"foobar"]]}
    )

    assert await wrapped_func(request) is not None


async def test_verify_apikey_scopes():
    def apikey_info(apikey, required_scopes=None):
        if apikey == "admin foobar" and required_scopes == ["admin"]:
            return {"sub": "foo"}
        return None

    security_handler_factory = ApiKeySecurityHandler()

    scheme_apikey = {
        "type": "apiKey",
        "name": "x-auth",
        "in": "header",
        "scopes": {"admin"},
    }

    with patch.object(
        security_handler_factory,
        f"{security_handler_factory._resolve_func.__name__}",
        return_value=apikey_info,
    ) as mock_resolve_func:
        wrapped_func = security_handler_factory.get_fn(scheme_apikey, ["admin"])
        mock_resolve_func.assert_called_once()

        request = ConnexionRequest(
            scope={"type": "http", "headers": [[b"x-auth", b"admin foobar"]]}
        )

        assert await wrapped_func(request) == {"sub": "foo"}


async def test_multiple_schemes():
    def apikey1_info(apikey, required_scopes=None):
        if apikey == "foobar":
            return {"sub": "foo"}
        return None

    def apikey2_info(apikey, required_scopes=None):
        if apikey == "bar":
            return {"sub": "bar"}
        return None

    security_handler_factory = SecurityHandlerFactory()
    apikey_security_handler_factory = ApiKeySecurityHandler()
    wrapped_func_key1 = apikey_security_handler_factory._get_verify_func(
        apikey1_info, "header", "X-Auth-1", []
    )
    wrapped_func_key2 = apikey_security_handler_factory._get_verify_func(
        apikey2_info, "header", "X-Auth-2", []
    )
    schemes = {
        "key1": wrapped_func_key1,
        "key2": wrapped_func_key2,
    }
    wrapped_func = security_handler_factory.verify_multiple_schemes(schemes)

    # Single key does not succeed
    request = ConnexionRequest(
        scope={"type": "http", "headers": [[b"x-auth-1", b"foobar"]]}
    )

    assert await wrapped_func(request) is NO_VALUE

    request = ConnexionRequest(
        scope={"type": "http", "headers": [[b"x-auth-2", b"bar"]]}
    )

    assert await wrapped_func(request) is NO_VALUE

    # Supplying both keys does succeed
    request = ConnexionRequest(
        scope={
            "type": "http",
            "headers": [[b"x-auth-1", b"foobar"], [b"x-auth-2", b"bar"]],
        }
    )

    expected_token_info = {
        "key1": {"sub": "foo"},
        "key2": {"sub": "bar"},
    }
    assert await wrapped_func(request) == expected_token_info


async def test_verify_security_oauthproblem():
    """Tests whether verify_security raises an OAuthProblem if there are no auth_funcs."""
    security_handler_factory = SecurityHandlerFactory()
    security_func = security_handler_factory.verify_security([])

    request = MagicMock(spec_set=ConnexionRequest)
    with pytest.raises(OAuthProblem) as exc_info:
        await security_func(request)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "No authorization token provided"


@pytest.mark.parametrize(
    "errors, most_specific",
    [
        ([OAuthProblem()], OAuthProblem),
        ([OAuthProblem(), OAuthScopeProblem([], [])], OAuthScopeProblem),
        (
            [OAuthProblem(), OAuthScopeProblem([], []), BadRequestProblem],
            OAuthScopeProblem,
        ),
        (
            [
                OAuthProblem(),
                OAuthScopeProblem([], []),
                BadRequestProblem,
                ConnexionException,
            ],
            OAuthScopeProblem,
        ),
        ([BadRequestProblem(), ConnexionException()], BadRequestProblem),
        ([ConnexionException()], ConnexionException),
    ],
)
def test_raise_most_specific(errors, most_specific):
    """Tests whether most specific exception is raised from a list."""
    security_handler_factory = SecurityHandlerFactory()
    with pytest.raises(most_specific):
        security_handler_factory._raise_most_specific(errors)


async def test_optional_kwargs_injected():
    """Test that optional keyword arguments 'required_scopes' and 'request' are injected when
    defined as arguments in the user security function. This test uses the ApiKeySecurityHandler,
    but the tested behavior is generic across handlers."""
    security_handler_factory = ApiKeySecurityHandler()

    request = ConnexionRequest(
        scope={"type": "http", "headers": [[b"x-auth", b"foobar"]]}
    )

    def apikey_info_no_kwargs(key):
        """Will fail if additional keywords are injected."""
        return {"sub": "no_kwargs"}

    wrapped_func_no_kwargs = security_handler_factory._get_verify_func(
        apikey_info_no_kwargs, "header", "X-Auth", None
    )
    assert await wrapped_func_no_kwargs(request) == {"sub": "no_kwargs"}

    def apikey_info_request(key, request):
        """Will fail if request is not injected."""
        return {"sub": "request"}

    wrapped_func_request = security_handler_factory._get_verify_func(
        apikey_info_request, "header", "X-Auth", None
    )
    assert await wrapped_func_request(request) == {"sub": "request"}

    def apikey_info_scopes(key, required_scopes):
        """Will fail if required_scopes is not injected."""
        return {"sub": "scopes"}

    wrapped_func_scopes = security_handler_factory._get_verify_func(
        apikey_info_scopes, "header", "X-Auth", None
    )
    assert await wrapped_func_scopes(request) == {"sub": "scopes"}

    def apikey_info_kwargs(key, **kwargs):
        """Will fail if request and required_scopes are not injected."""
        assert "request" in kwargs
        assert "required_scopes" in kwargs
        return {"sub": "kwargs"}

    wrapped_func_kwargs = security_handler_factory._get_verify_func(
        apikey_info_kwargs, "header", "X-Auth", None
    )
    assert await wrapped_func_kwargs(request) == {"sub": "kwargs"}


def _apikey_schemes(token_info_key1, token_info_key2):
    """Build two apikey verify funcs, keyed by scheme name, returning the provided
    token infos for valid keys."""

    def apikey1_info(apikey, required_scopes=None):
        if apikey == "foobar":
            return token_info_key1
        return None

    def apikey2_info(apikey, required_scopes=None):
        if apikey == "bar":
            return token_info_key2
        return None

    apikey_security_handler = ApiKeySecurityHandler()
    return {
        "key1": apikey_security_handler._get_verify_func(
            apikey1_info, "header", "X-Auth-1", []
        ),
        "key2": apikey_security_handler._get_verify_func(
            apikey2_info, "header", "X-Auth-2", []
        ),
    }


def _both_keys_request():
    """Request with valid credentials for both apikey schemes."""
    return ConnexionRequest(
        scope={
            "type": "http",
            "headers": [[b"x-auth-1", b"foobar"], [b"x-auth-2", b"bar"]],
        }
    )


def test_security_handler_factory_default_identity_aggregator():
    """The identity aggregator is optional and defaults to None."""
    assert SecurityHandlerFactory().identity_aggregator is None


async def test_multiple_schemes_identity_aggregator():
    """The identity aggregator receives the verification results keyed by scheme
    name and its return value becomes the token_info."""
    received = {}

    def aggregator(token_infos):
        received.update(token_infos)
        subs = {token_info["sub"] for token_info in token_infos.values()}
        if len(subs) != 1:
            raise OAuthProblem(detail="Schemes do not belong to the same subject")
        return {"sub": subs.pop()}

    security_handler_factory = SecurityHandlerFactory(identity_aggregator=aggregator)
    schemes = _apikey_schemes({"sub": "foo"}, {"sub": "foo"})
    wrapped_func = security_handler_factory.verify_multiple_schemes(schemes)

    assert await wrapped_func(_both_keys_request()) == {"sub": "foo"}
    assert received == {"key1": {"sub": "foo"}, "key2": {"sub": "foo"}}


async def test_multiple_schemes_identity_aggregator_async():
    """The identity aggregator can be a coroutine function, and can be passed
    directly to verify_multiple_schemes."""

    async def aggregator(token_infos):
        return {"sub": token_infos["key1"]["sub"]}

    security_handler_factory = SecurityHandlerFactory()
    schemes = _apikey_schemes({"sub": "foo"}, {"sub": "foo"})
    wrapped_func = security_handler_factory.verify_multiple_schemes(
        schemes, identity_aggregator=aggregator
    )

    assert await wrapped_func(_both_keys_request()) == {"sub": "foo"}


async def test_multiple_schemes_identity_aggregator_subject_mismatch():
    """A request for which both schemes verify successfully, but resolve to
    different subjects, is rejected by the aggregator."""

    def aggregator(token_infos):
        subs = {token_info["sub"] for token_info in token_infos.values()}
        if len(subs) != 1:
            raise OAuthProblem(detail="Schemes do not belong to the same subject")
        return {"sub": subs.pop()}

    security_handler_factory = SecurityHandlerFactory(identity_aggregator=aggregator)
    schemes = _apikey_schemes({"sub": "foo"}, {"sub": "bar"})

    # Both schemes verify successfully on their own
    request = _both_keys_request()
    assert await schemes["key1"](request) == {"sub": "foo"}
    assert await schemes["key2"](request) == {"sub": "bar"}

    # But the AND combination is rejected
    verify_func = security_handler_factory.verify_security(
        [security_handler_factory.verify_multiple_schemes(schemes)]
    )
    with pytest.raises(OAuthProblem) as exc_info:
        await verify_func(request)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Schemes do not belong to the same subject"
    # The rejected request does not pollute the context
    assert "user" not in request.context
    assert "token_info" not in request.context


async def test_multiple_schemes_identity_aggregator_returns_none():
    """An identity aggregator returning None rejects the request."""
    security_handler_factory = SecurityHandlerFactory(
        identity_aggregator=lambda token_infos: None
    )
    schemes = _apikey_schemes({"sub": "foo"}, {"sub": "foo"})
    wrapped_func = security_handler_factory.verify_multiple_schemes(schemes)

    with pytest.raises(OAuthResponseProblem) as exc_info:
        await wrapped_func(_both_keys_request())

    assert exc_info.value.status_code == 401


async def test_multiple_schemes_identity_aggregator_anonymous_scheme():
    """If a scheme in the AND combination does not find credentials, the
    combination evaluates to NO_VALUE and the aggregator is not called."""
    aggregator = MagicMock(return_value={"sub": "foo"})
    security_handler_factory = SecurityHandlerFactory(identity_aggregator=aggregator)
    schemes = _apikey_schemes({"sub": "foo"}, {"sub": "foo"})
    wrapped_func = security_handler_factory.verify_multiple_schemes(schemes)

    request = ConnexionRequest(
        scope={"type": "http", "headers": [[b"x-auth-1", b"foobar"]]}
    )
    assert await wrapped_func(request) is NO_VALUE
    aggregator.assert_not_called()


async def test_multiple_schemes_identity_aggregator_cancelled():
    """Cancellation of the identity aggregator is not swallowed."""

    async def aggregator(token_infos):
        raise asyncio.CancelledError

    security_handler_factory = SecurityHandlerFactory(identity_aggregator=aggregator)
    schemes = _apikey_schemes({"sub": "foo"}, {"sub": "foo"})
    wrapped_func = security_handler_factory.verify_multiple_schemes(schemes)

    with pytest.raises(asyncio.CancelledError):
        await wrapped_func(_both_keys_request())


async def test_verify_security_only_successful_branch_commits_context():
    """In an OR combination, only the token_info of the branch that actually
    succeeds is committed to the context."""

    async def failing_branch(request):
        raise OAuthProblem(detail="Provided credentials are not valid")

    async def successful_branch(request):
        return {"sub": "foo"}

    security_handler_factory = SecurityHandlerFactory()
    verify_func = security_handler_factory.verify_security(
        [failing_branch, successful_branch]
    )

    request = ConnexionRequest(scope={"type": "http", "headers": []})
    await verify_func(request)

    assert request.context["user"] == "foo"
    assert request.context["token_info"] == {"sub": "foo"}


async def test_verify_security_all_branches_fail_context_untouched():
    """If all branches of an OR combination fail, the context is not updated."""

    async def failing_branch(request):
        raise OAuthProblem(detail="Provided credentials are not valid")

    security_handler_factory = SecurityHandlerFactory()
    verify_func = security_handler_factory.verify_security(
        [failing_branch, failing_branch]
    )

    request = ConnexionRequest(scope={"type": "http", "headers": []})
    with pytest.raises(OAuthProblem):
        await verify_func(request)

    assert "user" not in request.context
    assert "token_info" not in request.context


async def test_verify_security_anonymous_branch():
    """An anonymous branch (empty security requirement) yields an empty
    token_info."""
    security_handler_factory = SecurityHandlerFactory()
    verify_func = security_handler_factory.verify_security(
        [security_handler_factory.verify_none]
    )

    request = ConnexionRequest(scope={"type": "http", "headers": []})
    await verify_func(request)

    assert request.context["user"] is None
    assert request.context["token_info"] == {}


async def test_verify_security_cancelled_branch_propagates():
    """Cancellation of a branch is not treated as a failed branch in the OR
    combination, but propagates instead."""

    async def cancelled_branch(request):
        raise asyncio.CancelledError

    async def successful_branch(request):
        return {"sub": "foo"}

    security_handler_factory = SecurityHandlerFactory()
    verify_func = security_handler_factory.verify_security(
        [cancelled_branch, successful_branch]
    )

    request = ConnexionRequest(scope={"type": "http", "headers": []})
    with pytest.raises(asyncio.CancelledError):
        await verify_func(request)
