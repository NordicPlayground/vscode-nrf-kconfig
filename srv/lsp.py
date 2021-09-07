import inspect
from io import TextIOWrapper
import os
from typing import Any, Callable, Union, Optional, List, Dict
import sys
import re
import json
import enum
from datetime import datetime

JSONRPC = '2.0'

def encode_json(o):
	def encoder(obj):
		if hasattr(obj, 'to_dict'):
			return obj.to_dict()
		return obj.__dict__
	return json.dumps(o, default=encoder)

class RPCMsg:
	def __init__(self, jsonrpc: str):
		self.jsonrpc = jsonrpc

class RPCRequest(RPCMsg):
	def __init__(self, id: Union[str, int], method: str, params: Union[object, list]=None):
		super().__init__(JSONRPC)
		self.id = id
		self.method = method
		self.params = params


class RPCErrorCode(enum.IntEnum):
	PARSE_ERROR = -32700
	INVALID_REQUEST = -32600
	METHOD_NOT_FOUND = -32601
	INVALID_PARAMS = -32602
	INTERNAL_ERROR = -32603
	SERVER_NOT_INITIALIZED = -32002
	UNKNOWN_ERROR_CODE = -32001
	CONTENT_MODIFIED = -32801
	REQUEST_CANCELLED = -32800

class RPCError(Exception):
	def __init__(self, code: int, message: str, data=None):
		super().__init__()
		self.code = code
		self.message = message
		self.data = data

	def to_dict(self):
		return {"code": self.code, "message": self.message, "data": self.data}

	@staticmethod
	def create(obj):
		return RPCError(obj['code'], obj['message'], obj.get('data'))

class RPCResponse(RPCMsg):
	def __init__(self, id: Optional[Union[str, int]]=None, result=None, error: RPCError=None):
		super().__init__(JSONRPC)
		self.id = id
		self.result = result
		self.error = error

class RPCNotification(RPCMsg):
	def __init__(self, method: str, params=None):
		super().__init__(JSONRPC)
		self.method = method
		self.params = params

def handler(method):
	def wrapper(f):
		f._rsp_method = method
		return f
	return wrapper

