"""
Cretonne meta language module.

This module provides classes and functions used to describe Cretonne
instructions.
"""
from __future__ import absolute_import
import re
import math
import importlib
from collections import OrderedDict
from .predicates import And, Predicate, FieldPredicate  # noqa

# The typing module is only required by mypy, and we don't use these imports
# outside type comments.
try:
    from typing import Tuple, Union, Any, Iterable, Sequence  # noqa
    MaybeBoundInst = Union['Instruction', 'BoundInstruction']
    AnyPredicate = Union['Predicate', 'FieldPredicate']
except ImportError:
    pass


camel_re = re.compile('(^|_)([a-z])')


def camel_case(s):
    # type: (str) -> str
    """Convert the string s to CamelCase"""
    return camel_re.sub(lambda m: m.group(2).upper(), s)


class Setting(object):
    """
    A named setting variable that can be configured externally to Cretonne.

    Settings are normally not named when they are created. They get their name
    from the `extract_names` method.
    """

    def __init__(self, doc):
        self.name = None  # Assigned later by `extract_names()`.
        self.__doc__ = doc
        # Offset of byte in settings vector containing this setting.
        self.byte_offset = None
        self.group = SettingGroup.append(self)

    def __str__(self):
        return '{}.{}'.format(self.group.name, self.name)

    def predicate_context(self):
        """
        Return the context where this setting can be evaluated as a (leaf)
        predicate.
        """
        return self.group

    def predicate_leafs(self, leafs):
        leafs.add(self)


class BoolSetting(Setting):
    """
    A named setting with a boolean on/off value.

    :param doc: Documentation string.
    :param default: The default value of this setting.
    """

    def __init__(self, doc, default=False):
        super(BoolSetting, self).__init__(doc)
        self.default = default

    def default_byte(self):
        """
        Get the default value of this setting, as a byte that can be bitwise
        or'ed with the other booleans sharing the same byte.
        """
        if self.default:
            return 1 << self.bit_offset
        else:
            return 0

    def rust_predicate(self, prec):
        """
        Return the Rust code to compute the value of this setting.

        The emitted code assumes that the setting group exists as a local
        variable.
        """
        return '{}.{}()'.format(self.group.name, self.name)


class NumSetting(Setting):
    """
    A named setting with an integral value in the range 0--255.

    :param doc: Documentation string.
    :param default: The default value of this setting.
    """

    def __init__(self, doc, default=0):
        super(NumSetting, self).__init__(doc)
        assert default == int(default)
        assert default >= 0 and default <= 255
        self.default = default

    def default_byte(self):
        return self.default


class EnumSetting(Setting):
    """
    A named setting with an enumerated set of possible values.

    The default value is always the first enumerator.

    :param doc: Documentation string.
    :param args: Tuple of unique strings representing the possible values.
    """

    def __init__(self, doc, *args):
        super(EnumSetting, self).__init__(doc)
        assert len(args) > 0, "EnumSetting must have at least one value"
        self.values = tuple(str(x) for x in args)
        self.default = self.values[0]

    def default_byte(self):
        return 0


