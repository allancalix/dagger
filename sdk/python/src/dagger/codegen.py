import functools
import logging
import re
import textwrap
from abc import ABC, abstractmethod
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from functools import partial
from itertools import chain, groupby
from keyword import iskeyword
from operator import attrgetter
from typing import Any, ClassVar, Generic, Iterator, Protocol, TypeGuard, TypeVar

from attrs import Factory, define
from graphql import (
    GraphQLArgument,
    GraphQLField,
    GraphQLInputField,
    GraphQLInputObjectType,
    GraphQLInputType,
    GraphQLLeafType,
    GraphQLList,
    GraphQLNamedType,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLOutputType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLType,
    GraphQLWrappingType,
    Undefined,
    is_leaf_type,
)
from graphql.pyutils import camel_to_snake

ACRONYM_RE = re.compile(r"([A-Z\d]+)(?=[A-Z\d]|$)")
"""Pattern for grouping initialisms."""

DEPRECATION_RE = re.compile(r"`([a-zA-Z\d_]+)`")
"""Pattern for extracting replaced references in deprecations."""

logger = logging.getLogger(__name__)

indent = partial(textwrap.indent, prefix=" " * 4)
wrap = partial(textwrap.wrap, drop_whitespace=False, replace_whitespace=False)
wrap_indent = partial(wrap, initial_indent=" " * 4, subsequent_indent=" " * 4)


class Scalars(Enum):
    ID = str
    Int = int
    String = str
    Float = float
    Boolean = bool
    Date = date
    DateTime = datetime
    Time = time
    Decimal = Decimal

    @classmethod
    def from_type(cls, t: GraphQLScalarType) -> str:
        try:
            return cls[t.name].value.__name__
        except KeyError:
            return t.name