class RPCServer:
	def __init__(self, istream=None, ostream=None):
		self._send_stream = ostream if ostream else sys.stdout
		self._recv_stream = istream if istream else sys.stdin
		self._req = None
		self.log_file = 'lsp.log'
		self.running = True
		self.handlers = {}
		self.requests = {}
		self.request_id = 0
		for method_name, _ in inspect.getmembers(self.__class__):
			method = getattr(self.__class__, method_name)
			if hasattr(method, '_rsp_method'):
				self.handlers[method._rsp_method] = method

		# Flush log file:
		with open(self.log_file, 'a') as f:
			f.write('=' * 80 + '\n')

	def dbg(self, *args):
		with open(self.log_file, 'a') as f:
			for line in args:
				f.write('dbg: ' + str(line) + '\n')

	def log(self, *args):
		sys.stderr.write('\n'.join(*args) + '\n')
		with open(self.log_file, 'a') as f:
			for line in args:
				f.write('inf: ' + str(line) + '\n')

	def _read_headers(self):
		length = 0
		content_type = ''
		while True:
			line = self._recv_stream.readline().strip()
			if len(line) == 0:
				return length, content_type

			parts = [p.strip() for p in line.split(':')]
			if len(parts) != 2:
				continue

			[key, value] = parts

			if key == 'Content-Length':
				length = int(value)
			elif key == 'Content-Type':
				content_type = value

	def rsp(self, result=None, error: RPCError =None):
		if not self._req:
			raise Exception('No command')

		self._send(RPCResponse(self._req.id, result, error))
		self._req = None

	def req(self, method: str, params, handler: Optional[Callable[[RPCResponse], Any]] = None):
		if handler:
			self.requests[self.request_id] = handler
		self._send(RPCRequest(self.request_id, method, params))
		self.request_id += 1

	def notify(self, method: str, params):
		"""
		Issue a notification to the client.

		Notifications must specify a remote method to invoke, and may optionally supply parameters
		to the method. Notifications do not get responses.

		Example::

			self.notify('remoteFunction', [1, 2, 3])

		Parameters
		----------
		method: str
			Remote method to invoke.
		params: Any
			Optional parameters for the method.
		"""

		self._send(RPCNotification(method, params))

	def _send(self, msg: RPCMsg):
		"""Internal: Send an RPCMessage to the client"""
		raw = encode_json(msg)
		self.dbg('send: ' + raw)
		self._send_stream.write(
			'Content-Type: "application/vscode-jsonrpc; charset=utf-8"\r\nContent-Length: {}\r\n\r\n{}'.format(len(raw), raw))
		self._send_stream.flush()

	def _recv(self) -> Union[RPCNotification, RPCRequest, RPCResponse]:
		length, _ = self._read_headers()
		data = self._recv_stream.read(length)
		self.dbg('recv: {}'.format(data))
		obj = json.loads(data)

		if 'id' in obj:
			if 'method' in obj:
				self._req = RPCRequest(obj['id'], obj['method'], obj['params'])
				return self._req
			return RPCResponse(obj['id'], obj.get('result'), RPCError.create(obj['error']) if 'error' in obj else None)

		return RPCNotification(obj['method'], obj['params'])

	def handle(self, msg: Union[RPCNotification, RPCRequest, RPCResponse]):
		if isinstance(msg, RPCResponse):
			handler = self.requests.get(msg.id)
			if handler:
				handler(msg)
				del self.requests[msg.id]
			return

		self.dbg('{} Method: {}'.format(type(msg).__name__, msg.method))

		if msg.method in self.handlers:
			error = None
			result = None
			start = datetime.now()
			try:
				result = self.handlers[msg.method](self, msg.params)
			except RPCError as e:
				self.dbg('Failed with error ' + str(e))
				error = e
				raise e
			except Exception as e:
				self.dbg('Failed with error ' + str(e))
				error = RPCError(RPCErrorCode.UNKNOWN_ERROR_CODE, 'Exception: "{}"'.format(e.args))
				raise e

			end = datetime.now()
			self.dbg('Handled in {} us'.format((end - start).microseconds))

			if self._req:
				self.rsp(result, error)
		else:
			self.dbg('No handler for "{}"'.format(msg.method))
			if self._req:
				self.rsp(None, RPCError(RPCErrorCode.METHOD_NOT_FOUND, 'Unknown method "{}"'.format(msg.method)))

	def loop(self):
		try:
			while self.running:
				self.handle(self._recv())
		except KeyboardInterrupt:
			pass


#################################################################################################################################
# Language Server Protocol Server
#################################################################################################################################


class Uri:
	def __init__(self, scheme:str, authority:str='', path: str='', query:str='', fragment:str=''):
		self.scheme = scheme
		self.authority = authority
		self.path = path
		self.query = query
		self.fragment = fragment

	def __repr__(self):
		uri = '{}://{}{}'.format(self.scheme, self.authority, self.path)
		if self.query:
			uri += '?' + self.query
		if self.fragment:
			uri += '#' + self.fragment
		return uri

	def __str__(self):
		return self.__repr__()

	def __eq__(self, o: object) -> bool:
		if isinstance(o, str):
			return Uri.parse(o) == self
		if not isinstance(o, Uri):
			return NotImplemented
		return str(self) == str(o)

	@property
	def basename(self):
		return os.path.basename(self.path)

	@staticmethod
	def parse(raw: str):
		def sanitize(part):
			if part:
				return re.sub(r'%([\da-fA-F]{2})', lambda x: chr(int(x.group(1), 16)), part)
			else:
				return ''

		if not isinstance(raw, str):
			return NotImplemented

		match = re.match(r'(.*?):(?://([^?\s/#]*))?(/[^?\s]*)?(?:\?([^#]+))?(?:#(.+))?', raw)
		if match:
			return Uri(*[sanitize(p) for p in match.groups()])

	@staticmethod
	def file(path: str):
		return Uri('file', '', path)

	def to_dict(self):
		return str(self)


class WorkspaceFolder:
	def __init__(self, uri: Uri, name: str):
		self.uri = uri
		self.name = name


