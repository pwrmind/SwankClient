# Python Swank Client

This repository contains a Python implementation of a client for the Swank protocol, originally written in Common Lisp (as seen in the `swank-client.lisp` file). The Swank protocol is used primarily by SLIME (Superior Lisp Interaction Mode for Emacs) to communicate with a running Lisp image.

> **Note:** This is a conceptual translation from Lisp to Python. While it aims to replicate the core logic and structure of the original `swank-client.lisp`, the behavior and integration with Emacs/SLIME might differ significantly and require further adaptation for a fully functional client.

## Features

*   **Connection Management:** Establishes TCP connections to a Swank server.
*   **Protocol Handling:** Implements the basic message encoding/decoding and event dispatching logic for the Swank protocol.
*   **Synchronous & Asynchronous Evaluation:** Supports sending Lisp forms for evaluation and handling results, both synchronously (`slime_eval`) and asynchronously (`slime_eval_async`).
*   **Thread Safety:** Uses Python's `threading` module to handle concurrent operations safely, mirroring the original Lisp code's use of locks.
*   **Secret File Handling:** Attempts to read the `.slime-secret` file for authentication, similar to the original client.

## Prerequisites

*   Python 3.x
*   Standard Python libraries: `socket`, `threading`, `queue`, `struct`, `json`, `logging`, `os`, `pathlib`

## Usage

```python
from swank_client import slime_connect, slime_close

# Connect to a Swank server running on localhost:4005
conn = slime_connect("localhost", 4005)

if conn:
    try:
        # Perform a synchronous evaluation
        result = conn.slime_eval("(+ 1 2)")
        print(f"Result of (+ 1 2): {result}")

        # Perform an asynchronous evaluation
        def my_continuation(value):
            print(f"Async result received: {value}")

        conn.slime_eval_async("(format nil \"Hello from Lisp! ~A\" (get-universal-time))", my_continuation)

        # Keep the main thread alive to receive events and async results
        import time
        time.sleep(10)

    finally:
        # Always close the connection when done
        slime_close(conn)
else:
    print("Failed to connect to the Swank server.")
```

## Architecture

*   **`SwankConnection` Class:** Represents a single connection to a Swank server, holding state (socket, continuations, locks) and methods for sending/receiving messages.
*   **Event Dispatching:** The `_slime_dispatch_event` method handles incoming messages from the server, routing them to specific handler methods (`_handle_return`, `_handle_debug`, etc.).
*   **Threading:** A dedicated thread (`_slime_dispatch_events_loop`) continuously reads messages from the socket for each connection.
*   **Synchronization:** Uses `threading.Lock`, `threading.RLock`, `threading.Event`, and `queue.Queue` for managing shared state and coordinating between threads, especially for synchronous evaluations.

## Limitations & Notes

*   **S-Expression Handling:** The original Lisp code deals with native S-expressions. This Python version uses JSON for serialization/deserialization of message structures. This might not perfectly capture all Lisp data types or symbol packages, potentially causing issues with complex data exchanges.
*   **`send-to-emacs` Integration:** The original Lisp code includes a function `send-to-emacs` to forward events back to Emacs. This Python version includes a placeholder comment for this functionality. Implementing this would require a separate mechanism to interface with Emacs, which is outside the scope of this direct translation.
*   **Completeness:** While the core logic is translated, some nuances of the original Lisp implementation, error handling specifics, or less common protocol messages might not be fully replicated or tested.
*   **Testing:** This code has been translated based on the structure of the Lisp code but may require significant testing and refinement to work reliably with a real Swank server and Emacs/SLIME setup.

## License

The original `swank-client.lisp` file is licensed under the GNU General Public License v2.0 or later. This Python translation is provided for educational and conceptual purposes. Please refer to the original license terms for details regarding derivative works.
```