class SettingGroup(object):
    """
    A group of settings.

    Whenever a :class:`Setting` object is created, it is added to the currently
    open group. A setting group must be closed explicitly before another can be
    opened.

    :param name: Short mnemonic name for setting group.
    :param parent: Parent settings group.
    """

    # The currently open setting group.
    _current = None  # type: SettingGroup

    def __init__(self, name, parent=None):
        self.name = name
        self.parent = parent
        self.settings = []
        # Named predicates computed from settings in this group or its
        # parents.
        self.named_predicates = []
        # All boolean predicates that can be accessed by number. This includes:
        # - All boolean settings in this group.
        # - All named predicates.
        # - Added anonymous predicates, see `number_predicate()`.
        # - Added parent predicates that are replicated in this group.
        # Maps predicate -> number.
        self.predicate_number = OrderedDict()

        self.open()

    def open(self):
        """
        Open this setting group such that future new settings are added to this
        group.
        """
        assert SettingGroup._current is None, (
                "Can't open {} since {} is already open"
                .format(self, SettingGroup._current))
        SettingGroup._current = self

    def close(self, globs=None):
        """
        Close this setting group. This function must be called before opening
        another setting group.

        :param globs: Pass in `globals()` to run `extract_names` on all
            settings defined in the module.
        """
        assert SettingGroup._current is self, (
                "Can't close {}, the open setting group is {}"
                .format(self, SettingGroup._current))
        SettingGroup._current = None
        if globs:
            for name, obj in globs.items():
                if isinstance(obj, Setting):
                    assert obj.name is None, obj.name
                    obj.name = name
                if isinstance(obj, Predicate):
                    assert obj.name is None
                    obj.name = name
                    self.named_predicates.append(obj)
        self.layout()

    @staticmethod
    def append(setting):
        g = SettingGroup._current
        assert g, "Open a setting group before defining settings."
        g.settings.append(setting)
        return g

    def number_predicate(self, pred):
        """
        Make sure that `pred` has an assigned number, and will be included in
        this group's bit vector.

        The numbered predicates include:
        - `BoolSetting` settings that belong to this group.
        - `Predicate` instances in `named_predicates`.
        - `Predicate` instances without a name.
        - Settings or computed predicates that belong to the parent group, but
          need to be accessible by number in this group.

        The numbered predicates are referenced by the encoding tables as ISA
        predicates. See the `isap` field on `Encoding`.

        :returns: The assigned predicate number in this group.
        """
        if pred in self.predicate_number:
            return self.predicate_number[pred]
        else:
            number = len(self.predicate_number)
            self.predicate_number[pred] = number
            return number

    def layout(self):
        """
        Compute the layout of the byte vector used to represent this settings
        group.

        The byte vector contains the following entries in order:

        1. Byte-sized settings like `NumSetting` and `EnumSetting`.
        2. `BoolSetting` settings.
        3. Precomputed named predicates.
        4. Other numbered predicates, including anonymous predicates and parent
           predicates that need to be accessible by number.

        Set `self.settings_size` to the length of the byte vector prefix that
        contains the settings. All bytes after that are computed, not
        configured.

        Set `self.boolean_offset` to the beginning of the numbered predicates,
        2. in the list above.

        Assign `byte_offset` and `bit_offset` fields in all settings.

        After calling this method, no more settings can be added, but
        additional predicates can be made accessible with `number_predicate()`.
        """
        assert len(self.predicate_number) == 0, "Too late for layout"

        # Assign the non-boolean settings.
        byte_offset = 0
        for s in self.settings:
            if not isinstance(s, BoolSetting):
                s.byte_offset = byte_offset
                byte_offset += 1

        # Then the boolean settings.
        self.boolean_offset = byte_offset
        for s in self.settings:
            if isinstance(s, BoolSetting):
                number = self.number_predicate(s)
                s.byte_offset = byte_offset + number // 8
                s.bit_offset = number % 8

        # This is the end of the settings. Round up to a whole number of bytes.
        self.boolean_settings = len(self.predicate_number)
        self.settings_size = self.byte_size()

        # Now assign numbers to all our named predicates.
        for p in self.named_predicates:
            self.number_predicate(p)

    def byte_size(self):
        """
        Compute the number of bytes required to hold all settings and
        precomputed predicates.

        This is the size of the byte-sized settings plus all the numbered
        predcate bits rounded up to a whole number of bytes.
        """
        return self.boolean_offset + (len(self.predicate_number) + 7) // 8


# Kinds of operands.
#
# Each instruction has an opcode and a number of operands. The opcode
# determines the instruction format, and the format determines the number of
# operands and the kind of each operand.
class OperandKind(object):
    """
    An instance of the `OperandKind` class corresponds to a kind of operand.
    Each operand kind has a corresponding type in the Rust representation of an
    instruction.
    """

    def __init__(self, name, doc, default_member=None, rust_type=None):
        self.name = name
        self.__doc__ = doc
        self.default_member = default_member
        # The camel-cased name of an operand kind is also the Rust type used to
        # represent it.
        self.rust_type = rust_type or camel_case(name)

    def __str__(self):
        return self.name

    def __repr__(self):
        return 'OperandKind({})'.format(self.name)

    def operand_kind(self):
        """
        An `OperandKind` instance can be used directly as the type of an
        `Operand` when defining an instruction.
        """
        return self

    def free_typevar(self):
        # Return the free typevariable controlling the type of this operand.
        return None

#: An SSA value operand. This is a value defined by another instruction.
value = OperandKind(
        'value', """
        An SSA value defined by another instruction.

        This kind of operand can represent any SSA value type, but the
        instruction format may restrict the valid value types for a given
        operand.
        """)

