#! /usr/bin/env python

from pyslet.py26 import *       # noqa

import string
import types
import base64
import threading
import logging
import io
import os
import zlib
import errno
from StringIO import StringIO

import pyslet.rfc2396 as uri
from pyslet.pep8 import PEP8Compatibility

import grammar
import params
import auth
import cookie


class BufferedEntityWrapper(io.RawIOBase):

    """A buffered wrapper for file-like objects.

    src
        A file-like object, we only require a read method

    buffsize
        The maximum size of the internal buffer

    On construction the src is read until an end of file condition is
    encountered or until buffsize bytes have been read.  EOF is signaled
    by an empty string returned by src's read method.  Instances then
    behave like readable streams transparently reading from the buffer
    or from the remainder of the src as applicable.

    Instances behave differently depending on whether or not the entire
    src is buffered.  If it is they become seekable and set a value for
    the length attribute.  Otherwise they are not seekable and the
    length attribute is None.

    If src is a non-blocking data source and it becomes blocked,
    indicated by read returning None rather than an empty string, then
    the instance reverts to non-seekable behaviour."""

    def __init__(self, src, buffsize=io.DEFAULT_BUFFER_SIZE):
        self.src = src
        self.buff = StringIO()
        self.bsize = 0
        self.overflow = False
        self.length = None
        while True:
            nbytes = buffsize - self.bsize
            if nbytes <= 0:
                # we've run out of buffer space
                self.overflow = True
                break
            data = src.read(nbytes)
            if data is None:
                # blocked, treat as overflow
                self.overflow = True
                break
            elif data:
                self.buff.write(data)
                self.bsize += len(data)
            else:
                # EOF
                break
        self.pos = 0
        self.buff.seek(0)
        if not self.overflow:
            self.length = self.bsize

    def readable(self):
        return True

    def writable(self):
        return False

    def seekable(self):
        return not self.overflow

    def tell(self):
        if self.overflow:
            raise UnsupportedOperation
        else:
            return self.pos

    def seek(self, offset, whence=io.SEEK_SET):
        if self.overflow:
            raise UnsupportedOperation
        elif whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        elif whence == io.SEEK_END:
            self.pos = self.length + offset
        else:
            raise ValueError("unrecognized whence value in seek: %s" %
                             repr(whence))
        self.buff.seek(self.pos)

    def readinto(self, b):
        if self.pos < self.bsize:
            # read from the buffer
            data = self.buff.read(len(b))
        elif self.overflow:
            # read from the original source
            data = self.src.read(len(b))
            if data is None:
                # handle blocked read
                return None
        else:
            # end of file
            data = ''
        self.pos += len(data)
        b[0:len(data)] = data
        return len(data)

    def peek(self, nbytes):
        """Read up to nbytes without advancing the position

        If the stream is not seekable and we have read past the end of
        the internal buffer then an empty string will be returned."""
        if self.pos < self.bsize:
            data = self.buff.read(nbytes)
            # reset the position of the buffer
            self.buff.seek(self.pos)
            return data
        else:
            return ''


class GzipEncoder(io.RawIOBase):

    """Wrapper to provide Gzip encoding of streams

    src
        A readable file-like object

    Instances act as readable streams that pull data from src, adding
    the gzip encoding to the data returned by read."""

    def __init__(self, src):
        self.src = src
        self.buffstr = None
        self.encoder = zlib.compressobj(6, zlib.DEFLATED, 31)

    def readable(self):
        return True

    def writable(self):
        return False

    def readinto(self, b):
        nbytes = len(b)
        while True:
            if self.buffstr:
                # we have some buffered data left over
                if len(self.buffstr) > nbytes:
                    self.buffstr = data[nbytes:]
                    b[:] = data[:nbytes]
                else:
                    nbytes = len(self.buffstr)
                    b[:nbytes] = self.buffstr
                self.buffstr = None
                return nbytes
            elif self.src is not None:
                data = self.src.read(nbytes)
                if data is None:
                    # we are read blocked
                    return None
                elif len(data) == 0:
                    # we are done
                    self.buffstr = self.encoder.flush()
                    self.src = None
                else:
                    self.buffstr = self.encoder.compress(data)
            else:
                return 0


class GzipDecoder(io.RawIOBase):

    """Wrapper to provide Gzip decoding of streams

    dst
        A writable file-like object

    Instances act as writable streams that push data into src, removing
    the gzip encoding from the data written to them."""

    def __init__(self, dst):
        self.dst = dst
        self.buffstr = None
        self.decoder = zlib.decompressobj(31)

    def readable(self):
        return False

    def writable(self):
        return True

    def flush(self):
        # flushes any remnants to the stream
        if self.decoder:
            if self.buffstr is not None:
                self.buffstr = self.buffstr + self.decoder.flush()
            else:
                self.buffstr = self.decoder.flush()
            self.decoder = None
        while self.buffstr:
            if isinstance(self.dst, io.RawIOBase):
                n = self.dst.write(self.buffstr)
            else:
                # assumem blocking write
                self.dst.write(self.buffstr)
                n = len(self.buffstr)
            if n is None:
                raise IOError(errno.EAGAIN, os.strerror(errno.EAGAIN))
            elif n < len(self.buffstr):
                self.buffstr = self.buffstr[n:]
            else:
                self.buffstr = None

    def write(self, zdata):
        wbytes = None
        while True:
            if self.buffstr:
                # we have some buffered data left over to write
                if isinstance(self.dst, io.RawIOBase):
                    n = self.dst.write(self.buffstr)
                else:
                    # assumem blocking write
                    self.dst.write(self.buffstr)
                    n = len(self.buffstr)
                if n is None:
                    # we're blocked
                    break
                elif n < len(self.buffstr):
                    # partial write
                    self.buffstr = self.buffstr[n:]
                else:
                    self.buffstr = None
            elif zdata:
                # decompress the data
                self.buffstr = self.decoder.decompress(zdata)
                wbytes = len(zdata)
                zdata = None
            else:
                # no buffer, no data, nothing do do
                break
        return wbytes


class ChunkedReader(io.RawIOBase):

    def __init__(self, src):
        self.src = src
        self.chunk_left = -1
        self.eof = False

    def _read_chunksize(self):
        # trailing CRLF needs to be read:
        if self.chunk_left == 0:
            self.src.readline()
        p = params.ParameterParser(self.src.readline().strip())
        self.chunk_left = int(p.parse_token() or "0", 16)
        if self.chunk_left == 0:
            self.eof = True
            # read the trailing headers and CRLF
            while True:
                line = self.src.readline()
                if not line.strip():
                    break

    def readable(self):
        return True

    def writable(self):
        return False

    def seekable(self):
        return False

    def readinto(self, b):
        if self.chunk_left <= 0:
            if not self.eof:
                self._read_chunksize()
            if self.eof:
                return 0
        nbytes = len(b)
        if nbytes > self.chunk_left:
            nbytes = self.chunk_left
        b[:nbytes] = self.src.read(nbytes)
        self.chunk_left -= nbytes
        return nbytes


