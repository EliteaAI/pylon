#!/usr/bin/python3
# coding=utf-8

#   Copyright 2026 EPAM Systems
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

""" Server """

import threading
from queue import SimpleQueue, Empty

import arbiter  # pylint: disable=E0401
import janus  # pylint: disable=E0401

import socketio  # pylint: disable=E0401
from socketio.pubsub_manager import PubSubManager  # pylint: disable=E0401
from socketio.async_pubsub_manager import AsyncPubSubManager  # pylint: disable=E0401

from pylon.core.tools import log
from pylon.core.tools import db_support


def create_socketio_instance(context):  # pylint: disable=R0914,R0912,R0915
    """ Create SocketIO instance """
    client_manager = create_client_manager(context)
    socketio_config = context.settings.get("socketio", {})
    #
    sio_kwargs = {}
    for arg_item in ["transports"]:
        if arg_item in socketio_config:
            sio_kwargs[arg_item] = socketio_config[arg_item]
    #
    if context.web_runtime == "gevent":
        context.sio = SIOPatchedServer(
            async_mode="gevent",
            client_manager=client_manager,
            cors_allowed_origins=socketio_config.get("cors_allowed_origins", "*"),
            **sio_kwargs,
        )
    elif context.web_runtime == "hypercorn":
        context.sio_async = SIOPatchedAsyncServer(
            async_mode="asgi",
            client_manager=client_manager,
            cors_allowed_origins=socketio_config.get("cors_allowed_origins", "*"),
            **sio_kwargs,
        )
        context.sio = SIOAsyncProxy(context)
    elif context.web_runtime == "waitress":
        context.sio = SIOPatchedServer(
            allow_upgrades=True,
            async_mode="threading",
            client_manager=client_manager,
            cors_allowed_origins=socketio_config.get("cors_allowed_origins", "*"),
            **sio_kwargs,
        )
    else:  # flask
        context.sio = SIOPatchedServer(
            async_mode="threading",
            client_manager=client_manager,
            cors_allowed_origins=socketio_config.get("cors_allowed_origins", "*"),
            **sio_kwargs,
        )


def create_client_manager(context):  # pylint: disable=R0912,R0914,R0915
    """ Make client_manager instance """
    client_manager = None
    #
    socketio_config = context.settings.get("socketio", {})
    #
    socketio_event_node = socketio_config.get("event_node", {})
    socketio_rabbitmq = socketio_config.get("rabbitmq", {})
    socketio_redis = socketio_config.get("redis", {})
    #
    socketio_channel = socketio_config.get("channel", "socketio")
    #
    if socketio_event_node:
        # Note: currently there is no way to properly close entities on shutdown
        try:
            event_node = arbiter.make_event_node(config=socketio_event_node)
            event_node.start()
            #
            if context.is_async:
                publish_queue = janus.Queue()
                listen_queue = janus.Queue()
                #
                forwarder = SIOSyncEventNodeForwarder(
                    event_node, socketio_channel,
                    publish_queue=publish_queue.sync_q,
                    listen_queue=listen_queue.sync_q,
                )
                forwarder.start()
                #
                client_manager = SIOAsyncEventNodeManager(
                    event_node,
                    publish_queue=publish_queue.async_q,
                    listen_queue=listen_queue.async_q,
                    channel=socketio_channel,
                )
            else:
                client_manager = SIOEventNodeManager(event_node, channel=socketio_channel)
        except:  # pylint: disable=W0702
            log.exception("Cannot make EventNodeManager instance, SocketIO is in standalone mode")
    elif socketio_rabbitmq:
        try:
            host = socketio_rabbitmq.get("host")
            port = socketio_rabbitmq.get("port", 5672)
            user = socketio_rabbitmq.get("user", "")
            password = socketio_rabbitmq.get("password", "")
            vhost = socketio_rabbitmq.get("vhost", "carrier")
            queue = socketio_rabbitmq.get("queue", "socketio")
            #
            url = f'ampq://{user}:{password}@{host}:{port}/{vhost}'
            #
            if context.is_async:
                client_manager = socketio.AsyncAioPikaManager(
                    url=url, channel=queue,
                )
            else:
                client_manager = socketio.KombuManager(
                    url=url, channel=queue,
                )
        except:  # pylint: disable=W0702
            log.exception("Cannot make KombuManager instance, SocketIO is in standalone mode")
    elif socketio_redis:
        try:
            host = socketio_redis.get("host")
            port = socketio_redis.get("port", 6379)
            password = socketio_redis.get("password", "")
            database = socketio_redis.get("database", 0)
            queue = socketio_redis.get("queue", "socketio")
            use_ssl = socketio_redis.get("use_ssl", False)
            username = socketio_redis.get("username", "")
            #
            if password is None:
                password = ""
            #
            if username is None:
                username = ""
            #
            username_password = ""
            #
            if password or username:
                username_password = ":".join([username, password])
                username_password = f"{username_password}@"
            #
            scheme = "rediss" if use_ssl else "redis"
            url = f'{scheme}://{username_password}{host}:{port}/{database}'
            #
            if context.is_async:
                client_manager = socketio.AsyncRedisManager(
                    url=url, channel=queue,
                )
            else:
                client_manager = socketio.RedisManager(
                    url=url, channel=queue,
                )
            # NOTE: no managed identity support
        except:  # pylint: disable=W0702
            log.exception("Cannot make RedisManager instance, SocketIO is in standalone mode")
    #
    return client_manager


