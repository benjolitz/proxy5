import socket
import sys
try:
    import asyncio
except ImportError:
    if sys.version_info.major > 2:
        raise ImportError("Requires asyncio for Python 3.")
    try:
        import trollius
    except ImportError:
        raise ImportError("Requires trollius for Python 2.")
    sys.modules['asyncio'] = trollius
    import asyncio
    import proxy_compat_py2 as compat
else:
    import proxy_compat_py3 as compat

import logging
import errno
import time
_StringIO = compat._StringIO
import ssl
import utils
import multiprocessing as parallel
import threading

# Proxy Connection messages
UNABLE_TO_FIGURE_HOST = \
    "Unable to locate a Host header or path. Closing!\nPath: {0!r}, headers: {1!r}"
REQUEST_LINE_HAS_NO_NETLOC = \
    "Unable to determine host from original path. Looking for Host header!"
CONTACTING_REMOTE = "[{0}] Contacting remote host {1}"

# HTTPClient
REMOTE_UPGRADING_PROXY_CONNECTION_TO_SSL = "Wrapping SSL socket for {0}"
PROXY_CONNECTION_FAILED_SSL_HANDSHAKE = "Failed to handshake {0}"
PROXY_CONNECTION_SSL_TIME = "Took {0} to handshake"
HTTP_CLIENT_READ_REQUEST = \
    "[{0}] Read response line {1}. Sending data frag (up to 50) {2}"
HTTP_CLIENT_READ_BODY_EMPTY = "[http_client.read_body] No more data incoming."
# Connection base messages
CONNECTION_STATE_CHANGE = "Setting state to {0}"
READ_REQUEST_MESSAGE = \
    "[{0}] Split out first line: {1!r}. We have {2} chars past that."
READ_HEADERS_MESSAGE = "[{0}] Parsing headers"
READ_BODY_MESSAGE = "[{0}] Reading body length {1}"
WAIT_FOR_CLIENT_CHANGE = "[{0}] Using {1}"
ON_STATE_CHANGE = "[{1}] Caught {0}"
STATE_CHANGE_ERROR = "[{0}] Unhandled exception"

# Connection close messages
ASKED_CONNECTION_CLOSE = "{0} is asked to close!"
CONNECTION_CLOSE_NO_TRANSPORT = "Cannot close -- no transport"
CONNECTION_CLOSE_ALREADY_CLOSED = "[{0}] already closed"
CONNECTION_CLOSE_SENDING_CONTENT = "[{0}] has stuff to send"
CONNECTION_CLOSE_SUCCESSFUL = "{0} is closed"

#
STATE_ERROR_INCOMPLETE_REQUEST_LINE = "Incomplete request line"
STATE_ERROR_INCOMPLETE_HEADERS = "Incomplete headers"
STATE_ERROR_WAITING_FOR_CHANGE = "Waiting for HTTP Client"


class StateError(ValueError):
    pass


class Proxy(object):
    def __init__(self, logger, port=0):
        self.proxy_connection_class = ProxyConnection
        self.proxy_logger = logging.getLogger(logger.name + '.ProxyConnection')
        self.http_client_logger = \
            logging.getLogger(logger.name + '.HTTPClient')
        self.certificate_authority = utils.CertificateAuthority()
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(
            socket.SOL_SOCKET, socket.SO_REUSEADDR, True)
        self.server_socket.bind(('127.0.0.1', port))
        self.port = self.server_socket.getsockname()[1]

    def start(self, loop=None):
        self.loop = asyncio.get_event_loop()
        server = self.loop.create_server(
            lambda: self.proxy_connection_class(
                self,
                self.proxy_logger, self.http_client_logger),
            sock=self.server_socket)
        asyncio.async(
            server)
        self.loop.run_forever()

    @asyncio.coroutine
    def _connect(self, connection, host, port, use_ssl):
        return self.loop.create_connection(
            (lambda: connection), host, port, ssl=use_ssl)

    connect = asyncio.coroutine(compat.connect)

    def close_connection(self, connection):
        if not connection:
            return
        connection.logger.debug(
            ASKED_CONNECTION_CLOSE.format(connection.url))
        if not connection.transport:
            connection.logger.warning(CONNECTION_CLOSE_NO_TRANSPORT)
            return

        if connection.transport._closing:
            connection.logger.debug(
                CONNECTION_CLOSE_ALREADY_CLOSED.format(
                    connection.__class__.__name__))
            return
        if connection.transport._buffer:
            connection.logger.debug(
                CONNECTION_CLOSE_SENDING_CONTENT.format(
                    connection.__class__.__name__))
            self.loop.call_later(
                1.0,
                self.close_connection, connection)
            return
        # Wrapping an unencrypted _sock into an SSL socket
        # can provoke odd errors. On CPython, it won't close
        # the connection via transport.close(). On PyPy, transport.close()
        # works wonderfully.
        # So ignore a EBADF -- it's not a threat.
        # Also ignore ECONNABORTED, as we might terminate
        # a connection early.
        if isinstance(connection.transport._sock, ssl.SSLSocket):
            try:
                connection.transport._sock.shutdown(socket.SHUT_RDWR)
                connection.transport._sock.close()
            except socket.error as e:
                if e.errno not in (errno.ECONNABORTED, errno.ENOTCONN):
                    raise
        try:
            connection.transport.close()
        except IOError as e:
            if e.errno != errno.EBADF:
                raise
        connection.logger.debug(
            CONNECTION_CLOSE_SUCCESSFUL.format(connection.url))


