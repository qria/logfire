from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from opentelemetry import context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.trace import Status, StatusCode

import logfire

from ..constants import (
    ATTRIBUTES_LOG_LEVEL_NUM_KEY,
    ATTRIBUTES_MESSAGE_KEY,
    ATTRIBUTES_MESSAGE_TEMPLATE_KEY,
    ATTRIBUTES_SPAN_TYPE_KEY,
    LEVEL_NUMBERS,
    PENDING_SPAN_NAME_SUFFIX,
    log_level_attributes,
)
from ..db_statement_summary import message_from_db_statement
from ..scrubbing import BaseScrubber
from ..utils import ReadableSpanDict, is_instrumentation_suppressed, span_to_dict, truncate_string
from .wrapper import WrapperSpanProcessor


class MainSpanProcessorWrapper(WrapperSpanProcessor):
    """Wrapper around other processors to intercept starting and ending spans with our own global logic.

    Suppresses starting/ending if the current context has a `suppress_instrumentation` value.
    Tweaks the send/receive span names generated by the ASGI middleware.
    """

    def __init__(self, processor: SpanProcessor, scrubber: BaseScrubber) -> None:
        super().__init__(processor)
        self.scrubber = scrubber

    def on_start(
        self,
        span: Span,
        parent_context: context.Context | None = None,
    ) -> None:
        if is_instrumentation_suppressed():
            return
        _set_log_level_on_asgi_send_receive_spans(span)
        with logfire.suppress_instrumentation():
            super().on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        if is_instrumentation_suppressed():
            return
        span_dict = span_to_dict(span)
        _tweak_asgi_send_receive_spans(span_dict)
        _tweak_sqlalchemy_connect_spans(span_dict)
        _tweak_http_spans(span_dict)
        _summarize_db_statement(span_dict)
        _set_error_level_and_status(span_dict)
        self.scrubber.scrub_span(span_dict)
        span = ReadableSpan(**span_dict)
        with logfire.suppress_instrumentation():
            super().on_end(span)


def _set_error_level_and_status(span: ReadableSpanDict) -> None:
    """Default the log level to error if the status code is error, and vice versa.

    This makes querying for `level` and `otel_status_code` interchangeable ways to find errors.
    """
    status = span['status']
    attributes = span['attributes']
    if status.status_code == StatusCode.ERROR and ATTRIBUTES_LOG_LEVEL_NUM_KEY not in attributes:
        span['attributes'] = {**attributes, **log_level_attributes('error')}
    elif status.is_unset:
        level = attributes.get(ATTRIBUTES_LOG_LEVEL_NUM_KEY)
        if isinstance(level, int) and level >= LEVEL_NUMBERS['error']:
            span['status'] = Status(status_code=StatusCode.ERROR, description=status.description)


def _set_log_level_on_asgi_send_receive_spans(span: Span) -> None:
    """Set the log level of ASGI send/receive spans to debug.

    If a span doesn't have a level set, it defaults to 'info'. This is too high for ASGI send/receive spans,
    which are generated for every request and are not particularly interesting.
    """
    if _is_asgi_send_receive_span(span.name, span.instrumentation_scope):
        span.set_attributes(log_level_attributes('debug'))


def _tweak_sqlalchemy_connect_spans(span: ReadableSpanDict) -> None:
    # Set the sqlalchemy 'connect' span to debug level so that it's hidden by default.
    # https://pydanticlogfire.slack.com/archives/C06EDRBSAH3/p1720205732316029
    if span['name'] != 'connect':
        return
    scope = span['instrumentation_scope']
    if scope is None or scope.name != 'opentelemetry.instrumentation.sqlalchemy':  # pragma: no cover
        return
    attributes = span['attributes']
    # We never expect db.statement to be in the attributes here.
    # This is just to be extra sure that we're not accidentally hiding an actual query span.
    if SpanAttributes.DB_SYSTEM not in attributes or SpanAttributes.DB_STATEMENT in attributes:  # pragma: no cover
        return
    span['attributes'] = {**attributes, **log_level_attributes('debug')}


def _tweak_asgi_send_receive_spans(span: ReadableSpanDict) -> None:
    """Make the name/message of spans generated by OTEL's ASGI middleware more useful.

    For example, a single request will typically generate two 'send' spans with the same message,
    e.g. 'GET /foo http send'. This function may add part of the ASGI event type to the name to make it more useful,
    so instead it shows e.g. 'http send response.start' and 'http send response.body'.
    """
    name = span['name']
    if _is_asgi_send_receive_span(name, span['instrumentation_scope']):
        attributes = span['attributes']
        # The attribute name should be `asgi.event.type` after this is merged and released:
        # https://github.com/open-telemetry/opentelemetry-python-contrib/pull/2300
        typ = attributes.get('asgi.event.type') or attributes.get('type')
        if not (
            isinstance(typ, str)
            and typ.startswith(('http.', 'websocket.'))
            and attributes.get(ATTRIBUTES_MESSAGE_KEY) == name
        ):  # pragma: no cover
            return

        # Strip the 'http.' or 'websocket.' prefix from the event type and add it to the span name.
        if typ in ('websocket.send', 'websocket.receive'):
            # No point in adding anything in this case, otherwise it'd say e.g. 'websocket send send'.
            # No other event types in https://asgi.readthedocs.io/en/latest/specs/www.html are redundant like this.
            new_name = name
        else:
            span['name'] = new_name = f'{name} {typ.split(".", 1)[1]}'

        span['attributes'] = {**attributes, ATTRIBUTES_MESSAGE_KEY: new_name}


