# swank_client.py
import json
import socket
import struct
import threading
import queue
import logging
import os
from pathlib import Path

# --- Constants ---
ABORT = (None, None)
MAXIMUM_THREAD_COUNT = 10000
SLIME_PROTOCOL_VERSION = "20210223"  # Пример, может отличаться

# --- Global Variables ---
THREAD_OFFSET = 0
OPEN_CONNECTIONS = []
CONNECTIONS_LOCK = threading.Lock()

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Helper Functions ---

def destructure_case(event, handlers):
    """Simulates Lisp's destructure-case for event dispatching."""
    if isinstance(event, list) and len(event) > 0:
        head = event[0]
        args = event[1:]
        handler = handlers.get(head)
        if handler:
            try:
                return handler(*args)
            except TypeError as e:
                logger.error(f"Handler {handler} failed for event {event}: {e}")
                return None
    logger.warning(f"No handler found for event: {event}")
    return None

def read_string_from_socket(sock, length):
    """Reads exactly 'length' bytes from the socket."""
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise EOFError("Socket connection broken while reading message")
        data += chunk
    return data

# --- Main Classes ---

class SwankConnection:
    def __init__(self, host_name, port, usocket):
        global THREAD_OFFSET
        self.host_name = host_name
        self.port = port
        self.usocket = usocket
        with CONNECTIONS_LOCK:
            self.thread_offset = THREAD_OFFSET
            THREAD_OFFSET += MAXIMUM_THREAD_COUNT
        self.continuation_counter = 0
        self.rex_continuations = {}  # {id: continuation_func}
        self.state = 'alive'  # 'alive', 'closing', 'dead'
        self.connection_lock = threading.RLock() # Reentrant lock for internal state
        self.dispatch_thread = None
        self.eval_results = {} # {id: result_queue} for sync evals
        self.eval_events = {} # {id: threading.Event} for sync evals

    def _slime_net_encode_length(self, n):
        """Encodes an integer as a 6-character, 24-bit hex string."""
        return f"{n:06X}"

    def _slime_net_send(self, sexp):
        """Sends SEXP to the Swank server over the socket."""
        try:
            payload = json.dumps(sexp)
            utf8_payload = payload.encode('utf-8')
            # The payload includes an encoded newline character at the end.
            payload_length = len(utf8_payload) + 1
            hex_length = self._slime_net_encode_length(payload_length)
            utf8_length = hex_length.encode('utf-8')

            message = utf8_length + utf8_payload + b'\n'
            self.usocket.sendall(message)
        except socket.error as e:
            logger.error(f"Network error in _slime_net_send: {e}")
            raise # Let caller handle network errors

    def slime_send(self, sexp):
        """Sends SEXP to the Swank server using the connection."""
        self._slime_net_send(sexp)
        try:
            self.usocket.flush() # Force output
        except socket.error as e:
            logger.error(f"Network error flushing output in slime_send: {e}")
            raise

    def _slime_net_read(self):
        """Reads a Swank message from the network connection."""
        try:
            length_hex = read_string_from_socket(self.usocket, 6).decode('utf-8')
            length = int(length_hex, 16)
            message_str = read_string_from_socket(self.usocket, length - 1).decode('utf-8')
            # Last byte is newline, already consumed by length
            return json.loads(message_str)
        except (socket.error, json.JSONDecodeError, ValueError, EOFError) as e:
            logger.error(f"Error reading message: {e}")
            return None # Signal error to dispatcher

    def _slime_dispatch_event(self, event):
        """Handles EVENT for this Swank CONNECTION."""
        # Define handlers for different event types
        handlers = {
            ":return": self._handle_return,
            ":debug-activate": self._handle_debug_activate,
            ":debug": self._handle_debug,
            ":debug-return": self._handle_debug_return,
            ":channel-send": self._handle_channel_send,
            ":read-from-minibuffer": self._handle_read_from_minibuffer,
            ":y-or-n-p": self._handle_y_or_n_p,
            ":eval-no-wait": self._handle_eval_no_wait,
            ":eval": self._handle_eval,
            ":ed-rpc-no-wait": self._handle_ed_rpc_no_wait,
            ":ed-rpc": self._handle_ed_rpc,
            ":ed": self._handle_ed,
            ":inspect": self._handle_inspect,
            ":background-message": self._handle_background_message,
            ":debug-condition": self._handle_debug_condition,
            ":ping": self._handle_ping,
            ":reader-error": self._handle_reader_error,
            ":invalid-rpc": self._handle_invalid_rpc,
            ":emacs-skipped-packet": self._handle_emacs_skipped_packet,
        }

        # Special handling for :emacs-rex which originates from this client
        if isinstance(event, list) and len(event) > 0 and event[0] == ":emacs-rex":
            self._handle_emacs_rex(event[1:])
            return

        destructure_case(event, handlers)

    def _handle_emacs_rex(self, args):
        # args = [form, package_name, thread, continuation]
        # This is handled by slime_eval_async/slime_eval when sending
        pass

    def _handle_return(self, value, msg_id):
        with self.connection_lock:
            if msg_id in self.rex_continuations:
                continuation = self.rex_continuations.pop(msg_id)
                # Check if this is a sync eval waiting
                if msg_id in self.eval_events:
                    self.eval_results[msg_id] = value
                    self.eval_events[msg_id].set()
                else:
                    # Call async continuation
                    try:
                        continuation(('ok', value))
                    except Exception as e:
                        logger.error(f"Error in continuation for id {msg_id}: {e}")
            else:
                # Result for a sync eval that might be waiting in another thread
                if msg_id in self.eval_events:
                    self.eval_results[msg_id] = value
                    self.eval_events[msg_id].set()
                else:
                    logger.warning(f"Received :return for unknown id: {msg_id}")


    def _handle_debug_activate(self, thread_id, level, select=None):
        # Modify thread ID to be unique across connections
        unique_thread_id = thread_id + self.thread_offset
        logger.info(f"Debug Activate: Thread {unique_thread_id}, Level {level}, Select {select}")
        # Forward to Emacs (placeholder)
        # send_to_emacs([":debug-activate", unique_thread_id, level, select])

    def _handle_debug(self, thread_id, level, condition, restarts, frames, continuations):
        unique_thread_id = thread_id + self.thread_offset
        logger.info(f"Debug: Thread {unique_thread_id}, Level {level}, Condition: {condition}")
        # Forward to Emacs (placeholder)
        # send_to_emacs([":debug", unique_thread_id, level, condition, restarts, frames, continuations])

    def _handle_debug_return(self, thread_id, level, stepping):
        unique_thread_id = thread_id + self.thread_offset
        logger.info(f"Debug Return: Thread {unique_thread_id}, Level {level}, Stepping: {stepping}")
        # Forward to Emacs (placeholder)
        # send_to_emacs([":debug-return", unique_thread_id, level, stepping])

    def _handle_channel_send(self, channel_id, msg):
        logger.info(f"Channel Send: ID {channel_id}, Message: {msg}")
        # print([":channel-send", channel_id, msg])

    def _handle_read_from_minibuffer(self, thread_id, tag, prompt, initial_value):
        logger.info(f"Read from Minibuffer: Thread {thread_id}, Tag {tag}, Prompt: {prompt}")
        # print([":read-from-minibuffer", thread_id, tag, prompt, initial_value])

    def _handle_y_or_n_p(self, thread_id, tag, question):
        logger.info(f"Y or N: Thread {thread_id}, Tag {tag}, Question: {question}")
        # print([":y-or-n-p", thread_id, tag, question])

    def _handle_eval_no_wait(self, form):
        logger.info(f"Eval No Wait: {form}")
        # print([":eval-no-wait", form])

    def _handle_eval(self, thread_id, tag, form_string):
        logger.info(f"Eval: Thread {thread_id}, Tag {tag}, Form: {form_string}")
        # print([":eval", thread_id, tag, form_string])

    def _handle_ed_rpc_no_wait(self, function_name, *args):
        logger.info(f"ED RPC No Wait: {function_name}, Args: {args}")
        # print([":ed-rpc-no-wait", function_name] + list(args))

    def _handle_ed_rpc(self, thread_id, tag, function_name, *args):
        logger.info(f"ED RPC: Thread {thread_id}, Tag {tag}, Function: {function_name}, Args: {args}")
        # print([":ed-rpc", thread_id, tag, function_name] + list(args))

    def _handle_ed(self, what):
        logger.info(f"Ed: {what}")
        # print([":ed", what])

    def _handle_inspect(self, what, wait_thread, wait_tag):
        logger.info(f"Inspect: {what}, Wait Thread: {wait_thread}, Wait Tag: {wait_tag}")
        # print([":inspect", what, wait_thread, wait_tag])

    def _handle_background_message(self, message):
        logger.info(f"Background Message: {message}")
        # print([":background-message", message])

    def _handle_debug_condition(self, thread_id, message):
        logger.info(f"Debug Condition: Thread {thread_id}, Message: {message}")
        # print([":debug-condition", thread_id, message])

    def _handle_ping(self, thread_id, tag):
        logger.info(f"Ping: Thread {thread_id}, Tag {tag}")
        # Respond with pong
        try:
            self.slime_send([":emacs-pong", thread_id, tag])
        except Exception as e:
            logger.error(f"Failed to send pong: {e}")

    def _handle_reader_error(self, packet, condition):
        logger.error(f"Reader Error: Packet {packet}, Condition: {condition}")
        # print([":reader-error", packet, condition])
        # Optionally raise an error or handle gracefully
        # raise ValueError("Invalid protocol message")

    def _handle_invalid_rpc(self, msg_id, message):
        logger.error(f"Invalid RPC: ID {msg_id}, Message: {message}")
        with self.connection_lock:
            self.rex_continuations.pop(msg_id, None) # Remove if exists
            self.eval_events.pop(msg_id, None) # Remove sync eval state if exists
        # print([":invalid-rpc", msg_id, message])

    def _handle_emacs_skipped_packet(self, packet):
        logger.info(f"Emacs Skipped Packet: {packet}")
        # print([":emacs-skipped-packet", packet])

    def _slime_dispatch_events_loop(self):
        """Main loop for reading and dispatching events in a thread."""
        while self.state == 'alive':
            try:
                event = self._slime_net_read()
                if event is None: # Error or EOF
                    logger.info("Connection closed or error occurred, exiting dispatch loop.")
                    break
                self._slime_dispatch_event(event)
            except Exception as e:
                logger.error(f"Error in dispatch loop: {e}")
                break

        # Cleanup on exit
        self._cleanup()

    def _cleanup(self):
        """Cleans up the connection state."""
        logger.info(f"Cleaning up connection to {self.host_name}:{self.port}")
        with self.connection_lock:
            self.state = 'dead'
        # Close socket
        try:
            self.usocket.shutdown(socket.SHUT_RDWR)
        except socket.error:
            pass # Socket might already be closed
        self.usocket.close()
        # Remove from global list
        remove_open_connection(self)

    def slime_eval_async(self, sexp, continuation=None):
        """Sends SEXP for async evaluation, calls CONTINUATION with result."""
        with self.connection_lock:
            if self.state != 'alive':
                 raise ConnectionError("Connection is not alive")
            msg_id = self.continuation_counter
            self.continuation_counter += 1
            if continuation:
                self.rex_continuations[msg_id] = continuation

        try:
            self.slime_send([":emacs-rex", sexp, "COMMON-LISP-USER", 1, msg_id])
        except Exception as e:
            logger.error(f"Network error in slime_eval_async: {e}")
            # Clean up continuation if send failed
            with self.connection_lock:
                self.rex_continuations.pop(msg_id, None)
            raise

    def slime_eval(self, sexp):
        """Sends SEXP for sync evaluation and waits for the result."""
        msg_id = None
        result_queue = queue.Queue()
        event = threading.Event()

        def sync_continuation(result_tuple):
            # result_tuple is (:ok value) or (:abort condition)
            result_queue.put(result_tuple)
            event.set()

        with self.connection_lock:
            if self.state != 'alive':
                 raise ConnectionError("Connection is not alive")
            msg_id = self.continuation_counter
            self.continuation_counter += 1
            # Store event and queue for sync eval
            self.eval_events[msg_id] = event
            self.eval_results[msg_id] = None # Placeholder

        try:
            self.slime_send([":emacs-rex", sexp, "COMMON-LISP-USER", 1, msg_id])
        except Exception as e:
            logger.error(f"Network error in slime_eval: {e}")
            # Clean up state if send failed
            with self.connection_lock:
                self.eval_events.pop(msg_id, None)
                self.eval_results.pop(msg_id, None)
            raise

        # Wait for the event to be set by _handle_return
        event.wait()
        # Retrieve result
        result = self.eval_results.pop(msg_id)
        self.eval_events.pop(msg_id, None) # Cleanup event

        if isinstance(result, tuple) and result[0] == 'ok':
            return result[1]
        else:
            # Assuming result is (:abort condition) or similar error tuple
            raise RuntimeError(f"Evaluation aborted or error: {result}")