class Position:
	def __init__(self, line: int, character: int):
		self.line = line
		self.character = character

	@property
	def range(self):
		return Range(self, self)

	def before(self, other):
		if not isinstance(other, Position):
			return NotImplemented
		return (self.line < other.line) or (self.line == other.line and self.character < other.character)

	def after(self, other):
		if not isinstance(other, Position):
			return NotImplemented
		return (self.line > other.line) or (self.line == other.line and self.character > other.character)

	def __eq__(self, other):
		if not isinstance(other, Position):
			return False
		return self.line == other.line and self.character == other.character

	def __repr__(self):
		return '{}:{}'.format(self.line + 1, self.character)

	@staticmethod
	def create(obj):
		return Position(obj['line'], obj['character'])

	@staticmethod
	def start():
		return Position(0, 0)

	@staticmethod
	def end():
		return Position(999999, 999999)


class Range:
	def __init__(self, start: Position, end: Position):
		self.start = start
		self.end = end

	def single_line(self):
		return self.start.line == self.end.line

	@staticmethod
	def union(a, b):
		if not isinstance(a, Range) or not isinstance(b, Range):
			return NotImplemented
		return Range(
			a.start if a.start.before(b.start) else b.start,
			b.end if a.end.before(b.end) else b.end
		)

	def contains(self, pos_or_range):
		if isinstance(pos_or_range, Position):
			return (not pos_or_range.before(self.start)) and (not self.end.before(pos_or_range))
		if isinstance(pos_or_range, Range):
			return self.contains(pos_or_range.start) and self.contains(pos_or_range.end)
		return NotImplemented

	def overlaps(self, range):
		if not isinstance(range, Range):
			return NotImplemented
		return not self.start.after(range.end) and not range.start.after(self.end)

	def __eq__(self, other):
		if not isinstance(other, Range):
            		return NotImplemented

		return self.start == other.start and self.end == other.end

	def __repr__(self):
		return '{} - {}'.format(self.start, self.end)

	@staticmethod
	def create(obj):
		return Range(Position.create(obj['start']), Position.create(obj['end']))


class Location:
	def __init__(self, uri: Uri, range: Range):
		self.uri = uri
		self.range = range

	def __repr__(self):
		return '{}: {}'.format(self.uri, self.range)

	@staticmethod
	def create(obj):
		return Location(Uri.parse(obj['uri']), Range.create(obj['range']))


