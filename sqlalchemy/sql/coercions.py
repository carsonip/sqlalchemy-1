# sql/coercions.py
# Copyright (C) 2005-2019 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php

import numbers
import re

from . import operators
from . import roles
from . import visitors
from .visitors import Visitable
from .. import exc
from .. import inspection
from .. import util
from ..util import collections_abc

elements = None  # type: types.ModuleType
schema = None  # type: types.ModuleType
selectable = None  # type: types.ModuleType
sqltypes = None  # type: types.ModuleType


def _is_literal(element):
    """Return whether or not the element is a "literal" in the context
    of a SQL expression construct.

    """
    return not isinstance(
        element, (Visitable, schema.SchemaEventTarget)
    ) and not hasattr(element, "__clause_element__")


def _document_text_coercion(paramname, meth_rst, param_rst):
    return util.add_parameter_text(
        paramname,
        (
            ".. warning:: "
            "The %s argument to %s can be passed as a Python string argument, "
            "which will be treated "
            "as **trusted SQL text** and rendered as given.  **DO NOT PASS "
            "UNTRUSTED INPUT TO THIS PARAMETER**."
        )
        % (param_rst, meth_rst),
    )


def expect(role, element, **kw):
    # major case is that we are given a ClauseElement already, skip more
    # elaborate logic up front if possible
    impl = _impl_lookup[role]

    if not isinstance(element, (elements.ClauseElement, schema.SchemaItem)):
        resolved = impl._resolve_for_clause_element(element, **kw)
    else:
        resolved = element

    if issubclass(resolved.__class__, impl._role_class):
        if impl._post_coercion:
            resolved = impl._post_coercion(resolved, **kw)
        return resolved
    else:
        return impl._implicit_coercions(element, resolved, **kw)


def expect_as_key(role, element, **kw):
    kw["as_key"] = True
    return expect(role, element, **kw)


def expect_col_expression_collection(role, expressions):
    for expr in expressions:
        strname = None
        column = None

        resolved = expect(role, expr)
        if isinstance(resolved, util.string_types):
            strname = resolved = expr
        else:
            cols = []
            visitors.traverse(resolved, {}, {"column": cols.append})
            if cols:
                column = cols[0]
        add_element = column if column is not None else strname
        yield resolved, column, strname, add_element


class RoleImpl(object):
    __slots__ = ("_role_class", "name", "_use_inspection")

    def _literal_coercion(self, element, **kw):
        raise NotImplementedError()

    _post_coercion = None

    def __init__(self, role_class):
        self._role_class = role_class
        self.name = role_class._role_name
        self._use_inspection = issubclass(role_class, roles.UsesInspection)

    def _resolve_for_clause_element(self, element, argname=None, **kw):
        literal_coercion = self._literal_coercion
        original_element = element
        is_clause_element = False

        while hasattr(element, "__clause_element__") and not isinstance(
            element, (elements.ClauseElement, schema.SchemaItem)
        ):
            element = element.__clause_element__()
            is_clause_element = True

        if not is_clause_element:
            if self._use_inspection:
                insp = inspection.inspect(element, raiseerr=False)
                if insp is not None:
                    try:
                        return insp.__clause_element__()
                    except AttributeError:
                        self._raise_for_expected(original_element, argname)

            return self._literal_coercion(element, argname=argname, **kw)
        else:
            return element

    def _implicit_coercions(self, element, resolved, argname=None, **kw):
        self._raise_for_expected(element, argname)

    def _raise_for_expected(self, element, argname=None):
        if argname:
            raise exc.ArgumentError(
                "%s expected for argument %r; got %r."
                % (self.name, argname, element)
            )
        else:
            raise exc.ArgumentError(
                "%s expected, got %r." % (self.name, element)
            )


class _StringOnly(object):
    def _resolve_for_clause_element(self, element, argname=None, **kw):
        return self._literal_coercion(element, **kw)


class _ReturnsStringKey(object):
    def _implicit_coercions(
        self, original_element, resolved, argname=None, **kw
    ):
        if isinstance(original_element, util.string_types):
            return original_element
        else:
            self._raise_for_expected(original_element, argname)

    def _literal_coercion(self, element, **kw):
        return element