class SIOSyncEventNodeForwarder(threading.Thread):
    """ Send/Receive to/from queues """

    def __init__(self, event_node, socketio_channel, publish_queue, listen_queue):
        super().__init__(daemon=True)
        #
        self.event_node = event_node
        self.socketio_channel = socketio_channel
        #
        self.publish_queue = publish_queue
        self.listen_queue = listen_queue
        #
        self.queue_get_timeout = 1
        #
        self.event_node.subscribe("socketio_manager_data", self.__on_manager_data)

    def run(self):
        """ Thread entrypoint """
        while not self.event_node.stop_event.is_set():
            try:
                data = self.publish_queue.get(timeout=self.queue_get_timeout)
                #
                self.event_node.emit(
                    "socketio_manager_data",
                    {
                        "channel": self.socketio_channel,
                        "data": data,
                    },
                )
            except Empty:
                pass
            except:  # pylint: disable=W0702
                log.exception("Error during data publishing, skipping")

    def __on_manager_data(self, _event_name, event_payload):
        if not isinstance(event_payload, dict):
            return
        #
        if event_payload.get("channel", None) != self.socketio_channel:
            return
        #
        if event_payload.get("data", None) is None:
            return
        #
        self.listen_queue.put(event_payload.get("data"))


class SIOAsyncEventNodeManager(AsyncPubSubManager):  # pylint: disable=R0903
    """ Pylon SocketIO EventNode-based AsyncPubSubManager """

    def __init__(  # pylint: disable=R0913
            self, event_node, publish_queue, listen_queue,
            channel="socketio", write_only=False, logger=None,
    ):
        super().__init__(channel=channel, write_only=write_only, logger=logger)
        #
        self.event_node = event_node
        self.channel = channel
        #
        self.publish_queue = publish_queue
        self.listen_queue = listen_queue

    async def _publish(self, data):
        await self.publish_queue.put(data)

    async def _listen(self):
        while True:
            data = await self.listen_queue.get()
            yield data


class SIOEventNodeManager(PubSubManager):  # pylint: disable=R0903
    """ Pylon SocketIO EventNode-based PubSubManager """

    def __init__(self, event_node, channel="socketio", write_only=False, logger=None):
        super().__init__(channel=channel, write_only=write_only, logger=logger)
        #
        self.event_node = event_node
        self.channel = channel
        #
        self.data_queue = SimpleQueue()
        self.queue_get_timeout = 1
        #
        self.event_node.subscribe("socketio_manager_data", self.__on_manager_data)

    def __on_manager_data(self, _event_name, event_payload):
        if not isinstance(event_payload, dict):
            return
        #
        if event_payload.get("channel", None) != self.channel:
            return
        #
        if event_payload.get("data", None) is None:
            return
        #
        self.data_queue.put(event_payload.get("data"))

    def _publish(self, data):
        self.event_node.emit(
            "socketio_manager_data",
            {
                "channel": self.channel,
                "data": data,
            },
        )

    def _listen(self):
        while not self.event_node.stop_event.is_set():
            try:
                data = self.data_queue.get(timeout=self.queue_get_timeout)
                yield data
            except Empty:
                pass
            except:  # pylint: disable=W0702
                log.exception("Error during data listening, skipping")


class SIOEventHandler:  # pylint: disable=R0903
    """ Pylon SocketIO Event handler """

    def __init__(self):
        self.handlers = []

    def __call__(self, *args, **kwargs):
        """ Call handler """
        for handler in self.handlers:
            try:
                handler(*args, **kwargs)
            except:  # pylint: disable=W0702
                log.exception("Failed to run SIO event handler '%s', skipping", handler)