class TextDocument:
	UNKNOWN_VERSION=-1
	def __init__(self, uri: Uri, text: str = None, languageId: str = None, version: int = None):
		if version == None:
			version = TextDocument.UNKNOWN_VERSION

		self.uri = uri
		self.languageId = languageId
		self.version = version
		self.modified = version != 0
		self._inside = False
		self._mode = None
		self._scanpos = 0
		self.lines = []
		self._cbs = []
		self._virtual = self.uri.scheme != 'file'
		self.loaded = False
		if text:
			self._set_text(text)

	def on_change(self, cb):
		self._cbs.append(cb)

	def _set_text(self, text):
		self.lines = text.splitlines()
		self.loaded = True
		for cb in self._cbs:
			cb(self)

	@property
	def text(self):
		return '\n'.join(self.lines) + '\n'

	def line(self, index):
		if index < len(self.lines):
			return self.lines[index]

	def offset(self, pos: Position):
		if pos.line >= len(self.lines):
			return len(self.text)
		character = min(len(self.lines[pos.line])+1, pos.character)
		return len(''.join([l + '\n' for l in self.lines[:pos.line]])) + character

	def pos(self, offset: int):
		content = self.text[:offset]
		lines = content.splitlines()
		if len(lines) == 0:
			return Position(0, 0)
		return Position(len(lines) - 1, len(lines[-1]))

	def get(self, range: Range = None):
		if not range:
			return self.text
		text = self.text[self.offset(range.start):self.offset(range.end)]

		# Trim trailing newline if the range doesn't end on the next line:
		if text.endswith('\n') and range.end.character != 0 and range.end.line < len(self.lines):
			return text[:-1]
		return text

	def word_at(self, pos: Position):
		line = self.line(pos.line)
		if line:
			return re.match(r'.*?(\w*)$', line[:pos.character])[1] + re.match(r'^\w*', line[pos.character:])[0]

	def replace(self, text:str, range: Range = None):
		# Ignore range if the file is empty:
		if range and len(self.lines) > 0:
			self._set_text(self.text[:self.offset(range.start)] + text + self.text[self.offset(range.end):])
		else:
			self._set_text(text)
		self.modified = True

	def _write_to_disk(self):
		if not self._virtual:
			with open(self.uri.path, 'w') as f:
				f.write(self.text)
			self.modified = False
			self.version = TextDocument.UNKNOWN_VERSION

	def _read_from_disk(self):
		# will raise environment error if the file doesn't exist. This has to be caught outside:
		with open(self.uri.path, 'r') as f:
			text = f.read()
		if text == None:
			raise IOError('Unable to read from file {}'.format(self.uri.path))

		self._set_text(text)
		self.modified = False
		self.version = TextDocument.UNKNOWN_VERSION

	@staticmethod
	def from_disk(uri: Uri):
		with open(uri.path, 'r') as f:
			doc = TextDocument(uri, f.read())
		return doc

	# Standard File behavior:

	def __enter__(self):
		self._inside = True
		return self

	def __exit__(self, type, value, traceback):
		if self._inside:
			self._inside = False
			self.close()

	class LineIterator:
		def __init__(self, doc):
			self._linenr = 0
			self._lines = doc.lines

		def __next__(self):
			if self._linenr >= len(self._lines):
				raise StopIteration
			line = self._lines[self._linenr]
			self._linenr += 1
			return line

	def __iter__(self):
		return TextDocument.LineIterator(self)

	def open(self, mode='r'):
		if not mode in ['w', 'a', 'r']:
			raise IOError('Unknown mode ' + str(mode))

		if mode == 'w':
			self._set_text('')
			self.modified = True
			self.version = TextDocument.UNKNOWN_VERSION
		elif not self.loaded:
			self._read_from_disk()
		self._mode = mode
		self._scanpos = 0
		return self

	def close(self):
		if self._mode in ['a', 'w'] and self.modified:
			self._write_to_disk()
		self._mode = None

	def write(self, text: str):
		if not self._mode in ['a', 'w']:
			raise IOError('Invalid mode for writing: ' + str(self._mode))
		if not self.loaded:
			raise IOError('File not loaded in RAM: {}'.format(self.uri.path))

		self._set_text(self.text + text)
		if self._mode == 'a':
			self._scanpos = len(self.text)
		self.modified = True
		self.version = TextDocument.UNKNOWN_VERSION

	def writelines(self, lines):
		for line in lines:
			self.write(line)

	def read(self, length=None):
		if self._mode != 'r':
			raise IOError('Invalid mode for reading: ' + str(self._mode))

		if self._scanpos >= len(self.text):
			return ''

		if length == None:
			out = self.text[self._scanpos:]
			self._scanpos = len(self.text)
		else:
			out = self.text[self._scanpos:self._scanpos + length]
			self._scanpos += length
		return out

	def readline(self, size=None):
		if self._mode != 'r':
			raise IOError('Invalid mode for reading: ' + str(self._mode))

		if self._scanpos >= len(self.text):
			return ''
		out = self.text[self._scanpos:].splitlines(True)[0]
		if size != None:
			out = out[:size]
		self._scanpos += len(out)
		return out

	def readlines(self, _=None):
		if self._mode != 'r':
			raise IOError('Invalid mode for reading: ' + str(self._mode))

		if self._scanpos >= len(self.text):
			return []
		out = self.text[self._scanpos:].splitlines()
		self._scanpos = len(self.text)
		return out

	def flush(self):
		pass

	def seek(self, offset):
		if self._mode == None:
			raise IOError('Cannot seek on closed file')
		self._scanpos = offset

	def tell(self):
		return self._scanpos

	def next(self):
		if self._mode != 'r':
			raise IOError('Invalid mode for reading: ' + str(self._mode))
		if self._scanpos >= len(self.text):
			raise StopIteration
		return self.readline()


class DocProvider:
	def __init__(self, scheme: str):
		self.scheme = scheme

	def get(self, uri) -> Optional[TextDocument]:
		return None

	def exists(self, uri):
		return self.get(uri) != None

class DocumentStore:
	def __init__(self):
		self.docs: Dict[str, TextDocument] = {}
		self._providers: Dict[str, DocProvider] = {}

	def open(self, doc: TextDocument):
		self.docs[str(doc.uri)] = doc

	def close(self, uri: Uri):
		pass

	def provider(self, provider):
		self._providers[provider.uri.scheme] = provider

	def get(self, uri: Uri, create=True):
		if uri.scheme in self._providers:
			return self._providers[uri.scheme].get(uri)

		if str(uri) in self.docs:
			return self.docs[str(uri)]

		try:
			if create:
				return self._from_disk(uri)
		except EnvironmentError as e:
			# File doesn't exist
			return None

	def _from_disk(self, uri: Uri):
		with open(uri.path, 'r') as f: # will raise environment error if the file doesn't exist. This has to be caught outside
			text = f.read()
		if text == None:
			return None
		doc = TextDocument(uri, text)
		self.docs[str(uri)] = doc
		return doc

	def create(self, uri: Uri):
		doc = self.get(uri)
		if doc:
			return doc
		return self._from_disk(uri)


