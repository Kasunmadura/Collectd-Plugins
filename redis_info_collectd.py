# redis-collectd-plugin - redis_info_collectd.py
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; only version 2 of the License is applicable.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA
#
# Authors:
#   Garret Heaton <powdahound at gmail.com>
#
# About this plugin:
#   This plugin uses collectd's Python plugin to record Redis information.
#
# collectd:
#   http://collectd.org
# Redis:
#   http://redis.googlecode.com
# collectd-python:
#   http://collectd.org/documentation/manpages/collectd-python.5.shtml

import collectd
import socket
import re


# Host to connect to. Override in config by specifying 'Host'.
REDIS_HOST = 'localhost'

# Port to connect on. Override in config by specifying 'Port'.
REDIS_PORT = 6379

# Auth, default is None (unused). Override in config by specifying 'Auth'.
REDIS_AUTH = None

# Verbose logging on/off. Override in config by specifying 'Verbose'.
VERBOSE_LOGGING = False


def fetch_info():
    """Connect to Redis server and request info"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((REDIS_HOST, REDIS_PORT))
        log_verbose('Connected to Redis at %s:%s' % (REDIS_HOST, REDIS_PORT))
    except socket.error, e:
        collectd.error('redis_info plugin: Error connecting to %s:%d - %r'
                       % (REDIS_HOST, REDIS_PORT, e))
        return None

    fp = s.makefile('r')

    if REDIS_AUTH is not None:
        log_verbose('Sending auth command')
        s.sendall('auth %s\r\n' % (REDIS_AUTH))

        status_line = fp.readline()
        if not status_line.startswith('+OK'):
            # -ERR invalid password
            # -ERR Client sent AUTH, but no password is set
            collectd.error('redis_info plugin: Error sending auth to %s:%d - %r'
                           % (REDIS_HOST, REDIS_PORT, status_line))
            return None

    log_verbose('Sending info command')
    s.sendall('info\r\n')

    status_line = fp.readline()
    content_length = int(status_line[1:-1]) # status_line looks like: $<content_length>
    data = fp.read(content_length)
    log_verbose('Received data: %s' % data)
    s.close()

    linesep = '\r\n' if '\r\n' in data else '\n'
    return parse_info(data.split(linesep))


def parse_info(info_lines):
    """Parse info response from Redis"""
    info = {}
    for line in info_lines:
        if "" == line or line.startswith('#'):
            continue

        if ':' not in line:
            collectd.warning('redis_info plugin: Bad format for info line: %s'
                             % line)
            continue

        key, val = line.split(':')

        # Handle multi-value keys (for dbs and slaves).
        # db lines look like "db0:keys=10,expire=0"
        # slave lines look like "slave0:ip=192.168.0.181,port=6379,state=online,offset=1650991674247,lag=1"
        if ',' in val:
            split_val = val.split(',')
            val = {}
            for sub_val in split_val:
                k, _, v = sub_val.rpartition('=')
                val[k] = v

        info[key] = val

    info["changes_since_last_save"] = info.get("changes_since_last_save", info.get("rdb_changes_since_last_save"))

    # For each slave add an additional entry that is the replication delay
    regex = re.compile("slave\d+")
    for key in info:
        if regex.match(key):
            info[key]['delay'] = int(info['master_repl_offset']) - int(info[key]['offset'])

    return info


def configure_callback(conf):
    """Receive configuration block"""
    global REDIS_HOST, REDIS_PORT, REDIS_AUTH, VERBOSE_LOGGING
    for node in conf.children:
        if node.key == 'Host':
            REDIS_HOST = node.values[0]
        elif node.key == 'Port':
            REDIS_PORT = int(node.values[0])
        elif node.key == 'Auth':
            REDIS_AUTH = node.values[0]
        elif node.key == 'Verbose':
            VERBOSE_LOGGING = bool(node.values[0])
        else:
            collectd.warning('redis_info plugin: Unknown config key: %s.'
                             % node.key)
    log_verbose('Configured with host=%s, port=%s, using_auth=%s' % (REDIS_HOST, REDIS_PORT, REDIS_AUTH!=None))


def dispatch_value(info, key, type, type_instance=None):
    """Read a key from info response data and dispatch a value"""
    if key not in info:
        collectd.warning('redis_info plugin: Info key not found: %s' % key)
        return

    if not type_instance:
        type_instance = key

    value = int(info[key])
    log_verbose('Sending value: %s=%s' % (type_instance, value))

    val = collectd.Values(plugin='redis_info')
    val.type = type
    val.type_instance = type_instance
    val.values = [value]
    val.dispatch()


def read_callback():
    log_verbose('Read callback called')
    info = fetch_info()

    if not info:
        collectd.error('redis plugin: No info received')
        return

    # send high-level values
    dispatch_value(info, 'uptime_in_seconds','uptime')
    dispatch_value(info, 'connected_clients', 'gauge', 'clients-connected')
    dispatch_value(info, 'connected_slaves', 'gauge', 'slaves-connected')
    dispatch_value(info, 'blocked_clients', 'gauge', 'clients-blocked')
    dispatch_value(info, 'evicted_keys', 'gauge', 'keys-evicted')
    dispatch_value(info, 'used_memory', 'bytes')
    dispatch_value(info, 'changes_since_last_save', 'gauge', 'changes-since_last_save')
    dispatch_value(info, 'total_connections_received', 'counter', 'connections-received')
    dispatch_value(info, 'total_commands_processed', 'counter', 'commands-processed')

    # send replication stats, but only if they exist (some belong to master only, some to slaves only)
    if 'master_repl_offset' in info: dispatch_value(info, 'master_repl_offset', 'gauge', 'repl_offset-master')
    if 'master_last_io_seconds_ago' in info: dispatch_value(info, 'master_last_io_seconds_ago', 'gauge', 'last_io_seconds_ago-master')
    if 'slave_repl_offset' in info: dispatch_value(info, 'slave_repl_offset', 'gauge', 'repl_offset-slave')

    # database and vm stats
    for key in info:
        if key.startswith('repl_'):
            dispatch_value(info, key, 'gauge', 'repl-%s' % key[5:])
        if key.startswith('vm_stats_'):
            dispatch_value(info, key, 'gauge', 'vm_stats-%s' % key[9:])
        if key.startswith('db'):
            dispatch_value(info[key], 'keys', 'gauge', 'db-%s-keys' % key)
        if key.startswith('slave'):
            dispatch_value(info[key], 'delay', 'delay', key)


def log_verbose(msg):
    if not VERBOSE_LOGGING:
        return
    collectd.info('redis plugin [verbose]: %s' % msg)


# register callbacks
collectd.register_config(configure_callback)
collectd.register_read(read_callback)