class ProxyProcess(parallel.Process, Proxy):
    def __init__(self, logger, port):
        self.is_running = parallel.Event()
        Proxy.__init__(self, logger, port)
        parallel.Process.__init__(self, name=logger.name)

    def run(self):
        self.is_running.set()

        def setup():
            'Ensure the event loop is active in this thread'
            asyncio.set_event_loop(asyncio.unix_events.SelectorEventLoop())
            Proxy.start(self)
        t = threading.Thread(target=setup)
        t.start()
        while self.is_running.is_set():
            time.sleep(0.1)
        self.loop.stop()

    def stop(self):
        self.is_running.clear()


class ConnectionLogger(object):
    def _on_open(self):
        '''
        Called on a new client
        '''
        pass

    def _on_headers_read(self):
        '''
        Called when headers are parsed
        '''
        pass

    def _on_method_read(self):
        pass

    def _on_contact_remote(self):
        pass

    def _on_response_received(self):
        pass

    def _on_close(self):
        '''
        Called when the connection is closed.
        '''
        pass


class Connection(asyncio.Protocol, ConnectionLogger):
    def __init__(self, logger, proxy):
        self.transport = None
        self.logger = logger
        self.proxy = proxy
        self._close_sentinel = None
        self.data = []
        self.next_state = self.read_request
        self.client_headers = self.request_line = None
        self.send_queue = []
        self.http_client = None
        self.url = None
        self.original_request_path = None

    @property
    def next_state(self):
        return self._next_state

    @next_state.setter
    def next_state(self, val):
        self.logger.debug(CONNECTION_STATE_CHANGE.format(
            val.__name__))
        self._next_state = val

    def read_request(self, datas):
        datas = b''.join(datas)
        if b'\r\n' in datas:
            pivot = datas.index(b'\r\n')+2
            data, rest = datas[:pivot], datas[pivot:]
            self.data = [rest]
            # This proxy does not support having multiple requests on
            # the same socket due to the primitive state machine architecture.
            # So force HTTP/1.0
            self.request_line, self.original_request_path = \
                utils.safe_split_http_request_line(
                    data[:-2], force_version=b'HTTP/1.0', pathify_url=True)

            self.next_state = self.read_headers
            self.logger.debug(
                READ_REQUEST_MESSAGE.format(
                    'read_request', data, len(rest)))
            self._on_method_read()
            # if rest == '\r\n':
            #     self.data.append('\r\n')
            return b' '.join(self.request_line) + b'\r\n'
        raise StateError(STATE_ERROR_INCOMPLETE_REQUEST_LINE)

    def read_headers(self, datas):
        datas = b''.join(datas)
        if b'\r\n\r\n' in datas:
            self.logger.debug(
                READ_HEADERS_MESSAGE.format('read_headers'))
            pivot = datas.index(b'\r\n\r\n')+4
            headers = datas[:pivot]
            self.data = [datas[pivot:]]
            self.client_headers = compat.read_headers(headers)
            self.next_state = self.read_body
            self._on_headers_read()
            return str(self.client_headers).encode('utf8').strip() + \
                b'\r\n\r\n'
        raise StateError(STATE_ERROR_INCOMPLETE_HEADERS)

    def read_body(self, datas):
        try:
            datas = b''.join(datas)
            self.logger.debug(
                READ_BODY_MESSAGE.format(
                    'read_body', len(datas)))
            if datas:
                self.data = []
                return datas
            self.next_state = self.contact_remote
        finally:
            self._on_response_received()

    def contact_remote(self, datas):
        self.next_state = self.wait_for_client_change

    def wait_for_client_change(self, datas):
        self.logger.debug(
            WAIT_FOR_CLIENT_CHANGE.format('wait_for_client_change', datas))
        self.http_client.signal_ready()
        raise StateError(STATE_ERROR_WAITING_FOR_CHANGE)

    def connection_made(self, transport):
        peername = transport.get_extra_info('peername')
        self.url = peername
        self.logger.debug(
            "[{0}] connection from {1}".format('connection_made', peername))
        self.transport = transport
        self._on_open()

    def data_received(self, data):
        self.data.append(data)
        while True:
            try:
                data = self.next_state(self.data)
            except StateError as e:
                self.logger.warning(
                    ON_STATE_CHANGE.format(
                        e, self.next_state.__name__))
                return None
            except Exception as e:
                self.logger.exception(STATE_CHANGE_ERROR.format(
                    self.next_state))
            else:
                if data is not None:
                    self._on_data_received(data)

    def _on_data_received(self, data):
        self.send_queue.append(data)

    def eof_received(self):
        if self.transport._buffer:
            if not self._close_sentinel:
                self._close_sentinel = \
                    self.proxy.loop.call_later(
                        1.0, self.proxy.close_connection, self)
        elif not self._close_sentinel:
            self.proxy.close_connection(self)
            self._close_sentinel = \
                self.proxy.loop.call_later(
                    1.0, self.proxy.close_connection, self.http_client)
        self._on_close()
        return True


