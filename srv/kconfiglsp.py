from typing import Optional, List, Dict
import kconfiglib
import sys
import os
import re
import enum
import argparse
from rpc import handler, RPCError
from lsp import CodeAction, CompletionItemKind, Diagnostic, DiagnosticRelatedInfo, DocumentSymbol, FileChangeKind, InsertTextFormat, LSPServer, MarkupContent, Position, Location, Snippet, SymbolKind, TextEdit, Uri, TextDocument, Range, documentStore

VERSION = '1.0'

#################################################################################################################################
# Kconfig LSP Server
#################################################################################################################################


# Environment variables passed to menuconfig:
# - ZEPHYR_BASE
# - ZEPHYR_TOOLCHAIN_VARIANT -> default to "zephyr"
# - PYTHON_EXECUTABLE
# - srctree=${ZEPHYR_BASE}
# - KERNELVERSION from ./VERSION, as a hex number, see version.cmake
# - KCONFIG_CONFIG=${PROJECT_BINARY_DIR}/.config
# - ARCH
# - ARCH_DIR
# - BOARD_DIR
# - SHIELD_AS_LIST
# - KCONFIG_BINARY_DIR=${CMAKE_BINARY_DIR}/Kconfig
# - TOOLCHAIN_KCONFIG_DIR -> default to ${TOOLCHAIN_ROOT}/cmake/toolchain/${ZEPHYR_TOOLCHAIN_VARIANT}
# - EDT_PICKLE
# - ZEPHYR_{modules}_MODULE_DIR -> get from west?
# - EXTRA_DTC_FLAGS -> Appear to be unused
# - DTS_POST_CPP -> ${PROJECT_BINARY_DIR}/${BOARD}.dts.pre.tmp
# - DTS_ROOT_BINDINGS -> ${DTS_ROOTs}/dts/bindings


KCONFIG_WARN_LVL=Diagnostic.WARNING
ID_SEP = '@'

class KconfigErrorCode(enum.IntEnum):
	"""Set of Kconfig specific error codes reported in response to failing requests"""
	UNKNOWN_NODE = 1 # The specified node is unknown.
	DESYNC = 2 # The kconfig data has been changed, and the menu tree is out of sync.
	PARSING_FAILED = 3 # Kconfig tree couldn't be parsed.

class Kconfig(kconfiglib.Kconfig):
	def __init__(self, filename='Kconfig'):
		"""
		Wrapper of kconfiglib's Kconfig object.

		Overrides the diagnostics mechanism to keep track of them in a dict instead
		of writing them to stdout.

		Overrides the _open function to inject live editor data from the documentStore.
		"""
		self.diags: Dict[str, List[Diagnostic]] = {}
		self.warn_assign_undef = True
		self.warn_assign_override = True
		self.warn_assign_redun = True
		self.filename = filename
		self.valid = False

	def parse(self):
		"""
		Parse the kconfig tree.

		This is split out from the constructor to avoid nixing the whole object on parsing errors.
		"""
		self.valid = False
		self._init(self.filename, True, False, 'utf-8')
		if self.unique_defined_syms:
			self.valid = True

	def loc(self):
		if self.filename and self.linenr != None:
			return Location(Uri.file(os.path.join(self.srctree, self.filename)), Range(Position(self.linenr - 1, 0), Position(self.linenr - 1, 99999)))

	# Overriding _open to work on virtual file storage when required:
	def _open(self, filename, mode):
		# Read from document store, but don't create an entry if it doesn't exist:
		doc = documentStore.get(Uri.file(filename), create=False)
		if doc:
			doc.open(mode)
			return doc
		if os.path.isdir(filename):
			raise kconfiglib.KconfigError(f'Attempting to open directory {filename} as file @{self.filename}:{self.linenr}')
		return super()._open(filename, mode)

	def _warn(self, msg: str, filename=None, linenr=None):
		super()._warn(msg, filename, linenr)
		if not filename:
			filename = ''
		if not linenr:
			linenr = 1

		ignored_diags = [
			'set more than once.'
		]

		if len([ignore for ignore in ignored_diags if ignore in msg]) > 0:
			# Ignore this diagnostic. It is either too verbose, or already covered by some
			# manual check.
			return

		if not filename in self.diags:
			self.diags[filename] = []

		# Strip out potentially very long definition references.
		# They're redundant, since the user can ctrl+click on the symbol to interactively find them.
		msg = re.sub(r'\s*\(defined at.*?\)\s*', ' ', msg)

		self.diags[filename].append(Diagnostic(msg, Position(int(linenr-1), 0).range, KCONFIG_WARN_LVL))


def _prompt(sym: kconfiglib.Symbol):
	"""
	Get the most accessible prompt for a given kconfig Symbol.

	Each symbol may have multiple prompts (as it may be defined in several kconfig files).
	Pick the first valid prompt.

	This'll only consider prompts whose if expressions are true.
	"""
	for node in sym.nodes:
		if node.prompt and kconfiglib.expr_value(node.prompt[1]):
			return node.prompt[0]