class _ColumnCoercions(object):
    def _warn_for_scalar_subquery_coercion(self):
        util.warn_deprecated(
            "coercing SELECT object to scalar subquery in a "
            "column-expression context is deprecated in version 1.4; "
            "please use the .scalar_subquery() method to produce a scalar "
            "subquery.  This automatic coercion will be removed in a "
            "future release."
        )

    def _implicit_coercions(
        self, original_element, resolved, argname=None, **kw
    ):
        if resolved._is_select_statement:
            self._warn_for_scalar_subquery_coercion()
            return resolved.scalar_subquery()
        elif (
            resolved._is_from_clause
            and isinstance(resolved, selectable.Alias)
            and resolved.original._is_select_statement
        ):
            self._warn_for_scalar_subquery_coercion()
            return resolved.original.scalar_subquery()
        else:
            self._raise_for_expected(original_element, argname)


def _no_text_coercion(
    element, argname=None, exc_cls=exc.ArgumentError, extra=None
):
    raise exc_cls(
        "%(extra)sTextual SQL expression %(expr)r %(argname)sshould be "
        "explicitly declared as text(%(expr)r)"
        % {
            "expr": util.ellipses_string(element),
            "argname": "for argument %s" % (argname,) if argname else "",
            "extra": "%s " % extra if extra else "",
        }
    )


class _NoTextCoercion(object):
    def _literal_coercion(self, element, argname=None, **kw):
        if isinstance(element, util.string_types) and issubclass(
            elements.TextClause, self._role_class
        ):
            _no_text_coercion(element, argname)
        else:
            self._raise_for_expected(element, argname)


class _CoerceLiterals(object):
    _coerce_consts = False
    _coerce_star = False
    _coerce_numerics = False

    def _text_coercion(self, element, argname=None):
        return _no_text_coercion(element, argname)

    def _literal_coercion(self, element, argname=None, **kw):
        if isinstance(element, util.string_types):
            if self._coerce_star and element == "*":
                return elements.ColumnClause("*", is_literal=True)
            else:
                return self._text_coercion(element, argname)

        if self._coerce_consts:
            if element is None:
                return elements.Null()
            elif element is False:
                return elements.False_()
            elif element is True:
                return elements.True_()

        if self._coerce_numerics and isinstance(element, (numbers.Number)):
            return elements.ColumnClause(str(element), is_literal=True)

        self._raise_for_expected(element, argname)


class ExpressionElementImpl(
    _ColumnCoercions, RoleImpl, roles.ExpressionElementRole
):
    def _literal_coercion(
        self, element, name=None, type_=None, argname=None, **kw
    ):
        if element is None:
            return elements.Null()
        else:
            try:
                return elements.BindParameter(
                    name, element, type_, unique=True
                )
            except exc.ArgumentError:
                self._raise_for_expected(element)


class BinaryElementImpl(
    ExpressionElementImpl, RoleImpl, roles.BinaryElementRole
):
    def _literal_coercion(
        self, element, expr, operator, bindparam_type=None, argname=None, **kw
    ):
        try:
            return expr._bind_param(operator, element, type_=bindparam_type)
        except exc.ArgumentError:
            self._raise_for_expected(element)

    def _post_coercion(self, resolved, expr, **kw):
        if (
            isinstance(resolved, elements.BindParameter)
            and resolved.type._isnull
        ):
            resolved = resolved._clone()
            resolved.type = expr.type
        return resolved


