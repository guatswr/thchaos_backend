"""THChaos protocol v1 的公开 API。"""

from .envelope import (
    PROTOCOL_VERSION,
    ClientRole,
    Envelope,
    MessageType,
    allowed_message_types,
    make_envelope,
    parse_message,
)
from .errors import ErrorCode, ErrorPayload, ProtocolError
from .payloads import *
from .types import *

__all__ = [
    "PROTOCOL_VERSION",
    "ClientRole",
    "Envelope",
    "MessageType",
    "allowed_message_types",
    "make_envelope",
    "parse_message",
    "ErrorCode",
    "ErrorPayload",
    "ProtocolError",
]
