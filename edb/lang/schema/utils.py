#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2008-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


import collections
import itertools
import typing

from edb.lang.common import levenshtein
from edb.lang.edgeql import ast as ql_ast

from . import error as s_err
from . import name as sn
from . import objects as so
from . import types as s_types


def ast_to_typeref(
        node: ql_ast.TypeName, *,
        modaliases: typing.Dict[typing.Optional[str], str],
        schema) -> so.ObjectRef:
    if node.subtypes is not None:
        coll = s_types.Collection.get_class(node.maintype.name)

        if issubclass(coll, s_types.Tuple):
            subtypes = collections.OrderedDict()
            # tuple declaration must either be named or unnamed, but not both
            named = None
            unnamed = None
            for si, st in enumerate(node.subtypes):
                if st.name:
                    named = True
                    type_name = st.name
                else:
                    unnamed = True
                    type_name = str(si)

                if named is not None and unnamed is not None:
                    raise s_err.ItemNotFoundError(
                        f'mixing named and unnamed tuple declaration '
                        f'is not supported',
                        context=node.subtypes[0].context,
                    )

                subtypes[type_name] = ast_to_typeref(
                    st, modaliases=modaliases, schema=schema)

            try:
                return coll.from_subtypes(subtypes, {'named': bool(named)})
            except s_err.SchemaError as e:
                # all errors raised inside are pertaining to subtypes, so
                # the context should point to the first subtype
                e.set_source_context(node.subtypes[0].context)
                raise e

        else:
            subtypes = []
            for st in node.subtypes:
                subtypes.append(ast_to_typeref(
                    st, modaliases=modaliases, schema=schema))

            try:
                return coll.from_subtypes(subtypes)
            except s_err.SchemaError as e:
                e.set_source_context(node.context)
                raise e

    nqname = node.maintype.name
    module = node.maintype.module
    if schema is not None:
        if module is not None:
            lname = sn.Name(module=module, name=nqname)
        else:
            lname = nqname
        obj = schema.get(lname, module_aliases=modaliases, default=None)
        if obj is not None:
            module = obj.name.module
    elif modaliases:
        module = modaliases.get(module)

    if module is None:
        raise s_err.ItemNotFoundError(
            f'unqualified name and no default module set',
            context=node.context
        )

    return so.ObjectRef(classname=sn.Name(module=module, name=nqname))


def typeref_to_ast(t: so.Object) -> ql_ast.TypeName:
    if not isinstance(t, s_types.Collection):
        if isinstance(t, so.ObjectRef):
            name = t.classname
        else:
            name = t.name

        result = ql_ast.TypeName(
            maintype=ql_ast.ObjectRef(
                module=name.module,
                name=name.name
            )
        )
    else:
        result = ql_ast.TypeName(
            maintype=ql_ast.ObjectRef(
                name=t.schema_name
            ),
            subtypes=[
                typeref_to_ast(st) for st in t.get_subtypes()
            ]
        )

    return result


def reduce_to_typeref(t: s_types.Type) -> so.Object:
    ref, _ = t._reduce_to_ref()
    return ref


def resolve_typeref(ref: so.Object, schema) -> so.Object:
    if isinstance(ref, s_types.Tuple):
        if any(isinstance(st, so.ObjectRef) for st in ref.get_subtypes()):
            subtypes = collections.OrderedDict()
            for st_name, st in ref.element_types.items():
                subtypes[st_name] = schema.get(st.classname)

            obj = ref.__class__.from_subtypes(
                subtypes, typemods=ref.get_typemods())
        else:
            obj = ref

    elif isinstance(ref, s_types.Collection):
        if any(isinstance(st, so.ObjectRef) for st in ref.get_subtypes()):
            subtypes = []
            for st in ref.get_subtypes():
                subtypes.append(schema.get(st.classname))

            obj = ref.__class__.from_subtypes(
                subtypes, typemods=ref.get_typemods())
        else:
            obj = ref

    else:
        obj = schema.get(ref.classname)

    return obj


def is_nontrivial_container(value):
    coll_classes = (collections.abc.Sequence, collections.abc.Set)
    trivial_classes = (str, bytes, bytearray, memoryview)
    return (isinstance(value, coll_classes) and
            not isinstance(value, trivial_classes))


def get_class_nearest_common_ancestor(classes):
    # First, find the intersection of parents
    classes = list(classes)
    first = classes[0].get_mro()
    common = set(first).intersection(
        *[set(c.get_mro()) for c in classes[1:]])
    common = sorted(common, key=lambda i: first.index(i))
    if common:
        return common[0]
    else:
        return None