class SIOPatchedServer(socketio.Server):  # pylint: disable=R0903
    """ SockerIO Server patched for Pylon """

    def __init__(self, *args, **kwargs):
        self.pylon_emit_queue = SimpleQueue()
        self.pylon_emitting_thread = threading.Thread(
            target=self.pylon_emitting_worker,
            daemon=True,
        )
        #
        self.pylon_any_handlers = []
        self.pylon_event_handlers = {}
        #
        self.pylon_emitting_thread.start()
        #
        super().__init__(*args, **kwargs)

    def emit(self, *args, **kwargs):
        """ Lock and emit() """
        emit_data = (args, kwargs)
        self.pylon_emit_queue.put(emit_data)

    def on(self, event, handler=None, namespace=None):  # pylint: disable=C0103
        """ Register an event handler """
        namespace = namespace or '/'
        #
        def _on(handler):
            #
            # Pylon part
            #
            if namespace not in self.pylon_event_handlers:
                self.pylon_event_handlers[namespace] = {}
            #
            if event not in self.pylon_event_handlers[namespace]:
                self.pylon_event_handlers[namespace][event] = SIOEventHandler()
            #
            if handler not in self.pylon_event_handlers[namespace][event].handlers:
                self.pylon_event_handlers[namespace][event].handlers.append(handler)
            #
            # SIO part
            #
            if namespace not in self.handlers:
                self.handlers[namespace] = {}
            #
            if event not in self.handlers[namespace]:
                self.handlers[namespace][event] = self.pylon_event_handlers[namespace][event]
            #
            return handler
        #
        if handler is None:
            return _on
        #
        _on(handler)

    def remove_handler(self, event, handler, namespace=None):
        """ Remove handler """
        namespace = namespace or "/"
        #
        if namespace not in self.handlers:
            return
        #
        if event not in self.handlers[namespace]:
            return
        #
        if handler not in self.handlers[namespace][event].handlers:
            return
        #
        self.handlers[namespace][event].handlers.remove(handler)

    def _trigger_event(self, event, namespace, *args):
        """ Call *any* handlers first """
        db_support.create_local_session()
        try:
            for any_handler in self.pylon_any_handlers:
                try:
                    any_handler(event, namespace, args)
                except:  # pylint: disable=W0702
                    log.exception("Failed to run SIO *any* handler, skipping")
            #
            return super()._trigger_event(event, namespace, *args)
        finally:
            db_support.close_local_session()

    def pylon_trigger_event(self, event, namespace, *args):
        """ Call original handlers """
        return super()._trigger_event(event, namespace, *args)

    def pylon_add_any_handler(self, handler):
        """ Add *any* handler """
        if handler in self.pylon_any_handlers:
            return
        #
        self.pylon_any_handlers.append(handler)

    def pylon_remove_any_handler(self, handler):
        """ Remove *any* handler """
        if handler not in self.pylon_any_handlers:
            return
        #
        self.pylon_any_handlers.remove(handler)

    def pylon_emitting_worker(self):
        """ Perform emits """
        while True:
            try:
                args, kwargs = self.pylon_emit_queue.get()
                super().emit(*args, **kwargs)
            except:  # pylint: disable=W0702
                log.exception("Failed to emit SIO event")


class SIOAsyncEventHandler:  # pylint: disable=R0903
    """ Pylon SocketIO Async Event handler """

    def __init__(self):
        self.handlers = []

    async def __call__(self, *args, **kwargs):
        """ Call handler """
        import asyncio  # pylint: disable=E0401,C0412,C0415
        #
        for handler in self.handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(*args, **kwargs)
                else:
                    handler(*args, **kwargs)
            except:  # pylint: disable=W0702
                log.exception("Failed to run SIO event handler '%s', skipping", handler)