class Message(PEP8Compatibility, object):

    """An abstract class to represent an HTTP message.

    The methods of this class are thread safe, using a :py:attr:`lock`
    to protect all access to internal structures.

    The generic syntax of a message involves a start line, followed by a
    number of message headers and an optional message body.

    entity_body
        The optional entity_body parameter is a byte string containing
        the *entity* body, a file like object or object derived from
        io.RawIOBase.  There are restrictions on the use of non-seekable
        streams, in particular the absence of a working seek may affect
        redirects and retries.

        There is a subtle difference between passing None, meaning no
        entity body and an empty string ''.  The difference is that an
        empty string will generate a Content-Length header indicating a
        zero length message body when the message is sent, whereas None
        will not.  Some message types are not allowed to have an entity
        body (e.g., a GET request) and these messages must not have a
        message body (even a zero length one) or an error will be raised.

        File-like objects do not generate a Content-Length header
        automatically as there is no way to determine their size when
        sending, however, if a Content-Length header is set explicitly
        then it will be used to constrain the amount of data read from
        the entity_body."""

    #: a mapping from lower case header name to preferred case name
    GENERAL_HEADERS = {
        "cache-control": "Cache-Control",
        "connection": "Connection",
        "date": "Date",
        "pragma": "Pragma",
        "trailer": "Trailer",
        "transfer-encoding": "Transfer-Encoding",
        "upgrade": "Upgrade",
        "via": "Via",
        "warning": "Warning"
    }

    #: A constant used to control the maximum read-ahead on an
    #: entity body's stream.  Entity bodies of undetermined length
    #: that exceed this size cannot be sent in requests to HTTP/1.0
    #: server.
    MAX_READAHEAD = 16 * io.DEFAULT_BUFFER_SIZE

    def __init__(self, entity_body=None, protocol=params.HTTP_1p1,
                 send_stream=None, recv_stream=None):
        #: the lock used to protect multi-threaded access
        PEP8Compatibility.__init__(self)
        self.lock = threading.RLock()
        self.protocol = protocol
        self.headers = {}
        if isinstance(entity_body, str):
            self.entity_body = StringIO(entity_body)
            self.body_start = 0
            self.body_len = len(entity_body)
        elif entity_body is None:
            # no entity body
            self.entity_body = None
            self.body_start = None
            self.body_len = None
        else:
            # must be file-like
            try:
                self.entity_body = entity_body
                self.body_start = entity_body.tell()
            except (IOError, AttributeError):
                self.body_start = None
            self.body_len = None
        #: by default we'll keep the connection alive
        self.keep_alive = True
        self.body_started = False
        self.send_stream = send_stream
        self.recv_stream = recv_stream

    def set_protocol(self, version):
        """Sets the protocol

        version
            An :py:class:`params.HTTPVersion` instance or a string that
            can be parsed for one."""
        with self.lock:
            if isinstance(version, types.StringTypes):
                version = params.HTTPVersion.from_str(version)
            if not isinstance(version, params.HTTPVersion):
                raise TypeError
            self.protocol = version

    def clear_keep_alive(self):
        """Clears the keep_alive flag on this message

        The flag always starts set to True and cannot be set once
        cleared."""
        with self.lock:
            self.keep_alive = False

    def start_sending(self, protocol=params.HTTP_1p1):
        """Starts sending this message

        protocol
            The protocol supported by the target of the message,
            defaults to HTTP/1.1 but can be overridden when the
            recipient only supports HTTP/1.0.  This has the effect of
            suppressing some features.

        The message is sent using the send\_ family of methods."""
        with self.lock:
            self.send_protocol = protocol
            if (self.send_protocol is not None and
                    self.send_protocol <= params.HTTP_1p0):
                # we don't do any keep-alive for HTTP/1.0
                self.clear_keep_alive()
            if self.body_started:
                if self.body_start is not None:
                    # we've been here before, seek back
                    self.entity_body.seek(self.body_start)
                else:
                    raise HTTPException("Can't resend "
                                        "non-seekable entity body")
            self.send_transferlength()
            self.transferPos = 0
            self.transferDone = False
            if self.transferchunked is False and self.transferlength is None:
                self.clear_keep_alive()
            if not self.keep_alive:
                # add a Connection: close header
                self.set_connection(["close"])

    def send_start(self):
        """Returns the start-line for this message"""
        raise NotImplementedError

    def send_header(self):
        """Returns a data string ready to send to the server"""
        buffer = []
        # Calculate the length of the message body for transfer
        self.send_transferlength()
        hlist = self.get_headerlist()
        for hKey in hlist:
            h = self.headers[hKey]
            hname = h[0]
            for hvalue in h[1:]:
                buffer.append("%s: %s\r\n" % (hname, hvalue))
        buffer.append("\r\n")
        return string.join(buffer, '')

    def send_transferlength(self):
        """Calculates the transfer length of the message

        It will read the Transfer-Encoding or Content-Length headers
        to determine the length.

        If the length of the entity body is known, this method will
        verify that it matches the Content-Length or set that header's
        value accordingly.

        If the length of the entity body is not known, this method will
        set a Transfer-Encoding header."""
        # calculate the transfer length of the message body
        self.transferchunked = False
        self.transferlength = 0
        self.transferbody = self.entity_body
        # If there is an encoding other than 'identity' then we're using
        # chunked
        encoding = self.get_transfer_encoding()
        content_length = self.get_content_length()
        if (self.send_protocol is None or
                self.send_protocol <= params.HTTP_1p0):
            # don't send a transfer encoding to a 1.0 or unknown recipient
            if encoding is not None:
                self.set_transfer_encoding(None)
                encoding = None
        elif encoding is not None:
            if content_length is not None:
                raise HTTPException("Content-Length not allowed with "
                                    "Transfer-Encoding")
            if encoding[-1].token == "chunked":
                self.transferchunked = True
            else:
                # read until end of connection
                self.transferchunked = False
            self.transferlength = None
            if len(encoding) > 1:
                for enc in encoding:
                    if enc.token == "gzip":
                        # wrap the body in a gzip compression wrapper
                        self.transferbody = GzipEncoder(self.transferbody)
                    elif enc.token == "identity":
                        continue
                    elif enc.token == "chunked":
                        # always the last encoding
                        break
                    else:
                        raise NotImplementedError(
                            "Unsupported transfer encoding %s" % enc.token)
            # overrides any Content-Length setting
            return
        # If there is a Content-Length header
        if content_length is not None:
            self.transferlength = content_length
            if self.body_len is not None:
                # Raise errors if these don't match our calculation
                if self.body_len < self.transferlength:
                    raise HTTPException("Too little data in entity body")
                elif self.body_len > self.transferlength:
                    raise HTTPException("Too much data in entity body")
            return
        # We don't yet support multipart/byteranges....so skip this
        # auto-calculate the content-length if there is an entity body
        if self.body_len is not None:
            self.set_content_length(self.body_len)
            self.transferlength = self.body_len
        elif self.entity_body is None:
            self.transferlength = 0
        elif (self.send_protocol is None or
                self.send_protocol <= params.HTTP_1p0):
            # send this message forever
            self.transferlength = None
        else:
            # We don't know the entity body size so force chunked
            self.set_transfer_encoding("chunked")
            self.transferchunked = True
            self.transferlength = None

    def send_body(self):
        """Returns (part of) the message body

        Returns an empty string when there is no more data to send.

        Returns None if the message is read blocked."""
        if self.transferDone:
            return ''
        if self.transferbody is None:
            self.transferDone = True
            return ''
        else:
            # We're reading from a stream
            if self.transferchunked:
                data = self.transferbody.read(io.DEFAULT_BUFFER_SIZE)
                if data is None:
                    return None
                self.transferPos += len(data)
                buffer = []
                if data:
                    self.body_started = True
                    buffer.append(str(params.Chunk(len(data))))
                    buffer.append(grammar.CRLF)
                    buffer.append(data)
                    buffer.append(grammar.CRLF)
                else:
                    buffer.append(str(params.Chunk()))
                    buffer.append(grammar.CRLF)
                    buffer.append(grammar.CRLF)
                    self.transferDone = True
                data = string.join(buffer, '')
                return data
            else:
                if self.transferlength is None:
                    chunklen = io.DEFAULT_BUFFER_SIZE
                else:
                    chunklen = self.transferlength - self.transferPos
                    if chunklen > io.DEFAULT_BUFFER_SIZE:
                        chunklen = io.DEFAULT_BUFFER_SIZE
                if chunklen:
                    data = self.transferbody.read(chunklen)
                    if data is None:
                        return None
                    if data:
                        self.transferPos += len(data)
                        self.body_started = True
                        return data
                    else:
                        # end of the message body (read forever mode)
                        self.transferDone = True
                        return ''
                else:
                    self.transferDone = True
                    return ''

    START_MODE = 1
    HEADER_MODE = 2
    CHUNK_HEAD_MODE = 3
    DATA_MODE = 4
    BLOCKED_MODE = 5
    CHUNK_END_MODE = 6
    CHUNK_TRAILER_MODE = 7

    def start_receiving(self):
        """Starts receiving this message

        The message is received using the :py:meth:`recv_mode` and
        :py:meth:`recv` methods."""
        with self.lock:
            self.transfermode = self.START_MODE
            self.protcolVersion = None
            self.headers = {}
            self._curr_header = None
            if self.body_started:
                if self.body_start is not None:
                    # we've been here before, truncate back
                    self.entity_body.truncate(self.body_start)
                else:
                    raise HTTPException("Can't truncate "
                                        "non-seekable message body")
            elif self.entity_body is None:
                # first time around promote None to a stream for reading
                self.entity_body = StringIO()
                self.body_start = 0
            # Transfer fields are set properly once we have the headers
            self.transferbody = self.entity_body
            self.transferchunked = False
            self.transferlength = None
            self.tboundary = None
            self.tboundary_match = None
            self.transferPos = 0
            self.transferDone = False

    #: recv_mode constant for a set of header lines terminated by CRLF,
    #: followed by a blank line.
    RECV_HEADERS = -3

    #: recv_mode constant for a single CRLF terminated line
    RECV_LINE = -2

    #: recv_mode constant for unlimited data read
    RECV_ALL = -1

    def recv_mode(self):
        """Indicates the type of data expected during recv

        The result is interpreted as follows, using the recv_mode
        constants defined above:

        RECV_HEADERS
            this message is expecting a set of headers, terminated by a
            blank line.  The next call to recv must be with a list of
            binary CRLF terminated strings the last of which must the
            string CRLF only.

        RECV_LINE
            this message is expecting a single terminated line.  The
            next call to recv must be with a binary string representing
            a single terminated line.

        integer > 0
            the minimum number of bytes we are waiting for when data is
            expected.  The next call to recv must be with a binary
            string of up to but not exceeding *integer* number of bytes

        0
            we are currently write-blocked but still need more data, the
            next call to recv must pass None to give the message time to
            write out existing buffered data.

        RECV_ALL
            we want to read until the connection closes, the next call
            to recv must be with a binary string.  The string can be of
            any length but an empty string signals the end of the data.

        None
            the message is not currently in receiving mode, calling
            recv will raise an error."""
        with self.lock:
            if self.transfermode == self.DATA_MODE:
                # How much data do we need?
                if self.transferlength:
                    return self.transferlength - self.transferPos
                else:
                    return self.RECV_ALL
            elif self.transfermode == self.BLOCKED_MODE:
                return 0
            elif self.transfermode in (self.HEADER_MODE,
                                       self.CHUNK_TRAILER_MODE):
                return self.RECV_HEADERS
            elif self.transfermode is not None:
                return self.RECV_LINE
            else:
                return None

    def recv(self, data):
        logging.debug("Message in transfer mode %i", self.transfermode)
        logging.debug("Message receiving: %s", repr(data))
        with self.lock:
            if self.transfermode == self.START_MODE:
                if data == grammar.CRLF:
                    # blank lines are ignored!
                    pass
                else:
                    self.recv_start(data)
                    self.transfermode = self.HEADER_MODE
            elif self.transfermode == self.HEADER_MODE:
                for line in data:
                    self._recv_header(line)
                # we're done reading headers
                self.recv_transferlength()
                if self.transferchunked:
                    self.transfermode = self.CHUNK_HEAD_MODE
                elif self.transferlength is None:
                    # We're going to read forever
                    self.transferPos = 0
                    self.transfermode = self.DATA_MODE
                elif self.transferlength:
                    self.transferPos = 0
                    self.transfermode = self.DATA_MODE
                else:
                    self.transferlength = self.transferPos = 0
                    self.transfermode = None
                self.handle_headers()
            elif self.transfermode == self.CHUNK_HEAD_MODE:
                # Read the chunk size
                chunk = params.Chunk.from_str(data[:-2])
                self.transferlength = chunk.size
                self.transferPos = 0
                if self.transferlength == 0:
                    # We've read the last chunk, parse through any headers
                    self.transfermode = self.CHUNK_TRAILER_MODE
                else:
                    self.transferPos = 0
                    self.transfermode = self.DATA_MODE
            elif self.transfermode == self.DATA_MODE:
                if data:
                    if self.tboundary is not None:
                        match = False
                        i = 0
                        while i < len(data):
                            c = data[i]
                            # scan in progress matches first
                            j = 0
                            while j < len(self.tboundary_match):
                                cpos = self.tboundary_match[j]
                                if self.tboundary[cpos] == c:
                                    cpos += 1
                                    if cpos >= len(self.tboundary):
                                        match = True
                                        break
                                    else:
                                        self.tboundary_match[j] = cpos
                                        j += 1
                                else:
                                    del self.tboundary_match[j]
                            i += 1
                            if match:
                                break
                            if self.tboundary[0] == c:
                                self.tboundary_match.append(1)
                        if match:
                            self.tboundary_match = []
                            self.tboundary = None
                            # switch recv mode at this point
                            self.transferlength = self.transferPos + i
                    self.transferPos += len(data)
                    if self.transferlength:
                        extra = self.transferPos - self.transferlength
                        if extra > 0:
                            logging.warn("%i bytes of spurious data in "
                                         "http Message.recv in %s",
                                         extra,
                                         str(self.get_content_type()))
                            self.recv_buffer = data[:-extra]
                        else:
                            self.recv_buffer = data
                    elif self.transferlength is None:
                        # we're receiving all data
                        self.recv_buffer = data
                    self._recv_buffered()
                else:
                    self.transfer_mode = None
            elif self.transfermode == self.BLOCKED_MODE:
                self._recv_buffered()
            elif self.transfermode == self.CHUNK_END_MODE:
                # must be a naked CRLF
                if data != grammar.CRLF:
                    raise ProtocolError("chunk-data termination error")
                self.transfermode = self.CHUNK_HEAD_MODE
            elif self.transfermode == self.CHUNK_TRAILER_MODE:
                for line in data:
                    self._recv_header(line)
                try:
                    self.transferbody.flush()
                    self.transfermode = None
                except IOError as e:
                    if e.errno == errno.EAGAIN:
                        self.transfermode = self.BLOCKED_MODE
                    else:
                        raise
            else:
                raise HTTPException(
                    "recv_line when in unknown mode: %i" % self.transfermode)
            if self.transfermode is None:
                logging.debug("Message complete")
                if self.transferbody:
                    self.transferbody.flush()
                self.handle_message()

    def recv_start(self, start_line):
        """Receives the start-line

        Implemented differently for requests and responses."""
        raise NotImplementedError

    def _recv_header(self, h):
        # returns True if we need more headers
        if h == grammar.CRLF:
            if self._curr_header:
                # strip off the trailing CRLF
                self.set_header(
                    self._curr_header[0], self._curr_header[1][:-2], True)
                self._curr_header = None
            return False
        else:
            fold = grammar.OctetParser(h).parse_lws()
            if fold and self._curr_header:
                # a continuation line
                self._curr_header[1] = self._curr_header[1] + h
            else:
                if self._curr_header:
                    self.set_header(
                        self._curr_header[0], self._curr_header[1][:-2], True)
                ch = h.split(':', 1)
                if len(ch) == 2:
                    self._curr_header = ch
                else:
                    # badly formed header line
                    raise ProtocolError(
                        "Badly formed header line: %s" % repr(h))
            return True

    def _recv_buffered(self):
        if self.recv_buffer:
            if isinstance(self.entity_body, io.IOBase):
                # write may accept fewer bytes
                while self.recv_buffer:
                    written = self.transferbody.write(self.recv_buffer)
                    if written is None:
                        # we're blocked
                        break
                    self.body_started = True
                    if written < len(self.recv_buffer):
                        self.recv_buffer = self.recv_buffer[written:]
                    else:
                        self.recv_buffer = None
            else:
                # file-like behaviour, assume a blocking write with no
                # return result
                self.transferbody.write(self.recv_buffer)
                self.body_started = True
                self.recv_buffer = None
        if self.recv_buffer:
            self.transfermode = self.BLOCKED_MODE
        elif self.transferchunked:
            if self.transferPos >= self.transferlength:
                self.transfermode = self.CHUNK_END_MODE
            else:
                self.transfermode = self.DATA_MODE
        elif self.transferlength is None:
            # we're in read forever mode
            self.transfermode = self.DATA_MODE
        elif self.transferPos >= self.transferlength:
            # not chunked, defined message body length
            # we need a flushing mode here
            try:
                self.transferbody.flush()
                self.transfermode = None
            except IOError as e:
                if e.errno == errno.EAGAIN:
                    self.transfermode = self.BLOCKED_MODE
                else:
                    raise
        else:
            # not chunked, still reading
            self.transfermode = self.DATA_MODE

    def handle_headers(self):
        """Hook for processing the message headers

        This method is called after all headers have been received but
        before the message body (if any) is received.  Derived classes
        should always call this implementation first (using super) to
        ensure basic validation is performed on the message before the
        body is received."""
        content_type = self.get_content_type()
        if content_type is not None and content_type.type == "multipart":
            # there must be boundary parameter
            try:
                content_type['boundary']
            except KeyError:
                raise ProtocolError("missing boundary parameter in "
                                    "multipart type")
        connection = self.get_connection()
        if (self.protocol <= params.HTTP_1p0 or
                (connection is not None and "close" in connection)):
            self.clear_keep_alive()

    def recv_transferlength(self):
        """Called to calculate the transfer length when receiving

        The values of :py:attr:`transferlength` and
        :py:attr:`transferchunked` are set by this method.  The default
        implementation checks for a Transfer-Encoding header and
        then a Content-Length header in that order.

        If it finds neither then behaviour is determined by the derived
        classes :py:class:`Request` and :py:class:`Response` which wrap
        this implementation.

        RFC2616:

            If a Transfer-Encoding header field is present and has any
            value other than "identity", then the transfer-length is
            defined by use of the "chunked" transfer-coding, unless the
            message is terminated by closing the connection

        This is a bit weird, if I have a non-identity value which fails
        to mention 'chunked' then it seems like I can't imply chunked
        encoding until the connection closes.  In practice, when we
        handle this case we assume chunked is not being used and read
        until connection close."""
        self.transferchunked = False
        # If there is an encoding other than 'identity' then we're using
        # chunked
        encoding = self.get_transfer_encoding()
        if encoding is not None and not (
                len(encoding) == 1 and encoding[0].token == "identity"):
            if encoding[-1].token == "chunked":
                self.transferchunked = True
            else:
                self.transferchunked = False
            self.transferlength = None
            if len(encoding) > 1:
                encoding = encoding[:-1]
                encoding.reverse()
                for enc in encoding:
                    if enc.token == "gzip":
                        # wrap the body in a gzip decoding wrapper
                        self.transferbody = GzipDecoder(self.transferbody)
                    elif enc.token == "identity":
                        continue
                    else:
                        raise NotImplementedError(
                            "Unsupported transfer encoding %s" % enc.token)
            # overrides any Content-Length setting
            return
        # If there is a Content-Length header
        content_length = self.get_content_length()
        if content_length is not None:
            self.transferlength = content_length
            return
        content_type = self.get_content_type()
        if (content_type and
                MULTIPART_BYTERANGES_RANGE.match_media_type(content_type)):
            # working looking at this thread:
            # http://lists.w3.org/Archives/Public/ietf-http-wg/2013AprJun/0505.html
            # this self-delimiting format never worked, for a number of
            # reasons but the ambiguity over the trailing CRLF is a
            # clincher for me as there is a real danger of getting it
            # wrong.  Fortunately, seems like people do use a content
            # length in practice so we should normally be OK.
            ctokens = self.get_connection()
            if "close" not in ctokens:
                logging.warn("multipart/byteranges message with implicit "
                             "content-length will terminate connection")
            self.clear_keep_alive()
            # but to make sure we don't massively over-read we do track
            # the boundary
            try:
                self.tboundary = "\r\n--%s--" % content_type[
                    'boundary'].strip()
                self.tboundary_match = []
            except KeyError:
                raise ProtocolError("boundary required on multipart type %s" %
                                    str(content_type))
        # No Content-Length or Transfer-Encoding means no body is
        # expected for a request, but for a response may indicate the
        # need to read until EOF, connection termination etc.
        self.transferlength = None

    def handle_message(self):
        """Hook for processing the message

        This method is called after the entire message has been
        received, including any chunk trailer."""
        pass

    def get_headerlist(self):
        """Returns all header names

        The list is alphabetically sorted and lower-cased."""
        with self.lock:
            hlist = self.headers.keys()
            hlist.sort()
            return hlist

    def has_header(self, field_name):
        """True if this message has a header with field_name"""
        with self.lock:
            return field_name.lower() in self.headers

    def get_header(self, field_name, list_mode=False):
        """Returns the header with *field_name* as a string.

        list_mode=False
            In this mode, get_header always returns a single string,
            this isn't always what you want as it automatically 'folds'
            multiple headers with the same name into a string using ", "
            as a separator.

        list_mode=True
            In this mode, get_header always returns a list of strings.

        If there is no header with *field_name* then None is returned
        in both modes."""
        with self.lock:
            h = self.headers.get(field_name.lower(), [None, None])
            if h[1] is None:
                return None
            if list_mode:
                return h[1:]
            else:
                return string.join(h[1:], ", ")

    def set_header(self, field_name, field_value, append_mode=False):
        """Sets the header with *field_name* to the string *field_value*.

        If *field_value* is None then the header is removed (if present).

        If a header already exists with *field_name* then the behaviour is
        determined by *append_mode*:

        append_mode==True
                *field_value* is joined to the existing value using ", " as
                a separator.

        append_mode==False (Default)
                *field_value* replaces the existing value."""
        with self.lock:
            fieldname_key = field_name.lower()
            if field_value is None:
                if fieldname_key in self.headers:
                    del self.headers[fieldname_key]
            else:
                if fieldname_key in self.headers and append_mode:
                    self.headers[fieldname_key].append(field_value.strip())
                    # field_value = self.headers[
                    #    fieldname_key][1] + ", " + field_value.strip()
                else:
                    field_value = field_value.strip()
                    self.headers[fieldname_key] = [field_name, field_value]

    def get_allow(self):
        """Returns an :py:class:`Allow` instance or None if no "Allow"
        header is present."""
        field_value = self.get_header("Allow")
        if field_value is not None:
            return Allow.from_str(field_value)
        else:
            return None

    def set_allow(self, allowed):
        """Sets the "Allow" header, replacing any existing value.

        *allowed*
                A :py:class:`Allow` instance or a string that one can be
                parsed from.

        If allowed is None any existing Allow header is removed."""
        if allowed is None:
            self.set_header("Allow", None)
        else:
            if type(allowed) in types.StringTypes:
                allowed = Allow.from_str(allowed)
            if not isinstance(allowed, Allow):
                raise TypeError
            self.set_header("Allow", str(allowed))

    def get_authorization(self):
        """Returns a :py:class:`~pyslet.http.auth.Credentials`
        instance.

        If there are no credentials None returned."""
        field_value = self.get_header("Authorization")
        if field_value is not None:
            return auth.Credentials.from_str(field_value)
        else:
            return None

    def set_authorization(self, credentials):
        """Sets the "Authorization" header

        credentials
                a :py:class:`~pyslet.http.auth.Credentials` instance"""
        self.set_header("Authorization", str(credentials))

    def get_cache_control(self):
        """Returns an :py:class:`CacheControl` instance or None if no
        "Cache-Control" header is present."""
        field_value = self.get_header("Cache-Control")
        if field_value is not None:
            return CacheControl.from_str(field_value)
        else:
            return None

    def set_cache_control(self, cc):
        """Sets the "Cache-Control" header, replacing any existing value.

        *cc*
                A :py:class:`CacheControl` instance or a string that one can
                be parsed from.

        If *cc* is None any existing Cache-Control header is removed."""
        if cc is None:
            self.set_header("Cache-Control", None)
        else:
            if type(cc) in types.StringTypes:
                cc = CacheControl.from_str(cc)
            if not isinstance(cc, CacheControl):
                raise TypeError
            self.set_header("Cache-Control", str(cc))

    def get_connection(self):
        """Returns a set of connection tokens from the Connection header

        If no Connection header was present an empty set is returned.
        All tokens are returned as lower case."""
        field_value = self.get_header("Connection")
        if field_value:
            hp = HeaderParser(field_value)
            return set(map(lambda x: x.lower(), hp.parse_tokenlist()))
        else:
            return set()

    def set_connection(self, connection_tokens):
        """Set the Connection tokens from an iterable set of
        *connection_tokens*

        If the list is empty any existing header is removed."""
        if connection_tokens:
            self.set_header(
                "Connection", string.join(list(connection_tokens), ", "))
        else:
            self.set_header("Connection", None)

    def get_content_encoding(self):
        """Returns a *list* of lower-cased content-coding tokens from
        the Content-Encoding header

        If no Content-Encoding header was present an empty list is
        returned.

        Content-codings are always listed in the order they have been
        applied."""
        field_value = self.get_header("Content-Encoding")
        if field_value:
            hp = HeaderParser(field_value)
            return map(lambda x: x.lower(), hp.parse_tokenlist())
        else:
            return []

    def set_content_encoding(self, content_codings):
        """Sets the Content-Encoding header from a an iterable list of
        *content-coding* tokens.  If the list is empty any existing
        header is removed."""
        if content_codings:
            self.set_header(
                "Content-Encoding", string.join(list(content_codings), ", "))
        else:
            self.set_header("Content-Encoding", None)

    def get_content_language(self):
        """Returns a *list* of :py:class:`LanguageTag` instances from
        the Content-Language header

        If no Content-Language header was present an empty list is
        returned."""
        field_value = self.get_header("Content-Language")
        if field_value:
            return params.LanguageTag.list_from_str(field_value)
        else:
            return []

    def set_content_language(self, lang_list):
        """Sets the Content-Language header from a an iterable list of
        :py:class:`LanguageTag` instances."""
        if lang_list:
            self.set_header(
                "Content-Language", string.join(map(str, lang_list), ", "))
        else:
            self.set_header("Content-Language", None)

    def get_content_length(self):
        """Returns the integer size of the entity from the
        Content-Length header

        If no Content-Length header was present None is returned."""
        field_value = self.get_header("Content-Length")
        if field_value is not None:
            return int(field_value.strip())
        else:
            return None

    def set_content_length(self, length):
        """Sets the Content-Length header from an integer or removes it
        if *length* is None."""
        if length is None:
            self.set_header("Content-Length", None)
        else:
            self.set_header("Content-Length", str(length))

    def get_content_location(self):
        """Returns a :py:class:`pyslet.rfc2396.URI` instance created from
        the Content-Location header.

        If no Content-Location header was present None is returned."""
        field_value = self.get_header("Content-Location")
        if field_value is not None:
            return uri.URI.from_octets(field_value.strip())
        else:
            return None

    def set_content_location(self, location):
        """Sets the Content-Location header from location, a
        :py:class:`pyslet.rfc2396.URI` instance or removes it if
        *location* is None."""
        if location is None:
            self.set_header("Content-Location", None)
        else:
            self.set_header("Content-Location", str(location))

    def get_content_md5(self):
        """Returns a 16-byte binary string read from the Content-MD5
        header or None if no Content-MD5 header was present.

        The result is suitable for comparing directly with the output
        of the Python's MD5 digest method."""
        field_value = self.get_header("Content-MD5")
        if field_value is not None:
            return base64.b64decode(field_value.strip())
        else:
            return None

    def set_content_md5(self, digest):
        """Sets the Content-MD5 header from a 16-byte binary string
        returned by Python's MD5 digest method or similar.  If digest is
        None any existing Content-MD5 header is removed."""
        if digest is None:
            self.set_header("Content-MD5", None)
        else:
            self.set_header("Content-MD5", base64.b64encode(digest))

    def get_content_range(self):
        """Returns a :py:class:`ContentRange` instance parsed from the
        Content-Range header.

        If no Content-Range header was present None is returned."""
        field_value = self.get_header("Content-Range")
        if field_value is not None:
            return ContentRange.from_str(field_value)
        else:
            return None

    def set_content_range(self, range):
        """Sets the Content-Range header from range, a
        :py:class:`ContentRange` instance or removes it if
        *range* is None."""
        if range is None:
            self.set_header("Content-Range", None)
        else:
            self.set_header("Content-Range", str(range))

    def get_content_type(self):
        """Returns a :py:class:`MediaType` instance parsed from the
        Content-Type header.

        If no Content-Type header was present None is returned."""
        field_value = self.get_header("Content-Type")
        if field_value is not None:
            mtype = params.MediaType.from_str(field_value)
            return mtype
        else:
            return None

    def set_content_type(self, mtype=None):
        """Sets the Content-Type header from mtype, a
        :py:class:`MediaType` instance, or removes it if
        *mtype* is None."""
        if mtype is None:
            self.set_header('Content-Type', None)
        else:
            self.set_header('Content-Type', str(mtype))

    def get_date(self):
        """Returns the value of the Date header.

        The return value is a :py:class:`params.FullDate` instance. If
        no Date header was present None is returned."""
        field_value = self.get_header("Date")
        if field_value is not None:
            return params.FullDate.from_http_str(field_value)
        else:
            return None

    def set_date(self, date=None):
        """Sets the value of the Date header

        date
            a :py:class:`params.FullDate` instance or None
            to remove the Date header.

        To set the date header to the current date use::

            set_date(params.FullDate.from_now_utc())"""
        if date is None:
            self.set_header("Date", None)
        else:
            self.set_header("Date", str(date))

    def get_expect_continue(self):
        field_value = self.get_header("Expect")
        if field_value is not None:
            return field_value.strip().lower() == "100-continue"
        else:
            return False

    def set_expect_continue(self, flag=True):
        if flag:
            self.set_header("Expect", "100-continue")
        else:
            self.set_header("Expect", None)

    def get_last_modified(self):
        """Returns the value of the Last-Modified header

        The result is a :py:class:`params.FullDate` instance.  If no
        Last-Modified header was present None is returned."""
        field_value = self.get_header("Last-Modified")
        if field_value is not None:
            return params.FullDate.from_http_str(field_value)
        else:
            return None

    def set_last_modified(self, date=None):
        """Sets the value of the Last-Modified header field

        date
            a :py:class:`FullDate` instance or None to remove
            the header

        To set the Last-Modified header to the current date use::

            set_last_modified(params.FullDate.from_now_utc())"""
        if date is None:
            self.set_header("Last-Modified", None)
        else:
            self.set_header("Last-Modified", str(date))

    def _check_transfer_encoding(self, telist):
        chunked = False
        for te in telist:
            if te.token == "chunked":
                if chunked:
                    raise ProtocolError("chunked transfer-coding applied "
                                        "multiple times to message")
                chunked = True
            else:
                if chunked:
                    raise ProtocolError("%s transfer-coding applied "
                                        "after chunked" % te.token)

    def get_transfer_encoding(self):
        """Returns a list of :py:class:`params.TransferEncoding`

        If no TransferEncoding header is present None is returned."""
        field_value = self.get_header("Transfer-Encoding")
        if field_value is not None:
            telist = params.TransferEncoding.list_from_str(field_value)
            # validate the list at this point
            self._check_transfer_encoding(telist)
            return telist
        else:
            return None

    def set_transfer_encoding(self, field_value):
        """Set the Transfer-Encoding header

        field_value
            A list of :py:class:`params.TransferEncoding` instances or a
            string from which one can be parsed.  If None then the
            header is removed."""
        if isinstance(field_value, types.StringTypes):
            field_value = params.TransferEncoding.list_from_str(field_value)
        if field_value is not None:
            self._check_transfer_encoding(field_value)
            self.set_header("Transfer-Encoding",
                            string.join(map(str, field_value), ", "))
        else:
            self.set_header("Transfer-Encoding", None)

    def get_upgrade(self):
        field_value = self.get_header("Upgrade")
        if field_value:
            hp = HeaderParser(field_value)
            return hp.require_product_token_list()
        else:
            return []

    def set_upgrade(self, protocols):
        """Sets the "Upgrade" header, replacing any existing value.

        protocols
            An iterable list of :py:class:`params.ProductToken` instances.

        In addition to setting the upgrade header this method ensures
        that "upgrade" is present in the Connection header."""
        if protocols:
            self.set_header(
                "Upgrade", string.join(map(str, protocols), ", "))
            connection = self.get_connection()
            if "upgrade" not in connection:
                connection.add("upgrade")
                self.set_connection(connection)
        else:
            self.set_header("Connection", None)

    def get_host(self):
        return self.get_header("Host")

    def set_host(self, server):
        self.set_header("Host", server)


