# Websocket Request/Response Handler
#
# Copyright (C) 2020 Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license

from __future__ import annotations
import logging
import ipaddress
import json
from tornado.ioloop import IOLoop
from tornado.websocket import WebSocketHandler, WebSocketClosedError
from tornado.locks import Lock
from utils import ServerError, SentinelClass

# Annotation imports
from typing import (
    TYPE_CHECKING,
    Any,
    Optional,
    Callable,
    Coroutine,
    Type,
    TypeVar,
    Union,
    Dict,
    List,
)
if TYPE_CHECKING:
    from moonraker import Server
    from app import APIDefinition, MoonrakerApp
    import components.authorization
    _T = TypeVar("_T")
    _C = TypeVar("_C", str, bool, float, int)
    IPUnion = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
    ConvType = Union[str, bool, float, int]
    ArgVal = Union[None, int, float, bool, str]
    RPCCallback = Callable[..., Coroutine]
    AuthComp = Optional[components.authorization.Authorization]

SENTINEL = SentinelClass.get_instance()

class Subscribable:
    def send_status(self, status: Dict[str, Any]) -> None:
        raise NotImplementedError

class WebRequest:
    def __init__(self,
                 endpoint: str,
                 args: Dict[str, Any],
                 action: Optional[str] = "",
                 conn: Optional[Subscribable] = None,
                 ip_addr: str = "",
                 user: Optional[Dict[str, Any]] = None
                 ) -> None:
        self.endpoint = endpoint
        self.action = action or ""
        self.args = args
        self.conn = conn
        self.ip_addr: Optional[IPUnion] = None
        try:
            self.ip_addr = ipaddress.ip_address(ip_addr)
        except Exception:
            self.ip_addr = None
        self.current_user = user

    def get_endpoint(self) -> str:
        return self.endpoint

    def get_action(self) -> str:
        return self.action

    def get_args(self) -> Dict[str, Any]:
        return self.args

    def get_connection(self) -> Optional[Subscribable]:
        return self.conn

    def get_ip_address(self) -> Optional[IPUnion]:
        return self.ip_addr

    def get_current_user(self) -> Optional[Dict[str, Any]]:
        return self.current_user

    def _get_converted_arg(self,
                           key: str,
                           default: Union[SentinelClass, _T],
                           dtype: Type[_C]
                           ) -> Union[_C, _T]:
        if key not in self.args:
            if isinstance(default, SentinelClass):
                raise ServerError(f"No data for argument: {key}")
            return default
        val = self.args[key]
        try:
            if dtype is not bool:
                return dtype(val)
            else:
                if isinstance(val, str):
                    val = val.lower()
                    if val in ["true", "false"]:
                        return True if val == "true" else False  # type: ignore
                elif isinstance(val, bool):
                    return val  # type: ignore
                raise TypeError
        except Exception:
            raise ServerError(
                f"Unable to convert argument [{key}] to {dtype}: "
                f"value recieved: {val}")

    def get(self,
            key: str,
            default: Union[SentinelClass, _T] = SENTINEL
            ) -> Union[_T, Any]:
        val = self.args.get(key, default)
        if isinstance(val, SentinelClass):
            raise ServerError(f"No data for argument: {key}")
        return val

    def get_str(self,
                key: str,
                default: Union[SentinelClass, _T] = SENTINEL
                ) -> Union[str, _T]:
        return self._get_converted_arg(key, default, str)

    def get_int(self,
                key: str,
                default: Union[SentinelClass, _T] = SENTINEL
                ) -> Union[int, _T]:
        return self._get_converted_arg(key, default, int)

    def get_float(self,
                  key: str,
                  default: Union[SentinelClass, _T] = SENTINEL
                  ) -> Union[float, _T]:
        return self._get_converted_arg(key, default, float)

    def get_boolean(self,
                    key: str,
                    default: Union[SentinelClass, _T] = SENTINEL
                    ) -> Union[bool, _T]:
        return self._get_converted_arg(key, default, bool)