#: A variable-sized list of value operands. Use for Ebb and function call
#: arguments.
variable_args = OperandKind(
        'variable_args', """
        A variable size list of `value` operands.

        Use this to represent arguemtns passed to a function call, arguments
        passed to an extended basic block, or a variable number of results
        returned from an instruction.
        """,
        default_member='varargs')


# Instances of immediate operand types are provided in the
# `cretonne.immediates` module.
class ImmediateKind(OperandKind):
    """
    The kind of an immediate instruction operand.

    :param default_member: The default member name of this kind the
                           `InstructionData` data structure.
    """

    def __init__(self, name, doc, default_member='imm', rust_type=None):
        super(ImmediateKind, self).__init__(
                name, doc, default_member, rust_type)

    def __repr__(self):
        return 'ImmediateKind({})'.format(self.name)


# Instances of entity reference operand types are provided in the
# `cretonne.entities` module.
class EntityRefKind(OperandKind):
    """
    The kind of an entity reference instruction operand.
    """

    def __init__(self, name, doc, default_member=None, rust_type=None):
        super(EntityRefKind, self).__init__(
                name, doc, default_member or name, rust_type)

    def __repr__(self):
        return 'EntityRefKind({})'.format(self.name)


# ValueType instances (i8, i32, ...) are provided in the cretonne.types module.
class ValueType(object):
    """
    A concrete SSA value type.

    All SSA values have a type that is described by an instance of `ValueType`
    or one of its subclasses.
    """

    # Map name -> ValueType.
    _registry = dict()  # type: Dict[str, ValueType]

    # List of all the scalar types.
    all_scalars = list()  # type: List[ValueType]

    def __init__(self, name, membytes, doc):
        self.name = name
        self.membytes = membytes
        self.__doc__ = doc
        assert name not in ValueType._registry
        ValueType._registry[name] = self

    def __str__(self):
        return self.name

    def operand_kind(self):
        """
        When a `ValueType` object is used to describe the type of an `Operand`
        in an instruction definition, the kind of that operand is an SSA value.
        """
        return value

    def free_typevar(self):
        return None

    @staticmethod
    def by_name(name):
        if name in ValueType._registry:
            return ValueType._registry[name]
        else:
            raise AttributeError("No type named '{}'".format(name))


class ScalarType(ValueType):
    """
    A concrete scalar (not vector) type.

    Also tracks a unique set of :py:class:`VectorType` instances with this type
    as the lane type.
    """

    def __init__(self, name, membytes, doc):
        super(ScalarType, self).__init__(name, membytes, doc)
        self._vectors = dict()
        # Assign numbers starting from 1. (0 is VOID).
        ValueType.all_scalars.append(self)
        self.number = len(ValueType.all_scalars)
        assert self.number < 16, 'Too many scalar types'

    def __repr__(self):
        return 'ScalarType({})'.format(self.name)

    def rust_name(self):
        return 'types::' + self.name.upper()

    def by(self, lanes):
        """
        Get a vector type with this type as the lane type.

        For example, ``i32.by(4)`` returns the :obj:`i32x4` type.
        """
        if lanes in self._vectors:
            return self._vectors[lanes]
        else:
            v = VectorType(self, lanes)
            self._vectors[lanes] = v
            return v


class VectorType(ValueType):
    """
    A concrete SIMD vector type.

    A vector type has a lane type which is an instance of :class:`ScalarType`,
    and a positive number of lanes.
    """

    def __init__(self, base, lanes):
        assert isinstance(base, ScalarType), 'SIMD lanes must be scalar types'
        super(VectorType, self).__init__(
                name='{}x{}'.format(base.name, lanes),
                membytes=lanes*base.membytes,
                doc="""
                A SIMD vector with {} lanes containing a `{}` each.
                """.format(lanes, base.name))
        self.base = base
        self.lanes = lanes
        self.number = 16*int(math.log(lanes, 2)) + base.number

    def __repr__(self):
        return ('VectorType(base={}, lanes={})'
                .format(self.base.name, self.lanes))


class IntType(ScalarType):
    """A concrete scalar integer type."""

    def __init__(self, bits):
        assert bits > 0, 'IntType must have positive number of bits'
        super(IntType, self).__init__(
                name='i{:d}'.format(bits),
                membytes=bits // 8,
                doc="An integer type with {} bits.".format(bits))
        self.bits = bits

    def __repr__(self):
        return 'IntType(bits={})'.format(self.bits)


