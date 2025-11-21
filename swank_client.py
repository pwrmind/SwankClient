# swank_client.py
import json
import socket
import struct
import threading
import queue
import logging
import os
from pathlib import Path
import re

# --- Constants ---
ABORT = (None, None)
MAXIMUM_THREAD_COUNT = 10000
SLIME_PROTOCOL_VERSION = "20210223"

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

# --- Lisp S-expression Serialization Helper ---

def escape_lisp_string(s):
    """Escapes quotes and backslashes in a string for Lisp."""
    return s.replace('\\', '\\\\').replace('"', '\\"')

def serialize_lisp_item(item):
    """
    Serializes a single Lisp item with proper handling of symbols vs strings.
    """
    if item is None:
        return "nil"
    elif item is True:
        return "t"
    elif item is False:
        return "nil"
    elif isinstance(item, str):
        # Check if it's a keyword (starts with colon)
        if item.startswith(':'):
            return item
        # Check if it's a symbol (simple identifier)
        elif re.match(r'^[a-zA-Z0-9_\-+*/=<>!?&%$#@~\.:]+$', item) and not item[0].isdigit():
            # Для стандартных функций Common Lisp используем CL: префикс
            common_lisp_symbols = {
                '+', '-', '*', '/', 'format', 'list', 'cons', 'car', 'cdr',
                'eq', 'equal', 'defun', 'defparameter', 'setq', 'let', 'if',
                'progn', 'princ', 'print', 'terpri'
            }
            if item in common_lisp_symbols:
                return f"CL:{item}"
            else:
                return item
        else:
            # It's a string literal - needs quotes and escaping
            escaped_item = escape_lisp_string(item)
            return f'"{escaped_item}"'
    elif isinstance(item, (int, float)):
        return str(item)
    elif isinstance(item, list):
        return serialize_lisp_list(item)
    else:
        # Fallback for other types
        return str(item)

def serialize_lisp_list(items):
    """Serializes a list of items into a Lisp list string."""
    elements = [serialize_lisp_item(item) for item in items]
    return f"({' '.join(elements)})"

# --- Advanced Lisp S-expression Parser ---