class CompletionItemKind(enum.IntEnum):
	TEXT = 1
	METHOD = 2
	FUNCTION = 3
	CONSTRUCTOR = 4
	FIELD = 5
	VARIABLE = 6
	CLASS = 7
	INTERFACE = 8
	MODULE = 9
	PROPERTY = 10
	UNIT = 11
	VALUE = 12
	ENUM = 13
	KEYWORD = 14
	SNIPPET = 15
	COLOR = 16
	FILE = 17
	REFERENCE = 18
	FOLDER = 19
	ENUM_MEMBER = 20
	CONSTANT = 21
	STRUCT = 22
	EVENT = 23
	OPERATOR = 24
	TYPE_PARAMETER = 25

class InsertTextFormat(enum.IntEnum):
	PLAINTEXT = 1
	SNIPPET = 2

class DiagnosticRelatedInfo:
	def __init__(self, loc, message):
		self.loc = loc
		self.message = message


class TextEdit:
	def __init__(self, range: Range, new_text: str):
		self.range = range
		self.newText = new_text

	@staticmethod
	def remove(range: Range):
		return TextEdit(range, '')


class WorkspaceEdit:
	def __init__(self):
		self.changes = {}

	def add(self, uri: Uri, edit: TextEdit):
		key = str(uri)
		if not key in self.changes:
			self.changes[key] = []
		self.changes[key].append(edit)

	def has_changes(self):
		return len([c for c in self.changes.values() if len(c) > 0]) > 0


class CodeActionKind(enum.Enum):
	QUICKFIX = 'quickfix'
	REFACTOR = 'refactor'
	REFACTOREXTRACT = 'refactor.extract'
	REFACTORINLINE = 'refactor.inline'
	REFACTORREWRITE = 'refactor.rewrite'
	SOURCE = 'source'
	SOURCEORGANIZEIMPORTS = 'source.organizeImports'
	SOURCEFIXALL = 'source.fixAll'


class CodeAction:

	def __init__(self, title, kind: CodeActionKind = CodeActionKind.QUICKFIX):
		self.title = title
		self.kind = kind
		self.command = None
		self.data = None
		self.diagnostics: List[Diagnostic] = []
		self.edit = WorkspaceEdit()

	def to_dict(self):
		result = {
			'title': self.title,
			'kind': self.kind.value,
		}
		if self.command:
			result['command'] = self.command,
		if self.data:
			result['data'] = self.data
		if len(self.diagnostics) > 0:
			result['diagnostics'] = self.diagnostics
		if self.edit.has_changes():
			result['edit'] = self.edit
		return result


class Diagnostic:
	ERROR = 1
	WARNING = 2
	INFORMATION = 3
	HINT = 4

	class Tag(enum.IntEnum):
		UNNECESSARY = 1
		DEPRECATED = 2

	def __init__(self, message, range: Range, severity=WARNING):
		self.message = message
		self.range = range
		self.severity = severity
		self.tags = []
		self.related_info = []
		self.actions = []

	@staticmethod
	def severity_str(severity):
		return [
			'Unknown',
			'Error',
			'Information',
			'Hint'
		][severity]

	def __str__(self) -> str:
		return '{}: {}: {}'.format(self.range, Diagnostic.severity_str(self.severity), self.message)

	def to_dict(self):
		obj = {"message": self.message, "range": self.range, "severity": self.severity}
		if len(self.tags):
			obj['tags'] = self.tags
		if len(self.related_info):
			obj['relatedInformation'] = [info.__dict__ for info in self.related_info]

		return obj

	def add_action(self, action: CodeAction):
		action.diagnostics.append(self)
		self.actions.append(action)

	def mark_unnecessary(self):
		self.tags.append(Diagnostic.Tag.UNNECESSARY)

	@staticmethod
	def err(message, range):
		return Diagnostic(message, range, Diagnostic.ERROR)

	@staticmethod
	def warn(message, range):
		return Diagnostic(message, range, Diagnostic.WARNING)

	@staticmethod
	def info(message, range):
		return Diagnostic(message, range, Diagnostic.INFORMATION)

	@staticmethod
	def hint(message, range):
		return Diagnostic(message, range, Diagnostic.HINT)

