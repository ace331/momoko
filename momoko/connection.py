# -*- coding: utf-8 -*-
"""
momoko.connection
=================

Connection handling.

Copyright 2011-2012 by Frank Smit.
MIT, see LICENSE for more details.
"""

from functools import partial
from contextlib import contextmanager
from collections import deque, defaultdict

from tornado import gen
from tornado.ioloop import IOLoop, PeriodicCallback

from .utils import Op, psycopg2, log
from .exceptions import PoolError


base_connection = psycopg2.extensions.connection
base_cursor = psycopg2.extensions.cursor

POLL_OK = psycopg2.extensions.POLL_OK
POLL_READ = psycopg2.extensions.POLL_READ
POLL_WRITE = psycopg2.extensions.POLL_WRITE
POLL_ERROR = psycopg2.extensions.POLL_ERROR

TRANSACTION_STATUS_IDLE = psycopg2.extensions.TRANSACTION_STATUS_IDLE


# The dummy callback is used to keep the asynchronous cursor alive in case no
# callback has been specified. This will prevent the cursor from being garbage
# collected once, for example, ``ConnectionPool.execute`` has finished.
def _dummy_callback(cursor, error):
    pass


class Pool:
    def __init__(self,
        dsn,
        connection_factory=None,
        minconn=1,
        maxconn=5,
        cleanup_timeout=10,
        ioloop=None
    ):
        self.dsn = dsn
        self.minconn = minconn
        self.maxconn = maxconn
        self.closed = False
        self.connection_factory = connection_factory

        self._ioloop = ioloop or IOLoop.instance()
        self._pool = []

        # Create connections
        for i in range(self.minconn):
            self.new()

        # Create a periodic callback that tries to close inactive connections
        self._cleaner = None
        if cleanup_timeout > 0:
            self._cleaner = PeriodicCallback(self._clean_pool,
                cleanup_timeout * 1000)
            self._cleaner.start()

    def new(self, callback=None):
        if len(self._pool) > self.maxconn:
            raise PoolError('connection pool exausted')

        def multi_callback(connection, error):
            if error:
                raise error
            if callback:
                callback(connection)
            self._pool.append(connection)

        Connection(self.dsn, self.connection_factory,
            multi_callback, self._ioloop)

    def _get_connection(self):
        for connection in self._pool:
            if not connection.busy():
                return connection

    # TODO
    def _clean_pool(self):
        pass

    def transaction(self,
        statements,
        cursor_factory=None,
        callback=_dummy_callback,
        connection=None
    ):
        connection = connection or self._get_connection()
        if connection:
            connection.transaction(statements, cursor_factory, callback)
            return

        self.new(lambda connection: self.transaction(
            statements, cursor_factory, callback, connection))

    def execute(self,
        operation,
        parameters=(),
        cursor_factory=None,
        callback=_dummy_callback,
        connection=None
    ):
        connection = connection or self._get_connection()
        if connection:
            connection.execute(operation, parameters, cursor_factory, callback)
            return

        self.new(lambda connection: self.execute(operation, parameters,
            cursor_factory, callback, connection))

    def callproc(self,
        procname,
        parameters=(),
        cursor_factory=None,
        callback=_dummy_callback,
        connection=None
    ):
        connection = connection or self._get_connection()
        if connection:
            connection.callproc(procname, parameters, cursor_factory, callback)
            return

        self.new(lambda connection: self.callproc(procname, parameters,
            cursor_factory, callback, connection))

    def mogrify(self,
        operation,
        parameters=(),
        callback=_dummy_callback,
        connection=None
    ):
        connection = connection or self._get_connection()
        if connection:
            connection.mogrify(operation, parameters, callback)
            return

        self.new(lambda connection: self.mogrify(operation, parameters,
            callback, connection))

    def close(self):
        if self.closed:
            raise PoolError('connection pool is already closed')

        for connection in self._pool:
            if not connection.closed:
                connection.close()

        if self._cleaner:
            self._cleaner.stop()
        self._pool = []
        self.closed = True


class Connection:
    def __init__(self,
        dsn,
        connection_factory=None,
        callback=None,
        ioloop=None
    ):
        self.connection = psycopg2.connect(dsn, async=1,
            connection_factory=connection_factory or base_connection)
        self.fileno = self.connection.fileno()
        self._transaction_status = self.connection.get_transaction_status
        self.ioloop = ioloop or IOLoop.instance()

        self.callback = partial(callback, self)
        self.ioloop.add_handler(self.fileno, self.io_callback, IOLoop.WRITE)

    def io_callback(self, fd=None, events=None):
        try:
            state = self.connection.poll()
        except (psycopg2.Warning, psycopg2.Error) as error:
            # When a DatabaseError is raised it means that the connection has been
            # closed and polling it would raise an exception from then IOLoop.
            if not isinstance(error, psycopg2.DatabaseError):
                self.ioloop.update_handler(self.fileno, 0)

            self.callback(error)
        else:
            if state == POLL_OK:
                self.ioloop.update_handler(self.fileno, 0)
                self.callback(None)
            elif state == POLL_READ:
                self.ioloop.update_handler(self.fileno, IOLoop.READ)
            elif state == POLL_WRITE:
                self.ioloop.update_handler(self.fileno, IOLoop.WRITE)
            else:
                raise OperationalError('poll() returned {0}'.format(state))

    def execute(self,
        operation,
        parameters=(),
        cursor_factory=None,
        callback=_dummy_callback
    ):
        cursor = self.connection.cursor(cursor_factory=cursor_factory or base_cursor)
        cursor.execute(operation, parameters)
        self.callback = partial(callback, cursor)
        self.ioloop.update_handler(self.fileno, IOLoop.WRITE)

    def callproc(self,
        procname,
        parameters=(),
        cursor_factory=None,
        callback=_dummy_callback
    ):
        cursor = self.connection.cursor(cursor_factory=cursor_factory or base_cursor)
        cursor.callproc(procname, parameters)
        self.callback = partial(callback, cursor)
        self.ioloop.update_handler(self.fileno, IOLoop.WRITE)

    def mogrify(self, operation, parameters=(), callback=_dummy_callback):
        cursor = self.connection.cursor()
        result = cursor.mogrify(operation, parameters)
        self.ioloop.add_callback(partial(callback, result, None))

    def transaction(self,
        statements,
        cursor_factory=None,
        callback=_dummy_callback
    ):
        cursors = []
        queue = deque()

        for statement in statements:
            if isinstance(statement, str):
                queue.append((statement, ()))
            else:
                queue.append(statement[:2])

        queue.appendleft(('BEGIN;', ()))
        queue.append(('COMMIT;', ()))

        def exec_statement(cursor=None, error=None):
            if error:
                self.execute('ROLLBACK;',
                    callback=partial(error_callback, error))
                return
            if cursor:
                cursors.append(cursor)
            if not queue:
                callback(cursors[1:-1], None)
                return

            operation, parameters = queue.popleft()
            self.execute(operation, parameters, cursor_factory, exec_statement)

        def error_callback(statement_error, cursor, rollback_error):
            log.error('An error occurred, transacion has been rolled back: {0}'
                .format(rollback_error or statement_error))
            callback(None, rollback_error or statement_error)

        self.ioloop.add_callback(exec_statement)

    def busy(self):
        return self.connection.isexecuting() or (self.connection.closed == 0 and
            self._transaction_status() != TRANSACTION_STATUS_IDLE)

    @property
    def closed(self):
        # 0 = open, 1 = closed, 2 = 'something horrible happened'
        return self.connection.closed > 0

    def close(self):
        self.ioloop.remove_handler(self.fileno)
        self.connection.close()