class Request(Message):

    # a mapping from upper case method name to True/False with True
    # indicating the method is idempotent
    IDEMPOTENT = {"GET": True, "HEAD": True, "PUT": True, "DELETE": True,
                  "OPTIONS": True, "TRACE": True, "CONNECT": False,
                  "POST": False}

    def __init__(self, **kwargs):
        super(Request, self).__init__(**kwargs)
        #: the http method, always upper case, e.g., 'POST'
        self.method = None
        #: the request uri as it appears in the start line
        self.request_uri = None
        #: the associated response
        self.response = None

    def send_start(self):
        """Returns the start-line for this message"""
        logging.info("Sending request to %s", self.get_host())
        start = self.get_start()
        logging.info(start)
        return (start + "\r\n")

    def send_transferlength(self):
        """Adds request-specific processing for transfer-length

        Request messages that must not have a message body are
        automatically detected and will raise an exception if they have
        a non-None body.

        Request messages that may have a message body but have a
        transfer-length of 0 bytes will have a Content-Length header of
        0 added if necessary"""
        super(Request, self).send_transferlength()
        if self.method.upper() in ("GET", "HEAD", "DELETE"):
            if self.transferlength is None or self.transferlength > 0:
                # can't send a message body with these requests
                raise HTTPException("message body not allowed for %s" %
                                    self.method)
        elif self.transferlength == 0:
            # by implication, a content length header is required here
            if self.get_content_length() is None:
                self.set_content_length(0)
        elif self.transferlength is None:
            if not self.transferchunked:
                # a request cannot be sent forever so this message is
                # unsendable (presumably because we're targetting a
                # server that is not known to speak HTTP/1.1
                self.entity_body = BufferedEntityWrapper(
                    self.entity_body, self.MAX_READAHEAD)
                if self.entity_body.length is not None:
                    # small enough to be buffered, try again
                    logging.warn("Request buffered to send to HTTP/1.0 server")
                    self.body_len = self.entity_body.length
                    super(Request, self).send_transferlength()
                else:
                    logging.error("Request too large for buffering")
                    raise HTTPException("Length required for HTTP/1.0 server")

    def recv_start(self, line):
        # Read the request line
        line = line[:-2]
        start_items = line.split()
        try:
            if len(start_items) != 3:
                raise ValueError
            self.method = start_items[0].upper()
            grammar.check_token(self.method)
            self.request_uri = start_items[1]
            self.protocol = params.HTTPVersion.from_str(start_items[2])
        except ValueError:
            raise ProtocolError("Badly formed Request-line: %s" % line)

    def recv_transferlength(self):
        super(Request, self).recv_transferlength()
        if self.transferlength is None and not self.transferchunked:
            # if we are left with transferlength == None and no chunked
            # encoding it means there is no message body as requests
            # cannot hang up to indicate the end of the message
            self.transferlength = 0

    def get_start(self):
        """Returns the start line"""
        with self.lock:
            return "%s %s %s" % (self.method, self.request_uri,
                                 str(self.protocol))

    def is_idempotent(self):
        """Returns True if this is an idempotent request"""
        return self.method and self.IDEMPOTENT.get(self.method, False)

    def set_method(self, method):
        with self.lock:
            self.method = method.upper()

    def set_request_uri(self, uri):
        with self.lock:
            self.request_uri = uri

    def extract_authority(self):
        """Extracts the authority from the request

        If the request_uri is an absolute URL then it is updated to
        contain the absolute path only and the Host header is updated
        with the authority information (host[:port]) extracted from it,
        otherwise the Host header is read for the authority information.
        If there is no authority information in the request None is
        returned.

        If the url contains user information it raises
        NotImplementedError"""
        url = uri.URI.from_octets(self.request_uri)
        if url.is_absolute():
            if not isinstance(url, params.HTTPURL):
                raise HTTPException(
                    "Scheme not supported: %s" % url.scheme)
            if url.userinfo:
                raise NotImplementedError(
                    "username(:password) in URL not yet supported")
            if url.host:
                authority = url.host
                if url.port:
                    # custom port, perhaps?
                    port = int(url.port)
                    if port != url.DEFAULT_PORT:
                        authority = authority + ":" + url.port
                self.set_host(authority)
            else:
                raise HTTPException("No host in request URL")
        else:
            authority = self.get_host()
        if url.abs_path:
            self.request_uri = url.abs_path
        elif url.rel_path:
            raise ValueError(
                "request URI cannot be relative: %s" % self.request_uri)
        else:
            self.request_uri = "/"
        if url.query is not None:
            self.request_uri = self.request_uri + '?' + url.query
        return authority

    def get_accept(self):
        """Returns an :py:class:`AcceptList` instance or None if no
        "Accept" header is present."""
        field_value = self.get_header("Accept")
        if field_value is not None:
            return AcceptList.from_str(field_value)
        else:
            return None

    def set_accept(self, accept_value):
        """Sets the "Accept" header, replacing any existing value.

        *accept_value*
                A :py:class:`AcceptList` instance or a string that one can
                be parsed from."""
        if type(accept_value) in types.StringTypes:
            accept_value = AcceptList.from_str(accept_value)
        if not isinstance(accept_value, AcceptList):
            raise TypeError
        self.set_header("Accept", str(accept_value))

    def get_accept_charset(self):
        """Returns an :py:class:`AcceptCharsetList` instance or None if
        no "Accept-Charset" header is present."""
        field_value = self.get_header("Accept-Charset")
        if field_value is not None:
            return AcceptCharsetList.from_str(field_value)
        else:
            return None

    def set_accept_charset(self, accept_value):
        """Sets the "Accept-Charset" header, replacing any existing value.

        *accept_value*
                A :py:class:`AcceptCharsetList` instance or a string that
                one can be parsed from."""
        if type(accept_value) in types.StringTypes:
            accept_value = AcceptCharsetList.from_str(accept_value)
        if not isinstance(accept_value, AcceptCharsetList):
            raise TypeError
        self.set_header("Accept-Charset", str(accept_value))

    def get_accept_encoding(self):
        """Returns an :py:class:`AcceptEncodingList` instance or None if
        no "Accept-Encoding" header is present."""
        field_value = self.get_header("Accept-Encoding")
        if field_value is not None:
            return AcceptEncodingList.from_str(field_value)
        else:
            return None

    def set_accept_encoding(self, accept_value):
        """Sets the "Accept-Encoding" header, replacing any existing value.

        *accept_value*
                A :py:class:`AcceptEncodingList` instance or a string that
                one can be parsed from."""
        if type(accept_value) in types.StringTypes:
            accept_value = AcceptEncodingList.from_str(accept_value)
        if not isinstance(accept_value, AcceptEncodingList):
            raise TypeError
        self.set_header("Accept-Encoding", str(accept_value))

    def get_cookie(self):
        """Reads the 'Cookie' header(s)

        Returns a dictionary of cookies.  If there are multiple values
        for a cookie the dictionary value is a set, otherwise it is a
        string."""
        field_value = self.get_header('Cookie')
        if field_value is not None:
            p = cookie.CookieParser(field_value)
            cookies = p.require_cookie_string()
            return cookies
        else:
            return {}

    def set_cookie(self, cookie_list):
        """Set a "Set-Cookie" header

        cookie_list
            a list of cookies such as would be returned by
            :meth:`pyslet.http.cookie.CookieStore.search`.

        If cookie list is None the Cookie header is removed."""
        if cookie_list is None:
            self.set_header('Cookie', None)
        else:
            self.set_header(
                'Cookie',
                string.join(map(lambda x: "%s=%s" % (x.name, x.value),
                                cookie_list), "; "))