class FloatType(ScalarType):
    """A concrete scalar floating point type."""

    def __init__(self, bits, doc):
        assert bits > 0, 'FloatType must have positive number of bits'
        super(FloatType, self).__init__(
                name='f{:d}'.format(bits),
                membytes=bits // 8,
                doc=doc)
        self.bits = bits

    def __repr__(self):
        return 'FloatType(bits={})'.format(self.bits)


class BoolType(ScalarType):
    """A concrete scalar boolean type."""

    def __init__(self, bits):
        assert bits > 0, 'BoolType must have positive number of bits'
        super(BoolType, self).__init__(
                name='b{:d}'.format(bits),
                membytes=bits // 8,
                doc="A boolean type with {} bits.".format(bits))
        self.bits = bits

    def __repr__(self):
        return 'BoolType(bits={})'.format(self.bits)


# Defining instructions.


class InstructionGroup(object):
    """
    Every instruction must belong to exactly one instruction group. A given
    target architecture can support instructions from multiple groups, and it
    does not necessarily support all instructions in a group.

    New instructions are automatically added to the currently open instruction
    group.
    """

    # The currently open instruction group.
    _current = None  # type: InstructionGroup

    def open(self):
        """
        Open this instruction group such that future new instructions are
        added to this group.
        """
        assert InstructionGroup._current is None, (
                "Can't open {} since {} is already open"
                .format(self, InstructionGroup._current))
        InstructionGroup._current = self

    def close(self):
        """
        Close this instruction group. This function should be called before
        opening another instruction group.
        """
        assert InstructionGroup._current is self, (
                "Can't close {}, the open instuction group is {}"
                .format(self, InstructionGroup._current))
        InstructionGroup._current = None

    def __init__(self, name, doc):
        self.name = name
        self.__doc__ = doc
        self.instructions = []
        self.open()

    @staticmethod
    def append(inst):
        assert InstructionGroup._current, \
                "Open an instruction group before defining instructions."
        InstructionGroup._current.instructions.append(inst)


class Operand(object):
    """
    An instruction operand can be an *immediate*, an *SSA value*, or an *entity
    reference*. The type of the operand is one of:

    1. A :py:class:`ValueType` instance indicates an SSA value operand with a
       concrete type.

    2. A :py:class:`TypeVar` instance indicates an SSA value operand, and the
       instruction is polymorphic over the possible concrete types that the
       type variable can assume.

    3. An :py:class:`ImmediateKind` instance indicates an immediate operand
       whose value is encoded in the instruction itself rather than being
       passed as an SSA value.

    4. An :py:class:`EntityRefKind` instance indicates an operand that
       references another entity in the function, typically something declared
       in the function preamble.

    """
    def __init__(self, name, typ, doc=''):
        self.name = name
        self.typ = typ
        self.__doc__ = doc
        self.kind = typ.operand_kind()

    def get_doc(self):
        if self.__doc__:
            return self.__doc__
        else:
            return self.typ.__doc__

    def __str__(self):
        return "`{}`".format(self.name)

    def is_value(self):
        """
        Is this an SSA value operand?
        """
        return self.kind is value


