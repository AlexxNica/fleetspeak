# Copyright 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Python library to communicate with Fleetspeak over grpc."""

import abc
import os
import threading
import time

from concurrent import futures

import grpc

import gflags
import logging

from fleetspeak.src.common.proto.fleetspeak import common_pb2
from fleetspeak.src.server.grpcservice.proto.fleetspeak_grpcservice import grpcservice_pb2_grpc
from fleetspeak.src.server.proto.fleetspeak_server import admin_pb2_grpc

FLAGS = gflags.FLAGS

gflags.DEFINE_string(
    "fleetspeak_message_listen_address", "",
    "The address to bind to, to listen for fleetspeak messages.")
gflags.DEFINE_string("fleetspeak_server", "",
                    "The address to find the fleetspeak admin server, e.g. "
                    "'localhost:8080'")


class Servicer(grpcservice_pb2_grpc.ProcessorServicer):
  """A wrapper to collect messages from incoming grpcs.

  This implementation of grpcservice_pb2_grpc.ProcessorServicer, it passes all
  received messages into a provided callback, after performing some basic sanity
  checking.

  Note that messages may be delivered twice.
  """

  def __init__(self, process_callback, service_name, **kwargs):
    """Create a Servicer.

    Args:
      process_callback: A callback to be executed when a message arrives.  Will
        be called as process_callback(msg, context) where msg is a
        common_pb2.Message and context is a grpc.ServicerContext.  Must be
        thread safe.
      service_name: The name of the service that we are running as.  Used to
        sanity check the destination address of received messages.
      **kwargs: Extra arguments passed to the constructor of the base
        class, grpcservice_pb2_grpc.ProcessorServicer.
    """
    super(Servicer, self).__init__(**kwargs)
    self._process_callback = process_callback
    self._service_name = service_name

  def Process(self, request, context):
    if not isinstance(request, common_pb2.Message):
      logging.error("Received unexpected request type: %s",
                    request.__class__.__name__)
      context.set_code(grpc.StatusCode.UNKNOWN)
      return common_pb2.EmptyMessage()
    if request.destination.client_id:
      logging.error("Received message for client: %s",
                    request.destination.client_id)
      context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
      return common_pb2.EmptyMessage()
    if request.destination.service_name != self._service_name:
      logging.error("Received message for unknown service: %s",
                    request.destination.service_name)
      context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
      return common_pb2.EmptyMessage()

    self._process_callback(request, context)
    return common_pb2.EmptyMessage()


class InvalidArgument(Exception):
  """Exception indicating unexpected input."""


class NotConfigured(Exception):
  """Exception indicating that the requested operation is not configured."""


class Sender(object):
  """A wrapper to send messages to a Fleetspeak server using grpc calls.

  This wrapper around a grpc channel makes calls to a Fleetspeak administrative
  interface to send messages to a fleetspeak service.
  """

  SEND_TIMEOUT = 30  # seconds

  def __init__(self, channel, service_name, stub=None):
    """Create a Sender.

    Args:
      channel: The grpc.Channel over which we should send messages.
      service_name: The name of the service that we are running as.
      stub: If set, used instead of AdminStub(channel). Intended to ease
        unit tests.
    """
    if stub:
      self._stub = stub
    else:
      self._stub = admin_pb2_grpc.AdminStub(channel)

    self._service_name = service_name

    self._shutdown = False
    self._shutdown_cv = threading.Condition()
    self._keep_alive_thread = threading.Thread(target=self._KeepAliveLoop)
    self._keep_alive_thread.daemon = True
    self._keep_alive_thread.start()

  def _KeepAliveLoop(self):
    try:
      while True:
        with self._shutdown_cv:
          if self._shutdown:
            return
          self._shutdown_cv.wait(timeout=5)
          if self._shutdown:
            return
        try:
          self._stub.KeepAlive(common_pb2.EmptyMessage(), timeout=1.0)
        except grpc.RpcError as e:
          logging.warning("KeepAlive rpc failed: %s", e)
    except Exception as e:  # pylint: disable=broad-except
      logging.error("Exception in KeepAlive: %s", e)

  def Send(self, message):
    """Sends a message to the Fleetspeak server.

    Args:
      message: common_pb2.Message
        The message to send.

    Raises:
      grpc.RpcError: if the RPC fails.
      InvalidArgument: if message is not a common_pb2.Message.
    """
    if not isinstance(message, common_pb2.Message):
      raise InvalidArgument("Attempt to send unexpected message type: %s" %
                            message.__class__.__name__)

    message.source.service_name = self._service_name
    message.source.ClearField("client_id")

    # TODO: Remove retry logic when possible.
    #
    # Sometimes GRPC reports failure, even though the call succeeded. To prevent
    # retry logic from creating duplicate messages we fix the message_id.

    if not message.message_id:
      message.message_id = os.urandom(32)

    deadline = time.time() + self.SEND_TIMEOUT
    timeout = self.SEND_TIMEOUT
    sleep = 1
    while True:
      try:
        self._stub.InsertMessage(message, timeout=timeout)
        return
      except grpc.RpcError:
        timeout = deadline - time.time()
        if time.time() + sleep > timeout:
          raise
        sleep *= 2
        time.sleep(sleep)
        timeout = deadline - time.time()

  def Shutdown(self):
    with self._shutdown_cv:
      self._shutdown = True
      self._shutdown_cv.notify()
    self._keep_alive_thread.join()