class InElementImpl(RoleImpl, roles.InElementRole):
    def _implicit_coercions(
        self, original_element, resolved, argname=None, **kw
    ):
        if resolved._is_from_clause:
            if (
                isinstance(resolved, selectable.Alias)
                and resolved.original._is_select_statement
            ):
                return resolved.original
            else:
                return resolved.select()
        else:
            self._raise_for_expected(original_element, argname)

    def _literal_coercion(self, element, expr, operator, **kw):
        if isinstance(element, collections_abc.Iterable) and not isinstance(
            element, util.string_types
        ):
            args = []
            for o in element:
                if not _is_literal(o):
                    if not isinstance(o, operators.ColumnOperators):
                        self._raise_for_expected(element, **kw)
                elif o is None:
                    o = elements.Null()
                else:
                    o = expr._bind_param(operator, o)
                args.append(o)

            return elements.ClauseList(*args)

        else:
            self._raise_for_expected(element, **kw)

    def _post_coercion(self, element, expr, operator, **kw):
        if element._is_select_statement:
            return element.scalar_subquery()
        elif isinstance(element, elements.ClauseList):
            if len(element.clauses) == 0:
                op, negate_op = (
                    (operators.empty_in_op, operators.empty_notin_op)
                    if operator is operators.in_op
                    else (operators.empty_notin_op, operators.empty_in_op)
                )
                return element.self_group(against=op)._annotate(
                    dict(in_ops=(op, negate_op))
                )
            else:
                return element.self_group(against=operator)

        elif isinstance(element, elements.BindParameter) and element.expanding:

            if isinstance(expr, elements.Tuple):
                element = element._with_expanding_in_types(
                    [elem.type for elem in expr]
                )
            return element
        else:
            return element


class WhereHavingImpl(
    _CoerceLiterals, _ColumnCoercions, RoleImpl, roles.WhereHavingRole
):

    _coerce_consts = True

    def _text_coercion(self, element, argname=None):
        return _no_text_coercion(element, argname)


class StatementOptionImpl(
    _CoerceLiterals, RoleImpl, roles.StatementOptionRole
):

    _coerce_consts = True

    def _text_coercion(self, element, argname=None):
        return elements.TextClause(element)


class ColumnArgumentImpl(_NoTextCoercion, RoleImpl, roles.ColumnArgumentRole):
    pass


class ColumnArgumentOrKeyImpl(
    _ReturnsStringKey, RoleImpl, roles.ColumnArgumentOrKeyRole
):
    pass


class ByOfImpl(_CoerceLiterals, _ColumnCoercions, RoleImpl, roles.ByOfRole):

    _coerce_consts = True

    def _text_coercion(self, element, argname=None):
        return elements._textual_label_reference(element)


class OrderByImpl(ByOfImpl, RoleImpl, roles.OrderByRole):
    def _post_coercion(self, resolved):
        if (
            isinstance(resolved, self._role_class)
            and resolved._order_by_label_element is not None
        ):
            return elements._label_reference(resolved)
        else:
            return resolved


class DMLColumnImpl(_ReturnsStringKey, RoleImpl, roles.DMLColumnRole):
    def _post_coercion(self, element, as_key=False):
        if as_key:
            return element.key
        else:
            return element


class ConstExprImpl(RoleImpl, roles.ConstExprRole):
    def _literal_coercion(self, element, argname=None, **kw):
        if element is None:
            return elements.Null()
        elif element is False:
            return elements.False_()
        elif element is True:
            return elements.True_()
        else:
            self._raise_for_expected(element, argname)


class TruncatedLabelImpl(_StringOnly, RoleImpl, roles.TruncatedLabelRole):
    def _implicit_coercions(
        self, original_element, resolved, argname=None, **kw
    ):
        if isinstance(original_element, util.string_types):
            return resolved
        else:
            self._raise_for_expected(original_element, argname)

    def _literal_coercion(self, element, argname=None, **kw):
        """coerce the given value to :class:`._truncated_label`.

        Existing :class:`._truncated_label` and
        :class:`._anonymous_label` objects are passed
        unchanged.
        """

        if isinstance(element, elements._truncated_label):
            return element
        else:
            return elements._truncated_label(element)


class DDLExpressionImpl(_CoerceLiterals, RoleImpl, roles.DDLExpressionRole):

    _coerce_consts = True

    def _text_coercion(self, element, argname=None):
        return elements.TextClause(element)


class DDLConstraintColumnImpl(
    _ReturnsStringKey, RoleImpl, roles.DDLConstraintColumnRole
):
    pass


class LimitOffsetImpl(RoleImpl, roles.LimitOffsetRole):
    def _implicit_coercions(self, element, resolved, argname=None, **kw):
        if resolved is None:
            return None
        else:
            self._raise_for_expected(element, argname)

    def _literal_coercion(self, element, name, type_, **kw):
        if element is None:
            return None
        else:
            value = util.asint(element)
            return selectable._OffsetLimitParam(
                name, value, type_=type_, unique=True
            )