class SIOPatchedAsyncServer(socketio.AsyncServer):  # pylint: disable=R0903
    """ SockerIO AsyncServer patched for Pylon """

    def __init__(self, *args, **kwargs):
        self.pylon_any_handlers = []  # 'public', expected to be used by proxy
        self.pylon_event_handlers = {}
        #
        super().__init__(*args, **kwargs)

    def on(self, event, handler=None, namespace=None):  # pylint: disable=C0103
        """ Register an event handler """
        namespace = namespace or '/'
        #
        def _on(handler):
            #
            # Pylon part
            #
            if namespace not in self.pylon_event_handlers:
                self.pylon_event_handlers[namespace] = {}
            #
            if event not in self.pylon_event_handlers[namespace]:
                self.pylon_event_handlers[namespace][event] = SIOAsyncEventHandler()
            #
            if handler not in self.pylon_event_handlers[namespace][event].handlers:
                self.pylon_event_handlers[namespace][event].handlers.append(handler)
            #
            # SIO part
            #
            if namespace not in self.handlers:
                self.handlers[namespace] = {}
            #
            if event not in self.handlers[namespace]:
                self.handlers[namespace][event] = self.pylon_event_handlers[namespace][event]
            #
            return handler
        #
        if handler is None:
            return _on
        #
        _on(handler)

    def remove_handler(self, event, handler, namespace=None):
        """ Remove handler """
        namespace = namespace or "/"
        #
        if namespace not in self.handlers:
            return
        #
        if event not in self.handlers[namespace]:
            return
        #
        if handler not in self.handlers[namespace][event].handlers:
            return
        #
        self.handlers[namespace][event].handlers.remove(handler)

    async def _trigger_event(self, event, namespace, *args):
        """ Call *any* handlers first """
        import asyncio  # pylint: disable=E0401,C0412,C0415
        #
        db_support.create_local_session()
        try:
            for any_handler in self.pylon_any_handlers:
                try:
                    if asyncio.iscoroutinefunction(any_handler):
                        await any_handler(event, namespace, args)
                    else:
                        any_handler(event, namespace, args)
                except:  # pylint: disable=W0702
                    log.exception("Failed to run SIO *any* handler, skipping")
            #
            return await super()._trigger_event(event, namespace, *args)
        finally:
            db_support.close_local_session()

    async def pylon_trigger_event(self, event, namespace, *args):
        """ Call original handlers """
        return await super()._trigger_event(event, namespace, *args)


class SIOAsyncProxy:  # pylint: disable=R0903
    """ Sync proxy to SockerIO AsyncServer """

    def __init__(self, context):
        self.context = context
        #
        self._any_async_handlers = {}  # handler -> async version
        #
        import asgiref.sync  # pylint: disable=E0401,C0412,C0415
        self.__sync_emit = asgiref.sync.AsyncToSync(
            self.context.sio_async.emit
        )
        self.__sync_trigger_event = asgiref.sync.AsyncToSync(
            self.context.sio_async.pylon_trigger_event
        )
        self.__sync_enter_room = asgiref.sync.AsyncToSync(
            self.context.sio_async.enter_room
        )
        self.__sync_leave_room = asgiref.sync.AsyncToSync(
            self.context.sio_async.leave_room
        )

    @staticmethod
    def _in_async():
        try:
            import asyncio  # pylint: disable=E0401,C0412,C0415
            event_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            if event_loop.is_running():
                return True
        #
        return False

    def __getattr__(self, name):
        if self._in_async():
            if hasattr(self, f"_async_{name}"):
                return getattr(self, f"_async_{name}")
        elif hasattr(self, f"_sync_{name}"):
            return getattr(self, f"_sync_{name}")
        #
        log.warning("[SIOAsyncProxy NotImplemented] %s", name)
        raise AttributeError

    def on(self, event, handler=None, namespace=None):  # pylint: disable=C0103
        """ Proxy method """
        return self.context.sio_async.on(event, handler, namespace)

    def remove_handler(self, event, handler, namespace=None):
        """ Proxy method """
        return self.context.sio_async.remove_handler(event, handler, namespace)

    async def _async_emit(self, *args, **kwargs):
        """ Proxy method """
        # FIXME: emit() is not thread-safe and can be re-entered
        return await self.context.sio_async.emit(*args, **kwargs)

    def _sync_emit(self, *args, **kwargs):
        """ Proxy method """
        # FIXME: emit() can be re-entered
        return self.__sync_emit(*args, **kwargs)

    def enter_room(self, *args, **kwargs):
        """ Proxy method """
        return self.__sync_enter_room(*args, **kwargs)

    def leave_room(self, *args, **kwargs):
        """ Proxy method """
        return self.__sync_leave_room(*args, **kwargs)

    def pylon_trigger_event(self, event, namespace, *args):
        """ Call original handlers """
        return self.__sync_trigger_event(event, namespace, *args)

    def pylon_add_any_handler(self, handler):
        """ Add *any* handler """
        if handler in self._any_async_handlers:
            return
        #
        import asgiref.sync  # pylint: disable=E0401,C0412,C0415
        async_handler = asgiref.sync.SyncToAsync(handler)
        #
        self._any_async_handlers[handler] = async_handler
        self.context.sio_async.pylon_any_handlers.append(async_handler)

    def pylon_remove_any_handler(self, handler):
        """ Remove *any* handler """
        if handler not in self._any_async_handlers:
            return
        #
        async_handler = self._any_async_handlers.pop(handler)
        self.context.sio_async.pylon_any_handlers.remove(async_handler)