class ServiceClient(object):
  """Bidirectional connection to Fleetspeak.

  This abstract class can be used to represent a bidirectional connection with
  fleetspeak. Users of this library are encourage to select (or provide) an
  implementation of this according to their grpc connection requirements.
  """

  __metaclass__ = abc.ABCMeta

  @abc.abstractmethod
  def __init__(
      self,
      service_name,):
    """Abstract constructor for ServiceClient.

    Args:
      service_name: string; The Fleetspeak service name to communicate with.
    """

  @abc.abstractmethod
  def Send(self, message):
    """Sends a message to the Fleetspeak server."""

  @abc.abstractmethod
  def Listen(self, process_callback):
    """Listens to messages from the Fleetspeak server.

    Args:
      process_callback: A callback to be executed when a messages arrives from
        the Fleetspeak server. See the process argument of Servicer.__init__.
    """


class InsecureGRPCServiceClient(ServiceClient):
  """An insecure bidirectional connection to Fleetspeak.

  This class implements ServiceClient by creating insecure grpc connections.  It
  is meant primarily for integration testing.
  """

  def __init__(self,
               service_name,
               fleetspeak_message_listen_address=None,
               fleetspeak_server=None,
               threadpool_size=5):
    """Constructor.

    Args:
      service_name: string The name of the service to communicate as.
      fleetspeak_message_listen_address: string
          The connection's read end address. If unset, the argv flag
          fleetspeak_message_listen_address will be used. If still unset, the
          connection will not be open for reading and Listen() will raise
          NotConfigured.
      fleetspeak_server: string
          The connection's write end address. If unset, the argv flag
          fleetspeak_server will be used. If still unset, the connection will
          not be open for writing and Send() will raise NotConfigured.
      threadpool_size: int
          The number of threads to use to process messages.

    Raises:
      NotConfigured:
          If both fleetspeak_message_listen_address and fleetspeak_server are
          unset.
    """
    super(InsecureGRPCServiceClient, self).__init__(service_name)

    if fleetspeak_message_listen_address is None:
      fleetspeak_message_listen_address = (
          FLAGS.fleetspeak_message_listen_address or None)

    if fleetspeak_server is None:
      fleetspeak_server = FLAGS.fleetspeak_server or None

    if fleetspeak_message_listen_address is None and fleetspeak_server is None:
      raise NotConfigured(
          "At least one of the arguments (fleetspeak_message_listen_address, "
          "fleetspeak_server) has to be provided.")

    self._service_name = service_name
    self._listen_address = fleetspeak_message_listen_address
    self._threadpool_size = threadpool_size

    if fleetspeak_server is None:
      logging.info(
          "fleetspeak_server is unset, not creating outbound connection to "
          "fleetspeak.")
      self._sender = None
    else:
      channel = grpc.insecure_channel(fleetspeak_server)
      self._sender = Sender(channel, service_name)
      logging.info("Fleetspeak GRPCService client connected to %s",
                   fleetspeak_server)

  def Send(self, message):
    if self._sender is None:
      raise NotConfigured("Send address not provided.")
    self._sender.Send(message)

  def Listen(self, process):
    if self._listen_address is None:
      raise NotConfigured("Listen address not provided.")
    self._server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=self._threadpool_size))
    self._server.add_insecure_port(self._listen_address)
    servicer = Servicer(process, self._service_name)
    grpcservice_pb2_grpc.add_ProcessorServicer_to_server(servicer, self._server)
    self._server.start()
    logging.info("Fleetspeak GRPCService client listening on %s",
                 self._listen_address)