class Response(Message):

    #: A dictionary mapping status code integers to their default
    #: message defined by RFC2616
    REASON = {
        100: "Continue",
        101: "Switching Protocols",
        200: "OK",
        201: "Created",
        202: "Accepted",
        203: "Non-Authoritative Information",
        204: "No Content",
        205: "Reset Content",
        206: "Partial Content",
        300: "Multiple Choices",
        301: "Moved Permanently",
        302: "Found",
        303: "See Other",
        304: "Not Modified",
        305: "Use Proxy",
        307: "Temporary Redirect",
        400: "Bad Request",
        401: "Unauthorized",
        402: "Payment Required",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        406: "Not Acceptable",
        407: "Proxy Authentication Required",
        408: "Request Time-out",
        409: "Conflict",
        410: "Gone",
        411: "Length Required",
        412: "Precondition Failed",
        413: "Request Entity Too Large",
        414: "Request-URI Too Large",
        415: "Unsupported Media Type",
        416: "Requested range not satisfiable",
        417: "Expectation Failed",
        500: "Internal Server Error",
        501: "Not Implemented",
        502: "Bad Gateway",
        503: "Service Unavailable",
        504: "Gateway Time-out",
        505: "HTTP Version not supported"
    }

    def __init__(self, request, **kwargs):
        super(Response, self).__init__(**kwargs)
        self.request = request
        request.response = self
        self.status = None
        self.reason = None

    def get_status(self):
        with self.lock:
            return self.status, self.reason

    def set_status(self, status, reason=None):
        with self.lock:
            self.status = status
            if reason is None:
                # set the default string for this status
                self.reason = self.REASON.get(self.status, "No reason")
            else:
                self.reason = reason

    def send_start(self):
        """Returns the start-line for this message"""
        if self.protocol is None:
            self.protocol = params.HTTP_1p1
        if self.status is None:
            self.status = 500
        if self.reason is None:
            self.reason = 'No reason'
        logging.info(
            "Sending response: %s %s %s",
            str(self.protocol), str(self.status), self.reason)
        return ("%s %s %s\r\n" %
                (str(self.protocol), str(self.status), self.reason))

    def send_transferlength(self):
        if (self.status // 100 == 1 or self.status == 204 or
                self.status == 304 or self.request.method == 'HEAD'):
            if self.entity_body is not None and self.body_len != 0:
                raise HTTPException(
                    "entity body not allowed in %i response to %s request" %
                    (self.status, self.request.method))
            self.transferchunked = False
            self.transferlength = 0
            self.transferbody = None
            return
        if (self.request.protocol <= params.HTTP_1p0 or
                self.protocol <= params.HTTP_1p0):
            # we mustn't use a transfer encoding, if we don't
            # know the size of the body write forever
            encoding = self.get_transfer_encoding()
            if encoding:
                # remove it!
                self.set_transfer_encoding(None)
            content_length = self.get_content_length()
            if (content_length is None and
                    self.entity_body is not None and
                    self.body_len is None):
                # entity body of unknown size, send forever
                self.transferchunked = False
                self.transferlength = None
                self.transferbody = self.entity_body
                self.clear_keep_alive()
                return
        if self.entity_body is None:
            # no body, the client expects a content length at least
            self.set_content_length(0)
            self.transferchunked = False
            self.transferlength = 0
            self.transferbody = None
            return
        # drop through to default behaviour
        super(Response, self).send_transferlength()

    def start_receiving(self):
        super(Response, self).start_receiving()
        self.status = None
        self.reason = None

    def recv_start(self, line):
        # Read the status line
        pstatus = params.ParameterParser(line[:-2], ignore_sp=False)
        self.protocol = pstatus.parse_http_version()
        pstatus.parse_sp()
        if pstatus.is_integer():
            self.status = pstatus.parse_integer()
        else:
            self.status = 0
        pstatus.parse_sp()
        self.reason = pstatus.parse_remainder()

    def recv_transferlength(self):
        self.transferchunked = False
        if (self.status // 100 == 1 or self.status == 204 or
                self.status == 304 or self.request.method == 'HEAD'):
            self.transferchunked = False
            self.transferlength = 0
            return
        super(Response, self).recv_transferlength()
        # if we are left with transferlength == None and no chunked
        # encoding it means we'll request bytes until the server hangs
        # up.

    def get_accept_ranges(self):
        """Returns an :py:class:`AcceptRanges` instance or None if no
        "Accept-Ranges" header is present."""
        field_value = self.get_header("Accept-Ranges")
        if field_value is not None:
            return AcceptRanges.from_str(field_value)
        else:
            return None

    def set_accept_ranges(self, accept_value):
        """Sets the "Accept-Ranges" header, replacing any existing value.

        *accept_value*
                A :py:class:`AcceptRanges` instance or a string that
                one can be parsed from."""
        if type(accept_value) in types.StringTypes:
            accept_value = AcceptRanges.from_str(accept_value)
        if not isinstance(accept_value, AcceptRanges):
            raise TypeError
        self.set_header("Accept-Ranges", str(accept_value))

    def get_age(self):
        """Returns an integer or None if no "Age" header is present."""
        field_value = self.get_header("Age")
        if field_value is not None:
            hp = HeaderParser(field_value)
            return hp.require_production_end(hp.parse_delta_seconds())
        else:
            return None

    def set_age(self, age):
        """Sets the "Age" header, replacing any existing value.

        age
                an integer or long value or None to remove the header"""
        if age is None:
            self.set_header("Age", None)
        else:
            self.set_header("Age", str(age))

    def get_etag(self):
        """Returns a :py:class:`EntityTag` instance parsed from the ETag
        header or None if no "ETag" header is present."""
        field_value = self.get_header("ETag")
        if field_value is not None:
            return params.EntityTag.from_str(field_value)
        else:
            return None

    def set_etag(self, etag):
        """Sets the "ETag" header, replacing any existing value.

        etag
                a :py:class:`EntityTag` instance or None to remove
                any ETag header."""
        if etag is None:
            self.set_header("ETag", None)
        else:
            self.set_header("ETag", str(etag))

    def get_location(self):
        """Returns a :py:class:`pyslet.rfc2396.URI` instance created from
        the Location header.

        If no Location header was present None is returned."""
        field_value = self.get_header("Location")
        if field_value is not None:
            return uri.URI.from_octets(field_value)
        else:
            return None

    def set_location(self, location):
        """Sets the Location header

        location:
            a :py:class:`pyslet.rfc2396.URI` instance or a string
            from which one can be parsed.  If None, the Location
            header is removed."""
        if isinstance(location, str):
            location = uri.URI.from_octets(location)
        if not isinstance(location, uri.URI):
            raise TypeError
        if not location.is_absolute():
            raise HTTPException("Location header must be an absolute URI")
        if location is None:
            self.set_header("Location", None)
        else:
            self.set_header("Location", str(location))

    def get_www_authenticate(self):
        """Returns a list of :py:class:`~pyslet.rfc2617.Challenge`
        instances.

        If there are no challenges an empty list is returned."""
        field_value = self.get_header("WWW-Authenticate")
        if field_value is not None:
            return auth.Challenge.list_from_str(field_value)
        else:
            return None

    def set_www_authenticate(self, challenges):
        """Sets the "WWW-Authenticate" header, replacing any exsiting
        value.

        challenges
                a list of :py:class:`~pyslet.rfc2617.Challenge` instances"""
        if challenges is None:
            self.set_header("WWW-Authenticate", None)
        else:
            self.set_header(
                "WWW-Authenticate", string.join(map(str, challenges), ", "))

    def get_set_cookie(self):
        """Reads all 'Set-Cookie' headers

        Returns a list of :class:`~pyslet.http.cookie.Cookie` instances
        """
        field_value = self.get_header('Set-Cookie', list_mode=True)
        if field_value is not None:
            return map(cookie.Cookie.from_str, field_value)
        else:
            return None

    def set_set_cookie(self, cookie, replace=False):
        """Set a "Set-Cookie" header

        cookie
            a :class:`~pyslet.http.cookie.Cookie` instance

        replace=True
            Remove all existing cookies from the response

        replace=False
            Add this cookie to the existing cookies in the response
            (default value)

        If called multiple times the header value will become a list
        of cookie values.  No folding together is performed.

        If cookie is None all Set-Cookie headers are removed,
        implying replace mode."""
        if cookie is None:
            self.set_header('Set-Cookie', None)
        else:
            if replace:
                self.set_header('Set-Cookie', None)
            self.set_header(
                'Set-Cookie', str(cookie), append_mode=True)


