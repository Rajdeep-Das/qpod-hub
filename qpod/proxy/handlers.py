# Authenticated HTTP proxy, Some original inspiration from https://github.com/senko/tornado-proxy

import os
import socket
import inspect

from aiohttp import ClientSession, ClientConnectionError
from asyncio import Lock
from simpervisor import SupervisedProcess
from tornado import httpclient, httputil, ioloop, web, websocket, version_info
from urllib.parse import urlunparse, urlparse

from .utils import url_path_join, utcnow
from .. import RequestHandler


class PingableWSClientConnection(websocket.WebSocketClientConnection):
    """A WebSocketClientConnection with an on_ping callback."""
    def __init__(self, **kwargs):
        if 'on_ping_callback' in kwargs:
            self._on_ping_callback = kwargs['on_ping_callback']
            del (kwargs['on_ping_callback'])
        super().__init__(**kwargs)

    def on_ping(self, data):
        if self._on_ping_callback:
            self._on_ping_callback(data)


def pingable_ws_connect(request=None, on_message_callback=None, on_ping_callback=None):
    """
    A variation on websocket_connect that returns a PingableWSClientConnection
    with on_ping_callback.
    """
    # Copy and convert the headers dict/object (see comments in AsyncHTTPClient.fetch)
    request.headers = httputil.HTTPHeaders(request.headers)
    request = httpclient._RequestProxy(request, httpclient.HTTPRequest._DEFAULTS)

    if version_info[0] == 4:  # for tornado 4.5.x compatibility
        conn = PingableWSClientConnection(
            request=request,
            on_message_callback=on_message_callback,
            on_ping_callback=on_ping_callback,
            io_loop=ioloop.IOLoop.current(),
        )
    else:
        conn = PingableWSClientConnection(
            request=request,
            on_message_callback=on_message_callback,
            on_ping_callback=on_ping_callback,
            max_message_size=getattr(websocket, '_default_max_message_size', 10 * 1024 * 1024)
        )

    return conn.connect_future