class MarkupContent:
	PLAINTEXT = 'plaintext'
	MARKDOWN = 'markdown'
	def __init__(self, value='', kind=None):
		self.value = value
		self.kind = kind if kind else MarkupContent.MARKDOWN

	def _sanitize(self, text):
		return re.sub(r'[`<>{}\[\]]', r'\\\0', text)

	def add_text(self, text):
		if self.kind == MarkupContent.MARKDOWN:
			self.value += self._sanitize(text)
		else:
			self.value += text

	def add_markdown(self, md):
		if self.kind == MarkupContent.PLAINTEXT:
			self.value = self._sanitize(self.value)
			self.kind = MarkupContent.MARKDOWN
		self.value += md

	def paragraph(self):
		self.value += '\n\n'

	def linebreak(self):
		if self.kind == MarkupContent.MARKDOWN:
			self.value += '\n\n'
		else:
			self.value += '\n'

	def add_code(self, lang, code):
		self.add_markdown('\n```{}\n{}\n```\n'.format(lang, code))

	def add_link(self, url, text=''):
		self.add_markdown('[{}]({})'.format(text, url))


	@staticmethod
	def plaintext(value):
		return MarkupContent(value, MarkupContent.PLAINTEXT)

	@staticmethod
	def markdown(value):
		return MarkupContent(value, MarkupContent.MARKDOWN)

	@staticmethod
	def code(lang, value):
		return MarkupContent.markdown('```{}\n{}\n```'.format(lang, value))

NEXT_TABSTOP=-1

class Snippet:
	def __init__(self, value=''):
		self.text = value
		self._next_tabstop = 1

	def add_text(self, text):
		self.text += text

	def add_tabstop(self, number=NEXT_TABSTOP):
		if number == NEXT_TABSTOP:
			number = self._next_tabstop

		self.text += ''.join(['${', str(number), '}'])
		self._next_tabstop = number + 1

	def add_placeholder(self, text, number=NEXT_TABSTOP):
		if number == NEXT_TABSTOP:
			number = self._next_tabstop
		self.text += ''.join(['${', str(number), ':', text, '}'])
		self._next_tabstop = number + 1

	def add_choice(self, choices, number=NEXT_TABSTOP):
		if number == NEXT_TABSTOP:
			number = self._next_tabstop
		self.text += ''.join(['${', str(number), '|', ','.join(choices), '|}'])
		self._next_tabstop = number + 1

class FileChangeKind(enum.IntEnum):
	CREATED = 1
	CHANGED = 2
	DELETED = 3

documentStore = DocumentStore()

