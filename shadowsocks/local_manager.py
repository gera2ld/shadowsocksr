#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright 2015 clowwindy
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from __future__ import absolute_import, division, print_function, \
    with_statement

import os
import errno
import traceback
import socket
import logging
import json
import collections
import sys
import asyncio
from aiohttp import web

if __name__ == '__main__':
    import inspect
    file_path = os.path.dirname(os.path.realpath(inspect.getfile(inspect.currentframe())))
    sys.path.insert(0, os.path.join(file_path, '../'))

from shadowsocks import common, eventloop, tcprelay, udprelay, asyncdns, shell

class Manager:

    def __init__(self, r, w):
        os.set_blocking(r, False)
        self.r = os.fdopen(r)
        self.w = os.fdopen(w, 'w')
        self._relays = {}  # (tcprelay, udprelay)
        self._loop = eventloop.EventLoop()
        self._dns_resolver = asyncdns.DNSResolver()
        self._dns_resolver.add_to_loop(self._loop)
        self._loop.add(self.r, eventloop.POLL_IN | eventloop.POLL_ERR, self)

    def add_port(self, raw_config):
        logging.info('add %s' % raw_config)
        config = shell.normalize_config(raw_config.copy(), True)
        local_port = int(config['local_port'])
        server = '%s:%d' % (config['server'], config['server_port'])
        servers = self._relays.get(server, None)
        if servers:
            logging.error("remote server already exists: %s" % server)
            return
        if self._relays.get(local_port):
            logging.error('local server already exists at port %s' % local_port)
        logging.info("adding server at %s, local port %d" % (server, local_port))
        t = tcprelay.TCPRelay(config, self._dns_resolver, True)
        u = udprelay.UDPRelay(config, self._dns_resolver, True)
        t.add_to_loop(self._loop)
        u.add_to_loop(self._loop)
        servers = t, u, raw_config
        self._relays[server] = servers
        self._relays[local_port] = servers

    def remove_port(self, local_port):
        servers = self._relays.get(local_port)
        if servers:
            config = servers[0]._config
            server = '%s:%d' % (config['server'], config['server_port'])
            logging.info("removing server at %s, local port %d" % (server, local_port))
            t, u = servers
            t.close(next_tick=False)
            u.close(next_tick=False)
            del self._relays[server]
            del self._relays[local_port]
        else:
            logging.error("server not exist at local port %d" % local_port)

    def handle_event(self, sock, fd, event):
        if sock == self.r:
            while True:
                line = self.r.readline().strip()
                if not line: break
                self.handle_data(json.loads(line))

    def handle_data(self, data):
        if data['q'] == 'meta':
            result = {}
            for server, (t, u, config) in self._relays.items():
                if isinstance(server, int):
                    continue
                result[server] = {
                    'config': config,
                    'tcp_u': t.server_transfer_ul,
                    'tcp_d': t.server_transfer_dl,
                    'tcp_uu': t.server_user_transfer_ul,
                    'tcp_ud': t.server_user_transfer_dl,
                }
                self.write({
                    'q': 'meta',
                    'r': result,
                })
        elif data['q'] == 'add':
            self.add_port(data['p'])
        elif data['q'] == 'remove':
            self.remove_port(data['p'])
        elif data['q'] == 'refreshdns':
            self._dns_resolver._parse_resolv()
            self._dns_resolver._parse_hosts()

    def write(self, data):
        self.w.write(json.dumps(data))
        self.w.write('\n')
        self.w.flush()

    def run(self):
        self._loop.run()

monitor = None

class MonitorHandler:
    def __init__(self, pid):
        self.pid = pid
        self.data = {}

    async def initialize(self, r, w):
        loop = asyncio.get_event_loop()
        self.reader = asyncio.StreamReader()
        read_protocol = asyncio.StreamReaderProtocol(self.reader)
        read_transport, _ = await loop.connect_read_pipe(
            lambda: read_protocol, os.fdopen(r))
        write_protocol = asyncio.StreamReaderProtocol(asyncio.StreamReader())
        write_transport, _ = await loop.connect_write_pipe(
            lambda: write_protocol, os.fdopen(w, 'w'))
        self.writer = asyncio.StreamWriter(write_transport, write_protocol, None, loop)

    def write(self, data):
        self.writer.write(json.dumps(data).encode())
        self.writer.write(b'\n')

    async def handle(self):
        while True:
            line = await self.reader.readline()
            line = line.strip()
            if not line: break
            data = json.loads(line)
            self.handle_data(data)

    def handle_data(self, data):
        if data['q'] == 'meta':
            self.data['meta'] = data['r']

    @staticmethod
    async def create(pid, r, w):
        handler = MonitorHandler(pid)
        await handler.initialize(r, w)
        return handler

    async def refresh_meta(self):
        while True:
            await asyncio.sleep(2)
            self.write({
                'q': 'meta',
            })

    async def refresh_dns(self):
        while True:
            await asyncio.sleep(60)
            self.write({
                'q': 'refreshdns',
            })

    def start(self):
        asyncio.ensure_future(self.handle())
        asyncio.ensure_future(self.refresh_meta())
        asyncio.ensure_future(self.refresh_dns())

def start_manager():
    r1, w1 = os.pipe()
    r2, w2 = os.pipe()
    pid = os.fork()
    if pid == 0:
        mng = Manager(r1, w2)
        mng.run()
        return
    return MonitorHandler.create(pid, r2, w1)

routes = web.RouteTableDef()

@routes.get('/rpc/query')
def rpc_query(request):
    data = monitor.data.copy()
    data['pid'] = monitor.pid
    return web.json_response(dict(data=data))

@routes.get('/rpc/add')
async def rpc_add(request):
    payload = await request.post()
    monitor.write({
        'q': 'add',
        'p': payload['config'],
    })
    return web.HTTPNoContent

async def initialize(config_list):
    global monitor
    future = start_manager()
    if future is None:
        return
    monitor = await future
    for config in config_list:
        monitor.write({
            'q': 'add',
            'p': config,
        })
    monitor.start()

def main():
    config = json.load(open('user-config-list.json'))
    asyncio.ensure_future(initialize(config['servers']))
    app = web.Application()
    app.add_routes(routes)
    try:
        web.run_app(app, port=config.get('port', 1079))
    except Exception as e:
        shell.print_exception(e)
        sys.exit(1)

if __name__ == '__main__':
    main()