class InstructionFormat(object):
    """
    Every instruction opcode has a corresponding instruction format which
    determines the number of operands and their kinds. Instruction formats are
    identified structurally, i.e., the format of an instruction is derived from
    the kinds of operands used in its declaration.

    Most instruction formats produce a single result, or no result at all. If
    an instruction can produce more than one result, the `multiple_results`
    flag must be set on its format. All results are of the `value` kind, and
    the instruction format does not keep track of how many results are
    produced. Some instructions, like `call`, may have a variable number of
    results.

    All instruction formats must be predefined in the
    :py:mod:`cretonne.formats` module.

    :param kinds: List of `OperandKind` objects describing the operands.
    :param name: Instruction format name in CamelCase. This is used as a Rust
        variant name in both the `InstructionData` and `InstructionFormat`
        enums.
    :param multiple_results: Set to `True` if this instruction format allows
        more than one result to be produced.
    :param boxed_storage: Set to `True` is this instruction format requires a
        `data: Box<...>` pointer to additional storage in its `InstructionData`
        variant.
    :param typevar_operand: Index of the input operand that is used to infer
        the controlling type variable. By default, this is the first `value`
        operand.
    """

    # Map (multiple_results, kind, kind, ...) -> InstructionFormat
    _registry = dict()  # type: Dict[Tuple, InstructionFormat]

    # All existing formats.
    all_formats = list()  # type: List[InstructionFormat]

    def __init__(self, *kinds, **kwargs):
        # type: (*Union[OperandKind, Tuple[str, OperandKind]], **Any) -> None # noqa
        self.name = kwargs.get('name', None)  # type: str
        self.multiple_results = kwargs.get('multiple_results', False)
        self.boxed_storage = kwargs.get('boxed_storage', False)
        self.members = list()  # type: List[str]
        self.kinds = tuple(self._process_member_names(kinds))

        # Which of self.kinds are `value`?
        self.value_operands = tuple(
                i for i, k in enumerate(self.kinds) if k is value)

        # The typevar_operand argument must point to a 'value' operand.
        self.typevar_operand = kwargs.get('typevar_operand', None)  # type: int
        if self.typevar_operand is not None:
            assert self.kinds[self.typevar_operand] is value, \
                    "typevar_operand must indicate a 'value' operand"
        elif len(self.value_operands) > 0:
            # Default to the first 'value' operand, if there is one.
            self.typevar_operand = self.value_operands[0]

        # Compute a signature for the global registry.
        sig = (self.multiple_results,) + self.kinds
        if sig in InstructionFormat._registry:
            raise RuntimeError(
                "Format '{}' has the same signature as existing format '{}'"
                .format(self.name, InstructionFormat._registry[sig]))
        InstructionFormat._registry[sig] = self
        InstructionFormat.all_formats.append(self)

    def _process_member_names(self, kinds):
        # type: (Sequence[Union[OperandKind, Tuple[str, OperandKind]]]) -> Iterable[OperandKind] # noqa
        """
        Extract names of all the immediate operands in the kinds tuple.

        Each entry is either an `OperandKind` instance, or a `(member, kind)`
        pair. The member names correspond to members in the Rust
        `InstructionData` data structure.

        Yields the operand kinds.
        """
        for arg in kinds:
            if isinstance(arg, OperandKind):
                member = arg.default_member
                k = arg
            else:
                member, k = arg
            self.members.append(member)
            yield k

    def __str__(self):
        args = ', '.join('{}: {}'.format(m, k) if m else str(k)
                         for m, k in zip(self.members, self.kinds))
        return '{}({})'.format(self.name, args)

    def __getattr__(self, attr):
        # type: (str) -> FormatField
        """
        Make instruction format members available as attributes.

        Each non-value format member becomes a corresponding `FormatField`
        attribute.
        """
        try:
            i = self.members.index(attr)
        except ValueError:
            raise AttributeError(
                    '{} is neither a {} member or a '
                    .format(attr, self.name) +
                    'normal InstructionFormat attribute')
        field = FormatField(self, i, attr)
        setattr(self, attr, field)
        return field

    @staticmethod
    def lookup(ins, outs):
        # type: (Sequence[Operand], Sequence[Operand]) -> InstructionFormat
        """
        Find an existing instruction format that matches the given lists of
        instruction inputs and outputs.

        The `ins` and `outs` arguments correspond to the
        :py:class:`Instruction` arguments of the same name, except they must be
        tuples of :py:`Operand` objects.
        """
        if len(outs) == 1:
            multiple_results = outs[0].kind == variable_args
        else:
            multiple_results = len(outs) > 1
        sig = (multiple_results,) + tuple(op.kind for op in ins)
        if sig not in InstructionFormat._registry:
            raise RuntimeError(
                    "No instruction format matches ins = ({}){}".format(
                        ", ".join(map(str, sig[1:])),
                        "[multiple results]" if multiple_results else ""))
        return InstructionFormat._registry[sig]

    @staticmethod
    def extract_names(globs):
        """
        Given a dict mapping name -> object as returned by `globals()`, find
        all the InstructionFormat objects and set their name from the dict key.
        This is used to name a bunch of global variables in a module.
        """
        for name, obj in globs.items():
            if isinstance(obj, InstructionFormat):
                assert obj.name is None
                obj.name = name