class LSPServer(RPCServer):
	def __init__(self, name: str, version: str, istream, ostream):
		super().__init__(istream, ostream)
		self.rootUri: str
		self.workspaceFolders: List[WorkspaceFolder]
		self.name = name
		self.version = version
		self.trace = 'off'
		self.watchers = []
		self.capability_id = 0

	def capabilities(self):
		def has(method):
			return method in self.handlers

		caps = {
			'hoverProvider': has('textDocument/hover'),
			'declarationProvider': has('textDocument/declaration'),
			'definitionProvider': has('textDocument/definition'),
			'typeDefinitionProvider': has('textDocument/typeDefinition'),
			'implementationProvider': has('textDocument/implementation'),
			'referencesProvider': has('textDocument/references'),
			'documentHighlightProvider': has('textDocument/documentHighlight'),
			'documentSymbolProvider': has('textDocument/documentSymbol'),
			'codeActionProvider': has('textDocument/codeAction'),
			'colorProvider': has('textDocument/documentColor'),
			'documentFormattingProvider': has('textDocument/formatting'),
			'documentRangeFormattingProvider': has('textDocument/rangeFormatting'),
			'renameProvider': has('textDocument/rename'),
			'foldingRangeProvider': has('textDocument/foldingRange'),
			'selectionRangeProvider': has('textDocument/selectionRange'),
			'linkedEditingRangeProvider': has('textDocument/linkedEditingRange'),
			'callHierarchyProvider': has('textDocument/prepareCallHierarchy'),
			'monikerProvider': has('textDocument/moniker'),
			'workspaceSymbolProvider': has('workspace/symbol'),
			'textDocumentSync': 2, # incremental
			# 'signatureHelpProvider'
			# 'codeLensProvider'
			# 'documentLinkProvider'
			# 'documentOnTypeFormattingProvider'
			# 'executeCommandProvider'
			# 'semanticTokensProvider'
			# workspace?: {
			# 	workspaceFolders?: WorkspaceFoldersServerCapabilities;
			# 	fileOperations?: {
			# 		didCreate?: FileOperationRegistrationOptions;
			# 		willCreate?: FileOperationRegistrationOptions;
			# 		didRename?: FileOperationRegistrationOptions;
			# 		willRename?: FileOperationRegistrationOptions;
			# 		didDelete?: FileOperationRegistrationOptions;
			# 		willDelete?: FileOperationRegistrationOptions;
			# 	}
			# }
			# experimental?: any;
		}

		if has('textDocument/completion'):
			caps['completionProvider'] = {}

		return caps

	def dbg(self, *args):
		super().dbg(*args)
		if self.trace != 'off':
			self.send(RPCNotification('$/logTrace', {'message': '\n'.join(args)}))

	def log(self, *args):
		super().log(*args)
		if self.trace == 'message':
			self.send(RPCNotification('$/logTrace', {'message': '\n'.join(args)}))

	def register_capability(self, method: str, options, handler: Optional[Callable[[RPCResponse], Any]] = None):
		self.capability_id += 1
		capability = {'id': str(self.capability_id), 'method': method, 'registerOptions': options}
		self.req('client/registerCapability', {'registrations': [capability]}, handler)
		return str(self.capability_id)

	def watch_files(self, pattern: str, created=True, changed=True, deleted=True):
		watcher = {
			'globPattern': pattern,
			'kind': (created * 1) + (changed * 2) + (deleted * 4),
		}
		self.watchers.append(watcher)
		self.register_capability('workspace/didChangeWatchedFiles', {'watchers': [watcher]})

	def on_file_change(self, uri: Uri, kind: FileChangeKind):
		pass # Override in extending class

	@handler('$/setTrace')
	def handle_set_trace(self, params):
		self.trace = params['value']

	@handler('$/cancelRequest')
	def handle_cancel(self, params):
		pass

	@handler('$/progress')
	def handle_progress(self, params):
		pass

	@handler('shutdown')
	def handle_shutdown(self, params):
		self.running = False

	@handler('initialize')
	def handle_initialize(self, params):
		self.rootUri = params['rootUri']
		if 'trace' in params:
			self.trace = params['trace']
		if 'workspaceFolders' in params:
			self.dbg('workspaceFolders: ' + str(params['workspaceFolders']))
			self.workspaceFolders = [WorkspaceFolder(Uri.parse(folder['uri']), folder['name']) for folder in params['workspaceFolders']]
		return {
			'capabilities': self.capabilities(),
			'serverInfo': {
				'name': self.name,
				'version': self.version
			}
		}

	@handler('textDocument/didOpen')
	def handle_open(self, params):
		doc = params['textDocument']
		uri = Uri.parse(doc['uri'])
		if uri:
			documentStore.open(TextDocument(uri, doc['text'], doc['languageId'], doc['version']))
		else:
			self.dbg(f'Invalid URI: {doc["uri"]}')

	@handler('textDocument/didChange')
	def handle_change(self, params):
		uri = Uri.parse(params['textDocument']['uri'])
		doc = documentStore.get(uri)
		if not doc:
			return

		for change in params['contentChanges']:
			if 'range' in change:
				range = Range.create(change['range'])
			else:
				range = None

			doc.replace(change['text'], range)

		doc.version = params['textDocument']['version']

	@handler('textDocument/didClose')
	def handle_close(self, params):
		documentStore.close(Uri.parse(params['textDocument']['uri']))

	@handler('workspace/didChangeWatchedFiles')
	def handle_changed_watched_files(self, params):
		for change in params['changes']:
			uri = Uri.parse(change['uri'])
			kind = FileChangeKind(change['type'])
			self.on_file_change(uri, kind)
