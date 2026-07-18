"""Per-platform extraction providers.

Each subpackage rips one platform into RIPart's common ``result`` shape:

- :mod:`ripart.providers.janitor` — JanitorAI (browser-driven).
- :mod:`ripart.providers.saucepan` — Saucepan (REST API).
- :mod:`ripart.providers.clank` — clank.world (tRPC + echo proxy).
- :mod:`ripart.providers.spicychat` — spicychat.ai (REST API, guest or login).
- :mod:`ripart.providers.chub` — chub.ai / CharacterHub (open archive, REST).
- :mod:`ripart.providers.tavern` — generic Tavern card-file ripper (any card URL).

The last two are *open* sites: nothing is gated, so extraction is a plain read
of a publicly served card, funnelled through :mod:`ripart.common.tavern`.
"""
