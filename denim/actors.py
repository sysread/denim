"""
TODO:
    * Client needs to handle non-linear messages (e.g. msg from server isn't
      necessarily in response to last request)
    * Manager
    * Logging
"""
from diesel.util.process import ProcessPool
import diesel

from denim.protocol import Msg, Task, ProtocolError


class Client(diesel.Client):
    """
    This is the basic protocol layer used for all client connections.  It makes
    available all commands, regardless of the remote host's support for those
    commands.
    """
    _cmd_err = 'Invalid reply from server: (%d) %r'

    def __init__(self, *args, **kwargs):
        super(Client, self).__init__(*args, **kwargs)

    def __hash__(self):
        """
        This hash mechanism allows clusters to index workers by their
        connection information and prevents multiple active connections to the
        same worker.
        """
        return (self.addr, self.port)

    def _expect(self, msg, *args):
        """
        Helper function that raises a `ProtocolError` if `msg.cmd` is not
        present in `*args`. If `msg.cmd` is `Msg.ERR`, a protocol error is
        raised no matter what.
        """
        cmds = set(args)
        if msg.cmd == Msg.ERR:
            raise msg.payload
        elif msg.cmd in cmds:
            return True
        else:
            raise ProtocolError(self.cmd_err % (msg.cmd, msg.payload))

    def _next_msg(self):
        """
        Reads the next line off the wire and decodes it into a `Msg`.
        """
        return Msg.decode(diesel.until_eol())

    def _send(self, msg):
        """
        Encodes and sends a Msg.
        """
        diesel.send(msg.encode())

    def _cmd(self, cmd, expect_cmd, payload=None):
        """
        Sends a command to the remote host and returns either the reply with
        the expected command value or raises a `ProtocolError`.
        """
        msg = Msg(cmd, payload=payload)
        self._send(msg)
        reply = self._next_msg()
        self._expect(reply, expect_cmd)
        return reply

    @diesel.call
    def queue(self, task):
        """
        Sends a `Task` to the remote host. Returns the `msgid`.
        """
        reply = self._cmd(Msg.QUEUE, Msg.ACK, task)
        return reply.msgid

    @diesel.call
    def wait(self, msgid):
        """
        Cedes thread control until `msgid` is completed. Returns the result of
        the originally `Task` or raises an exception if the `Task` triggered
        one during processing.
        """
        reply = self._cmd(Msg.COLLECT, Msg.DONE, msgid)
        return reply.payload.get_result()

    @diesel.call
    def register(self, host, port):
        """
        Registers with a remote Manager. The remote host will connect back to
        the provided `host` and `port`.
        """
        self._cmd(Msg.REG, Msg.ACK, (host, port))
        return True


class Dispatcher(object):
    """
    Dispatches messages based on the message cmd.
    """
    def __init__(self):
        self.dispatch = {}

    def responds_to(self, cmd, cb):
        """
        Registers a callback for messages based on the message command.
        The callback must return a Message object.
        """
        self.dispatch[cmd] = cb

    def get_response(self, msg):
        """
        Attempts to call the handler callback for a given message based
        on the message command.
        """
        if msg.cmd in self.dispatch:
            return self.dispatch[msg.cmd](msg)
        else:
            raise ProtocolError('Command not handled')


class Service(Dispatcher):
    """
    Provides a callable suitable to be passed to a diesel loop that implements
    the Dispatcher as a TCP/IP service.
    """
    def __init__(self, *args, **kwargs):
        super(Service, self).__init__(*args, **kwargs)
        self.responds_to(Msg.PING, self.handle_ping)

    def on_service_init(self, service):
        """
        This is called by the diesel service before starting the loop. It lets
        the denim service learn the host and port on which the service is
        listening.
        """
        self.host = service.iface
        self.port = service.port

    def _next_msg(self):
        """
        Reads the next line off the wire and decodes it into a `Msg`.
        """
        return Msg.decode(diesel.until_eol())

    def _send(self, msg):
        """
        Encodes and sends a Msg.
        """
        diesel.send(msg.encode())

    def get_response(self, msg, addr):
        """
        Allows child classes to intercept by address (Worker in particular).
        """
        return super(Service, self).get_response(msg)

    def service_request(self, addr):
        """
        Reads a `Msg` in, attempts to dispatch it, and sends the result back.
        'Msg.cmd' values that are not registered via `responds_to` send a
        `Msg.ERR` message back.

        TODO log decode errors
        """
        try:
            msg = self._next_msg()
        except ProtocolError, e:
            return

        try:
            reply = self.get_response(msg, addr=addr)
        except Exception, e:
            reply = msg.reply(Msg.ERR, e)

        if not isinstance(reply, Msg):
            reply = msg.reply(Msg.ERR, 'The server generated an invalid response')

        self._send(reply)

    def __call__(self, addr):
        while True:
            self.service_request(addr)

    def handle_ping(self, msg):
        return msg.reply(Msg.ACK)