# https://stackoverflow.com/questions/38663666/how-can-i-serve-a-http-page-and-a-websocket-on-the-same-url-in-tornado
class WebSocketHandlerMixin(websocket.WebSocketHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # calling the super() constructor since the parent doesn't keep
        bases = inspect.getmro(type(self))
        assert WebSocketHandlerMixin in bases
        meindex = bases.index(WebSocketHandlerMixin)
        try:
            nextparent = bases[meindex + 1]
        except IndexError:
            raise Exception("WebSocketHandlerMixin should be followed by another parent to make sense")

        # un-disallow methods --- t.ws.WebSocketHandler disallows methods, re-enable these methods
        def wrapper(method):
            def un_disallow(*args2, **kwargs2):
                getattr(nextparent, method)(self, *args2, **kwargs2)

            return un_disallow

        for method in ["write", "redirect", "set_header", "set_cookie",
                       "set_status", "flush", "finish"]:
            setattr(self, method, wrapper(method))
        nextparent.__init__(self, *args, **kwargs)

    async def get(self, *args, **kwargs):
        if self.request.headers.get("Upgrade", "").lower() != 'websocket':
            return await self.http_get(*args, **kwargs)
        # super get is not async
        super().get(*args, **kwargs)


class ServersInfoHandler(RequestHandler):
    def initialize(self, server_processes):
        self.server_processes = server_processes

    @web.authenticated
    async def get(self):
        data = []
        # Pick out and send only metadata
        # Don't send anything that might be a callable, or leak sensitive info
        for sp in self.server_processes:
            # Manually recurse to convert namedtuples into JSONable structures
            data.append({
                'name': sp.get('name', 'Unknown')
            })

        self.write({'server_processes': data})


class AddSlashHandler(RequestHandler):
    """Add trailing slash to URLs that need them."""

    @web.authenticated
    def get(self, *args):
        src = urlparse(self.request.uri)
        dest = src._replace(path=src.path + '/')
        self.redirect(urlunparse(dest))


class LocalProxyHandler(WebSocketHandlerMixin, RequestHandler):
    def __init__(self, *args, **kwargs):
        self.proxy_base = ''
        self.rewrite = kwargs.pop('rewrite', '/')
        super().__init__(*args, **kwargs)

    async def open(self, port, proxied_path=''):
        """
        Called when a client opens a websocket connection.
        We establish a websocket connection to the proxied backend &
        set up a callback to relay messages through.
        """
        if not proxied_path.startswith('/'):
            proxied_path = '/' + proxied_path

        client_uri = '{uri}:{port}{path}'.format(
            uri='ws://127.0.0.1',
            port=port,
            path=proxied_path
        )
        if self.request.query:
            client_uri += '?' + self.request.query
        headers = self.request.headers

        def message_cb(message):
            """
            Callback when the backend sends messages to us
            We just pass it back to the frontend
            """
            # Websockets support both string (utf-8) and binary data, so let's
            # make sure we signal that appropriately when proxying
            self._record_activity()
            if message is None:
                self.close()
            else:
                self.write_message(message, binary=isinstance(message, bytes))

        def ping_cb(data):
            """
            Callback when the backend sends pings to us.
            We just pass it back to the frontend.
            """
            self._record_activity()
            self.ping(data)

        async def start_websocket_connection():
            self.log.info('Trying to establish websocket connection to {}'.format(client_uri))
            self._record_activity()
            request = httpclient.HTTPRequest(url=client_uri, headers=headers)
            self.ws = await pingable_ws_connect(request=request,
                                                on_message_callback=message_cb, on_ping_callback=ping_cb)
            self._record_activity()
            self.log.info('Websocket connection established to {}'.format(client_uri))

        ioloop.IOLoop.current().add_callback(start_websocket_connection)

    def on_message(self, message):
        """
        Called when we receive a message from our client. We proxy it to the backend.
        """
        self._record_activity()
        if hasattr(self, 'ws'):
            self.ws.write_message(message, binary=isinstance(message, bytes))

    def on_ping(self, data):
        """
        Called when the client pings our websocket connection. We proxy it to the backend.
        """
        self.log.debug('proxy: on_ping: {}'.format(data))
        self._record_activity()
        if hasattr(self, 'ws'):
            self.ws.protocol.write_ping(data)

    def on_pong(self, data):
        """
        Called when we receive a ping back.
        """
        self.log.debug('proxy: on_pong: {}'.format(data))

    def on_close(self):
        """
        Called when the client closes our websocket connection. We close our connection to the backend too.
        """
        if hasattr(self, 'ws'):
            self.ws.close()

    def _record_activity(self):
        """Record proxied activity as API activity avoids proxied traffic being ignored by the proxy internal idle-shutdown mechanism"""
        self.settings['api_last_activity'] = utcnow()

    def _get_context_path(self, port):
        """
        Some applications need to know where they are being proxied from.
        This is either:
        - {base_url}/proxy/{port}
        - {base_url}/{proxy_base}
        """
        if self.proxy_base:
            return url_path_join(self.base_url, self.proxy_base)
        if self.rewrite in ('/', ''):
            return url_path_join(self.base_url, 'proxy', str(port))

        raise ValueError('Unsupported rewrite: "{}"'.format(self.rewrite))

    def _build_proxy_request(self, port, proxied_path, body):
        context_path = self._get_context_path(port)
        if self.rewrite:  # non-empty string, '/' by default
            client_path = proxied_path
        else:  # empty string, absolute path
            client_path = url_path_join(context_path, proxied_path)

        client_uri = '{uri}:{port}{path}'.format(
            uri='http://localhost',
            port=port,
            path=client_path
        )
        if self.request.query:
            client_uri += '?' + self.request.query

        headers = self.proxy_request_headers()

        # Some applications check X-Forwarded-Context and X-ProxyContextPath
        # headers to see if and where they are being proxied from.
        if self.rewrite:
            headers['X-Forwarded-Context'] = context_path
            headers['X-ProxyContextPath'] = context_path

        req = httpclient.HTTPRequest(
            client_uri, method=self.request.method, body=body,
            headers=headers, **self.proxy_request_options())
        return req

    @web.authenticated
    async def proxy(self, port, proxied_path):
        """
        This serverextension handles:
          {base_url}/proxy/{port([0-9]+)}/{proxied_path}
          {base_url}/{proxy_base}/{proxied_path}
        """

        if 'Proxy-Connection' in self.request.headers:
            del self.request.headers['Proxy-Connection']

        self._record_activity()

        if self.request.headers.get("Upgrade", "").lower() == 'websocket':
            # We wanna websocket!
            self.log.info("we wanna websocket, but we don't define WebSocketProxyHandler")
            self.set_status(500)

        body = self.request.body
        if not body:
            if self.request.method == 'POST':
                body = b''
            else:
                body = None

        client = httpclient.AsyncHTTPClient()
        req = self._build_proxy_request(port, proxied_path, body)        
        response = await client.fetch(req, raise_error=False)
        # record activity at start and end of requests
        self._record_activity()

        # For all non http errors...
        if response.error and type(response.error) is not httpclient.HTTPError:
            self.set_status(500)
            self.write(str(response.error))
        else:
            self.set_status(response.code, response.reason)

            # clear tornado default header
            self._headers = httputil.HTTPHeaders()

            for header, v in response.headers.get_all():
                if header not in ('Content-Length', 'Transfer-Encoding',
                                  'Content-Encoding', 'Connection'):
                    # some header appear multiple times, eg 'Set-Cookie'
                    self.add_header(header, v)

            if response.body:
                self.write(response.body)

    def proxy_request_headers(self):
        """A dictionary of headers to be used when constructing
        a tornado.httpclient.HTTPRequest instance for the proxy request."""
        return self.request.headers.copy()

    def proxy_request_options(self):
        """A dictionary of options to be used when constructing
        a tornado.httpclient.HTTPRequest instance for the proxy request."""
        return dict(follow_redirects=False)

    # Support all the methods that torando does by default except for GET which
    # is passed to WebSocketHandlerMixin and then to WebSocketHandler.

    async def http_get(self, port, proxy_path=''):
        """Our non-websocket GET."""
        return await self.proxy(port, proxy_path)

    def post(self, port, proxy_path=''):
        return self.proxy(port, proxy_path)

    def put(self, port, proxy_path=''):
        return self.proxy(port, proxy_path)

    def delete(self, port, proxy_path=''):
        return self.proxy(port, proxy_path)

    def head(self, port, proxy_path=''):
        return self.proxy(port, proxy_path)

    def patch(self, port, proxy_path=''):
        return self.proxy(port, proxy_path)

    def options(self, port, proxy_path=''):
        return self.proxy(port, proxy_path)

    def check_xsrf_cookie(self):
        """
        http://www.tornadoweb.org/en/stable/guide/security.html
        Defer to proxied apps.
        """
        pass

    def select_subprotocol(self, subprotocols):
        """Select a single Sec-WebSocket-Protocol during handshake."""
        if isinstance(subprotocols, list) and subprotocols:
            self.log.info('Client sent subprotocols: {}'.format(subprotocols))
            return subprotocols[0]
        return super().select_subprotocol(subprotocols)


class SuperviseAndProxyHandler(LocalProxyHandler):
    """Manage a given process and requests to it """

    def initialize(self, state):
        self.state = state
        if 'proc_lock' not in state:
            state['proc_lock'] = Lock()

    name = 'process'

    @property
    def port(self):
        """
        Allocate a random empty port for use by application
        """
        if 'port' not in self.state:
            sock = socket.socket()
            sock.bind(('', 0))
            self.state['port'] = sock.getsockname()[1]
            sock.close()
        return self.state['port']

    def get_cwd(self):
        """Get the current working directory for our process
        Override in subclass to launch the process in a directory
        other than the current.
        """
        return os.getcwd()

    def get_env(self):
        """Set up extra environment variables for process. Typically overridden in subclasses."""
        return {}

    def get_timeout(self):
        """Return timeout (in s) to wait before giving up on process readiness"""
        return 5

    async def _http_ready_func(self, p):
        url = 'http://localhost:{}'.format(self.port)
        async with ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    self.log.debug('Got code {} back from {}'.format(resp.status, url))
                    return True
            except ClientConnectionError:
                self.log.debug('Connection to {} refused'.format(url))
                return False

    async def ensure_process(self):
        """ Start the process """
        # We don't want multiple requests trying to start the process at the same time
        # FIXME: Make sure this times out properly?
        # Invariant here should be: when lock isn't being held, either 'proc' is in state & running, or not.
        with (await self.state['proc_lock']):
            if 'proc' not in self.state:
                # FIXME: Prevent races here
                # FIXME: Handle graceful exits of spawned processes here
                cmd = self.get_cmd()
                server_env = os.environ.copy()

                # Set up extra environment variables for process
                server_env.update(self.get_env())

                timeout = self.get_timeout()

                proc = SupervisedProcess(self.name, *cmd, env=server_env, ready_func=self._http_ready_func,
                                         ready_timeout=timeout, log=self.log)
                self.state['proc'] = proc

                try:
                    await proc.start()

                    is_ready = await proc.ready()

                    if not is_ready:
                        await proc.kill()
                        raise web.HTTPError(500, 'could not start {} in time'.format(self.name))
                except:
                    # Make sure we remove proc from state in any error condition
                    del self.state['proc']
                    raise

    @web.authenticated
    async def proxy(self, port, path):
        if not path.startswith('/'):
            path = '/' + path

        await self.ensure_process()

        return await super().proxy(self.port, path)

    async def http_get(self, path):
        return await self.proxy(self.port, path)

    async def open(self, path):
        await self.ensure_process()
        return await super().open(self.port, path)

    def post(self, path):
        return self.proxy(self.port, path)

    def put(self, path):
        return self.proxy(self.port, path)

    def delete(self, path):
        return self.proxy(self.port, path)

    def head(self, path):
        return self.proxy(self.port, path)

    def patch(self, path):
        return self.proxy(self.port, path)

    def options(self, path):
        return self.proxy(self.port, path)