class MediaRange(params.MediaType):

    """Represents an HTTP media-range.

    The built-in str function can be used to format instances according
    to the grammar defined in the specification.

    Instances are immutable, they define comparison methods and a hash
    implementation to allow them to be used as keys in dictionaries.
    Quoting from the specification:

        "Media ranges can be overridden by more specific media ranges or
        specific media types. If more than one media range applies to a
        given type, the most specific reference has precedence."

    In other words, the following media ranges would be sorted in the
    order shown:

    1.  image/png
    2.  image/\*
    3.  text/plain;charset=utf-8
    4.  text/plain
    5.  text/\*
    6.  \*/\*

    If we have two rules with identical precedence then we sort them
    alphabetically by type; sub-type and ultimately alphabetically by
    parameters"""

    def __init__(self, type="*", subtype="*", parameters={}):
        super(MediaRange, self).__init__(type, subtype, parameters)

    @classmethod
    def from_str(cls, source):
        """Creates a media-rannge from a *source* string.

        Unlike the parent media-type we ignore all spaces."""
        p = HeaderParser(source)
        mr = p.require_media_range()
        p.require_end("media-range")
        return mr

    def __repr__(self):
        return ("MediaType(%s,%s,%s)" % (repr(self.type),
                                         repr(self.subtype),
                                         repr(self.parameters)))

    def __cmp__(self, other):
        result = self._cmp_types(self.type, other.type)
        if result:
            return result
        result = self._cmp_types(self.subtype, other.subtype)
        if result:
            return result
        # more parameters means higher precedence
        result = -cmp(len(self.parameters), len(other.parameters))
        if result:
            return result
        return cmp(self.parameters, other.parameters)

    def _cmp_types(self, atype, btype):
        if atype == '*':
            if btype == '*':
                return 0
            else:
                return 1
        else:
            if btype == '*':
                return -1
            else:
                return cmp(atype.lower(), btype.lower())

    def match_media_type(self, mtype):
        """Tests whether a media-type matches this range.

        mtype
            A :py:class:`MediaType` instance to be compared to this
            range.

        The matching algorithm takes in to consideration wild-cards so
        that \*/\* matches all types, image/\* matches any image type
        and so on.

        If a media-range contains parameters then each of these must be
        matched exactly in the media-type being tested. Parameter names
        are treated case-insensitively and any additional parameters in
        the media type are ignored.  As a result:

        *   text/plain *does not match* the range
            text/plain;charset=utf-8

        *   application/myapp;charset=utf-8;option=on *does* match the
            range application/myapp;option=on

        """
        if self.type == '*':
            return True
        elif self.type.lower() != mtype.type.lower():
            return False
        if self.subtype == '*':
            return True
        elif self.subtype.lower() != mtype.subtype.lower():
            return False
        # all the parameters in the range must be matched
        for p, v in self._hp:
            if p not in mtype.parameters or mtype.parameters[p][1] != v:
                # e.g. suppose we have a range
                # type/subtype;paramA=1;paramB=2 then
                # type/subtype;paramA=1 does not match (we needed
                # paramB=2 as well) and type/subtype;paramA=1;paramB=3
                # does not match either but
                # type/subtype;paramA=1;paramB=2;paramC=3 does match
                return False
        return True