class FormatField(object):
    """
    A field in an instruction format.

    This corresponds to a single member of a variant of the `InstructionData`
    data type.

    :param format: Parent `InstructionFormat`.
    :param operand: Operand number in parent.
    :param name: Member name in `InstructionData` variant.
    """

    def __init__(self, format, operand, name):
        # type: (InstructionFormat, int, str) -> None
        self.format = format
        self.operand = operand
        self.name = name

    def __str__(self):
        return '{}.{}'.format(self.format.name, self.name)

    def rust_name(self):
        # type: () -> str
        if self.format.boxed_storage:
            return 'data.' + self.name
        else:
            return self.name


class Instruction(object):
    """
    The operands to the instruction are specified as two tuples: ``ins`` and
    ``outs``. Since the Python singleton tuple syntax is a bit awkward, it is
    allowed to specify a singleton as just the operand itself, i.e., `ins=x`
    and `ins=(x,)` are both allowed and mean the same thing.

    :param name: Instruction mnemonic, also becomes opcode name.
    :param doc: Documentation string.
    :param ins: Tuple of input operands. This can be a mix of SSA value
                operands and other operand kinds.
    :param outs: Tuple of output operands. The output operands must be SSA
                values or `variable_args`.
    :param is_terminator: This is a terminator instruction.
    :param is_branch: This is a branch instruction.
    """

    def __init__(self, name, doc, ins=(), outs=(), **kwargs):
        # type: (str, str, Union[Sequence[Operand], Operand], Union[Sequence[Operand], Operand], **Any) -> None # noqa
        self.name = name
        self.camel_name = camel_case(name)
        self.__doc__ = doc
        self.ins = self._to_operand_tuple(ins)
        self.outs = self._to_operand_tuple(outs)
        self.format = InstructionFormat.lookup(self.ins, self.outs)
        # Indexes into outs for value results. Others are `variable_args`.
        self.value_results = tuple(
                i for i, o in enumerate(self.outs) if o.is_value())
        self._verify_polymorphic()
        InstructionGroup.append(self)

    def __str__(self):
        prefix = ', '.join(o.name for o in self.outs)
        if prefix:
            prefix = prefix + ' = '
        suffix = ', '.join(o.name for o in self.ins)
        return '{}{} {}'.format(prefix, self.name, suffix)

    def blurb(self):
        """Get the first line of the doc comment"""
        for line in self.__doc__.split('\n'):
            line = line.strip()
            if line:
                return line
        return ""

    def _verify_polymorphic(self):
        """
        Check if this instruction is polymorphic, and verify its use of type
        variables.
        """
        poly_ins = [
                i for i in self.format.value_operands
                if self.ins[i].typ.free_typevar()]
        poly_outs = [
                i for i, o in enumerate(self.outs)
                if o.typ.free_typevar()]
        self.is_polymorphic = len(poly_ins) > 0 or len(poly_outs) > 0
        if not self.is_polymorphic:
            return

        # Prefer to use the typevar_operand to infer the controlling typevar.
        self.use_typevar_operand = False
        typevar_error = None
        if self.format.typevar_operand is not None:
            try:
                tv = self.ins[self.format.typevar_operand].typ
                if tv is tv.free_typevar():
                    self.other_typevars = self._verify_ctrl_typevar(tv)
                    self.ctrl_typevar = tv
                    self.use_typevar_operand = True
            except RuntimeError as e:
                typevar_error = e

        if not self.use_typevar_operand:
            # The typevar_operand argument doesn't work. Can we infer from the
            # first result instead?
            if len(self.outs) == 0:
                if typevar_error:
                    raise typevar_error
                else:
                    raise RuntimeError(
                            "typevar_operand must be a free type variable")
            tv = self.outs[0].typ
            if tv is not tv.free_typevar():
                raise RuntimeError("first result must be a free type variable")
            self.other_typevars = self._verify_ctrl_typevar(tv)
            self.ctrl_typevar = tv

    def _verify_ctrl_typevar(self, ctrl_typevar):
        """
        Verify that the use of TypeVars is consistent with `ctrl_typevar` as
        the controlling type variable.

        All polymorhic inputs must either be derived from `ctrl_typevar` or be
        independent free type variables only used once.

        All polymorphic results must be derived from `ctrl_typevar`.

        Return list of other type variables used, or raise an error.
        """
        other_tvs = []
        # Check value inputs.
        for opidx in self.format.value_operands:
            typ = self.ins[opidx].typ
            tv = typ.free_typevar()
            # Non-polymorphic or derived form ctrl_typevar is OK.
            if tv is None or tv is ctrl_typevar:
                continue
            # No other derived typevars allowed.
            if typ is not tv:
                raise RuntimeError(
                        "{}: type variable {} must be derived from {}"
                        .format(self.ins[opidx], typ.name, ctrl_typevar))
            # Other free type variables can only be used once each.
            if tv in other_tvs:
                raise RuntimeError(
                        "type variable {} can't be used more than once"
                        .format(tv.name))
            other_tvs.append(tv)

        # Check outputs.
        for result in self.outs:
            typ = result.typ
            tv = typ.free_typevar()
            # Non-polymorphic or derived from ctrl_typevar is OK.
            if tv is None or tv is ctrl_typevar:
                continue
            raise RuntimeError(
                    "type variable in output not derived from ctrl_typevar")

        return other_tvs

    @staticmethod
    def _to_operand_tuple(x):
        # type: (Union[Sequence[Operand], Operand]) -> Tuple[Operand, ...]
        # Allow a single Operand instance instead of the awkward singleton
        # tuple syntax.
        if isinstance(x, Operand):
            x = (x,)
        else:
            x = tuple(x)
        for op in x:
            assert isinstance(op, Operand)
        return x

    def bind(self, *args):
        # type: (*ValueType) -> BoundInstruction
        """
        Bind a polymorphic instruction to a concrete list of type variable
        values.
        """
        assert self.is_polymorphic
        return BoundInstruction(self, args)

    def __getattr__(self, name):
        # type: (str) -> BoundInstruction
        """
        Bind a polymorphic instruction to a single type variable with dot
        syntax:

        >>> iadd.i32
        """
        return self.bind(ValueType.by_name(name))

    def fully_bound(self):
        # type: () -> Tuple[Instruction, Tuple[ValueType, ...]]
        """
        Verify that all typevars have been bound, and return a
        `(inst, typevars)` pair.

        This version in `Instruction` itself allows non-polymorphic
        instructions to duck-type as `BoundInstruction`\s.
        """
        assert not self.is_polymorphic, self
        return (self, ())

    def __call__(self, *args):
        """
        Create an `ast.Apply` AST node representing the application of this
        instruction to the arguments.
        """
        from .ast import Apply
        return Apply(self, args)