def parse_lisp_string(s):
    """
    A robust Lisp s-expression parser that handles nested structures and strings properly.
    """
    s = s.strip()
    if not s:
        return None
    
    def tokenize(s):
        """Tokenize the input string into Lisp tokens."""
        tokens = []
        i = 0
        n = len(s)
        
        while i < n:
            if s[i] in ' \t\n':
                i += 1
                continue
            elif s[i] == '(':
                tokens.append('(')
                i += 1
            elif s[i] == ')':
                tokens.append(')')
                i += 1
            elif s[i] == '"':
                # Parse string
                j = i + 1
                while j < n and not (s[j] == '"' and s[j-1] != '\\'):
                    j += 1
                if j < n:
                    tokens.append(s[i:j+1])
                    i = j + 1
                else:
                    tokens.append(s[i:])
                    i = n
            else:
                # Parse atom
                j = i
                while j < n and s[j] not in ' \t\n()"':
                    j += 1
                tokens.append(s[i:j])
                i = j
        
        return tokens
    
    def parse_tokens(tokens):
        """Parse tokens into Lisp data structures."""
        if not tokens:
            return None, tokens
        
        token = tokens[0]
        if token == '(':
            # Parse list
            lst = []
            tokens = tokens[1:]  # Skip '('
            while tokens and tokens[0] != ')':
                item, tokens = parse_tokens(tokens)
                if item is not None:
                    lst.append(item)
            if tokens and tokens[0] == ')':
                tokens = tokens[1:]  # Skip ')'
            return lst, tokens
        elif token == ')':
            raise SyntaxError("Unexpected ')'")
        else:
            # Parse atom
            return parse_atom(token), tokens[1:]
    
    def parse_atom(token):
        """Parse a single token into a Lisp atom."""
        if token == 'nil':
            return None
        elif token == 't':
            return True
        elif token.startswith('"') and token.endswith('"'):
            # String
            content = token[1:-1]
            # Handle escape sequences
            content = content.replace('\\"', '"').replace('\\\\', '\\')
            return content
        elif token.startswith(':'):
            # Keyword
            return token
        else:
            # Number or symbol
            try:
                return int(token)
            except ValueError:
                try:
                    return float(token)
                except ValueError:
                    return token
    
    try:
        tokens = tokenize(s)
        result, remaining = parse_tokens(tokens)
        if remaining:
            logger.warning(f"Not all tokens were parsed. Remaining: {remaining}")
        return result
    except Exception as e:
        logger.error(f"Error parsing Lisp string: {s}, error: {e}")
        return None

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
            # Use the corrected serialization
            payload = serialize_lisp_list(sexp)
            logger.debug(f"Sending: {payload}")
            
            utf8_payload = payload.encode('utf-8')
            payload_length = len(utf8_payload) + 1  # +1 for newline
            hex_length = self._slime_net_encode_length(payload_length)
            utf8_length = hex_length.encode('utf-8')

            message = utf8_length + utf8_payload + b'\n'
            self.usocket.sendall(message)
        except socket.error as e:
            logger.error(f"Network error in _slime_net_send: {e}")
            raise

    def slime_send(self, sexp):
        """Sends SEXP to the Swank server using the connection."""
        self._slime_net_send(sexp)

    def _slime_net_read(self):
        """Reads a Swank message from the network connection using Lisp parser."""
        try:
            # Read length (6 bytes)
            length_data = read_string_from_socket(self.usocket, 6)
            length_hex = length_data.decode('utf-8', errors='ignore')
            
            # Validate hex string
            if not all(c in '0123456789ABCDEF' for c in length_hex):
                logger.error(f"Invalid length hex: {length_hex}")
                return None
                
            length = int(length_hex, 16)
            if length <= 0:
                logger.error(f"Invalid message length: {length}")
                return None
                
            # Read message body (length bytes, which includes the newline)
            message_data = read_string_from_socket(self.usocket, length)
            message_str = message_data.decode('utf-8', errors='ignore')
            
            # Parse as Lisp s-expression
            parsed = parse_lisp_string(message_str)
            logger.debug(f"Received raw: {message_str}")
            logger.debug(f"Received parsed: {parsed}")
            return parsed
            
        except (socket.error, ValueError, EOFError, UnicodeDecodeError) as e:
            logger.error(f"Error reading message: {e}")
            return None

    def _slime_dispatch_event(self, event):
        """Handles EVENT for this Swank CONNECTION."""
        if event is None:
            logger.warning("Received None event in _slime_dispatch_event, skipping.")
            return

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
            ":emacs-rex": self._handle_emacs_rex,
        }

        destructure_case(event, handlers)

    def _handle_emacs_rex(self, form, package_name, thread, continuation_id):
        """Handle :emacs-rex events - send form to server for evaluation."""
        logger.info(f"Emacs REX: Form={form}, Package={package_name}, Thread={thread}, ID={continuation_id}")

    def _handle_return(self, value, msg_id):
        logger.info(f"Return: value={value}, msg_id={msg_id}")
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

    def _handle_debug(self, thread_id, level, condition, *args):
        """Handle debug events with variable number of arguments."""
        unique_thread_id = thread_id + self.thread_offset
        logger.info(f"Debug: Thread {unique_thread_id}, Level {level}, Condition: {condition}")
        logger.info(f"Debug args: {args}")

    def _handle_debug_return(self, thread_id, level, stepping):
        unique_thread_id = thread_id + self.thread_offset
        logger.info(f"Debug Return: Thread {unique_thread_id}, Level {level}, Stepping: {stepping}")

    def _handle_channel_send(self, channel_id, msg):
        logger.info(f"Channel Send: ID {channel_id}, Message: {msg}")

    def _handle_read_from_minibuffer(self, thread_id, tag, prompt, initial_value):
        logger.info(f"Read from Minibuffer: Thread {thread_id}, Tag {tag}, Prompt: {prompt}")

    def _handle_y_or_n_p(self, thread_id, tag, question):
        logger.info(f"Y or N: Thread {thread_id}, Tag {tag}, Question: {question}")

    def _handle_eval_no_wait(self, form):
        logger.info(f"Eval No Wait: {form}")

    def _handle_eval(self, thread_id, tag, form_string):
        logger.info(f"Eval: Thread {thread_id}, Tag {tag}, Form: {form_string}")

    def _handle_ed_rpc_no_wait(self, function_name, *args):
        logger.info(f"ED RPC No Wait: {function_name}, Args: {args}")

    def _handle_ed_rpc(self, thread_id, tag, function_name, *args):
        logger.info(f"ED RPC: Thread {thread_id}, Tag {tag}, Function: {function_name}, Args: {args}")

    def _handle_ed(self, what):
        logger.info(f"Ed: {what}")

    def _handle_inspect(self, what, wait_thread, wait_tag):
        logger.info(f"Inspect: {what}, Wait Thread: {wait_thread}, Wait Tag: {wait_tag}")

    def _handle_background_message(self, message):
        logger.info(f"Background Message: {message}")

    def _handle_debug_condition(self, thread_id, message):
        logger.info(f"Debug Condition: Thread {thread_id}, Message: {message}")

    def _handle_ping(self, thread_id, tag):
        logger.info(f"Ping: Thread {thread_id}, Tag {tag}")
        # Respond with pong
        try:
            self.slime_send([":emacs-pong", thread_id, tag])
        except Exception as e:
            logger.error(f"Failed to send pong: {e}")

    def _handle_reader_error(self, packet, condition):
        logger.error(f"Reader Error: Packet {packet}, Condition: {condition}")

    def _handle_invalid_rpc(self, msg_id, message):
        logger.error(f"Invalid RPC: ID {msg_id}, Message: {message}")
        with self.connection_lock:
            # For sync evals, set the result to an exception
            if msg_id in self.eval_events:
                self.eval_results[msg_id] = RuntimeError(f"Invalid RPC: {message}")
                self.eval_events[msg_id].set()
            # Remove from continuations
            self.rex_continuations.pop(msg_id, None)
            self.eval_events.pop(msg_id, None)

    def _handle_emacs_skipped_packet(self, packet):
        logger.info(f"Emacs Skipped Packet: {packet}")

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
            # Use "CL-USER" package and "T" for current thread
            self.slime_send([":emacs-rex", sexp, "CL-USER", "T", msg_id])
        except Exception as e:
            logger.error(f"Network error in slime_eval_async: {e}")
            # Clean up continuation if send failed
            with self.connection_lock:
                self.rex_continuations.pop(msg_id, None)
            raise

    def slime_eval(self, sexp):
        """Sends SEXP for sync evaluation and waits for the result."""
        msg_id = None
        event = threading.Event()

        def sync_continuation(result_tuple):
            # result_tuple is (:ok value) or (:abort condition)
            with self.connection_lock:
                self.eval_results[msg_id] = result_tuple
                self.eval_events[msg_id].set()

        with self.connection_lock:
            if self.state != 'alive':
                 raise ConnectionError("Connection is not alive")
            msg_id = self.continuation_counter
            self.continuation_counter += 1
            # Store event and queue for sync eval
            self.eval_events[msg_id] = event
            self.eval_results[msg_id] = None
            # Register continuation for async response handling
            self.rex_continuations[msg_id] = sync_continuation

        try:
            # Use "CL-USER" package and "T" for current thread
            self.slime_send([":emacs-rex", sexp, "CL-USER", "T", msg_id])
        except Exception as e:
            logger.error(f"Network error in slime_eval: {e}")
            # Clean up state if send failed
            with self.connection_lock:
                self.eval_events.pop(msg_id, None)
                self.eval_results.pop(msg_id, None)
                self.rex_continuations.pop(msg_id, None)
            raise

        # Wait for the event to be set by _handle_return
        event.wait(timeout=30.0)  # 30 second timeout
        
        # Retrieve result
        with self.connection_lock:
            if not event.is_set():
                raise TimeoutError("Evaluation timed out")
                
            result_tuple = self.eval_results.pop(msg_id)
            self.eval_events.pop(msg_id, None)
            self.rex_continuations.pop(msg_id, None)

        if result_tuple is None:
            raise RuntimeError("Evaluation failed with no result")
            
        if isinstance(result_tuple, Exception):
            raise result_tuple
            
        if isinstance(result_tuple, (list, tuple)) and len(result_tuple) > 0:
            if result_tuple[0] == ':ok':
                return result_tuple[1] if len(result_tuple) > 1 else result_tuple[0]
            elif result_tuple[0] == ':abort':
                raise RuntimeError(f"Evaluation aborted: {result_tuple[1]}")
        
        return result_tuple