def add_open_connection(connection):
    with CONNECTIONS_LOCK:
        OPEN_CONNECTIONS.append(connection)

def remove_open_connection(connection):
    with CONNECTIONS_LOCK:
        OPEN_CONNECTIONS.remove(connection)

def find_connection_for_thread_id(thread_id):
    """Finds the connection associated with a given thread ID."""
    offset = (thread_id // MAXIMUM_THREAD_COUNT) * MAXIMUM_THREAD_COUNT
    with CONNECTIONS_LOCK:
        for conn in OPEN_CONNECTIONS:
            if conn.thread_offset == offset:
                return conn
    return None

def server_thread_id(thread_id):
    """Maps the thread ID known locally to the ID known by the remote server."""
    return thread_id % MAXIMUM_THREAD_COUNT

def slime_secret():
    """Finds the secret file in the user's home directory."""
    secret_file = Path.home() / ".slime-secret"
    try:
        with open(secret_file, 'r') as f:
            return f.readline().strip()
    except FileNotFoundError:
        return None

def socket_keep_alive(sock):
    """Configures TCP keep alive for the socket."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    # Platform-specific options might be needed, similar to the Lisp code
    # Example for Linux (values are illustrative)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 30)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 1)
    except (AttributeError, OSError):
        # Options might not be available on all platforms
        pass

def slime_net_connect(host_name, port):
    """Establishes a TCP connection to the Swank server."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((host_name, port))
        socket_keep_alive(s)
        return s
    except socket.error as e:
        logger.error(f"Failed to connect to {host_name}:{port} - {e}")
        return None

def slime_connect(host_name, port, connection_closed_hook=None):
    """Connects to the Swank server and starts the event dispatch thread."""
    usock = slime_net_connect(host_name, port)
    if not usock:
        return None

    connection = SwankConnection(host_name, port, usock)
    add_open_connection(connection)

    # Send secret if it exists
    secret = slime_secret()
    if secret:
        try:
            connection.slime_send(secret)
        except Exception as e:
            logger.error(f"Failed to send secret: {e}")
            connection._cleanup()
            return None

    # Start the dispatch thread
    connection.dispatch_thread = threading.Thread(
        target=connection._slime_dispatch_events_loop,
        name=f"swank dispatcher for {host_name}/{port}",
        daemon=True # Dies when main program exits
    )
    connection.dispatch_thread.start()

    return connection

def slime_close(connection):
    """Initiates closing of the Swank connection."""
    with connection.connection_lock:
        if connection.state == 'alive':
            connection.state = 'closing'
    # The dispatch loop will detect the state change and exit, triggering cleanup

# --- Example Usage (Wrapped in Context Manager for safety) ---
# This is a conceptual example. A real context manager would be more complex.
# class SwankConnectionManager:
#     def __init__(self, host, port):
#         self.host = host
#         self.port = port
#         self.connection = None
#
#     def __enter__(self):
#         self.connection = slime_connect(self.host, self.port)
#         if not self.connection:
#             raise ConnectionError(f"Could not connect to {self.host}:{self.port}")
#         return self.connection
#
#     def __exit__(self, exc_type, exc_val, exc_tb):
#         if self.connection:
#             slime_close(self.connection)

# Example:
# if __name__ == "__main__":
#     try:
#         # conn = slime_connect("localhost", 4005)
#         with SwankConnectionManager("localhost", 4005) as conn:
#             if conn:
#                 result = conn.slime_eval("(+ 1 2)")
#                 print(f"Result: {result}")
#                 import time
#                 time.sleep(10) # Keep alive to see events
#     except Exception as e:
#         print(f"Error: {e}")
