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
import mimetypes
from aiohttp import web

if __name__ == '__main__':
    import inspect
    file_path = os.path.dirname(os.path.realpath(inspect.getfile(inspect.currentframe())))
    sys.path.insert(0, os.path.join(file_path, '../'))

from shadowsocks import common, eventloop, tcprelay, udprelay, asyncdns, shell

def get_key_from_config(config):
    return '%s:%d' % (config['server'], config['server_port'])

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
        server = get_key_from_config(config)
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
            server = get_key_from_config(config)
            logging.info("removing server at %s, local port %d" % (server, local_port))
            t, u, _ = servers
            t.close(next_tick=False)
            u.close(next_tick=False)
            del self._relays[server]
            del self._relays[local_port]
            return True
        else:
            logging.error("server not exist at local port %d" % local_port)
            return False

    def create_callback(self, req):
        called = False
        def callback(data=None):
            nonlocal called
            qi = req['qi']
            if called or qi is None: return
            self.write({
                'qi': qi,
                'q': req['q'],
                'd': data,
            })
            called = True
        return callback

    def handle_event(self, sock, fd, event):
        if sock == self.r:
            while True:
                line = self.r.readline().strip()
                if not line: break
                req = json.loads(line)
                callback = self.create_callback(req)
                self.handle_data(req['q'], req.get('d'), callback)
                callback()

    def handle_data(self, q, d, callback):
        logging.debug('manager: %s %s' % (q, json.dumps(d)))
        if q == 'meta':
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
                callback(result)
        elif q == 'add':
            config = d
            try:
                self.add_port(config)
            except ValueError as e:
                logging.error('invalid config: %s' % e)
                callback(False)
            else:
                callback(True)
        elif q == 'remove':
            callback(self.remove_port(d))
        elif q == 'refreshdns':
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
    def __init__(self, pid, filename):
        self.pid = pid
        self.data = {}
        self.filename = filename
        try:
            self.config = json.load(open(filename))
        except:
            self.config = {}
        self.config.setdefault('verbose', False)
        for item in self.config.setdefault('servers', []):
            item.pop('verbose', None)
            item.setdefault('key', get_key_from_config(item['config']))

    def dump(self):
        json.dump(self.config, open(self.filename, 'w'), indent=2)

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
        asyncio.ensure_future(self.initialize_servers())

    async def initialize_servers(self):
        for item in self.config['servers']:
            if item['enabled']:
                config = item['config'].copy()
                if not await self.add_config(config):
                    item['enabled'] = False
        self.dump()

    def write(self, data):
        self.writer.write(json.dumps(data).encode())
        self.writer.write(b'\n')

    _id = 0
    futures = {}
    def send_message(self, q, d=None):
        self._id += 1
        qi = self._id
        data = {
            'qi': qi,
            'q': q,
            'd': d,
        }
        future = asyncio.Future()
        self.futures[qi] = future
        self.write(data)
        return future

    async def handle(self):
        while True:
            line = await self.reader.readline()
            line = line.strip()
            if not line: break
            data = json.loads(line)
            self.handle_data(data)

    def handle_data(self, data):
        qi = data.pop('qi')
        if qi is not None:
            future = self.futures.pop(qi)
            if future is None:
                return
            future.set_result(data['d'])
            return

    @staticmethod
    async def create(r, w, pid, filename):
        handler = MonitorHandler(pid, filename)
        await handler.initialize(r, w)
        return handler

    async def refresh_meta(self):
        while True:
            await asyncio.sleep(2)
            meta = await self.send_message('meta')
            self.data['meta'] = meta

    async def refresh_dns(self):
        while True:
            await asyncio.sleep(60)
            await self.send_message('refreshdns')

    def start(self):
        asyncio.ensure_future(self.handle())
        asyncio.ensure_future(self.refresh_meta())
        asyncio.ensure_future(self.refresh_dns())

    async def add_config(self, config):
        config = config.copy()
        config['verbose'] = self.config['verbose']
        return await self.send_message('add', config)

    async def remove_config(self, config):
        return await self.send_message('remove', config['local_port'])

    async def add(self, config):
        item = {
            'key': get_key_from_config(config),
            'enabled': True,
            'config': config,
        }
        self.config['servers'].append(item)
        item['enabled'] = await self.add_config(config)
        self.dump()
        return item['enabled']

    async def toggle(self, key, enabled):
        for item in self.config['servers']:
            if key == item['key']:
                do_toggle = self.remove_config if item['enabled'] else self.add_config
                if await do_toggle(item['config']):
                    item['enabled'] = not item['enabled']
                    self.dump()
                return item['enabled']

    async def remove(self, key):
        for i, item in enumerate(self.config['servers']):
            if key == item['key']:
                if await self.remove_config(item['config']):
                    item['enabled'] = False
                break
        else:
            i = None
        if i is not None and not self.config['servers'][i]['enabled']:
            del self.config['servers'][i]
            self.dump()

routes = web.RouteTableDef()

@routes.post('/rpc/query')
def rpc_query(request):
    data = monitor.data.copy()
    data['pid'] = monitor.pid
    data['config'] = monitor.config
    return web.json_response(dict(data=data))

@routes.post('/rpc/add')
async def rpc_add(request):
    payload = await request.json()
    data = await monitor.add(payload['config'])
    return web.json_response(dict(data=data))

@routes.post('/rpc/toggle')
async def rpc_toggle(request):
    payload = await request.json()
    data = await monitor.toggle(payload['key'], payload['enabled'])
    return web.json_response(dict(data=data))

@routes.post('/rpc/remove')
async def rpc_remove(request):
    payload = await request.json()
    data = await monitor.remove(payload['key'])
    return web.json_response(dict(data=data))

root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../static'))

@routes.get(r'/{pathname:.*}')
def serve_csv(request):
    pathname = request.match_info['pathname']
    if pathname == '':
        pathname = 'index.html'
    full_path = f'{root_dir}/{pathname}'
    try:
        last_modified_time = os.path.getmtime(full_path)
        etag = f'W/"{last_modified_time}"'
        mimetype, _encoding = mimetypes.guess_type(pathname)
        cache_headers = {
            'Etag': etag,
            'Vary': 'Origin',
        }
        kw = {
            'headers': cache_headers,
            'content_type': mimetype,
        }
        if etag in map(str.strip, request.headers.get('If-None-Match', '').split(',')):
            return web.HTTPNotModified(**kw)
        raw = open(full_path, 'rb').read()
        return web.Response(body=raw, **kw)
    except OSError:
        raise web.HTTPNotFound

async def initialize():
    global monitor
    r1, w1 = os.pipe()
    r2, w2 = os.pipe()
    pid = os.fork()
    if pid == 0:
        mng = Manager(r1, w2)
        mng.run()
        return
    monitor = await MonitorHandler.create(r2, w1, pid, 'user-config-list.json')
    monitor.start()
    return monitor

def main():
    loop = asyncio.get_event_loop()
    monitor = loop.run_until_complete(initialize())
    app = web.Application()
    app.add_routes(routes)
    try:
        web.run_app(app, port=monitor.config.get('port', 1079))
    except Exception as e:
        shell.print_exception(e)
        sys.exit(1)

if __name__ == '__main__':
    main()