def _visible(node):
	"""Check whether a node is visible."""
	return node.prompt and kconfiglib.expr_value(node.prompt[1]) and not \
	    (node.item == kconfiglib.MENU and not kconfiglib.expr_value(node.visibility))

def _children(node):
	"""Get the child nodes of a given MenuNode"""
	children = []
	node = node.list
	while node:
		children.append(node)
		if node.list and not node.is_menuconfig:
			children.extend(_children(node))
		node = node.next

	return children

def _suboption_depth(node):
	"""In menuconfig, nodes that aren't children of menuconfigs are rendered
	   in the same menu, but indented. Get the depth of this indentation.
	"""
	parent = node.parent
	depth = 0
	while not parent.is_menuconfig:
		depth += 1
		parent = parent.parent
	return depth

def _val(sym: kconfiglib.Symbol):
	"""Get the native python value of the given symbol."""
	if sym.orig_type == kconfiglib.STRING:
		return sym.str_value
	if sym.orig_type in (kconfiglib.INT, kconfiglib.HEX):
		return int(sym.str_value)
	if sym.orig_type == kconfiglib.BOOL:
		return sym.tri_value != 0
	if sym.orig_type == kconfiglib.TRISTATE:
		return sym.tri_value

def _path(node):
	"""Unique path ID of each node, allowing us to identify each node in a menu"""
	if node.parent:
		i = 0
		it = node.parent.list
		while it and it != node:
			it = it.next
			i += 1
		if not it:
			raise RPCError(KconfigErrorCode.DESYNC, 'Tree is invalid')
		return _path(node.parent) + [i]
	return [0]

def _loc(sym: kconfiglib.Symbol):
	"""Get a list of locations where the given kconfig symbol is defined"""
	return [Location(Uri.file(os.path.join(n.kconfig.srctree, n.filename)), Position(n.linenr-1, 0).range) for n in sym.nodes]

def _symbolitem(sym: kconfiglib.Symbol):
	item = {
		'name': sym.name,
		'visible': sym.visibility > 0,
		'type': kconfiglib.TYPE_TO_STR[sym.type],
		'help': next((n.help for n in sym.nodes if n.help), '')
	}

	prompt = _prompt(sym)
	if prompt:
		item['prompt'] = prompt
	return item

def _filter_match(filter: str, name: str):
	"""Filter match function used for narrowing lists in searches and autocompletions"""
	return name.startswith(filter) # TODO: implement fuzzy match?


def _missing_deps(sym):
	"""
	Get a list of the dependency expressions that fail for a symbol
	"""
	deps = kconfiglib.split_expr(sym.direct_dep, kconfiglib.AND)
	return [dep for dep in deps if kconfiglib.expr_value(dep) == 0]


class KconfigMenu:
	def __init__(self, ctx, node: kconfiglib.MenuNode, id):
		"""
		A single level in a Menuconfig menu.
		"""
		self.ctx = ctx
		self.node = node
		self.id = id

	@property
	def name(self):
		return str(self.node)

	def _menuitem(self, node):
		sym = node.item
		item = {
			'visible': _visible(node),
			'loc': Location(Uri.file(node.filename), Position(node.line, 0).range),
			'is_menu': node.is_menuconfig,
			'depth': _suboption_depth(node),
			'id': self.ctx._node_id(node),
		}

		if node.prompt:
			item['prompt'] = node.prompt[0]

		if 'help' in node:
			item['help'] = node['help']

		if isinstance(sym, kconfiglib.Symbol):
			item['type'] = kconfiglib.TYPE_TO_STR[sym.orig_type]
			item['val'] = _val(sym)
			item['name'] = sym.name
			if 'assignable' in sym:
				item['options'] = list(sym.assignable)

		return item

	@property
	def items(self):
		"""The list of MenuItems this menu presents."""
		return [self._menuitem(node) for node in _children(self.node)]

	def to_dict(self):
		return {
			'name': self.name,
			'id': self.id,
			'items': self.items,
		}