MULTIPART_BYTERANGES_RANGE = MediaRange("multipart", "byteranges")


class AcceptItem(MediaRange):

    """Represents a single item in an Accept header

    The built-in str function can be used to format instances according
    to the grammar defined in the specification.

    Instances are immutable, they define comparison
    methods and a hash implementation to allow them to be used as keys
    in dictionaries.

    Accept items are sorted by their media ranges.  Equal media ranges
    sort by *descending* qvalue, for example:

            text/plain;q=0.75 < text/plain;q=0.5

    Extension parameters are ignored in all comparisons."""

    def __init__(self, range=MediaRange(), qvalue=1.0, extensions={}):
        #: the :py:class:`MediaRange` instance that is acceptable
        self.range = range
        self.q = qvalue         #: the q-value (defaults to 1.0)
        self.params = extensions  # : any accept-extension parameters

    @classmethod
    def from_str(cls, source):
        """Creates a single AcceptItem instance from a *source* string."""
        p = HeaderParser(source)
        p.parse_sp()
        ai = p.require_accept_item()
        p.parse_sp()
        p.require_end("Accept header item")
        return ai

    def __str__(self):
        result = [str(self.range)]
        if self.params or self.q != 1.0:
            qstr = "%.3f" % self.q
            qstr = qstr.rstrip('0')
            qstr = qstr.rstrip('.')
            result.append("; q=%s" % qstr)
            result.append(grammar.format_parameters(self.params))
        return string.join(result, '')

    def __cmp__(self, other):
        if type(other) in types.StringTypes:
            other = AcceptItem.from_str(other)
        elif not isinstance(other, AcceptItem):
            raise TypeError
        result = cmp(self.range, other.range)
        if result == 0:
            if self.q > other.q:
                return -1
            elif self.q < other.q:
                return 1
        return result

    def __hash__(self):
        return hash(self.range, self.q)