class BoundInstruction(object):
    """
    A polymorphic `Instruction` bound to concrete type variables.
    """

    def __init__(self, inst, typevars):
        # type: (Instruction, Tuple[ValueType, ...]) -> None
        self.inst = inst
        self.typevars = typevars
        assert len(typevars) <= 1 + len(inst.other_typevars)

    def __str__(self):
        return '.'.join([self.inst.name, ] + list(map(str, self.typevars)))

    def bind(self, *args):
        # type: (*ValueType) -> BoundInstruction
        """
        Bind additional typevars.
        """
        return BoundInstruction(self.inst, self.typevars + args)

    def __getattr__(self, name):
        # type: (str) -> BoundInstruction
        """
        Bind an additional typevar dot syntax:

        >>> uext.i32.i8
        """
        return self.bind(ValueType.by_name(name))

    def fully_bound(self):
        # type: () -> Tuple[Instruction, Tuple[ValueType, ...]]
        """
        Verify that all typevars have been bound, and return a
        `(inst, typevars)` pair.
        """
        if len(self.typevars) < 1 + len(self.inst.other_typevars):
            unb = ', '.join(
                    str(tv) for tv in
                    self.inst.other_typevars[len(self.typevars) - 1:])
            raise AssertionError("Unbound typevar {} in {}".format(unb, self))
        assert len(self.typevars) == 1 + len(self.inst.other_typevars)
        return (self.inst, self.typevars)

    def __call__(self, *args):
        """
        Create an `ast.Apply` AST node representing the application of this
        instruction to the arguments.
        """
        from .ast import Apply
        return Apply(self, args)


# Defining target ISAs.


