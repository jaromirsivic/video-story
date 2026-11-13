import math

import pytest

from video_story.host import cli, mcp_server, waiting
from video_story.host.errors import HostError


@pytest.mark.parametrize("value", [-1, math.nan, math.inf, -math.inf, "x", None])
def test_invalid_timeouts_are_explicit(value):
    with pytest.raises(HostError) as exc:
        waiting.timeout_seconds(value)
    assert exc.value.code == "INVALID_TIMEOUT"
    assert mcp_server.tool_wait("any", value)["code"] == "INVALID_TIMEOUT"


def test_defaults_and_small_timeouts():
    assert cli.build_parser().parse_args(["wait"]).timeout == 45
    assert waiting.timeout_seconds(0) == 0
    assert mcp_server.clamp_timeout(0.001) == 0.001
    with pytest.raises(HostError):
        mcp_server.clamp_timeout(0)