class ConfEntry:
	def __init__(self, name: str, loc: Location, assignment: str, value_range: Range):
		"""
		Single configuration entry in a prj.conf file, like CONFIG_ABC=y
		"""
		self.name = name
		self.loc = loc
		self.raw = assignment.strip()
		self.value_range = value_range

	@property
	def range(self):
		"""Range of the name text, ie CONFIG_ABC"""
		return self.loc.range

	def __eq__(self, o: object) -> bool:
		if not isinstance(o, ConfEntry):
			return False
		return self.loc == o.loc

	@property
	def full_range(self):
		"""Range of the entire assignment, ie CONFIG_ABC=y"""
		return Range(self.range.start, self.value_range.end)

	def is_string(self):
		return self.raw.startswith('"') and self.raw.endswith('"')

	def is_bool(self):
		return self.raw in ['y', 'n']

	def is_hex(self):
		return re.match(r'0x[a-fA-F\d]+', self.raw)

	def is_int(self):
		return re.match(r'\d+', self.raw)

	@property
	def value(self):
		"""Value assigned in the entry, as seen by kconfiglib"""
		if self.is_string():
			return self.raw[1:-1] # strip out quotes
		if self.is_bool():
			return self.raw
		if self.is_hex():
			return int(self.raw, 16)
		if self.is_int():
			return int(self.raw)

	@property
	def type(self):
		"""Human readable entry type, derived from the assigned value."""
		if self.is_string():
			return kconfiglib.TYPE_TO_STR[kconfiglib.STRING]
		if self.is_hex():
			return kconfiglib.TYPE_TO_STR[kconfiglib.HEX]
		if self.is_int():
			return kconfiglib.TYPE_TO_STR[kconfiglib.INT]
		if self.is_bool():
			return kconfiglib.TYPE_TO_STR[kconfiglib.BOOL]

		return kconfiglib.TYPE_TO_STR[kconfiglib.UNKNOWN]

	@property
	def line_range(self):
		"""Entire line range."""
		return Range(
			Position(self.range.start.line, 0), Position(self.range.start.line + 1, 0))

	def remove(self, title='Remove entry') -> CodeAction:
		"""Create a code action that will remove this entry"""
		action = CodeAction(title)
		action.edit.add(self.loc.uri, TextEdit.remove(self.line_range))
		return action


class ConfFile:
	def __init__(self, uri: Uri):
		"""
		Single .conf file.

		Each Kconfig context may contain a list of conf files that must be parsed.
		The .conf file does not parse or understand the entry names and their interpreted value.
		"""
		self.uri = uri
		self.diags: List[Diagnostic] = []

	@property
	def doc(self) -> TextDocument:
		"""The TextDocument this file represents"""
		return documentStore.get(self.uri)

	def entries(self) -> List[ConfEntry]:
		"""The ConfEntries in this file"""
		entries = []
		for linenr, line in enumerate(self.doc.lines):
			match = re.match(r'^\s*(CONFIG_(\w+))\s*\=("[^"]+"|\w+)', line)
			if match:
				range = Range(
					Position(linenr, match.start(1)), Position(linenr, match.end(1)))
				value_range = Range(
					Position(linenr, match.start(3)), Position(linenr, match.end(3)))
				entries.append(ConfEntry(match[2], Location(self.uri, range), match[3], value_range))
		return entries

	def find(self, name) -> List[ConfEntry]:
		"""Find all ConfEntries that configure a symbol with the given name."""
		return [entry for entry in self.entries() if entry.name == name]

	def __repr__(self):
		return str(self.uri)


class BoardConf:
	def __init__(self, name, arch, dir):
		"""Board configuration object, representing a single Zephyr board"""
		self.name = name
		self.arch = arch
		self.dir = dir

	@property
	def conf_file(self):
		"""Get the path of the conf file that must be included when building with this board"""
		return ConfFile(Uri.file(os.path.join(self.dir, self.name + '_defconfig')))