class LabeledColumnExprImpl(
    ExpressionElementImpl, roles.LabeledColumnExprRole
):
    def _implicit_coercions(
        self, original_element, resolved, argname=None, **kw
    ):
        if isinstance(resolved, roles.ExpressionElementRole):
            return resolved.label(None)
        else:
            new = super(LabeledColumnExprImpl, self)._implicit_coercions(
                original_element, resolved, argname=argname, **kw
            )
            if isinstance(new, roles.ExpressionElementRole):
                return new.label(None)
            else:
                self._raise_for_expected(original_element, argname)


class ColumnsClauseImpl(_CoerceLiterals, RoleImpl, roles.ColumnsClauseRole):

    _coerce_consts = True
    _coerce_numerics = True
    _coerce_star = True

    _guess_straight_column = re.compile(r"^\w\S*$", re.I)

    def _text_coercion(self, element, argname=None):
        element = str(element)

        guess_is_literal = not self._guess_straight_column.match(element)
        raise exc.ArgumentError(
            "Textual column expression %(column)r %(argname)sshould be "
            "explicitly declared with text(%(column)r), "
            "or use %(literal_column)s(%(column)r) "
            "for more specificity"
            % {
                "column": util.ellipses_string(element),
                "argname": "for argument %s" % (argname,) if argname else "",
                "literal_column": "literal_column"
                if guess_is_literal
                else "column",
            }
        )


class ReturnsRowsImpl(RoleImpl, roles.ReturnsRowsRole):
    pass


class StatementImpl(_NoTextCoercion, RoleImpl, roles.StatementRole):
    pass


class CoerceTextStatementImpl(_CoerceLiterals, RoleImpl, roles.StatementRole):
    def _text_coercion(self, element, argname=None):
        return elements.TextClause(element)


class SelectStatementImpl(
    _NoTextCoercion, RoleImpl, roles.SelectStatementRole
):
    def _implicit_coercions(
        self, original_element, resolved, argname=None, **kw
    ):
        if resolved._is_text_clause:
            return resolved.columns()
        else:
            self._raise_for_expected(original_element, argname)


class HasCTEImpl(ReturnsRowsImpl, roles.HasCTERole):
    pass


class FromClauseImpl(_NoTextCoercion, RoleImpl, roles.FromClauseRole):
    def _implicit_coercions(
        self, original_element, resolved, argname=None, **kw
    ):
        if resolved._is_text_clause:
            return resolved
        else:
            self._raise_for_expected(original_element, argname)


class StrictFromClauseImpl(FromClauseImpl, roles.StrictFromClauseRole):
    def _implicit_coercions(
        self,
        original_element,
        resolved,
        argname=None,
        allow_select=False,
        **kw
    ):
        if resolved._is_select_statement and allow_select:
            util.warn_deprecated(
                "Implicit coercion of SELECT and textual SELECT constructs "
                "into FROM clauses is deprecated; please call .subquery() "
                "on any Core select or ORM Query object in order to produce a "
                "subquery object."
            )
            return resolved.subquery()
        else:
            self._raise_for_expected(original_element, argname)


class AnonymizedFromClauseImpl(
    StrictFromClauseImpl, roles.AnonymizedFromClauseRole
):
    def _post_coercion(self, element, flat=False, **kw):
        return element.alias(flat=flat)


class DMLSelectImpl(_NoTextCoercion, RoleImpl, roles.DMLSelectRole):
    def _implicit_coercions(
        self, original_element, resolved, argname=None, **kw
    ):
        if resolved._is_from_clause:
            if (
                isinstance(resolved, selectable.Alias)
                and resolved.original._is_select_statement
            ):
                return resolved.original
            else:
                return resolved.select()
        else:
            self._raise_for_expected(original_element, argname)


class CompoundElementImpl(
    _NoTextCoercion, RoleImpl, roles.CompoundElementRole
):
    def _implicit_coercions(self, original_element, resolved, argname=None):
        if resolved._is_from_clause:
            return resolved
        else:
            self._raise_for_expected(original_element, argname)


_impl_lookup = {}


for name in dir(roles):
    cls = getattr(roles, name)
    if name.endswith("Role"):
        name = name.replace("Role", "Impl")
        if name in globals():
            impl = globals()[name](cls)
            _impl_lookup[cls] = impl
