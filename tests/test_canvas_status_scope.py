"""The authorized Canvas proxy exposes only its own session's ASP identities."""
import json

from canvas.backend.main import _scope_status_frame


def test_stream_filters_events_and_snapshots_without_changing_cursor():
    assert _scope_status_frame(['id: 4', 'event: heartbeat', 'data: {"agentId":"other"}'], {'mine'}) is None
    result = _scope_status_frame(['id: 5', 'event: snapshot', 'data: ' + json.dumps({
        'kind': 'snapshot', 'cursor': 5, 'agents': [{'agentId': 'mine'}, {'agentId': 'other'}],
    })], {'mine'})
    assert result is not None and b'id: 5' in result
    assert json.loads(result.decode().split('data: ')[1])['agents'] == [{'agentId': 'mine'}]
    assert _scope_status_frame([': keepalive'], {'mine'}) == b': keepalive\n\n'
