"""Constellate's LEDDriver contract, run against cambium's HTTP endpoint.

The adapter below is shaped exactly like Constellate's http_generic driver
pointed at a real daemon (--driver http --http-light-url
"http://host:8600/constellate/light?led={led}"), but drives the in-process
test client. Passing the vendored contract check means `constellate serve`
can sweep a cambium-fronted fleet with zero Constellate code changes.
"""

from aiohttp.test_utils import TestClient, TestServer

from cambium.config import CambiumConfig
from cambium.daemon import Daemon
from cambium.fakefleet.runner import FakeFleet, synthetic_fixtures
from cambium.roster import Roster
from cambium.transport.loopback import LoopbackTransport

from .vendor.driver_contract import check_driver_contract


class HttpSweepDriver:
    """LEDDriver-shaped adapter over cambium's /constellate endpoints."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    async def light(self, n: int) -> None:
        resp = await self._client.get(f"/constellate/light?led={n}")
        resp.raise_for_status()

    async def all_off(self) -> None:
        resp = await self._client.get("/constellate/off")
        resp.raise_for_status()

    async def close(self) -> None:
        pass  # the TestClient owns the connection; nothing to release


async def test_cambium_endpoint_satisfies_constellate_driver_contract():
    fixtures = synthetic_fixtures(4)
    transport = LoopbackTransport()
    fleet = FakeFleet(fixtures)
    daemon = Daemon(CambiumConfig(), transport, Roster(list(fixtures)))
    await daemon.start(serve_http=False)
    client = TestClient(TestServer(daemon.app))
    await client.start_server()
    await fleet.start(transport.peer)
    try:
        await check_driver_contract(HttpSweepDriver(client), leds=(0, 1, 2))
        # The physical half Constellate cannot check: light(n) leaves exactly
        # one virtual fixture lit.
        await client.get("/constellate/light?led=2")
        lit = [
            mac for mac, vf in fleet.fixtures.items() if vf.identify is not None
        ]
        assert lit == [fixtures[2].mac]
    finally:
        await fleet.stop()
        await client.close()
        await daemon.stop()