class JsonRPC:
    def __init__(self) -> None:
        self.methods: Dict[str, RPCCallback] = {}

    def register_method(self,
                        name: str,
                        method: RPCCallback
                        ) -> None:
        self.methods[name] = method

    def remove_method(self, name: str) -> None:
        self.methods.pop(name, None)

    async def dispatch(self,
                       data: str,
                       ws: WebSocket
                       ) -> Optional[str]:
        response: Any = None
        try:
            request: Union[Dict[str, Any], List[dict]] = json.loads(data)
        except Exception:
            msg = f"Websocket data not json: {data}"
            logging.exception(msg)
            response = self.build_error(-32700, "Parse error")
            return json.dumps(response)
        logging.debug(f"Websocket Request::{data}")
        if isinstance(request, list):
            response = []
            for req in request:
                resp = await self.process_request(req, ws)
                if resp is not None:
                    response.append(resp)
            if not response:
                response = None
        else:
            response = await self.process_request(request, ws)
        if response is not None:
            response = json.dumps(response)
            logging.debug("Websocket Response::" + response)
        return response

    async def process_request(self,
                              request: Dict[str, Any],
                              ws: WebSocket
                              ) -> Optional[Dict[str, Any]]:
        req_id: Optional[int] = request.get('id', None)
        rpc_version: str = request.get('jsonrpc', "")
        method_name = request.get('method', None)
        if rpc_version != "2.0" or not isinstance(method_name, str):
            return self.build_error(-32600, "Invalid Request", req_id)
        method = self.methods.get(method_name, None)
        if method is None:
            return self.build_error(-32601, "Method not found", req_id)
        if 'params' in request:
            params = request['params']
            if isinstance(params, list):
                response = await self.execute_method(
                    method, req_id, ws, *params)
            elif isinstance(params, dict):
                response = await self.execute_method(
                    method, req_id, ws, **params)
            else:
                return self.build_error(-32600, "Invalid Request", req_id)
        else:
            response = await self.execute_method(method, req_id, ws)
        return response

    async def execute_method(self,
                             method: RPCCallback,
                             req_id: Optional[int],
                             ws: WebSocket,
                             *args,
                             **kwargs
                             ) -> Optional[Dict[str, Any]]:
        try:
            result = await method(ws, *args, **kwargs)
        except TypeError as e:
            return self.build_error(
                -32603, f"Invalid params:\n{e}", req_id, True)
        except ServerError as e:
            return self.build_error(e.status_code, str(e), req_id, True)
        except Exception as e:
            return self.build_error(-31000, str(e), req_id, True)

        if req_id is None:
            return None
        else:
            return self.build_result(result, req_id)

    def build_result(self, result: Any, req_id: int) -> Dict[str, Any]:
        return {
            'jsonrpc': "2.0",
            'result': result,
            'id': req_id
        }

    def build_error(self,
                    code: int,
                    msg: str,
                    req_id: Optional[int] = None,
                    is_exc: bool = False
                    ) -> Dict[str, Any]:
        log_msg = f"JSON-RPC Request Error: {code}\n{msg}"
        if is_exc:
            logging.exception(log_msg)
        else:
            logging.info(log_msg)
        return {
            'jsonrpc': "2.0",
            'error': {'code': code, 'message': msg},
            'id': req_id
        }