def add_open_connection(connection):
    with CONNECTIONS_LOCK:
        OPEN_CONNECTIONS.append(connection)

def remove_open_connection(connection):
    with CONNECTIONS_LOCK:
        if connection in OPEN_CONNECTIONS:
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
    # Platform-specific options
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
            # Send secret as a string literal
            secret_payload = f'"{escape_lisp_string(secret)}"'
            connection._slime_net_send(secret_payload)
        except Exception as e:
            logger.error(f"Failed to send secret: {e}")
            connection._cleanup()
            return None

    # Start the dispatch thread
    connection.dispatch_thread = threading.Thread(
        target=connection._slime_dispatch_events_loop,
        name=f"swank dispatcher for {host_name}/{port}",
        daemon=False
    )
    connection.dispatch_thread.start()

    return connection

def slime_close(connection):
    """Initiates closing of the Swank connection."""
    with connection.connection_lock:
        if connection.state == 'alive':
            connection.state = 'closing'
    # The dispatch loop will detect the state change and exit, triggering cleanup

# --- Context Manager for safety ---
class SwankConnectionManager:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.connection = None

    def __enter__(self):
        self.connection = slime_connect(self.host, self.port)
        if not self.connection:
            raise ConnectionError(f"Could not connect to {self.host}:{self.port}")
        return self.connection

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.connection:
            slime_close(self.connection)
            # Wait for cleanup to complete
            if self.connection.dispatch_thread and self.connection.dispatch_thread.is_alive():
                self.connection.dispatch_thread.join(timeout=5.0)

# --- Example Usage ---
if __name__ == "__main__":
    # Enable debug logging for example
    logging.getLogger().setLevel(logging.DEBUG)
    
    try:
        with SwankConnectionManager("localhost", 4005) as conn:
            if conn:
                # Test with CL package functions - use symbols, not strings
                # Send (+ 1 2) as a proper Lisp form - теперь будет сериализовано как (CL:+ 1 2)
                result = conn.slime_eval(['+', 1, 2])
                print(f"Result: {result}")
                
                # Test with a string evaluation
                result2 = conn.slime_eval(['format', 'nil', 'Hello, ~a', 'World'])
                print(f"Format result: {result2}")
                
                import time
                time.sleep(2) # Keep alive to see events
    except Exception as e:
        print(f"Error: {e}")