class Worker(Service):
    """
    Workers are the end-points of the task workflow in denim. They maintain a
    pool of processes that are used to service requests from a Manager. Workers
    will only respond to requests from Managers.
    """
    timeout = 5
    reconnect_retry_time = 5

    # TODO reconnect to mgr when disconnected
    def __init__(self, processes, manager, *args, **kwargs):
        super(Worker, self).__init__(*args, **kwargs)
        """
        Starts a new worker service. The worker will spawn a process pool with
        `processes` number of processes when initialized as a diesel service,
        then register with `manager`, which should be specified as a string of
        "host:port".
        """
        self.procs = processes
        self.manager = manager
        self.responds_to(Msg.QUEUE, self.handle_queue)

    def manager_addr(self):
        """
        Returns a tuple of the manager's hostname and port.
        """
        host, port = self.manager.split(':')
        return (host, int(port))

    def on_service_init(self, service):
        """
        Initializes the process pool and registers with the manager.
        """
        super(Worker, self).on_service_init(service)
        self.pool = ProcessPool(self.procs, self._worker)
        self.register()

    def get_response(self, msg, addr):
        """
        Overrides get_response to throw and error if a message is not from the
        manager.
        """
        if addr != self.manager:
            raise ProtocolError('Tasks must be queued through the manager')

        return super(Worker, self).get_response(msg, addr)

    def register(self):
        """
        Continuously retries to connect to server. If a connection fails, retries
        after `Worker.reconnect_retry_time` seconds.

        TODO log instead of print
        """
        print 'Connecting to manager: %s' % self.manager
        host, port = self.manager_addr()

        while True:
            try:
                client = Client(host, port, timeout=self.timeout)
            except diesel.ClientConnectionError, e:
                print 'Manager is unavailable (%s). Retrying in %d seconds.' % (e,
                        self.reconnect_retry_time)
                diesel.sleep(self.reconnect_retry_time)
                continue

            with client:
                client.register(self.host, self.port)
                return

    def _worker(self, task):
        """
        Worker process handler code. Wraps `task.perform` and returns the
        `Task` object passed in.
        """
        task.perform()
        return task

    def handle_queue(self, msg):
        """
        Accepts new messages from a client (the manager) with the `Msg.QUEUE`
        command. Raises a `ProtocolError` if the message payload is not a
        `Task` object.
        """
        if not isinstance(msg.payload, Task):
            raise ProtocolError('Invalid message payload')

        task = self.pool(msg.payload)
        return msg.reply(Msg.DONE, payload=task)


class Manager(Dispatcher):
    def __init__(self, *args, **kwargs):
        super(Manager, self).__init__(*args, **kwargs)
        self.workers = set()
        self.responds_to(Msg.REG, self.handle_reg)
        self.responds_to(Msg.QUEUE, self.handle_queue)
        self.responds_to(Msg.COLLECT, self.handle_queue)

    def handle_reg(self, msg):
        host, port = msg.payload
        if not host or not port:
            raise ProtocolError('Invalid host or port')

        client = Client(host, port)
        self.workers.add(client)

        #loop = diesel.Loop(self.ping_loop)
        #diesel.runtime.current_app.add_loop(loop)

        return msg.reply(Msg.ACK)

    def handle_queue(self, msg):
        raise NotImplemented

    def handle_collect(self, msg):
        raise NotImplemented