class ProxyConnection(Connection):
    def __init__(self, proxy, proxy_logger, http_client_logger):
        super(ProxyConnection, self).__init__(proxy_logger, proxy)
        self.http_client_logger = http_client_logger
        self.http_client_class = HTTPClient

    def contact_remote(self, datas):
        super(ProxyConnection, self).contact_remote(datas)
        if self.http_client:
            return None
        request = utils.urlparse.urlparse(self.original_request_path)
        host = \
            request.netloc
        port = 80
        use_connect = (self.request_line.method.lower() == b'connect')
        if not host:
            self.logger.debug(REQUEST_LINE_HAS_NO_NETLOC)
            if 'Host' not in self.client_headers:
                self.logger.critical(
                    UNABLE_TO_FIGURE_HOST.format(
                        self.original_request_path,
                        self.client_headers))
                self.eof_received()
                return
            host = self.client_headers['Host'].strip()
            if ':' in host:
                host, port = host.split(':', 1)
                port = int(port)

        if request.scheme.endswith(b's') or use_connect:
            port = 443
        if isinstance(host, bytes):
            host = host.decode('utf8')
        self.http_client = self.http_client_class(
            self.proxy,
            host,
            self, use_connect, self.http_client_logger)

        self.logger.debug(
            CONTACTING_REMOTE.format(
                'contact_remote', (host, port,)))
        self._on_contact_remote()

        asyncio.async(
            self.proxy.connect(
                self.http_client, host, port, port == 443))

    def read_headers(self, datas):
        super(ProxyConnection, self).read_headers(datas)
        # Delete troublesome headers
        if b'Connection' in self.client_headers:
            del self.client_headers[b'Connection']
        compat.insert_header(self.client_headers, b'Connection', b'close')
        del self.client_headers[b'Accept-Encoding']
        del self.client_headers[b'Proxy-Connection']

        if b'Host' not in self.client_headers:
            parsed_req_path = \
                utils.urlparse.urlparse(self.original_request_path)
            host = parsed_req_path.netloc
            if not host:
                self.logger.critical(
                    UNABLE_TO_FIGURE_HOST.format(
                        self.original_request_path,
                        self.client_headers))
                self.eof_received()
                return
            port = 80
            if parsed_req_path.scheme and \
                    parsed_req_path.scheme.endswith(b's'):
                port = 443
            if ':' in host:
                host, port = host.split(':', 1)
                port = int(port)
            host = host.decode('utf8')
            compat.insert_header(self.client_headers, b'Host', host)
        return str(self.client_headers).encode('utf8').strip() + b'\r\n\r\n'


