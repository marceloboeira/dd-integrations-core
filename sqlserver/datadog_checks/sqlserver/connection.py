# (C) Datadog, Inc. 2020-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
import logging
import socket
from contextlib import closing, contextmanager

from six import raise_from

from datadog_checks.base import AgentCheck, ConfigurationError
from datadog_checks.base.log import get_check_logger

try:
    import adodbapi
    from adodbapi.apibase import OperationalError
    from pywintypes import com_error
except ImportError:
    adodbapi = None

try:
    import pyodbc
except ImportError:
    pyodbc = None

logger = logging.getLogger(__file__)

DATABASE_EXISTS_QUERY = 'select name, collation_name from sys.databases;'
DEFAULT_CONN_PORT = 1433


class SQLConnectionError(Exception):
    """Exception raised for SQL instance connection issues"""

    pass


def split_sqlserver_host_port(host):
    """
    Splits the host & port out of the provided SQL Server host connection string, returning (host, port).
    """
    if not host:
        return host, None
    host_split = [s.strip() for s in host.split(',')]
    if len(host_split) == 1:
        return host_split[0], None
    if len(host_split) == 2:
        return host_split
    # else len > 2
    s_host, s_port = host_split[0:2]
    logger.warning(
        "invalid sqlserver host string has more than one comma: %s. using only 1st two items: host:%s, port:%s",
        host,
        s_host,
        s_port,
    )
    return s_host, s_port


# we're only including the bare minimum set of special characters required to parse the connection string while
# supporting escaping using braces, letting the client library or the database ultimately decide what's valid
CONNECTION_STRING_SPECIAL_CHARACTERS = set('=;{}')


def parse_connection_string_properties(cs):
    """
    Parses the properties portion of a SQL Server connection string (i.e. "key1=value1;key2=value2;...") into a map of
    {key -> value}. The string must contain *properties only*, meaning the subprotocol, serverName, instanceName and
    portNumber are not included in the string.
    See https://docs.microsoft.com/en-us/sql/connect/jdbc/building-the-connection-url
    """
    cs = cs.strip()
    params = {}
    i = 0
    escaping = False
    key, parsed, key_done = "", "", False
    while i < len(cs):
        if escaping:
            if cs[i : i + 2] == '}}':
                parsed += '}'
                i += 2
                continue
            if cs[i] == '}':
                escaping = False
                i += 1
                continue
            parsed += cs[i]
            i += 1
            continue
        if cs[i] == '{':
            escaping = True
            i += 1
            continue
        # ignore leading whitespace, i.e. between two keys "A=B;  C=D"
        if not key_done and not parsed and cs[i] == ' ':
            i += 1
            continue
        if cs[i] == '=':
            if key_done:
                raise ConfigurationError(
                    "Invalid connection string: unexpected '=' while parsing value at index={}: {}".format(i, cs)
                )
            key, parsed, key_done = parsed, "", True
            if not key:
                raise ConfigurationError("Invalid connection string: empty key at index={}: {}".format(i, cs))
            i += 1
            continue
        if cs[i] == ';':
            if not parsed:
                raise ConfigurationError("Invalid connection string: empty value at index={}: {}".format(i, cs))
            params[key] = parsed
            key, parsed, key_done = "", "", False
            i += 1
            continue
        if cs[i] in CONNECTION_STRING_SPECIAL_CHARACTERS:
            raise ConfigurationError(
                "Invalid connection string: invalid character '{}' at index={}: {}".format(cs[i], i, cs)
            )
        parsed += cs[i]
        i += 1
    # the last ';' can be omitted so check for a final remaining param here
    if escaping:
        raise ConfigurationError(
            "Invalid connection string: did not find expected matching closing brace '}}': {}".format(cs)
        )
    if key:
        if not parsed:
            raise ConfigurationError(
                "Invalid connection string: empty value at the end of the connection string: {}".format(cs)
            )
        params[key] = parsed
    return params


known_hresult_codes = {
    -2147352567: "unable to connect",
    -2147217843: "login failed for user",
    # this error can also be caused by a failed TCP connection but we are already reporting on the TCP
    # connection status via test_network_connectivity so we don't need to explicitly state that
    # as an error condition in this message
    -2147467259: "could not open database requested by login",
}


