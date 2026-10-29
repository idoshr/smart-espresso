"""systemd notifications without libsystemd.

``Type=notify`` and ``WatchdogSec`` read datagrams on ``NOTIFY_SOCKET``.
When that variable is unset (a laptop, a test, a foreground run) this is a
no-op.
"""

import os
import socket


def notify_systemd(message: str) -> None:
    """Send one sd_notify datagram, such as ``READY=1`` or ``WATCHDOG=1``."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    # Abstract namespace sockets are passed as ``@name``.
    if address.startswith("@"):
        address = "\0" + address[1:]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.sendto(message.encode("utf-8"), address)
    except OSError as error:
        print(f"systemd notify failed: {error}")
    finally:
        sock.close()
