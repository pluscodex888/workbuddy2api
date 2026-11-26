"""Normalize gateway requests to the upstream SSE contract.

The public API can remain non-streaming: :mod:`wb_proxy` buffers the upstream
SSE frames and returns one JSON object to that caller.  The WorkBuddy upstream
is more reliable when every chat request uses its streaming contract, so this
module keeps that transport decision in one small, testable boundary.
"""


def prepare_body(body):
    """Return a shallow request copy that always asks upstream for SSE."""
    prepared = dict(body or {})
    prepared["stream"] = True
    options = prepared.get("stream_options")
    options = dict(options) if isinstance(options, dict) else {}
    options["include_usage"] = True
    prepared["stream_options"] = options
    return prepared


def prepare_request(body, headers=None):
    """Prepare the body and headers for one upstream streaming request.

    The caller's header mapping is never mutated.  ``Accept`` is explicit so
    gateways that negotiate the response format from headers cannot route a
    client non-stream request through their JSON/rate-limit path by mistake.
    """
    prepared = prepare_body(body)
    outgoing = dict(headers or {})
    outgoing["Accept"] = "text/event-stream"
    outgoing["Cache-Control"] = "no-cache"
    return prepared, outgoing