class AcceptList(object):

    """Represents the value of an Accept header

    The built-in str function can be used to format instances according
    to the grammar defined in the specification.

    Instances are immutable, they are constructed from one or more
    :py:class:`AcceptItem` instances.  There are no comparison methods.

    Instances behave like read-only lists implementing len, indexing and
    iteration in the usual way."""

    def __init__(self, *args):
        self._items = list(args)
        self._items.sort()

    def select_type(self, mtype_list):
        """Returns the best match from mtype_list, a list of media-types

        In the event of a tie, the first item in mtype_list is
        returned."""
        bestmatch = None
        bestq = 0
        for mtype in mtype_list:
            # calculate a match score for each input item, highest score wins
            for aitem in self._items:
                if aitem.range.match_media_type(mtype):
                    # we break at the first match as ranges are ordered by
                    # precedence
                    if aitem.q > bestq:
                        # this is the best match so far, we use strictly
                        # greater as q=0 means unacceptable and input
                        # types are assumed to be ordered by preference
                        # of the caller.
                        bestmatch = mtype
                        bestq = aitem.q
                    break
        return bestmatch

    @classmethod
    def from_str(cls, source):
        """Create an AcceptList from a *source* string."""
        p = HeaderParser(source)
        al = p.require_accept_list()
        p.require_end("Accept header")
        return al

    def __str__(self):
        return string.join(map(str, self._items), ', ')

    def __len__(self):
        return len(self._items)

    def __getitem__(self, index):
        return self._items[index]

    def __iter__(self):
        return self._items.__iter__()


class AcceptToken(object):

    """Represents a single item in a token-based Accept-* header

    The built-in str function can be used to format instances according
    to the grammar defined in the specification.

    Instances are immutable, they define comparison
    methods and a hash implementation to allow them to be used as keys
    in dictionaries.

    AcceptToken items are sorted by their token, with wild cards sorting
    behind specified tokens.  Equal values sort by *descending* qvalue,
    for example:

            iso-8859-2;q=0.75 < iso-8859-2;q=0.5"""

    def __init__(self, token="*", qvalue=1.0):
        #: the token that is acceptable or "*" for any token
        self.token = token
        self._token = token.lower()
        self.q = qvalue         #: the q-value (defaults to 1.0)

    @classmethod
    def from_str(cls, source):
        """Creates a single AcceptToken instance from a *source* string."""
        p = HeaderParser(source)
        p.parse_sp()
        at = p.require_accept_token(cls)
        p.parse_sp()
        p.require_end("Accept token")
        return at

    def __str__(self):
        result = [self.token]
        if self.q != 1.0:
            qstr = "%.3f" % self.q
            qstr = qstr.rstrip('0')
            qstr = qstr.rstrip('.')
            result.append(";q=%s" % qstr)
        return string.join(result, '')

    def __cmp__(self, other):
        if type(other) in types.StringTypes:
            other = AcceptToken.from_str(other)
        elif not isinstance(other, AcceptToken):
            raise TypeError
        if self.token == "*":
            if other.token == "*":
                result = 0
            else:
                return 1
        elif other.token == "*":
            return -1
        else:
            result = cmp(self._token, other._token)
        if result == 0:
            if self.q > other.q:
                return -1
            elif self.q < other.q:
                return 1
        return result

    def __hash__(self):
        return hash(self._token, self.q)


class AcceptTokenList(object):

    """Represents the value of a token-based Accept-* header

    The built-in str function can be used to format instances according
    to the grammar defined in the specification.

    Instances are immutable, they are constructed from one or more
    :py:class:`AcceptToken` instances.  There are no comparison methods.

    Instances behave like read-only lists implementing len, indexing and
    iteration in the usual way."""

    #: the class used to create new items in this list
    ItemClass = AcceptToken

    def __init__(self, *args):
        self._items = list(args)
        self._items.sort()

    def select_token(self, token_list):
        """Returns the best match from token_list, a list of tokens.

        In the event of a tie, the first item in token_list is
        returned."""
        bestmatch = None
        bestq = 0
        for token in token_list:
            _token = token.lower()
            # calculate a match score for each input item, highest score wins
            for aitem in self._items:
                if aitem._token == _token or aitem._token == "*":
                    # we break at the first match as accept-tokens are
                    # ordered by precedence i.e., with wild-cards at the
                    # end of the list as a catch-all
                    if aitem.q > bestq:
                        # this is the best match so far, we use strictly
                        # greater as q=0 means unacceptable and input
                        # types are assumed to be ordered by preference
                        # of the caller.
                        bestmatch = token
                        bestq = aitem.q
                    break
        return bestmatch

    @classmethod
    def from_str(cls, source):
        """Create an AcceptTokenList from a *source* string."""
        p = HeaderParser(source)
        al = p.require_accept_token_list(cls)
        p.require_end("Accept header")
        return al

    def __str__(self):
        return string.join(map(str, self._items), ', ')

    def __len__(self):
        return len(self._items)

    def __getitem__(self, index):
        return self._items[index]

    def __iter__(self):
        return self._items.__iter__()


class AcceptCharsetItem(AcceptToken):

    """Represents a single item in an Accept-Charset header"""
    pass


class AcceptCharsetList(AcceptTokenList):

    """Represents an Accept-Charset header"""
    ItemClass = AcceptCharsetItem

    def select_token(self, token_list):
        """Overridden to provide default handling of iso-8859-1"""
        bestmatch = None
        bestq = 0
        for token in token_list:
            _token = token.lower()
            # calculate a match score for each input item, highest score wins
            match = False
            for aitem in self._items:
                if aitem._token == _token or aitem._token == "*":
                    match = True
                    if aitem.q > bestq:
                        bestmatch = token
                        bestq = aitem.q
                    break
            if not match and _token == "iso-8859-1":
                if 1.0 > bestq:
                    bestmatch = token
                    bestq = 1.0
        return bestmatch


class AcceptEncodingItem(AcceptToken):

    """Represents a single item in an Accept-Encoding header"""
    pass


class AcceptEncodingList(AcceptTokenList):

    """Represents an Accept-Encoding header"""
    ItemClass = AcceptEncodingItem

    def select_token(self, token_list):
        """Overridden to provide default handling of identity"""
        bestmatch = None
        bestq = 0
        for token in token_list:
            _token = token.lower()
            # calculate a match score for each input item, highest score wins
            match = False
            for aitem in self._items:
                if aitem._token == _token or aitem._token == "*":
                    match = True
                    if aitem.q > bestq:
                        bestmatch = token
                        bestq = aitem.q
                    break
            if not match and _token == "identity":
                # the specification says identity is always acceptable,
                # not that it is always the best choice.  Given that it
                # explicitly says that the default charset has
                # acceptability q=1 the omission of a similar phrase
                # here suggests that we should use the lowest possible q
                # value in this case.  We do this by re-using 0 to mean
                # minimally acceptable.
                if bestq == 0:
                    bestmatch = token
        return bestmatch


class AcceptLanguageItem(AcceptToken):

    """Represents a single item in an Accept-Language header."""

    def __init__(self, token="*", qvalue=1.0):
        super(AcceptLanguageItem, self).__init__(token, qvalue)
        if self.token == "*":
            self._range = ()
        else:
            self._range = tuple(self._token.split("-"))

    def __cmp__(self, other):
        if type(other) in types.StringTypes:
            other = AcceptLanguageItem.from_str(other)
        elif not isinstance(other, AcceptLanguageItem):
            raise TypeError
        # sort first by length, longest first to catch most specific match
        result = cmp(len(other._range), len(self._range))
        if result == 0:
            # and then secondary sort on alphabetical
            result = cmp(self._range, other._range)
        return result


class AcceptLanguageList(AcceptTokenList):

    """Represents an Accept-Language header"""

    #: the class used to create items in this token list
    ItemClass = AcceptLanguageItem

    def __init__(self, *args):
        super(AcceptLanguageList, self).__init__(*args)

    def select_token(self, token_list):
        """Remapped to :py:meth:`select_language`"""
        return str(self.select_language(map(params.LanguageTag.from_str,
                                            token_list)))

    def select_language(self, lang_list):
        bestmatch = None
        bestq = 0
        for lang in lang_list:
            # calculate a match score for each input item, highest score wins
            for aitem in self._items:
                if lang.partial_match(aitem._range):
                    if aitem.q > bestq:
                        bestmatch = lang
                        bestq = aitem.q
                    break
        return bestmatch


class AcceptRanges(object):

    """Represents the value of an Accept-Ranges response header.

    The built-in str function can be used to format instances according
    to the grammar defined in the specification.

    Instances are immutable, they are constructed from
    a list of string arguments.  If the argument list is empty then a
    value of "none" is assumed.

    Instances behave like read-only lists implementing len, indexing and
    iteration in the usual way.  Comparison methods are provided."""

    def __init__(self, *args):
        self._ranges = args
        self._sorted = map(lambda x: x.lower(), list(args))
        if "none" in self._sorted:
            if len(self._sorted) == 1:
                self._ranges = ()
                self._sorted = []
            else:
                raise grammar.BadSyntax("none is not a valid range-unit")
        self._sorted.sort()

    @classmethod
    def from_str(cls, source):
        """Create an AcceptRanges value from a *source* string."""
        p = HeaderParser(source)
        ar = p.parse_tokenlist()
        if not ar:
            raise grammar.BadSyntax(
                "range-unit or none required in Accept-Ranges")
        p.require_end("Accept-Ranges header")
        return AcceptRanges(*ar)

    def __str__(self):
        if self._ranges:
            return string.join(map(str, self._ranges), ', ')
        else:
            return "none"

    def __len__(self):
        return len(self._ranges)

    def __getitem__(self, index):
        return self._ranges[index]

    def __iter__(self):
        return self._ranges.__iter__()

    def __cmp__(self, other):
        if type(other) in types.StringTypes:
            other = AcceptRanges.from_str(other)
        if not isinstance(other, AcceptRanges):
            raise TypeError
        return cmp(self._sorted, other._sorted)


