import os

# librespot 0.0.1 is not compatible with newer protobuf runtimes unless we
# force the pure-Python protobuf implementation before protobuf is imported.
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

__version__ = "1.9.5"