class KconfigContext:
	def __init__(self, uri: Uri, root, conf_files: List[ConfFile]=[], env={}):
		"""A single instance of a kconfig compilation.
		Represents one configuration of one application, equalling a single
		build in Zephyr.
		"""
		self.uri = uri
		self.env = env
		self.conf_files = conf_files
		self.board = BoardConf(env['BOARD'], env['ARCH'], env['BOARD_DIR'])
		self.version = 0
		self._root = root
		self._kconfig: Optional[Kconfig] = None
		self.menu = None
		self.cmd_diags: List[Diagnostic] = []
		self.kconfig_diags: Dict[str, List[Diagnostic]] = {}

	def initialize_env(self):
		"""
		Apply the context environment for the entire process.

		kconfiglib will access os.environ without a wrapper to
		resolve variables like ZEPHYR_BASE.
		"""
		for key, value in self.env.items():
			os.environ[key] = value

		functions_path = os.path.join(self.env['ZEPHYR_BASE'], 'scripts', 'kconfig')
		if not functions_path in sys.path:
			sys.path.append(functions_path)

	def parse(self):
		"""
		Parse the full kconfig tree.
		Will set up the environment and invoke kconfiglib to parse the entire kconfig
		file tree. This is only necessary to do once - or if any files in the Kconfig
		file tree changes.

		Throws kconfig errors if the tree can't be parsed.
		"""
		self.menu = None
		self.modified = {}
		self.clear_diags()
		self.initialize_env()

		self._kconfig = Kconfig(self._root)

		try:
			self._kconfig.parse()
		except kconfiglib.KconfigError as e:
			loc = self._kconfig.loc()

			# Strip out the GCC-style location indicator that is placed on the start of the
			# error message for some messages:
			match = re.match(r'(^[\w\/\\-]+:\d+:\s*)?(error:)?\s*(.*)', str(e))
			if match:
				msg = match[3]
			else:
				msg = str(e)

			if loc:
				self.kconfig_diag(loc.uri, Diagnostic.err(msg, loc.range))
			else:
				self.cmd_diags.append(Diagnostic.err(
					msg, Range(Position.start(), Position.start())))
		except Exception as e:
			self.cmd_diags.append(Diagnostic.err('Kconfig failed: ' + str(e),
                                       Range(Position.start(), Position.start())))
		self.version += 1

	def kconfig_diag(self, uri: Uri, diag: Diagnostic):
		if not str(uri) in self.kconfig_diags:
			self.kconfig_diags[str(uri)] = []
		self.kconfig_diags[str(uri)].append(diag)

	@property
	def valid(self):
		return self._kconfig and self._kconfig.valid

	def invalidate(self):
		if self._kconfig:
			self._kconfig.valid = False

	def has_file(self, uri: Uri):
		"""Check whether the given URI represents a conf file this context uses. Does not check board files."""
		return any([(file.uri == uri) for file in self.conf_files if file.doc]) or self.board.conf_file.uri == uri

	def _node_id(self, node: kconfiglib.MenuNode):
		"""Encode a unique ID string for the given menu node"""
		if not self._kconfig:
			return ''

		if node == self._kconfig.top_node:
			parts = ['MAINMENU']
		elif node.item == kconfiglib.MENU:
			parts = ['MENU', str(self._kconfig.menus.index(node))]
		elif isinstance(node.item, kconfiglib.Symbol):
			parts = ['SYM', node.item.name, str(node.item.nodes.index(node))]
		elif isinstance(node.item, kconfiglib.Choice):
			parts = ['CHOICE', self._kconfig.choices.index(node)]
		elif node.item == kconfiglib.COMMENT:
			parts = ['COMMENT', self._kconfig.comments.index(node)]
		else:
			parts = ['UNKNOWN', node.filename, node.linenr]

		parts.insert(0, str(self.version))

		return ID_SEP.join(parts)

	def find_node(self, id):
		"""Find a menu node based on a node ID"""
		[version, type, *parts] = id.split(ID_SEP)

		if int(version) != self.version:
			# Since we're building on the exact layout of the internals of the
			# kconfig tree, the node IDs depend on the fact that the tree is unchanged:
			return None

		if type == 'MENU':
			return self._kconfig.menus[int(parts[0])]

		if type == 'SYM':
			return self._kconfig.syms[parts[0]].nodes[int(parts[1])]

		if type == 'CHOICE':
			return self._kconfig.choices[int(parts[0])]

		if type == 'COMMENT':
			return self._kconfig.comments[int(parts[0])]

		if type == 'MAINMENU':
			return self._kconfig.top_node

	def get_menu(self, id=None):
		"""Get the KconfigMenu for the menu node with the given ID"""
		if not id:
			if not self.menu:
				return
			id = self.menu

		node = self.find_node(id)
		if not node:
			return
		return KconfigMenu(node, id)

	def set(self, name, val):
		"""Set a config value (without changing the conf files)"""
		sym = self.get(name)
		if not sym:
			raise RPCError(KconfigErrorCode.UNKNOWN_NODE, 'Unknown symbol {}'.format(name))
		valid = sym.set_value(val)
		if valid and not name in self.modified:
			self.modified.append(name)

	def unset(self, name):
		"""Revert a previous self.set() call."""
		sym = self.get(name)
		if sym:
			sym.unset_value()

	def get(self, name) -> kconfiglib.Symbol:
		"""Get a kconfig symbol based on its name. The name should NOT include the CONFIG_ prefix."""
		if self._kconfig:
			return self._kconfig.syms.get(name)

	def conf_file(self, uri):
		"""Get the config file with the given URI, if any."""
		return next((file for file in [self.board.conf_file] + self.conf_files if file.uri == uri), None)

	def diags(self, uri):
		"""Get the diagnostics for the conf file with the given URI"""
		conf = self.conf_file(uri)
		if conf:
			return conf.diags

	def clear_diags(self):
		"""Clear all diagnostics"""
		if self._kconfig:
			self._kconfig.diags.clear()
		self.kconfig_diags.clear()
		self.cmd_diags.clear()
		for conf in self.conf_files:
			conf.diags.clear()

	def symbols(self, filter):
		"""Get a list of symbols matching the given filter string. Can be used for search or auto completion."""
		if filter and filter.startswith('CONFIG_'):
			filter = filter[len('CONFIG_'):]
		return [sym for sym in self._kconfig.syms.values() if not filter or _filter_match(filter, sym.name)]

	def symbol_search(self, query):
		"""Search for a symbol with a specific name. Returns a list of symbols as SymbolItems."""
		return map(_symbolitem, self.symbols(query))

	def all_entries(self) -> List[ConfEntry]:
		files = [self.board.conf_file] + self.conf_files
		entries = []
		for file in files:
			entries.extend(file.entries())
		return entries

	# Link checks for config file entries:

	def check_undefined(self, file: ConfFile, entry: ConfEntry, sym: kconfiglib.Symbol):
		if sym.type == kconfiglib.UNKNOWN:
			file.diags.append(Diagnostic.err(f'Undefined symbol CONFIG_{sym.name}', entry.full_range))
			return True

	def check_type(self, file: ConfFile, entry: ConfEntry, sym: kconfiglib.Symbol):
		"""Check that the configured value has the right type."""
		if kconfiglib.TYPE_TO_STR[sym.type] != entry.type:
			diag = Diagnostic.err(
				f'Invalid type. Expected {kconfiglib.TYPE_TO_STR[sym.type]}', entry.full_range)

			# Add action to convert between hex and int:
			if sym.type in [kconfiglib.HEX, kconfiglib.INT] and (entry.is_hex() or entry.is_int()):
				action = CodeAction(
					'Convert value to ' + str(kconfiglib.TYPE_TO_STR[sym.type]))
				if sym.type == kconfiglib.HEX:
					action.edit.add(entry.loc.uri, TextEdit(
						entry.value_range, hex(entry.value)))
				else:
					action.edit.add(entry.loc.uri, TextEdit(
						entry.value_range, str(entry.value)))
				diag.add_action(action)

			file.diags.append(diag)
			return True

	def check_assignment(self, file: ConfFile, entry: ConfEntry, sym: kconfiglib.Symbol):
		"""Check that the assigned value actually was propagated."""
		user_value = sym.user_value
		if sym.type in [kconfiglib.BOOL, kconfiglib.TRISTATE]:
			user_value = kconfiglib.TRI_TO_STR[user_value]

		actions = []
		if user_value == sym.str_value:
			if user_value == 'y':
				return
			msg = f'CONFIG_{sym.name} was already disabled.'
			severity = Diagnostic.HINT
		elif len(sym.str_value):
			msg = f'CONFIG_{sym.name} was assigned the value {entry.raw}, but got the value {sym.str_value}.'
			severity = Diagnostic.WARNING
		else:
			msg = f'CONFIG_{sym.name} couldn\'t be set.'
			severity = Diagnostic.WARNING

		deps = _missing_deps(sym)
		if deps:
			msg += ' Missing dependencies:\n'
			msg += ' && '.join([kconfiglib.expr_str(dep) for dep in deps])
			edits = []

			for dep in deps:
				if isinstance(dep, kconfiglib.Symbol) and dep.type == kconfiglib.BOOL:
					dep_entry = next((entry for entry in file.entries() if entry.name == dep.name), None)
					if dep_entry:
						edits.append({'dep': dep.name, 'edit': TextEdit(dep_entry.value_range, 'y')})
					else:
						edits.append({'dep': dep.name, 'edit': TextEdit(Range(entry.line_range.start, entry.line_range.start), f'CONFIG_{dep.name}=y\n')})

			if len(edits) == 1:
				action = CodeAction(f'Enable CONFIG_{edits[0]["dep"]} to resolve dependency')
				action.edit.add(file.uri, edits[0]['edit'])
				actions.append(action)
			elif len(edits) > 1:
				action = CodeAction(f'Enable {len(edits)} entries to resolve dependencies')

				# Dependencies are registered with a "nearest first" approach in kconfiglib.
				# As the nearest dependency is likely lowest in the menu hierarchy, we'll
				# reverse the list of edits, so the highest dependency is inserted first:
				edits.reverse()

				for edit in edits:
					action.edit.add(file.uri, edit['edit'])
				actions.append(action)

			actions.append(entry.remove())

			diag = Diagnostic(msg, entry.range, severity)
			if severity == Diagnostic.HINT:
				diag.mark_unnecessary()
			for action in actions:
				diag.add_action(action)

			file.diags.append(diag)
			return True

	def check_visibility(self, file: ConfFile, entry: ConfEntry, sym: kconfiglib.Symbol):
		"""Check whether the configuration entry actually can be set in config files."""
		if not any(node.prompt for node in sym.nodes):
			diag = Diagnostic.warn(f'Symbol CONFIG_{entry.name} cannot be set (has no prompt)', entry.full_range)
			diag.add_action(entry.remove())
			file.diags.append(diag)
			return True

	def check_defaults(self, file: ConfFile, entry: ConfEntry, sym: kconfiglib.Symbol):
		"""Check whether an entry's value matches the default value, and mark it as redundant"""
		if sym._str_default() == sym.user_value:
			diag = Diagnostic.hint(f'Value is {entry.raw} by default', entry.full_range)
			diag.mark_unnecessary()
			diag.add_action(entry.remove('Remove redundant entry'))
			file.diags.append(diag)
			return True

	def check_multiple_assignments(self, file: ConfFile, entry: ConfEntry, all_entries: List[ConfEntry]):
		matching = [e for e in all_entries if e.name == entry.name]
		if len(matching) > 1 and matching[0] != entry:
			existing = matching[0]
			diag = Diagnostic.warn(f'{entry.name} set more than once. Old value "{existing.value}", new value "{entry.value}".', entry.full_range)
			diag.related_info = [DiagnosticRelatedInfo(
				e.loc, f'Already set to "{e.value}" here') for e in matching if e != entry]
			if existing.value == entry.value:
				diag.mark_unnecessary()
				diag.severity = Diagnostic.HINT
				diag.add_action(entry.remove('Remove redundant entry'))
			file.diags.append(diag)
			return True


	def lint(self):
		"""
		Run a set of checks on the contents of the conf files.

		Adds diagnostics to the failing entries to help developers fix errors
		that will come up when compiling. Reimplements some checks from
		generate_config.py that show up during the build, as these aren't
		part of kconfiglib.
		"""
		all_entries = self.all_entries()
		for file in self.conf_files:
			entries = file.entries()
			for entry in entries:
				if not entry.name in self._kconfig.syms:
					continue

				sym: kconfiglib.Symbol = self._kconfig.syms[entry.name]
				if self.check_undefined(file, entry, sym):
					continue
				if self.check_type(file, entry, sym):
					continue
				if self.check_assignment(file, entry, sym):
					continue
				if self.check_visibility(file, entry, sym):
					continue
				if self.check_defaults(file, entry, sym):
					continue
				if self.check_multiple_assignments(file, entry, all_entries):
					continue

	def load_config(self):
		"""Load configuration files and update the diagnostics"""
		if not self.valid:
			pass

		try:
			self._kconfig.load_config(self.board.conf_file.uri.path, replace=True)

			for file in self.conf_files:
				self._kconfig.load_config(file.uri.path, replace=False)

			self.lint()

			for filename, diags in self._kconfig.diags.items():
				if filename == '':
					self.cmd_diags.extend(diags)
				else:
					uri = Uri.file(filename)
					conf = self.conf_file(uri)
					if conf:
						conf.diags.extend(diags)
					else:
						self.cmd_diags.extend(diags)
		except AttributeError as e:
			self.cmd_diags.append(Diagnostic.err(
				'Kconfig tree parse failed: Invalid attribute ' + str(e), Range(Position.start(), Position.start())))
		except Exception as e:
			self.cmd_diags.append(Diagnostic.err(
				'Kconfig tree parse failed: ' + str(e), Range(Position.start(), Position.start())))

	def symbol_at(self, uri, pos):
		"""Get the symbol referenced at a given position in a conf file."""
		doc = documentStore.get(uri)
		if not doc:
			return

		word = doc.word_at(pos)
		if word and word.startswith('CONFIG_'):
			return self.get(word[len('CONFIG_'):])

	def __repr__(self):
		return str(self.uri)

