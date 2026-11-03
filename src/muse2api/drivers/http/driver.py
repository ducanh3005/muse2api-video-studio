"""Direct Noise Gateway RPC / HTTP-over-WebSocket driver.

Communicates with the Muse Hatch VM daemon (`wss://<vm_id>.metaaivm.com/`) over
the `Noise_XX_25519_AESGCM_SHA256` encrypted RPC channel (`chat.stream`,
`chat.history`, `fs.write`, `fs.raw`) without touching or typing into the
Muse web dashboard DOM.
"""

from __future__ import annotations

from ..browser.driver import BrowserDriver


class HttpDriver(BrowserDriver):
    name = "http"