def minimize_class_set_by_most_generic(classes):
    """Minimize the given Object set by filtering out all subclasses."""

    classes = list(classes)
    mros = [set(p.get_mro()) for p in classes]
    count = len(classes)
    smap = itertools.starmap

    # Return only those entries that do not have other entries in their mro
    result = [
        scls for i, scls in enumerate(classes)
        if not any(smap(set.__contains__,
                        ((mros[i], classes[j])
                         for j in range(count) if j != i)))
    ]

    return result


def minimize_class_set_by_least_generic(classes):
    """Minimize the given Object set by filtering out all superclasses."""

    classes = list(classes)
    mros = [set(p.get_mro()) for p in classes]
    count = len(classes)
    smap = itertools.starmap

    # Return only those entries that are not present in other entries' mro
    result = [
        scls for i, scls in enumerate(classes)
        if not any(smap(set.__contains__,
                        ((mros[j], classes[i])
                         for j in range(count) if j != i)))
    ]

    return result


def get_inheritance_map(classes):
    """Return a dict where values are strict subclasses of the key."""
    return {scls: [p for p in classes if p != scls and p.issubclass(scls)]
            for scls in classes}


def get_full_inheritance_map(schema, classes):
    """Same as :func:`get_inheritance_map`, but considers full hierarchy."""

    chain = itertools.chain.from_iterable
    result = {}

    for p, descendants in get_inheritance_map(classes).items():
        result[p] = (set(chain(d.descendants(schema) for d in descendants)) |
                     set(descendants))

    return result


def merge_reduce(target, sources, field_name, *, schema, f):
    values = []
    ours = getattr(target, field_name)
    if ours is not None:
        values.append(ours)
    for source in sources:
        theirs = getattr(source, field_name)
        if theirs is not None:
            values.append(theirs)

    if values:
        return f(values)
    else:
        return None


def merge_sticky_bool(target, sources, field_name, *, schema):
    return merge_reduce(target, sources, field_name, schema=schema, f=max)


def merge_weak_bool(target, sources, field_name, *, schema):
    return merge_reduce(target, sources, field_name, schema=schema, f=min)


def find_item_suggestions(
        name, modaliases, schema, *, item_types=None, limit=3,
        collection=None):
    if isinstance(name, sn.Name):
        orig_modname = name.module
        short_name = name.name
    else:
        orig_modname = None
        short_name = name

    modname = modaliases.get(orig_modname, orig_modname)

    suggestions = []

    if collection is not None:
        suggestions.extend(collection)
    else:
        if modname:
            module = schema.get_module(modname)
            suggestions.extend(module.get_objects())

        if not orig_modname:
            suggestions.extend(schema.get_module('std').get_objects())

    if item_types:
        suggestions = list(
            filter(lambda s: isinstance(s, item_types), suggestions))

    # Compute Levenshtein distance for each suggestion.
    with_distance = [
        (s, levenshtein.distance(short_name, s.shortname.name))
        for s in suggestions
    ]

    # Filter out suggestions that are too dissimilar.
    max_distance = 3
    closest = list(filter(lambda s: s[1] < max_distance, with_distance))

    # Sort by proximity, then by whether the suggestion is contains
    # the source string at the beginning, then by suggestion name.
    closest.sort(
        key=lambda s: (
            s[1],
            not s[0].shortname.name.startswith(short_name),
            s[0].displayname
        )
    )

    return [s[0] for s in closest[:limit]]


def enrich_schema_lookup_error(
        error, item_name, modaliases, schema, *,
        item_types, suggestion_limit=3, name_template=None, collection=None):

    suggestions = find_item_suggestions(
        item_name, modaliases, schema,
        item_types=item_types, limit=suggestion_limit, collection=collection)

    if suggestions:
        names = []
        current_module_name = modaliases.get(None)

        for suggestion in suggestions:
            if (suggestion.name.module == 'std' or
                    suggestion.name.module == current_module_name):
                names.append(suggestion.shortname.name)
            else:
                names.append(str(suggestion.displayname))

        if name_template is not None:
            names = [name_template.format(name=name) for name in names]

        if len(names) > 1:
            hint = f'did you mean one of these: {", ".join(names)}?'
        else:
            hint = f'did you mean {names[0]!r}?'

        error.set_hint_and_details(hint=hint)


def is_legal_function_arg(
        argt: s_types.Type, paramt: s_types.Type,
        returnt: s_types.Type) -> bool:
    # If the parameter type is 'any', the return type is
    # 'array<any>', and the argument is also an 'array', then
    # this is an invalid function invocation.
    return not (
        paramt.name == 'std::any' and
        isinstance(returnt, s_types.Array) and
        returnt.get_subtypes()[0].name == 'std::any' and
        isinstance(argt, s_types.Array)
    )