class Allow(object):

    """Represents the value of an Allow entity header.

    The built-in str function can be used to format instances according
    to the grammar defined in the specification.

    Instances are immutable, they are constructed from
    a list of string arguments which may be empty.

    Instances behave like read-only lists implementing len, indexing and
    iteration in the usual way.  Comparison methods are provided."""

    def __init__(self, *args):
        self._methods = map(lambda x: x.upper(), args)
        self._sorted = list(self._methods)
        self._sorted.sort()

    @classmethod
    def from_str(cls, source):
        """Create an Allow value from a *source* string."""
        p = HeaderParser(source)
        allow = p.parse_tokenlist()
        p.require_end("Allow header")
        return Allow(*allow)

    def is_allowed(self, method):
        """Tests if *method* is allowed by this value."""
        return method.upper() in self._sorted

    def __str__(self):
        return string.join(self._methods, ', ')

    def __len__(self):
        return len(self._methods)

    def __getitem__(self, index):
        return self._methods[index]

    def __iter__(self):
        return self._methods.__iter__()

    def __cmp__(self, other):
        if type(other) in types.StringTypes:
            other = Allow.from_str(other)
        if not isinstance(other, Allow):
            raise TypeError
        return cmp(self._sorted, other._sorted)


class CacheControl(object):

    """Represents the value of a Cache-Control general header.

    The built-in str function can be used to format instances according
    to the grammar defined in the specification.

    Instances are immutable, they are constructed from a list of
    arguments which must not be empty.  Arguments are treated as follows:

    string
        a simple directive with no parmeter

    2-tuple of string and non-tuple
        a directive with a simple parameter

    2-tuple of string and tuple
        a directive with a quoted list-style parameter

    Instances behave like read-only lists implementing len, indexing and
    iteration in the usual way.  Instances also support basic key lookup
    of directive names by implementing __contains__ and __getitem__
    (which returns None for defined directives with no parameter and
    raises KeyError for undefined directives).  Instances are not truly
    dictionary like."""

    def __init__(self, *args):
        self._directives = []
        self._values = {}
        if not len(args):
            raise TypeError(
                "At least one directive required for Cache-Control")
        for a in args:
            if isinstance(a, tuple):
                # must be a 2-tuple
                d, v = a
            else:
                d, v = a, None
            d = d.lower()
            self._directives.append(d)
            self._values[d] = v

    @classmethod
    def from_str(cls, source):
        """Create a Cache-Control value from a *source* string."""
        p = HeaderParser(source)
        cc = p.ParseCacheControl()
        p.require_end("Cache-Control header")
        return cc

    def __str__(self):
        result = []
        for d in self._directives:
            v = self._values[d]
            if v is None:
                result.append(d)
            elif isinstance(v, tuple):
                result.append(
                    "%s=%s" %
                    (d, grammar.quote_string(string.join(map(str, v), ", "))))
            else:
                result.append(
                    "%s=%s" % (d, grammar.quote_string(str(v), force=False)))
        return string.join(result, ", ")

    def __len__(self):
        return len(self._directives)

    def __getitem__(self, index):
        if type(index) in types.StringTypes:
            # look up by key
            return self._values[index.lower()]
        else:
            d = self._directives[index]
            v = self._values[d]
            if v is None:
                return d
            else:
                return (d, v)

    def __iter__(self):
        for d in self._directives:
            v = self._values[d]
            if v is None:
                yield d
            else:
                yield (d, v)

    def __contains__(self, key):
        return key.lower() in self._values


class ContentRange(object):

    """Represents a single content range

    first_byte
        Specifies the first byte of the range

    last_byte
        Specifies the last byte of the range

    total_len
        Specifies the total length of the entity

    With no arguments an invalid range representing an unsatisfied range
    request from an entity of unknown length is created.

    If first_byte is specified on construction last_byte must also be
    specified or TypeError is raised.

    The built-in str function can be used to format instances according
    to the grammar defined in the specification.

    Instances are immutable."""

    def __init__(self, first_byte=None, last_byte=None, total_len=None):
        self.first_byte = first_byte        #: first byte in the range
        self.last_byte = last_byte          #: last byte in the range
        # : total length of the entity or None if not known
        self.total_len = total_len
        if self.first_byte is not None and self.last_byte is None:
            raise TypeError("ContentRange: last_byte must not be None "
                            "when first_byte=%i" % self.first_byte)

    @classmethod
    def from_str(cls, source):
        """Creates a single ContentRange instance from a *source* string."""
        p = HeaderParser(source)
        p.parse_sp()
        cr = p.require_contentrange()
        p.parse_sp()
        p.require_end("Content-Range specification")
        return cr

    def __str__(self):
        result = ["bytes "]
        if self.first_byte is None:
            result.append('*')
        else:
            result.append("%i-%i" % (self.first_byte, self.last_byte))
        result.append("/")
        if self.total_len is None:
            result.append("*")
        else:
            result.append(str(self.total_len))
        return string.join(result, '')

    def __len__(self):
        if self.first_byte is not None:
            result = self.last_byte - self.first_byte + 1
            if result > 0:
                return result
        raise ValueError("Invalid content-range for len")

    def is_valid(self):
        """Returns True if this range is valid, False otherwise.

        A valid range is any non-empty byte range wholly within the
        entity described by the total length.  Unsatisfied content
        ranges are treated as *invalid*."""
        return (self.first_byte is not None and
                self.first_byte <= self.last_byte and
                self.first_byte >= 0 and
                (self.total_len is None or self.last_byte < self.total_len))


class HeaderParser(params.ParameterParser):

    """A special parser for parsing HTTP headers from TEXT"""

    def parse_media_range(self):
        savepos = self.pos
        try:
            return self.require_media_range()
        except grammar.BadSyntax:
            self.setpos(savepos)
            return None

    def require_media_range(self):
        """Parses a :py:class:`MediaRange` instance.

        Raises BadSyntax if no media-type was found."""
        self.parse_sp()
        type = self.require_token("media-type").lower()
        self.require_separator('/', "media-type")
        subtype = self.require_token("media-subtype").lower()
        self.parse_sp()
        parameters = {}
        self.parse_parameters(parameters, ignore_allsp=False, qmode='q')
        return MediaRange(type, subtype, parameters)

    def require_accept_item(self):
        """Parses a :py:class:`AcceptItem` instance

        Raises BadSyntax if no item was found."""
        self.parse_sp()
        extensions = {}
        range = self.require_media_range()
        self.parse_sp()
        if self.parse_separator(';'):
            self.parse_sp()
            qparam = self.require_token("q parameter")
            if qparam.lower() != 'q':
                raise grammar.BadSyntax(
                    "Unrecognized q-parameter: %s" % qparam)
            self.parse_sp()
            self.require_separator('=', "q parameter")
            self.parse_sp()
            qvalue = self.parse_qvalue()
            if qvalue is None:
                raise grammar.BadSyntax(
                    "Unrecognized q-value: %s" % repr(self.the_word))
            self.parse_parameters(extensions)
        else:
            qvalue = 1.0
        return AcceptItem(range, qvalue, extensions)

    def require_accept_list(self):
        """Parses a :py:class:`AcceptList` instance

        Raises BadSyntax if no valid items were found."""
        items = []
        self.parse_sp()
        while self.the_word:
            a = self.parse_production(self.require_accept_item)
            if a is None:
                break
            items.append(a)
            self.parse_sp()
            if not self.parse_separator(','):
                break
        if items:
            return AcceptList(*items)
        else:
            raise grammar.BadSyntax("Expected Accept item")

    def require_accept_token(self, cls=AcceptToken):
        """Parses a single :py:class:`AcceptToken` instance

        Raises BadSyntax if no item was found.

        cls
            An optional sub-class of :py:class:`AcceptToken` to create
            instead."""
        self.parse_sp()
        token = self.require_token()
        self.parse_sp()
        if self.parse_separator(';'):
            self.parse_sp()
            qparam = self.require_token("q parameter")
            if qparam.lower() != 'q':
                raise grammar.BadSyntax(
                    "Unrecognized q-parameter: %s" % qparam)
            self.parse_sp()
            self.require_separator('=', "q parameter")
            self.parse_sp()
            qvalue = self.parse_qvalue()
            if qvalue is None:
                raise grammar.BadSyntax(
                    "Unrecognized q-value: %s" % repr(self.the_word))
        else:
            qvalue = 1.0
        return cls(token, qvalue)

    def require_accept_token_list(self, cls=AcceptTokenList):
        """Parses a list of token-based accept items

        Returns a :py:class:`AcceptTokenList` instance.  If no tokens
        were found then an *empty* list is returned.

        cls
            An optional sub-class of :py:class:`AcceptTokenList` to
            create instead."""
        items = []
        self.parse_sp()
        while self.the_word:
            a = self.parse_production(self.require_accept_token, cls.ItemClass)
            if a is None:
                break
            items.append(a)
            self.parse_sp()
            if not self.parse_separator(','):
                break
        return cls(*items)

    def require_contentrange(self):
        """Parses a :py:class:`ContentRange` instance."""
        self.parse_sp()
        unit = self.require_token("bytes-unit")
        if unit.lower() != 'bytes':
            raise grammar.BadSyntax(
                "Unrecognized unit in content-range: %s" % unit)
        self.parse_sp()
        spec = self.require_token()
        # the spec must be an entire token, '-' is not a separator
        if spec == "*":
            first_byte = last_byte = None
        else:
            spec = spec.split('-')
            if (len(spec) != 2 or not grammar.is_digits(spec[0]) or
                    not grammar.is_digits(spec[1])):
                raise grammar.BadSyntax(
                    "Expected digits or * in byte-range-resp-spec")
            first_byte = int(spec[0])
            last_byte = int(spec[1])
        self.parse_sp()
        self.require_separator('/', "byte-content-range-spec")
        self.parse_sp()
        total_len = self.require_token()
        if total_len == "*":
            total_len = None
        elif grammar.is_digits(total_len):
            total_len = int(total_len)
        else:
            raise grammar.BadSyntax(
                "Expected digits or * for instance-length")
        return ContentRange(first_byte, last_byte, total_len)

    def require_product_token_list(self):
        """Parses a list of product tokens

        Returns a list of :py:class:`params.ProductToken` instances.  If
        no tokens were found then an empty list is returned."""
        items = []
        while self.the_word:
            self.parse_sp()
            pt = self.parse_production(self.require_product_token)
            if pt is not None:
                items.append(pt)
                self.parse_sp()
                if not self.parse_separator(','):
                    break
            elif not self.parse_separator(','):
                break
        return items


class HTTPException(Exception):

    """Class for all HTTP message-related errors."""
    pass


class ProtocolError(HTTPException):

    """Indicates a violation of the HTTP protocol"""
    pass