class TargetISA(object):
    """
    A target instruction set architecture.

    The `TargetISA` class collects everything known about a target ISA.

    :param name: Short mnemonic name for the ISA.
    :param instruction_groups: List of `InstructionGroup` instances that are
        relevant for this ISA.
    """

    def __init__(self, name, instruction_groups):
        self.name = name
        self.settings = None
        self.instruction_groups = instruction_groups
        self.cpumodes = list()

    def finish(self):
        """
        Finish the definition of a target ISA after adding all CPU modes and
        settings.

        This computes some derived properties that are used in multilple
        places.

        :returns self:
        """
        self._collect_encoding_recipes()
        self._collect_predicates()
        return self

    def _collect_encoding_recipes(self):
        """
        Collect and number all encoding recipes in use.
        """
        self.all_recipes = list()
        rcps = set()
        for cpumode in self.cpumodes:
            for enc in cpumode.encodings:
                recipe = enc.recipe
                if recipe not in rcps:
                    recipe.number = len(rcps)
                    rcps.add(recipe)
                    self.all_recipes.append(recipe)

    def _collect_predicates(self):
        """
        Collect and number all predicates in use.

        Sets `instp.number` for all used instruction predicates and places them
        in `self.all_instps` in numerical order.

        Ensures that all ISA predicates have an assigned bit number in
        `self.settings`.
        """
        self.all_instps = list()
        instps = set()
        for cpumode in self.cpumodes:
            for enc in cpumode.encodings:
                instp = enc.instp
                if instp and instp not in instps:
                    # assign predicate number starting from 0.
                    instp.number = len(instps)
                    instps.add(instp)
                    self.all_instps.append(instp)

                # All referenced ISA predicates must have a number in
                # `self.settings`. This may cause some parent predicates to be
                # replicated here, which is OK.
                if enc.isap:
                    self.settings.number_predicate(enc.isap)


class CPUMode(object):
    """
    A CPU mode determines which instruction encodings are active.

    All instruction encodings are associated with exactly one `CPUMode`, and
    all CPU modes are associated with exactly one `TargetISA`.

    :param name: Short mnemonic name for the CPU mode.
    :param target: Associated `TargetISA`.
    """

    def __init__(self, name, isa):
        self.name = name
        self.isa = isa
        self.encodings = []
        isa.cpumodes.append(self)

    def __str__(self):
        return self.name

    def enc(self, *args, **kwargs):
        """
        Add a new encoding to this CPU mode.

        Arguments are the `Encoding constructor arguments, except for the first
        `CPUMode argument which is implied.
        """
        self.encodings.append(Encoding(self, *args, **kwargs))


class EncRecipe(object):
    """
    A recipe for encoding instructions with a given format.

    Many different instructions can be encoded by the same recipe, but they
    must all have the same instruction format.

    :param name: Short mnemonic name for this recipe.
    :param format: All encoded instructions must have this
            :py:class:`InstructionFormat`.
    """

    def __init__(self, name, format, instp=None, isap=None):
        self.name = name
        self.format = format
        self.instp = instp
        self.isap = isap
        if instp:
            assert instp.predicate_context() == format

    def __str__(self):
        return self.name


class Encoding(object):
    """
    Encoding for a concrete instruction.

    An `Encoding` object ties an instruction opcode with concrete type
    variables together with and encoding recipe and encoding bits.

    :param cpumode: The CPU mode where the encoding is active.
    :param inst: The :py:class:`Instruction` or :py:class:`BoundInstruction`
                 being encoded.
    :param recipe: The :py:class:`EncRecipe` to use.
    :param encbits: Additional encoding bits to be interpreted by `recipe`.
    :param instp: Instruction predicate, or `None`.
    :param isap: ISA predicate, or `None`.
    """

    def __init__(self, cpumode, inst, recipe, encbits, instp=None, isap=None):
        # type: (CPUMode, MaybeBoundInst, EncRecipe, int, AnyPredicate, AnyPredicate) -> None # noqa
        assert isinstance(cpumode, CPUMode)
        assert isinstance(recipe, EncRecipe)
        self.inst, self.typevars = inst.fully_bound()
        self.cpumode = cpumode
        assert self.inst.format == recipe.format, (
                "Format {} must match recipe: {}".format(
                    self.inst.format, recipe.format))
        self.recipe = recipe
        self.encbits = encbits
        # Combine recipe predicates with the manually specified ones.
        self.instp = And.combine(recipe.instp, instp)
        self.isap = And.combine(recipe.isap, isap)

    def __str__(self):
        return '[{}#{:02x}]'.format(self.recipe, self.encbits)

    def ctrl_typevar(self):
        """
        Get the controlling type variable for this encoding or `None`.
        """
        if self.typevars:
            return self.typevars[0]
        else:
            return None


# Import the fixed instruction formats now so they can be added to the
# registry.
importlib.import_module('cretonne.formats')
