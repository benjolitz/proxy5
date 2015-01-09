from trollius import From
import socket
from mimetools import Message
try:
    import cStringIO as _StringIO
except ImportError:
    import StringIO as _StringIO


def read_headers(bytes):
    return Message(_StringIO.StringIO(bytes))


def insert_header(message, key, value):
    message.headers.insert(
        -1, b'{0}: {1}\r\n'.format(key, value))


def connect(self, connection, host, port, use_ssl):
    connection.url = host
    try:
        yield From(self._connect(connection, host, port, use_ssl))
    except socket.error:
        self.proxy_logger.exception("Unable to contact {0}".format(host))
        connection.proxy_connection.eof_received()