class WebsocketManager:
    def __init__(self, server: Server) -> None:
        self.server = server
        self.websockets: Dict[int, WebSocket] = {}
        self.ws_lock = Lock()
        self.rpc = JsonRPC()

        self.rpc.register_method("server.websocket.id", self._handle_id_request)

    def register_notification(self,
                              event_name: str,
                              notify_name: Optional[str] = None
                              ) -> None:
        if notify_name is None:
            notify_name = event_name.split(':')[-1]

        async def notify_handler(*args):
            await self.notify_websockets(notify_name, *args)
        self.server.register_event_handler(
            event_name, notify_handler)

    def register_local_handler(self,
                               api_def: APIDefinition,
                               callback: Callable[[WebRequest], Coroutine]
                               ) -> None:
        for ws_method, req_method in \
                zip(api_def.ws_methods, api_def.request_methods):
            rpc_cb = self._generate_local_callback(
                api_def.endpoint, req_method, callback)
            self.rpc.register_method(ws_method, rpc_cb)

    def register_remote_handler(self, api_def: APIDefinition) -> None:
        ws_method = api_def.ws_methods[0]
        rpc_cb = self._generate_callback(api_def.endpoint)
        self.rpc.register_method(ws_method, rpc_cb)

    def remove_handler(self, ws_method: str) -> None:
        self.rpc.remove_method(ws_method)

    def _generate_callback(self, endpoint: str) -> RPCCallback:
        async def func(ws: WebSocket, **kwargs) -> Any:
            result = await self.server.make_request(
                WebRequest(endpoint, kwargs, conn=ws, ip_addr=ws.ip_addr,
                           user=ws.current_user))
            return result
        return func

    def _generate_local_callback(self,
                                 endpoint: str,
                                 request_method: str,
                                 callback: Callable[[WebRequest], Coroutine]
                                 ) -> RPCCallback:
        async def func(ws: WebSocket, **kwargs) -> Any:
            result = await callback(
                WebRequest(endpoint, kwargs, request_method, ws,
                           ip_addr=ws.ip_addr, user=ws.current_user))
            return result
        return func

    async def _handle_id_request(self,
                                 ws: WebSocket,
                                 **kwargs
                                 ) -> Dict[str, int]:
        return {'websocket_id': ws.uid}

    def has_websocket(self, ws_id: int) -> bool:
        return ws_id in self.websockets

    def get_websocket(self, ws_id: int) -> Optional[WebSocket]:
        return self.websockets.get(ws_id, None)

    async def add_websocket(self, ws: WebSocket) -> None:
        async with self.ws_lock:
            self.websockets[ws.uid] = ws
            logging.info(f"New Websocket Added: {ws.uid}")

    async def remove_websocket(self, ws: WebSocket) -> None:
        async with self.ws_lock:
            old_ws = self.websockets.pop(ws.uid, None)
            if old_ws is not None:
                self.server.remove_subscription(old_ws)
                logging.info(f"Websocket Removed: {ws.uid}")

    async def notify_websockets(self,
                                name: str,
                                data: Any = SENTINEL
                                ) -> None:
        msg: Dict[str, Any] = {'jsonrpc': "2.0", 'method': "notify_" + name}
        if data != SENTINEL:
            msg['params'] = [data]
        async with self.ws_lock:
            for ws in list(self.websockets.values()):
                try:
                    ws.write_message(msg)
                except WebSocketClosedError:
                    self.websockets.pop(ws.uid, None)
                    self.server.remove_subscription(ws)
                    logging.info(f"Websocket Removed: {ws.uid}")
                except Exception:
                    logging.exception(
                        f"Error sending data over websocket: {ws.uid}")

    async def close(self) -> None:
        async with self.ws_lock:
            for ws in list(self.websockets.values()):
                ws.close()
            self.websockets = {}

class WebSocket(WebSocketHandler, Subscribable):
    def initialize(self) -> None:
        app: MoonrakerApp = self.settings['parent']
        self.server = app.get_server()
        self.wsm = app.get_websocket_manager()
        self.rpc = self.wsm.rpc
        self.uid = id(self)
        self.is_closed: bool = False
        self.ip_addr: str = self.request.remote_ip

    async def open(self, *args, **kwargs) -> None:
        await self.wsm.add_websocket(self)

    def on_message(self, message: Union[bytes, str]) -> None:
        io_loop = IOLoop.current()
        io_loop.spawn_callback(self._process_message, message)

    async def _process_message(self, message: str) -> None:
        try:
            response = await self.rpc.dispatch(message, self)
            if response is not None:
                self.write_message(response)
        except Exception:
            logging.exception("Websocket Command Error")

    def send_status(self, status: Dict[str, Any]) -> None:
        if not status or self.is_closed:
            return
        try:
            self.write_message({
                'jsonrpc': "2.0",
                'method': "notify_status_update",
                'params': [status]})
        except WebSocketClosedError:
            self.is_closed = True
            logging.info(
                f"Websocket Closed During Status Update: {self.uid}")
        except Exception:
            logging.exception(
                f"Error sending data over websocket: {self.uid}")

    def on_close(self) -> None:
        self.is_closed = True
        io_loop = IOLoop.current()
        io_loop.spawn_callback(self.wsm.remove_websocket, self)

    def check_origin(self, origin: str) -> bool:
        if not super(WebSocket, self).check_origin(origin):
            auth: AuthComp = self.server.lookup_component('authorization', None)
            if auth is not None:
                return auth.check_cors(origin)
            return False
        return True

    # Check Authorized User
    def prepare(self):
        auth: AuthComp = self.server.lookup_component('authorization', None)
        if auth is not None:
            self.current_user = auth.check_authorized(self.request)