def _format_connection_exception(e):
    """
    Formats the provided database connection exception.
    If the exception comes from an ADO Provider and contains a misleading 'Invalid connection string attribute' message
    then the message is replaced with more descriptive messages based on the contained HResult error codes.
    """
    if adodbapi is not None:
        if isinstance(e, OperationalError) and e.args and isinstance(e.args[0], com_error):
            e_comm = e.args[0]
            hresult = e_comm.hresult
            sub_hresult = None
            internal_message = None
            if e_comm.args and len(e_comm.args) == 4:
                internal_args = e_comm.args[2]
                if len(internal_args) == 6:
                    internal_message = internal_args[2]
                    sub_hresult = internal_args[5]
            if internal_message == 'Invalid connection string attribute':
                base_message = known_hresult_codes.get(hresult)
                sub_message = known_hresult_codes.get(sub_hresult)
                if base_message and sub_message:
                    return base_message + ": " + sub_message
    return repr(e)


class Connection(object):
    """Manages the connection to a SQL Server instance."""

    DEFAULT_COMMAND_TIMEOUT = 5
    DEFAULT_DATABASE = 'master'
    DEFAULT_DRIVER = 'SQL Server'
    DEFAULT_DB_KEY = 'database'
    DEFAULT_SQLSERVER_VERSION = 1e9
    SQLSERVER_2014 = 2014
    PROC_GUARD_DB_KEY = 'proc_only_if_database'

    valid_adoproviders = ['SQLOLEDB', 'MSOLEDBSQL', 'MSOLEDBSQL19', 'SQLNCLI11']
    default_adoprovider = 'SQLOLEDB'

    def __init__(self, init_config, instance_config, service_check_handler):
        self.instance = instance_config
        self.service_check_handler = service_check_handler
        self.log = get_check_logger()

        # mapping of raw connections based on conn_key to different databases
        self._conns = {}
        self.timeout = int(self.instance.get('command_timeout', self.DEFAULT_COMMAND_TIMEOUT))
        self.existing_databases = None
        self.server_version = int(self.instance.get('server_version', self.DEFAULT_SQLSERVER_VERSION))

        self.adoprovider = self.default_adoprovider

        self.valid_connectors = []
        if adodbapi is not None:
            self.valid_connectors.append('adodbapi')
        if pyodbc is not None:
            self.valid_connectors.append('odbc')

        connector = init_config.get('connector')
        if connector is None or connector.lower() not in self.valid_connectors:
            if connector is None:
                self.log.debug("`connector` config value was not set, defaulting to adodbapi")
            else:
                self.log.error("Invalid database connector %s, defaulting to adodbapi", connector)
            self.default_connector = 'adodbapi'
        else:
            self.default_connector = connector

        self.connector = self.get_connector()

        self.adoprovider = init_config.get('adoprovider', self.default_adoprovider)
        if self.adoprovider.upper() not in self.valid_adoproviders:
            self.log.error(
                "Invalid ADODB provider string %s, defaulting to %s",
                self.adoprovider,
                self.default_adoprovider,
            )
            self.adoprovider = self.default_adoprovider

        self.log.debug('Connection initialized.')

    @contextmanager
    def get_managed_cursor(self, key_prefix=None):
        cursor = self.get_cursor(self.DEFAULT_DB_KEY, key_prefix=key_prefix)
        try:
            yield cursor
        finally:
            self.close_cursor(cursor)

    def get_cursor(self, db_key, db_name=None, key_prefix=None):
        """
        Return a cursor to execute query against the db
        Cursor are cached in the self.connections dict
        """
        conn_key = self._conn_key(db_key, db_name, key_prefix)
        try:
            conn = self._conns[conn_key]
        except KeyError:
            # We catch KeyError to avoid leaking the auth info used to compose the key
            # FIXME: we should find a better way to compute unique keys to map opened connections other than
            # using auth info in clear text!
            raise SQLConnectionError("Cannot find an opened connection for host: {}".format(self.instance.get('host')))
        return conn.cursor()

    def close_cursor(self, cursor):
        """
        We close the cursor explicitly b/c we had proven memory leaks
        We handle any exception from closing, although according to the doc:
        "in adodbapi, it is NOT an error to re-close a closed cursor"
        """
        try:
            cursor.close()
        except Exception as e:
            self.log.warning("Could not close adodbapi cursor\n%s", e)

    def check_database(self):
        with self.open_managed_default_database():
            db_exists, context = self._check_db_exists()

        return db_exists, context

    def check_database_conns(self, db_name):
        self.open_db_connections(None, db_name=db_name, is_default=False)
        self.close_db_connections(None, db_name)

    @contextmanager
    def open_managed_default_database(self):
        with self._open_managed_db_connections(None, db_name=self.DEFAULT_DATABASE):
            yield

    @contextmanager
    def open_managed_default_connection(self, key_prefix=None):
        with self._open_managed_db_connections(self.DEFAULT_DB_KEY, key_prefix=key_prefix):
            yield

    @contextmanager
    def _open_managed_db_connections(self, db_key, db_name=None, key_prefix=None):
        self.open_db_connections(db_key, db_name, key_prefix=key_prefix)
        try:
            yield
        finally:
            self.close_db_connections(db_key, db_name, key_prefix=key_prefix)

    def open_db_connections(self, db_key, db_name=None, is_default=True, key_prefix=None):
        """
        We open the db connections explicitly, so we can ensure they are open
        before we use them, and are closable, once we are finished. Open db
        connections keep locks on the db, presenting issues such as the SQL
        Server Agent being unable to stop.
        """
        conn_key = self._conn_key(db_key, db_name, key_prefix)

        _, host, _, _, database, _ = self._get_access_info(db_key, db_name)

        cs = self.instance.get('connection_string', '')
        cs += ';' if cs != '' else ''

        self._connection_options_validation(db_key, db_name)

        try:
            if self.connector == 'adodbapi':
                cs += self._conn_string_adodbapi(db_key, db_name=db_name)
                # autocommit: true disables implicit transaction
                rawconn = adodbapi.connect(cs, {'timeout': self.timeout, 'autocommit': True})
            else:
                cs += self._conn_string_odbc(db_key, db_name=db_name)
                rawconn = pyodbc.connect(cs, timeout=self.timeout, autocommit=True)
                rawconn.timeout = self.timeout

            self.service_check_handler(AgentCheck.OK, host, database, is_default=is_default)
            if conn_key not in self._conns:
                self._conns[conn_key] = rawconn
            else:
                try:
                    # explicitly trying to avoid leaks...
                    self._conns[conn_key].close()
                except Exception as e:
                    self.log.info("Could not close adodbapi db connection\n%s", e)

                self._conns[conn_key] = rawconn
            self._setup_new_connection(rawconn)
        except Exception as e:
            error_message = self.test_network_connectivity()
            tcp_connection_status = error_message if error_message else "OK"
            message = "Unable to connect to SQL Server (host={} database={}). TCP-connection({}). Exception: {}".format(
                host, database, tcp_connection_status, _format_connection_exception(e)
            )

            password = self.instance.get('password')
            if password is not None:
                message = message.replace(password, "*" * 6)

            self.service_check_handler(AgentCheck.CRITICAL, host, database, message, is_default=is_default)

            # Only raise exception on the default instance database
            if is_default:
                raise_from(SQLConnectionError(message), None)

    def _setup_new_connection(self, rawconn):
        with rawconn.cursor() as cursor:
            # ensure that by default, the agent's reads can never block updates to any tables it's reading from
            cursor.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")

    def close_db_connections(self, db_key, db_name=None, key_prefix=None):
        """
        We close the db connections explicitly b/c when we don't they keep
        locks on the db. This presents as issues such as the SQL Server Agent
        being unable to stop.
        """
        conn_key = self._conn_key(db_key, db_name, key_prefix)
        if conn_key not in self._conns:
            return

        try:
            self._conns[conn_key].close()
            del self._conns[conn_key]
        except Exception as e:
            self.log.warning("Could not close adodbapi db connection\n%s", e)

    def _check_db_exists(self):
        """
        Check for existence of a database, but take into consideration whether the db is case-sensitive or not.

        If not case-sensitive, then we normalize the database name to lowercase on both sides and check.
        If case-sensitive, then we only accept exact-name matches.

        If the check fails, then we won't do any checks if `ignore_missing_database` is enabled, or we will fail
        with a ConfigurationError otherwise.
        """

        _, host, _, _, database, _ = self._get_access_info(self.DEFAULT_DB_KEY)
        context = "{} - {}".format(host, database)
        if self.existing_databases is None:
            cursor = self.get_cursor(None, self.DEFAULT_DATABASE)

            try:
                self.existing_databases = {}
                cursor.execute(DATABASE_EXISTS_QUERY)
                for row in cursor:
                    # collation_name can be NULL if db offline, in that case assume its case_insensitive
                    case_insensitive = not row.collation_name or 'CI' in row.collation_name
                    self.existing_databases[row.name.lower()] = (
                        case_insensitive,
                        row.name,
                    )

            except Exception as e:
                self.log.error("Failed to check if database %s exists: %s", database, e)
                return False, context
            finally:
                self.close_cursor(cursor)

        exists = False
        if database.lower() in self.existing_databases:
            case_insensitive, cased_name = self.existing_databases[database.lower()]
            if case_insensitive or database == cased_name:
                exists = True

        return exists, context

    def get_connector(self):
        connector = self.instance.get('connector', self.default_connector)
        if connector != self.default_connector:
            if connector.lower() not in self.valid_connectors:
                self.log.warning(
                    "Invalid database connector %s using default %s",
                    connector,
                    self.default_connector,
                )
                connector = self.default_connector
            else:
                self.log.debug(
                    "Overriding default connector for %s with %s",
                    self.instance['host'],
                    connector,
                )
        return connector

    def _get_adoprovider(self):
        provider = self.instance.get('adoprovider', self.default_adoprovider)
        if provider != self.adoprovider:
            if provider.upper() not in self.valid_adoproviders:
                self.log.warning(
                    "Invalid ADO provider %s using default %s",
                    provider,
                    self.adoprovider,
                )
                provider = self.adoprovider
            else:
                self.log.debug(
                    "Overriding default ADO provider for %s with %s",
                    self.instance['host'],
                    provider,
                )
        return provider

    def _get_access_info(self, db_key, db_name=None):
        """Convenience method to extract info from instance"""
        dsn = self.instance.get('dsn')
        username = self.instance.get('username')
        password = self.instance.get('password')
        database = self.instance.get(db_key) if db_name is None else db_name
        driver = self.instance.get('driver')
        host = self._get_host_with_port()

        if not dsn:
            if not host:
                self.log.debug("No host provided, falling back to defaults: host=127.0.0.1, port=1433")
                host = "127.0.0.1,1433"
            if not database:
                self.log.debug(
                    "No database provided, falling back to default: %s",
                    self.DEFAULT_DATABASE,
                )
                database = self.DEFAULT_DATABASE
            if not driver:
                self.log.debug(
                    "No driver provided, falling back to default: %s",
                    self.DEFAULT_DRIVER,
                )
                driver = self.DEFAULT_DRIVER
        return dsn, host, username, password, database, driver

    def _get_host_with_port(self):
        """Return a string with format host,port.
        If the host string in the config contains a port, that port is used.
        If not, any port provided as a separate port config option is used.
        If the port is misconfigured or missing, default port is used.
        """
        host = self.instance.get("host")
        if not host:
            return None

        port = str(DEFAULT_CONN_PORT)
        split_host, split_port = split_sqlserver_host_port(host)
        config_port = self.instance.get("port")

        if split_port is not None:
            port = split_port
        elif config_port is not None:
            port = config_port
        try:
            int(port)
        except ValueError:
            self.log.warning("Invalid port %s; falling back to default 1433", port)
            port = str(DEFAULT_CONN_PORT)

        return split_host + "," + port

    def _conn_key(self, db_key, db_name=None, key_prefix=None):
        """Return a key to use for the connection cache"""
        dsn, host, username, password, database, driver = self._get_access_info(db_key, db_name)
        if not key_prefix:
            key_prefix = ""
        return '{}{}:{}:{}:{}:{}:{}'.format(key_prefix, dsn, host, username, password, database, driver)

    def _connection_options_validation(self, db_key, db_name):
        cs = self.instance.get('connection_string')
        username = self.instance.get('username')
        password = self.instance.get('password')

        adodbapi_options = {
            'PROVIDER': 'adoprovider',
            'Data Source': 'host',
            'Initial Catalog': db_name or db_key,
            'User ID': 'username',
            'Password': 'password',
        }
        odbc_options = {
            'DSN': 'dsn',
            'DRIVER': 'driver',
            'SERVER': 'host',
            'DATABASE': db_name or db_key,
            'UID': 'username',
            'PWD': 'password',
        }

        if self.connector == 'adodbapi':
            other_connector = 'odbc'
            connector_options = adodbapi_options
            other_connector_options = odbc_options

        else:
            other_connector = 'adodbapi'
            connector_options = odbc_options
            other_connector_options = adodbapi_options

        for option in {
            value
            for key, value in other_connector_options.items()
            if value not in connector_options.values() and self.instance.get(value) is not None
        }:
            self.log.warning(
                "%s option will be ignored since %s connection is used",
                option,
                self.connector,
            )

        if cs is None:
            return

        parsed_cs = parse_connection_string_properties(cs)
        lowercased_keys_cs = {k.lower(): v for k, v in parsed_cs.items()}

        if lowercased_keys_cs.get('trusted_connection', "false").lower() in {
            'yes',
            'true',
        } and (username or password):
            self.log.warning("Username and password are ignored when using Windows authentication")

        for key, value in connector_options.items():
            if key.lower() in lowercased_keys_cs and self.instance.get(value) is not None:
                raise ConfigurationError(
                    "%s has been provided both in the connection string and as a "
                    "configuration option (%s), please specify it only once" % (key, value)
                )
        for key in other_connector_options.keys():
            if key.lower() in lowercased_keys_cs:
                raise ConfigurationError(
                    "%s has been provided in the connection string. "
                    "This option is only available for %s connections,"
                    " however %s has been selected" % (key, other_connector, self.connector)
                )

    def _conn_string_odbc(self, db_key, conn_key=None, db_name=None):
        """Return a connection string to use with odbc"""
        if conn_key:
            dsn, host, username, password, database, driver = conn_key.split(":")
        else:
            dsn, host, username, password, database, driver = self._get_access_info(db_key, db_name)

        # The connection resiliency feature is supported on Microsoft Azure SQL Database
        # and SQL Server 2014 (and later) server versions. See the SQLServer docs for more information
        # https://docs.microsoft.com/en-us/sql/connect/odbc/connection-resiliency?view=sql-server-ver15
        conn_str = ''
        if self.server_version >= self.SQLSERVER_2014:
            conn_str += 'ConnectRetryCount=2;'
        if dsn:
            conn_str += 'DSN={};'.format(dsn)
        if driver:
            conn_str += 'DRIVER={};'.format(driver)
        if host:
            conn_str += 'Server={};'.format(host)
        if database:
            conn_str += 'Database={};'.format(database)
        if username:
            conn_str += 'UID={};'.format(username)
        self.log.debug("Connection string (before password) %s", conn_str)
        if password:
            conn_str += 'PWD={};'.format(password)
        return conn_str

    def _conn_string_adodbapi(self, db_key, conn_key=None, db_name=None):
        """Return a connection string to use with adodbapi"""
        if conn_key:
            _, host, username, password, database, _ = conn_key.split(":")
        else:
            _, host, username, password, database, _ = self._get_access_info(db_key, db_name)

        provider = self._get_adoprovider()
        retry_conn_count = ''
        if self.server_version >= self.SQLSERVER_2014:
            retry_conn_count = 'ConnectRetryCount=2;'
        conn_str = '{}Provider={};Data Source={};Initial Catalog={};'.format(retry_conn_count, provider, host, database)

        if username:
            conn_str += 'User ID={};'.format(username)
        self.log.debug("Connection string (before password) %s", conn_str)
        if password:
            conn_str += 'Password={};'.format(password)
        if not username and not password:
            conn_str += 'Integrated Security=SSPI;'
        return conn_str

    def test_network_connectivity(self):
        """
        Tries to establish a TCP connection to the database host.
        If there is an error, it returns a description of the error.

        :return: error_message if failed connection else None
        """
        host, port = split_sqlserver_host_port(self.instance.get('host'))
        if port is None:
            port = DEFAULT_CONN_PORT
            provided_port = self.instance.get("port")
            if provided_port is not None:
                port = provided_port

        try:
            port = int(port)
        except ValueError as e:
            return "ERROR: invalid port: {}".format(repr(e))

        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            sock.settimeout(self.timeout)
            try:
                sock.connect((host, port))
            except Exception as e:
                return "ERROR: {}".format(e.strerror if hasattr(e, 'strerror') else repr(e))

        return None