def joiner(func):
    """Joins elements with a new line from an iterator."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> str:
        return "\n".join(func(*args, **kwargs))

    return wrapper


@joiner
def generate(schema: GraphQLSchema, sync: bool = False) -> Iterator[str]:
    """Code generation main function."""

    yield textwrap.dedent(
        """\
        # Code generated by dagger. DO NOT EDIT.

        from typing import NewType

        from dagger.api.base import Arg, Root, Type

        """
    )

    # collect object types for all id return types
    # used to replace custom scalars by objects in inputs
    id_map: dict[str, str] = {}
    for type_name, t in schema.type_map.items():
        if is_wrapping_type(t):
            t = t.of_type
        if not is_object_type(t):
            continue
        fields: dict[str, GraphQLField] = t.fields
        for field_name, f in fields.items():
            if field_name != "id":
                continue
            field_type = f.type
            if is_wrapping_type(field_type):
                field_type = field_type.of_type
            id_map[field_type.name] = type_name

    handlers: tuple[Handler, ...] = (
        Scalar(sync, id_map),
        Input(sync, id_map),
        Object(sync, id_map),
    )

    def sort_key(t: GraphQLNamedType) -> tuple[int, str]:
        for i, handler in enumerate(handlers):
            if handler.predicate(t):
                return i, t.name
        return -1, t.name

    def group_key(t: GraphQLNamedType) -> Handler | None:
        for handler in handlers:
            if handler.predicate(t):
                return handler

    all_types = sorted(schema.type_map.values(), key=sort_key)

    for handler, types in groupby(all_types, group_key):
        for t in types:
            if handler is None or t.name.startswith("__"):
                continue
            yield handler.render(t)


# FIXME: these typeguards should be contributed upstream
#        https://github.com/graphql-python/graphql-core/issues/183


def is_required_type(t: GraphQLType) -> TypeGuard[GraphQLNonNull]:
    return isinstance(t, GraphQLNonNull)


def is_list_type(t: GraphQLType) -> TypeGuard[GraphQLList]:
    return isinstance(t, GraphQLList)


def is_wrapping_type(t: GraphQLType) -> TypeGuard[GraphQLWrappingType]:
    return isinstance(t, GraphQLWrappingType)


def is_scalar_type(t: GraphQLType) -> TypeGuard[GraphQLScalarType]:
    return isinstance(t, GraphQLScalarType)


def is_input_object_type(t: GraphQLType) -> TypeGuard[GraphQLInputObjectType]:
    return isinstance(t, GraphQLInputObjectType)


def is_object_type(t: GraphQLType) -> TypeGuard[GraphQLObjectType]:
    return isinstance(t, GraphQLObjectType)


def is_output_leaf_type(t: GraphQLOutputType) -> TypeGuard[GraphQLLeafType]:
    return is_leaf_type(t) or (is_wrapping_type(t) and is_output_leaf_type(t.of_type))


def is_custom_scalar_type(t: GraphQLNamedType) -> TypeGuard[GraphQLScalarType]:
    if is_wrapping_type(t):
        return is_custom_scalar_type(t.of_type)
    return is_scalar_type(t) and t.name not in Scalars.__members__


def format_name(s: str) -> str:
    # rewrite acronyms, initialisms and abbreviations
    s = ACRONYM_RE.sub(lambda m: m.group(0).title(), s)
    s = camel_to_snake(s)
    if iskeyword(s):
        s += "_"
    return s


def format_input_type(t: GraphQLInputType, id_map: dict[str, str]) -> str:
    """This may be used in an input object field or an object field parameter."""

    if is_required_type(t):
        t = t.of_type
        fmt = "%s"
    else:
        fmt = "%s | None"

    if is_list_type(t):
        return fmt % f"list[{format_input_type(t.of_type, id_map)}]"

    if is_custom_scalar_type(t) and t.name in id_map:
        return fmt % id_map[t.name]

    if is_scalar_type(t):
        return fmt % Scalars.from_type(t)

    assert not isinstance(t, GraphQLNonNull)

    return fmt % t.name


def format_output_type(t: GraphQLOutputType) -> str:
    """This may be used as the output type of an object field."""

    # only wrap optional and list when ready
    if is_output_leaf_type(t):
        return format_input_type(t, {})

    # when building the query return shouldn't be None
    # even if optional to not break the chain while
    # we're only building the query
    # FIXME: detect this when returning the scalar
    #        since it affects the result
    if is_wrapping_type(t):
        return format_output_type(t.of_type)

    return Scalars.from_type(t) if is_scalar_type(t) else t.name


def output_type_description(t: GraphQLOutputType) -> str:
    if is_wrapping_type(t):
        return output_type_description(t.of_type)
    return t.description if isinstance(t, GraphQLNamedType) else ""


def doc(s: str) -> str:
    """Wrap string in docstring quotes."""
    if "\n" in s:
        s = f"{s}\n"
    return f'"""{s}"""'


class _InputField:
    """Input object field or object field argument."""

    def __init__(
        self,
        name: str,
        graphql: GraphQLInputField | GraphQLArgument,
        id_map: dict[str, str],
    ) -> None:
        self.graphql_name = name
        self.graphql = graphql

        self.name = format_name(name)
        self.type = format_input_type(graphql.type, id_map if name != "id" else {})
        self.description = graphql.description

        self.has_default = graphql.default_value is not Undefined
        self.default_value = graphql.default_value

        if not is_required_type(graphql.type) and not self.has_default:
            self.default_value = None
            self.has_default = True

    def __str__(self) -> str:
        """Output for an InputObject field."""
        sig = self.as_param()
        return f"{sig}\n{doc(self.description)}" if self.description else sig

    def as_param(self) -> str:
        """As a parameter in a function signature."""
        type_ = self.type
        if is_custom_scalar_type(self.graphql.type):
            # custom scalar inputs will be converted to object types
            # so we need to quote in case it's not defined yet
            type_ = f'"{type_}"'
        out = f"{self.name}: {type_}"
        if self.has_default:
            out = f"{out} = {repr(self.default_value)}"
        return out

    @joiner
    def as_doc(self) -> str:
        """As a part of a docstring."""
        yield f"{self.name}:"
        if self.description:
            for line in self.description.split("\n"):
                yield from wrap_indent(line)

    def as_arg(self) -> str:
        """As a Arg object for the query builder."""
        params = [f"'{self.graphql_name}'", self.name]
        if self.has_default:
            params.append(repr(self.default_value))
        return f"Arg({', '.join(params)}),"


class _ObjectField:
    def __init__(
        self,
        name: str,
        field: GraphQLField,
        id_map: dict[str, str],
        sync: bool,
    ) -> None:
        self.graphql_name = name
        self.graphql = field
        self.sync = sync

        self.name = format_name(name)
        self.args = sorted(
            (_InputField(*args, id_map) for args in field.args.items()),
            key=attrgetter("has_default"),
        )
        self.description = field.description
        self.is_leaf = is_output_leaf_type(field.type)
        self.is_custom_scalar = is_custom_scalar_type(field.type)
        self.type = format_output_type(field.type)

    def __str__(self) -> str:
        return f"{self.func_signature()}:\n{indent(self.func_body())}\n"

    def func_signature(self) -> str:
        params = ", ".join(chain(("self",), (a.as_param() for a in self.args)))
        sig = f"def {self.name}({params})"
        ret_type = self.type
        if not self.is_leaf:
            # object return type may not be defined yet
            ret_type = f'"{ret_type}"'
        elif not self.sync:
            sig = f"async {sig}"
        return f"{sig} -> {ret_type}"

    @joiner
    def func_body(self) -> str:
        if docstring := self.func_doc():
            yield doc(docstring)

        if not self.args:
            yield "_args: list[Arg] = []"
        else:
            yield "_args = ["
            yield from (indent(arg.as_arg()) for arg in self.args)
            yield "]"

        yield f'_ctx = self._select("{self.graphql_name}", _args)'

        if self.is_leaf:
            if self.sync:
                yield f"return _ctx.execute_sync({self.type})"
            else:
                yield f"return await _ctx.execute({self.type})"
        else:
            yield f"return {self.type}(_ctx)"

    def func_doc(self) -> str:
        def _out():
            if self.description:
                for line in self.description.split("\n"):
                    yield wrap(line)

            if deprecated := self.deprecated():
                yield chain(
                    (".. deprecated::",),
                    wrap_indent(deprecated),
                )

            if self.name == "id":
                yield (
                    "Note",
                    "----",
                    "This is lazyly evaluated, no operation is actually run.",
                )

            if any(arg.description for arg in self.args):
                yield chain(
                    (
                        "Parameters",
                        "----------",
                    ),
                    (arg.as_doc() for arg in self.args),
                )

            if self.is_leaf and (
                return_doc := output_type_description(self.graphql.type)
            ):
                yield chain(
                    (
                        "Returns",
                        "-------",
                        self.type,
                    ),
                    wrap_indent(return_doc),
                )

        return "\n\n".join("\n".join(section) for section in _out())

    def deprecated(self) -> str:
        def _format_name(m):
            name = format_name(m.group().strip("`"))
            return f":py:meth:`{name}`"

        return (
            DEPRECATION_RE.sub(_format_name, reason)
            if (reason := self.graphql.deprecation_reason)
            else ""
        )


_H = TypeVar("_H", bound=GraphQLNamedType)
"""Handler generic type"""


class Predicate(Protocol):
    def __call__(self, _: Any) -> bool:
        ...


@define
class Handler(ABC, Generic[_H]):
    sync: bool = False
    """Sync or async."""

    id_map: dict[str, str] = Factory(dict)
    """Map to convert ids (custom scalars) to corresponding types."""

    predicate: ClassVar[Predicate] = staticmethod(lambda _: True)
    """Does this handler render the given type?"""

    def render(self, t: _H) -> str:
        return f"{self.render_head(t)}\n{self.render_body(t)}"

    @abstractmethod
    def render_head(self, t: _H) -> str:
        ...

    def render_body(self, t: _H) -> str:
        return f"{doc(t.description)}\n\n" if t.description else ""


@define
class Scalar(Handler[GraphQLScalarType]):
    predicate: ClassVar[Predicate] = staticmethod(is_custom_scalar_type)

    def render_head(self, t: GraphQLScalarType) -> str:
        return f'{t.name} = NewType("{t.name}", str)'


class Field(Protocol):
    name: str

    def __str__(self) -> str:
        ...


_O = TypeVar("_O", GraphQLInputObjectType, GraphQLObjectType)
"""Object handler generic type"""


class ObjectHandler(Handler[_O], Generic[_O]):
    @property
    @abstractmethod
    def field_class(self) -> type[Field]:
        ...

    def render_head(self, t: _O) -> str:
        return "class Client(Root):" if t.name == "Query" else f"class {t.name}(Type):"

    def render_body(self, t: _O) -> str:
        return indent(self._render_body(t))

    @joiner
    def _render_body(self, t: _O) -> str:
        if t.description:
            yield from wrap(doc(t.description))
        yield from (
            str(self.field_class(*args, id_map=self.id_map))
            for args in t.fields.items()
        )


class Input(ObjectHandler[GraphQLInputObjectType]):
    predicate: ClassVar[Predicate] = staticmethod(is_input_object_type)

    @property
    def field_class(self):
        return _InputField


class Object(ObjectHandler[GraphQLObjectType]):
    predicate: ClassVar[Predicate] = staticmethod(is_object_type)

    @property
    def field_class(self):
        return partial(_ObjectField, sync=self.sync)
