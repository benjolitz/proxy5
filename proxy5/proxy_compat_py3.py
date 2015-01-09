import socket
from email import message_from_bytes as read_headers
try:
    import cStringIO as _StringIO
except ImportError:
    import io as _StringIO


def insert_header(message, key, value):
    message.add_header(key.decode('utf8'), value.decode('utf8'))


def connect(self, connection, host, port, use_ssl):
    connection.url = host
    try:
        yield from self._connect(
            connection, host, port, use_ssl)
    except socket.error:
        self.proxy_logger.exception("Unable to contact {0}".format(host))
        connection.proxy_connection.eof_received()
