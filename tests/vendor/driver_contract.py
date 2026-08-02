"""Reusable LEDDriver conformance check.

VENDORED from Constellate tests/driver_contract.py
(sha256 c55b3cd1c63ac680f0a124fc0570268c35c4d0c4df3ab585b0e34743814e6827),
with one change: the `isinstance(LEDDriver)` check becomes a structural
check, because cambium's venv does not install Constellate -- the contract
here is the SHAPE Constellate's sweep depends on, not the base class.

Original docstring:
The first thing to run against any new driver (see DRIVERS.md): it exercises
the whole abstract surface -- light() awaitable and callable back-to-back,
all_off(), and an idempotent close(). It cannot check the physical half of
the contract (that the node is actually lit when light() returns) -- that's
what `constellate blink` on real hardware is for.
"""

import inspect


async def check_driver_contract(driver, leds=(0, 1, 2)) -> None:
    # Structural stand-in for isinstance(driver, LEDDriver).
    for method in ("light", "all_off", "close"):
        fn = getattr(driver, method, None)
        assert fn is not None and inspect.iscoroutinefunction(fn), (
            f"driver lacks async {method}() -- not LEDDriver-shaped"
        )
    for n in leds:
        assert await driver.light(n) is None
    await driver.all_off()
    await driver.close()
    await driver.close()      # close must be safe to call twice