class HTTPClient(Connection):
    def __init__(self, proxy, domain, proxy_connection, use_connect, logger):
        self.proxy_connection = proxy_connection
        self.use_connect = use_connect
        self.domain = domain
        super(HTTPClient, self).__init__(logger, proxy)

    def signal_ready(self):
        pass

    def connection_made(self, transport):
        super(HTTPClient, self).connection_made(transport)
        if self.use_connect:
            # Reset the state of the proxy to read more headers
            self.proxy_connection.next_state = \
                self.proxy_connection.read_request
            self.proxy_connection.send_queue[:] = []
            # Signal to our client that we're going to SSL-ize it.
            self.proxy_connection.transport.write(
                self.proxy_connection.request_line.version +
                b' 200 Connection established\r\n\r\n')
            self.proxy_connection.logger.debug(
                REMOTE_UPGRADING_PROXY_CONNECTION_TO_SSL.format(self.url))
            # Upgrade the _sock to SSL
            try:
                self.proxy_connection.transport._sock = ssl.wrap_socket(
                    self.proxy_connection.transport._sock, server_side=True,
                    certfile=self.proxy.certificate_authority[self.domain],
                    do_handshake_on_connect=False, suppress_ragged_eofs=True)
            except (socket.error, OSError) as e:
                if e.errno == errno.EINVAL:
                    self.logger.exception(
                        "Failed to handshake {0}".format(self.domain))
                else:
                    self.logger.exception(
                        "Unhandled fault on {0}".format(self.domain))
                self.eof_received()
            ts = time.time()
            while time.time() - ts < 15:
                try:
                    self.proxy_connection.transport._sock.do_handshake()
                except AttributeError:
                    self.eof_received()
                    return
                except ssl.SSLError as e:
                    if e.errno == ssl.SSL_ERROR_ZERO_RETURN:
                        self.eof_received()
                        return
                    if e.errno not in (ssl.SSL_ERROR_WANT_READ,):
                        self.proxy_connection.logger.exception(
                            PROXY_CONNECTION_FAILED_SSL_HANDSHAKE.format(
                                self.url))
                    time.sleep(0.1)
                else:
                    break
            ts = time.time() - ts
            method = self.logger.debug
            if ts > 4:
                method = self.logger.critical
            method(PROXY_CONNECTION_SSL_TIME.format(ts))
        else:
            self.send_proxy_data_to_remote()
        self.signal_ready = self.send_proxy_data_to_remote

    def send_proxy_data_to_remote(self):
        self.transport.write(
            b''.join(self.proxy_connection.send_queue))
        self.proxy_connection.send_queue[:] = []

    def _on_data_received(self, data):
        self.proxy_connection.transport.write(data)

    def read_request(self, datas):
        datas = b''.join(datas)
        if b'\r\n' in datas:
            pivot = datas.index(b'\r\n')+2
            data, rest = datas[:pivot], datas[pivot:]
            self.data = [rest]
            try:
                self.request_line = \
                    utils.safe_split_http_response_line(data[:-2])
            except Exception:
                self.logger.exception(repr(data[:-2]))
                raise
            self.next_state = self.read_headers
            self.logger.debug(
                HTTP_CLIENT_READ_REQUEST.format(
                    'read_request', self.request_line,
                    repr(data[:50])))
            return data
        raise StateError(STATE_ERROR_INCOMPLETE_REQUEST_LINE)

    def read_body(self, datas):
        if not datas or not any(datas):
            raise StateError(HTTP_CLIENT_READ_BODY_EMPTY)
        result = super(HTTPClient, self).read_body(datas)
        self.next_state = self.read_body
        return result

    def eof_received(self):
        if not self.use_connect:
            super(HTTPClient, self).eof_received()
        if self.proxy_connection and not \
                self.proxy_connection.transport._closing:
            self.proxy.close_connection(self.proxy_connection)
        self.proxy_connection = None
        if not self.use_connect:
            return True
        return False
