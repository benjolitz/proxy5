from proxy_base import (ProxyProcess, ProxyConnection, HTTPClient)
import urlparse


class MitmProxy(ProxyProcess):
    def __init__(self, logger, port):
        super(MitmProxy, self).__init__(logger, port)
        self.proxy_connection_class = MitmProxyConnection


class MitmProxyConnection(ProxyConnection):
    def __init__(self, proxy, proxy_logger, http_client_logger):
        super(MitmProxyConnection, self).__init__(
            proxy, proxy_logger, http_client_logger)
        self.http_client_class = MitmHTTPClient


class MitmHTTPClient(HTTPClient):
    def __init__(self, *args, **kwargs):
        super(MitmHTTPClient, self).__init__(*args, **kwargs)
        self.__passthrough = True
        self.__cur_buffer = []

    def _on_data_received(self, data):
        if self.__passthrough:
            super(MitmHTTPClient, self)._on_data_received(data)
            return
        self.__cur_buffer.append(data)

    def _on_headers_read(self):
        url_frag = self.proxy_connection.original_request_path.lower()
        if 'javascript' in \
                (self.client_headers.get('content-type') or '').lower():
            self.__passthrough = False
        elif urlparse.urlparse(url_frag).path.endswith('.js'):
            self.__passthrough = False
            # self.logger.warning(
            #     "Not javascript content type but ends with: {0}".format(
            #         url_frag))

    def eof_received(self):
        if not self.__passthrough:
            data = ''.join(self.__cur_buffer)
            index = data.index('\r\n\r\n')
            data = data[:index+4] + 'debugger;\n' + data[index+4:]
            super(MitmHTTPClient, self)._on_data_received(
                data)
        super(MitmHTTPClient, self).eof_received()
