"""Channels — pluggable transports. One contract (``Channel.emit`` + the control-plane intake/answer
seam); this release ships the web REST API + dashboard. No business logic lives in a channel; it
normalizes inbound and renders outbound events.
"""