def _is_asgi_send_receive_span(name: str, instrumentation_scope: InstrumentationScope | None) -> bool:
    return (
        instrumentation_scope is not None
        and instrumentation_scope.name
        in (
            'opentelemetry.instrumentation.asgi',
            'opentelemetry.instrumentation.starlette',
            'opentelemetry.instrumentation.fastapi',
        )
    ) and (name.endswith((' http send', ' http receive', ' websocket send', ' websocket receive')))


def _tweak_http_spans(span: ReadableSpanDict):
    """Tweak spans from HTTP instrumentations, particularly the span name and message.

    Also derives `http.target` from `http.url` if needed.

    The span names from OTEL instrumentations are an inconsistent and generally lacking mess.
    This is partly due to not having a concept of 'message' separate from span names.

    This function checks if the current name is some combination of method and route/target, and if so sets:
    - The span name to method + route (low cardinality)
    - The message to method + target (more information)
    In both cases, if only one of method and route/target is available, it just uses that.

    For some spans (e.g. ASGI) this actually removes information (the target) from the span name,
    but leaves it in the message.
    """
    attributes = span['attributes']

    # Check that this generally looks like a span not generated by logfire methods.
    # This is intended for OTEL instrumentations of frameworks like FastAPI, but written to be general.
    if ATTRIBUTES_MESSAGE_TEMPLATE_KEY in attributes:
        return

    name = span['name']
    is_pending = attributes.get(ATTRIBUTES_SPAN_TYPE_KEY) == 'pending_span'
    if is_pending:
        name = name[: -len(PENDING_SPAN_NAME_SUFFIX)]
    if name != attributes.get(ATTRIBUTES_MESSAGE_KEY):  # pragma: no cover
        return

    method = attributes.get(SpanAttributes.HTTP_METHOD)
    route = attributes.get(SpanAttributes.HTTP_ROUTE)
    target = attributes.get(SpanAttributes.HTTP_TARGET)
    url = attributes.get(SpanAttributes.HTTP_URL)
    if not (method or route or target or url):
        return

    if not target and url and isinstance(url, str):
        try:
            target = urlparse(url).path
            span['attributes'] = attributes = {**attributes, SpanAttributes.HTTP_TARGET: target}
        except Exception:  # pragma: no cover
            pass

    # Build up a list of possible span names and messages in order from worst to best
    names: list[str] = []
    messages: list[str] = []
    if method and isinstance(method, str):
        names.append(method)
        messages.append(method)
    if target and isinstance(target, str):  # pragma: no branch
        messages.append(target)
    if route and isinstance(route, str):
        names.append(route)

    # If both method and target/route are present, also use the combination
    for lst in (names, messages):
        if len(lst) == 2:
            lst.append(' '.join(lst))

    # If the name doesn't already consist of method and/or target/route, leave it alone
    if name not in names + messages:
        return

    # For each of name and message, update to the best option, which is the last in the list.
    # Minor optimization: only do this if there's a change.
    if names and (new_name := names[-1]) != name:
        if is_pending:
            new_name += PENDING_SPAN_NAME_SUFFIX
        span['name'] = new_name

    if not messages:  # pragma: no cover
        return

    message = messages[-1]

    # Add query params to the message if:
    # 1. The message currently ends with the target
    # 2. We have a URL to parse query params from
    # 3. Some query params exist
    # 4. The target doesn't already end with the query string
    #       (it's supposed to according to the spec, but the OTEL libraries don't include it)
    if (
        url and target and isinstance(url, str) and isinstance(target, str) and message.endswith(target)
    ):  # pragma: no branch
        query_string = urlparse(url).query
        query_params = parse_qs(query_string)
        if query_params and not target.endswith(query_string):
            pairs = [(k, v) for k, vs in query_params.items() for v in vs]
            # Put shorter query params first so that they'll be visible in the UI even if the whole message isn't.
            pairs.sort(key=lambda pair: (len(pair[0]) + len(pair[1]), pair))
            # Limit keys and values to 20 chars each.
            truncated_pairs = [[truncate_string(s, max_length=20, middle='…') for s in pair] for pair in pairs]
            # Show
            #   /path?foo=1&bar=2%203
            # as:
            #   /path ? foo='1' & bar='2 3'
            # to make things nice and readable.
            # Note that we show decoded values, e.g. %20 -> ' '.
            message += ' ? ' + ' & '.join(f'{k}={v!r}' for k, v in truncated_pairs)

    if message != name:
        span['attributes'] = {**attributes, ATTRIBUTES_MESSAGE_KEY: message}


def _summarize_db_statement(span: ReadableSpanDict):
    attributes = span['attributes']
    message: str | None = attributes.get(ATTRIBUTES_MESSAGE_KEY)  # type: ignore
    summary = message_from_db_statement(attributes, message, span['name'])
    if summary is not None:
        span['attributes'] = {**attributes, ATTRIBUTES_MESSAGE_KEY: summary}