class KconfigServer(LSPServer):

	def __init__(self, istream=None, ostream=None):
		"""
		The Kconfig LSP Server.

		The LSP Server should be instantiated once for each IDE instance, and is capable of
		handling multiple different Kconfig contexts using create_ctx().

		To run a kconfig server, instantiate it and call loop():
		KconfigServer().loop()

		This will keep running until KconfigServer.running is false.
		"""
		super().__init__('zephyr-kconfig', VERSION, istream, ostream)
		self.last_ctx = None
		self.main_uri = None
		self.ctx: Dict[str, KconfigContext] = {}
		self.dbg('Python version: ' + sys.version)

	def publish_diags(self, uri, diags: List[Diagnostic]):
		"""Send a diagnostics publication notification"""
		self.notify('textDocument/publishDiagnostics', {
			'uri': uri,
			'diagnostics': diags,
		})

	def refresh_ctx(self, ctx: KconfigContext):
		"""Reparse the given Kconfig context, and publish diagsnostics"""
		ctx.clear_diags()
		if not ctx.valid:
			self.dbg('Parsing...')
			ctx.parse()

		self.dbg('Load config...')
		ctx.load_config()

		if ctx.valid:
			self.dbg('Done. {} diags, {} warnings'.format(sum([len(file.diags) for file in ctx.conf_files]), len(ctx._kconfig.warnings)))

		for conf in ctx.conf_files:
			self.publish_diags(conf.uri, conf.diags)

		self.publish_diags(Uri.file('command-line'), ctx.cmd_diags)

		for uri, diags in ctx.kconfig_diags.items():
			self.publish_diags(uri, diags)

	def create_ctx(self, uri: Uri, root, conf_files, env):
		"""
		Create a Kconfig Context with the given parameters.

		A context represents a single build directory.
		"""
		self.dbg(f'Creating context {uri}')
		ctx = KconfigContext(uri, root, conf_files, env)

		self.ctx[str(uri)] = ctx
		return ctx

	def best_ctx(self, uri: Uri):
		"""
		Get the context that is the most likely owner of the given URI.

		Keeps track of the currently referenced context, and will prefer
		this if it owns the given URI.
		"""
		if self.main_uri:
			ctx = self.ctx.get(str(self.main_uri))
			if ctx and ctx.has_file(uri):
				self.last_ctx = ctx
				return ctx

		if self.last_ctx and self.last_ctx.has_file(uri):
			return self.last_ctx

		ctx = next((ctx for ctx in self.ctx.values() if ctx.has_file(uri)), None)
		if ctx:
			self.last_ctx = ctx
		return ctx

	def get_sym(self, params):
		"""
		Get the symbol located at the given Location.
		Interprets location from a common location parameter format:
		- textDocument.uri -> URI
		- position -> Position
		"""
		uri = Uri.parse(params['textDocument']['uri'])
		ctx = self.best_ctx(uri)
		if not ctx:
			self.dbg('No context for {}'.format(uri.path))
			return

		if not ctx.valid:
			self.refresh_ctx(ctx)

		return ctx.symbol_at(uri, Position.create(params['position']))

	@handler('initialized')
	def handle_initialized(self, params):
		self.watch_files('**/Kconfig*')
		self.watch_files('**/edt.pickle')

	@handler('kconfig/addBuild')
	def handle_add_build(self, params):
		uri = Uri.parse(params['uri'])
		if uri:
			confFiles = [ConfFile(Uri.file(f)) for f in params['conf']]
			ctx = self.create_ctx(uri, params['root'], confFiles, params['env'])

			# This is the active build. Parse it right away:
			if uri == self.main_uri:
				self.last_ctx = ctx
				self.refresh_ctx(ctx)
			return {'id': ctx.uri}

	@handler('kconfig/removeBuild')
	def handle_remove_build(self, params):
		uri = Uri.parse(params['uri'])
		if self.ctx.get(str(uri)):
			del self.ctx[str(uri)]
			self.dbg('Deleted build ' + str(uri))

	@handler('kconfig/setMainBuild')
	def handle_set_build(self, params):
		uri = Uri.parse(params['uri'])
		self.main_uri = uri
		ctx = self.ctx.get(str(self.main_uri))
		if ctx:
			self.dbg(f'Main build: {uri}')
			self.dbg('\t' + "\n\t".join([str(f) for f in ctx.conf_files]))
			self.last_ctx = ctx
			self.refresh_ctx(ctx)

	@handler('kconfig/search')
	def handle_search(self, params):
		ctx = self.ctx[params['ctx']]
		if not ctx:
			raise RPCError(KconfigErrorCode.UNKNOWN_CTX, 'Unknown context')

		return {
			'ctx': params['ctx'],
			'query': params['query'],
			'symbols': ctx.symbol_search(params['query']),
		}

	# TODO: This attempts to create a virtual configuration from the open prj.conf file.
	# This needs a bit more thought to work, as we'll need to emulate the build files
	# @handler('textDocument/didOpen')
	# def handle_open(self, params):
	# 	result = super().handle_open(params)
	# 	if params['textDocument'].get('languageId') == 'properties':
	# 		self.create_ctx('Kconfig', [ConfFile(self.docs.get(Uri.parse(params['textDocument']['uri'])))], {})
	# 	return result

	@handler('textDocument/didChange')
	def handle_change(self, params):
		super().handle_change(params)
		if self.last_ctx:
			self.refresh_ctx(self.last_ctx)

		# TODO: Add handling of Kconfig changes:
		# - Reparse the active configuration
		# - Mark other configurations as dirty
		# - Rerun last_ctx.load_config()?
		# - Also need to check which conf files were actually changed

	@handler('kconfig/setMenu')
	def handle_set_menu(self, params):
		ctx = self.ctx[params['ctx']]
		ctx.menu = params['id']
		return ctx.get_menu(params['id'])

	@handler('kconfig/setVal')
	def handle_setval(self, params):
		ctx = self.ctx[params['ctx']]
		if 'val' in params:
			ctx.set(params['name'], params['val'])
		else:
			ctx.unset(params['name'])

	# @handler('kconfig/getEntry')
	# def handle_getentry(self, params):
	# 	pass # TODO: Should get the "help" page for the entry

	@handler('textDocument/completion')
	def handle_completion(self, params):
		uri = Uri.parse(params['textDocument']['uri'])
		ctx = self.best_ctx(uri)
		if not ctx:
			self.dbg('No context for {}'.format(uri.path))
			return

		if not ctx.valid:
			self.refresh_ctx(ctx)

		doc = documentStore.get(uri)
		if not doc:
			self.dbg('Unknown document')
			return

		pos = Position.create(params['position'])
		line = doc.line(pos.line)
		show_non_visible = False
		if line:
			prefix = line[:pos.character]
			word = prefix.lstrip()

			if len(word) > 0:
				# Ensure word starts with 'CONFIG_'. By using commonprefix, we can also detect and correct
				# partial matches:
				common = os.path.commonprefix([word, 'CONFIG_'])
				if len(common) < len('CONFIG_'):
					word = 'CONFIG_' + word[len(common):]
				show_non_visible = True

		else:
			word = None

		def insert_text(sym: kconfiglib.Symbol):
			insert = Snippet('CONFIG_')
			insert.add_text(sym.name)
			insert.add_text('=')
			if sym.type in [kconfiglib.BOOL, kconfiglib.TRISTATE]:
				choices = [kconfiglib.TRI_TO_STR[val] for val in list(sym.assignable)]
				choices.reverse() # sym.assignable shows 'n' first, but user normally wants 'y'
				insert.add_choice(choices)
			elif sym.type == kconfiglib.STRING:
				insert.add_text('"')
				insert.add_tabstop()
				insert.add_text('"')
			elif sym.type == kconfiglib.HEX:
				insert.add_text('0x')
			else:
				pass # freeform value

			return insert.text

		items = [{
				'label': 'CONFIG_' + sym.name,
				'kind': CompletionItemKind.VARIABLE,
				'detail': kconfiglib.TYPE_TO_STR[sym.type],
				'documentation': next((n.help.replace('\n', ' ') for n in sym.nodes if n.help), ' '),
				'insertText': insert_text(sym),
				'insertTextFormat': InsertTextFormat.SNIPPET
			}
			for sym in ctx.symbols(word) if sym.visibility or show_non_visible] # Only show visible symbols on completion without a prefix

		self.dbg('Filter: "{}" Total symbols: {} Results: {}'.format(word, len(ctx._kconfig.syms.items()), len(items)))
		# When performing a completion request without any prefix, we'll only show the visible symbols.
		# Since we want to start showing users non-visible symbols when they start typing, we need
		# to mark the non-prefixed completion list incomplete to make the client re-requests a new list
		return {
			'isIncomplete': not show_non_visible,
			'items': items
		}

	@handler('textDocument/definition')
	def handle_definition(self, params):
		sym = self.get_sym(params)
		if sym:
			return _loc(sym)

	@handler('textDocument/hover')
	def handle_hover(self, params):
		sym = self.get_sym(params)
		if not sym:
			return

		contents = MarkupContent('')

		prompt = next((node.prompt[0] for node in sym.nodes if node.prompt), None)
		if prompt:
			contents.add_text(prompt)
		else:
			contents.add_text(sym.name_and_loc)

		contents.paragraph()
		contents.add_markdown('Type: `{}`'.format(kconfiglib.TYPE_TO_STR[sym.type]))
		contents.linebreak()
		contents.add_markdown("Value: `{}`".format(sym.str_value))
		contents.paragraph()

		help = '\n\n'.join([n.help.replace('\n', ' ') for n in sym.nodes if n.help])
		if help:
			contents.add_text(help)

		return {'contents': contents}

	@handler('textDocument/documentSymbol')
	def handle_doc_symbols(self, params):
		uri = Uri.parse(params['textDocument']['uri'])
		ctx = self.best_ctx(uri)
		if not ctx:
			return

		file = ctx.conf_file(uri)
		if not file:
			return

		def doc_sym(e: ConfEntry):
			sym = ctx.get(e.name)
			prompt = ''
			if sym:
				for node in sym.nodes:
					if node.prompt:
						prompt = node.prompt[0]

			return DocumentSymbol('CONFIG_' + e.name, SymbolKind.PROPERTY, e.full_range, prompt)

		return [doc_sym(e) for e in file.entries()]

	@handler('textDocument/codeAction')
	def handle_code_action(self, params):
		uri = Uri.parse(params['textDocument']['uri'])
		ctx = self.best_ctx(uri)
		if not ctx:
			self.dbg('No context for {}'.format(uri.path))
			return

		if not ctx.valid:
			self.refresh_ctx(ctx)

		conf = ctx.conf_file(uri)
		if not conf:
			self.dbg('No conf file for {}'.format(uri.path))
			return

		range: Range = Range.create(params['range'])
		actions = []
		for diag in conf.diags:
			if range.overlaps(diag.range):
				actions.extend(diag.actions)

		return actions

	def on_file_change(self, uri: Uri, kind: FileChangeKind):
		if uri.basename.startswith('Kconfig'):
			for ctx in self.ctx.values():
				ctx.invalidate()
				self.dbg(f'Invalidated context because of change in {uri}')
		elif uri.basename == 'edt.pickle':
			# When the DTS context for this context changes, it should be invalidated:
			changedCtx = self.ctx.get(str(Uri.file(uri.path.replace('/zephyr/edt.pickle', ''))))
			if changedCtx:
				changedCtx.invalidate()
				self.dbg(f'Invalidated {changedCtx} due to dts changes.')


def wait_for_debugger():
	import debugpy
	# 5678 is the default attach port in the VS Code debug configurations.
	debugpy.listen(5678)
	debugpy.wait_for_client()

def parse_args():
	parser = argparse.ArgumentParser()
	parser.add_argument('--debug', action='store_true', help='Enable debug mode. Will wait for a debugger to attach before starting the server.')
	return parser.parse_args()

if __name__ == "__main__":
	args = parse_args()

	if args.debug:
		wait_for_debugger()

	srv = KconfigServer()
	srv.loop()
