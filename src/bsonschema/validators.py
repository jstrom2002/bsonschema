"""
Creation and extension of validators, with implementations for existing drafts.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from operator import methodcaller
from typing import TYPE_CHECKING
from urllib.parse import unquote, urldefrag, urljoin, urlsplit
from urllib.request import urlopen
from warnings import warn
import contextlib
import json
import reprlib
import warnings

from attrs import define, field, fields
from rpds import HashTrieMap
import referencing.exceptions
import referencing.jsonschema

import bsonschema._format
import bsonschema._keywords
import bsonschema._legacy_keywords
import bsonschema._types
import bsonschema._typing
import bsonschema._utils
import bsonschema.exceptions


if TYPE_CHECKING:
    from bsonschema.protocols import Validator

_UNSET = bsonschema._utils.Unset()

_VALIDATORS: dict[str, Validator] = {}
_META_SCHEMAS = bsonschema._utils.URIDict()

SPECIFICATIONS: referencing.jsonschema.Registry[referencing.jsonschema.Specification[referencing.jsonschema.Schema]] = referencing.jsonschema.Registry(
    {  # type: ignore[reportGeneralTypeIssues]  # :/ internal vs external types
        dialect_id: referencing.jsonschema.Resource.opaque(specification)
        for dialect_id, specification in [
            ("http://json-schema.org/draft-03/schema", referencing.jsonschema.DRAFT3),
        ]
    },
)

draft3string = """
{
	"$schema" : "http://json-schema.org/draft-03/schema#",
	"id" : "http://json-schema.org/draft-03/schema#",
	"type" : "object",
	
	"properties" : {
		"type" : {
			"type" : ["string", "array"],
			"items" : {
				"type" : ["string", {"$ref" : "#"}]
			},
			"uniqueItems" : true,
			"default" : "any"
		},
		
		"properties" : {
			"type" : "object",
			"additionalProperties" : {"$ref" : "#"},
			"default" : {}
		},
		
		"patternProperties" : {
			"type" : "object",
			"additionalProperties" : {"$ref" : "#"},
			"default" : {}
		},
		
		"additionalProperties" : {
			"type" : [{"$ref" : "#"}, "boolean"],
			"default" : {}
		},
		
		"items" : {
			"type" : [{"$ref" : "#"}, "array"],
			"items" : {"$ref" : "#"},
			"default" : {}
		},
		
		"additionalItems" : {
			"type" : [{"$ref" : "#"}, "boolean"],
			"default" : {}
		},
		
		"required" : {
			"type" : "boolean",
			"default" : false
		},
		
		"dependencies" : {
			"type" : "object",
			"additionalProperties" : {
				"type" : ["string", "array", {"$ref" : "#"}],
				"items" : {
					"type" : "string"
				}
			},
			"default" : {}
		},
		
		"minimum" : {
			"type" : "number"
		},
		
		"maximum" : {
			"type" : "number"
		},
		
		"exclusiveMinimum" : {
			"type" : "boolean",
			"default" : false
		},
		
		"exclusiveMaximum" : {
			"type" : "boolean",
			"default" : false
		},
		
		"minItems" : {
			"type" : "integer",
			"minimum" : 0,
			"default" : 0
		},
		
		"maxItems" : {
			"type" : "integer",
			"minimum" : 0
		},
		
		"uniqueItems" : {
			"type" : "boolean",
			"default" : false
		},
		
		"pattern" : {
			"type" : "string",
			"format" : "regex"
		},
		
		"minLength" : {
			"type" : "integer",
			"minimum" : 0,
			"default" : 0
		},
		
		"maxLength" : {
			"type" : "integer"
		},
		
		"enum" : {
			"type" : "array",
			"minItems" : 1,
			"uniqueItems" : true
		},
		
		"default" : {
			"type" : "any"
		},
		
		"title" : {
			"type" : "string"
		},
		
		"description" : {
			"type" : "string"
		},
		
		"format" : {
			"type" : "string"
		},
		
		"divisibleBy" : {
			"type" : "number",
			"minimum" : 0,
			"exclusiveMinimum" : true,
			"default" : 1
		},
		
		"disallow" : {
			"type" : ["string", "array"],
			"items" : {
				"type" : ["string", {"$ref" : "#"}]
			},
			"uniqueItems" : true
		},
		
		"extends" : {
			"type" : [{"$ref" : "#"}, "array"],
			"items" : {"$ref" : "#"},
			"default" : {}
		},
		
		"id" : {
			"type" : "string",
			"format" : "uri"
		},
		
		"$ref" : {
			"type" : "string",
			"format" : "uri"
		},
		
		"$schema" : {
			"type" : "string",
			"format" : "uri"
		}
	},
	
	"dependencies" : {
		"exclusiveMinimum" : "minimum",
		"exclusiveMaximum" : "maximum"
	},
	
	"default" : {}
}"""


def __getattr__(name):
    if name == "ErrorTree":
        warnings.warn(
            "Importing ErrorTree from jsonschema.validators is deprecated. "
            "Instead import it from jsonschema.exceptions.",
            DeprecationWarning,
            stacklevel=2,
        )
        from bsonschema.exceptions import ErrorTree
        return ErrorTree
    elif name == "validators":
        warnings.warn(
            "Accessing jsonschema.validators.validators is deprecated. "
            "Use jsonschema.validators.validator_for with a given schema.",
            DeprecationWarning,
            stacklevel=2,
        )
        return _VALIDATORS
    elif name == "meta_schemas":
        warnings.warn(
            "Accessing jsonschema.validators.meta_schemas is deprecated. "
            "Use jsonschema.validators.validator_for with a given schema.",
            DeprecationWarning,
            stacklevel=2,
        )
        return _META_SCHEMAS
    elif name == "RefResolver":
        warnings.warn(
            _RefResolver._DEPRECATION_MESSAGE,
            DeprecationWarning,
            stacklevel=2,
        )
        return _RefResolver
    raise AttributeError(f"module {__name__} has no attribute {name}")


_REMOTE_WARNING_REGISTRY = SPECIFICATIONS


def create(
    meta_schema: referencing.jsonschema.ObjectSchema,
    validators: (
        Mapping[str, bsonschema._typing.SchemaKeywordValidator]
        | Iterable[tuple[str, bsonschema._typing.SchemaKeywordValidator]]
    ) = (),
    type_checker: bsonschema._types.TypeChecker = bsonschema._types.draft3_type_checker,
    format_checker: bsonschema._format.FormatChecker = bsonschema._format.draft3_format_checker,
    id_of: bsonschema._typing.id_of = referencing.jsonschema.DRAFT3.id_of,
    applicable_validators: bsonschema._typing.ApplicableValidators = methodcaller(
        "items",
    ),
):
    # preemptively don't shadow the `Validator.format_checker` local
    format_checker_arg = format_checker

    specification = (referencing.jsonschema.DRAFT3)

    @define
    class Validator:

        VALIDATORS = dict(validators)  # noqa: RUF012
        META_SCHEMA = referencing.jsonschema.DRAFT3  # noqa: RUF012
        TYPE_CHECKER = type_checker
        FORMAT_CHECKER = format_checker_arg
        ID_OF = staticmethod(id_of)

        _APPLICABLE_VALIDATORS = applicable_validators

        schema: referencing.jsonschema.Schema = field(repr=reprlib.repr)
        _ref_resolver = field(default=None, repr=False, alias="resolver")
        format_checker: bsonschema._format.FormatChecker | None = field(default=None)
        # TODO: include new meta-schemas added at runtime
        _registry: referencing.jsonschema.SchemaRegistry = field(
            default=_REMOTE_WARNING_REGISTRY,
            kw_only=True,
            repr=False,
        )
        _resolver = field(
            alias="_resolver",
            default=None,
            kw_only=True,
            repr=False,
        )

        def __init_subclass__(cls):
            warnings.warn(
                (
                    "Subclassing validator classes is not intended to "
                    "be part of their public API. A future version "
                    "will make doing so an error, as the behavior of "
                    "subclasses isn't guaranteed to stay the same "
                    "between releases of jsonschema. Instead, prefer "
                    "composition of validators, wrapping them in an object "
                    "owned entirely by the downstream library."
                ),
                DeprecationWarning,
                stacklevel=2,
            )

            def evolve(self, **changes):
                cls = self.__class__
                schema = changes.setdefault("schema", self.schema)
                NewValidator = validator_for(schema, default=cls)

                for field in fields(cls):  # noqa: F402
                    if not field.init:
                        continue
                    attr_name = field.name
                    init_name = field.alias
                    if init_name not in changes:
                        changes[init_name] = getattr(self, attr_name)

                return NewValidator(**changes)

            cls.evolve = evolve

        def __attrs_post_init__(self):
            if self._resolver is None:
                registry = self._registry
                resource = specification.create_resource(self.schema)
                self._resolver = registry.resolver_with_root(resource)

            # REMOVEME: Legacy ref resolution state management.
            push_scope = getattr(self._ref_resolver, "push_scope", None)
            if push_scope is not None:
                id = id_of(self.schema)
                if id is not None:
                    push_scope(id)

        @classmethod
        def check_schema(cls, schema, format_checker=_UNSET):
            Validator = validator_for(referencing.jsonschema.DRAFT3, default=cls)
            if format_checker is _UNSET:
                format_checker = Validator.FORMAT_CHECKER
            validator = Validator(
                schema=referencing.jsonschema.DRAFT3,
                format_checker=format_checker,
            )
            for error in validator.iter_errors(schema):
                raise bsonschema.exceptions.SchemaError.create_from(error)

        @property
        def resolver(self):
            warnings.warn(
                (
                    f"Accessing {self.__class__.__name__}.resolver is "
                    "deprecated as of v4.18.0, in favor of the "
                    "https://github.com/python-jsonschema/referencing "
                    "library, which provides more compliant referencing "
                    "behavior as well as more flexible APIs for "
                    "customization."
                ),
                DeprecationWarning,
                stacklevel=2,
            )
            if self._ref_resolver is None:
                self._ref_resolver = _RefResolver.from_schema(
                    self.schema,
                    id_of=id_of,
                )
            return self._ref_resolver

        def evolve(self, **changes):
            schema = changes.setdefault("schema", self.schema)
            NewValidator = validator_for(schema, default=self.__class__)

            for (attr_name, init_name) in evolve_fields:
                if init_name not in changes:
                    changes[init_name] = getattr(self, attr_name)

            return NewValidator(**changes)

        def iter_errors(self, instance, _schema=None):
            if _schema is not None:
                warnings.warn(
                    (
                        "Passing a schema to Validator.iter_errors "
                        "is deprecated and will be removed in a future "
                        "release. Call validator.evolve(schema=new_schema)."
                        "iter_errors(...) instead."
                    ),
                    DeprecationWarning,
                    stacklevel=2,
                )
            else:
                _schema = self.schema

            if _schema is True:
                return
            elif _schema is False:
                yield bsonschema.exceptions.ValidationError(
                    f"False schema does not allow {instance!r}",
                    validator=None,
                    validator_value=None,
                    instance=instance,
                    schema=_schema,
                )
                return

            for k, v in applicable_validators(_schema):
                validator = self.VALIDATORS.get(k)
                if validator is None:
                    continue

                errors = validator(self, v, instance, _schema) or ()
                for error in errors:
                    # set details if not already set by the called fn
                    error._set(
                        validator=k,
                        validator_value=v,
                        instance=instance,
                        schema=_schema,
                        type_checker=self.TYPE_CHECKER,
                    )
                    if k not in {"if", "$ref"}:
                        error.schema_path.appendleft(k)
                    yield error

        def descend(
            self,
            instance,
            schema,
            path=None,
            schema_path=None,
            resolver=None,
        ):
            if schema is True:
                return
            elif schema is False:
                yield bsonschema.exceptions.ValidationError(
                    f"False schema does not allow {instance!r}",
                    validator=None,
                    validator_value=None,
                    instance=instance,
                    schema=schema,
                )
                return

            if self._ref_resolver is not None:
                evolved = self.evolve(schema=schema)
            else:
                if resolver is None:
                    resolver = self._resolver.in_subresource(
                        specification.create_resource(schema),
                    )
                evolved = self.evolve(schema=schema, _resolver=resolver)

            for k, v in applicable_validators(schema):
                validator = evolved.VALIDATORS.get(k)
                if validator is None:
                    continue

                errors = validator(evolved, v, instance, schema) or ()
                for error in errors:
                    # set details if not already set by the called fn
                    error._set(
                        validator=k,
                        validator_value=v,
                        instance=instance,
                        schema=schema,
                        type_checker=evolved.TYPE_CHECKER,
                    )
                    if k not in {"if", "$ref"}:
                        error.schema_path.appendleft(k)
                    if path is not None:
                        error.path.appendleft(path)
                    if schema_path is not None:
                        error.schema_path.appendleft(schema_path)
                    yield error

        def validate(self, *args, **kwargs):
            for error in self.iter_errors(*args, **kwargs):
                raise error

        def is_type(self, instance, type):
            try:
                return self.TYPE_CHECKER.is_type(instance, type)
            except bsonschema.exceptions.UndefinedTypeCheck:
                raise bsonschema.exceptions.UnknownType(type, instance, self.schema)


        def _validate_reference(self, ref, instance):
            if self._ref_resolver is None:
                try:
                    resolved = self._resolver.lookup(ref)
                except referencing.exceptions.Unresolvable as err:
                    raise bsonschema.exceptions._WrappedReferencingError(err)

                return self.descend(
                    instance,
                    resolved.contents,
                    resolver=resolved.resolver,
                )
            else:
                resolve = getattr(self._ref_resolver, "resolve", None)
                if resolve is None:
                    with self._ref_resolver.resolving(ref) as resolved:
                        return self.descend(instance, resolved)
                else:
                    scope, resolved = resolve(ref)
                    self._ref_resolver.push_scope(scope)

                    try:
                        return list(self.descend(instance, resolved))
                    finally:
                        self._ref_resolver.pop_scope()
        
        def is_valid(self, instance, _schema=None):
            if _schema is not None:
                warnings.warn(
                    (
                        "Passing a schema to Validator.is_valid is deprecated "
                        "and will be removed in a future release. Call "
                        "validator.evolve(schema=new_schema).is_valid(...) "
                        "instead."
                    ),
                    DeprecationWarning,
                    stacklevel=2,
                )
                self = self.evolve(schema=_schema)

            error = next(self.iter_errors(instance), None)
            return error is None

    evolve_fields = [
        (field.name, field.alias)
        for field in fields(Validator)
        if field.init
    ]

    return Validator


def extend(
    validator,
    validators=(),
    type_checker=None,
    format_checker=None,
):
    """
    Create a new validator class by extending an existing one.

    Arguments:

        validator (jsonschema.protocols.Validator):

            an existing validator class

        validators (collections.abc.Mapping):

            a mapping of new validator callables to extend with, whose
            structure is as in `create`.

            .. note::

                Any validator callables with the same name as an
                existing one will (silently) replace the old validator
                callable entirely, effectively overriding any validation
                done in the "parent" validator class.

                If you wish to instead extend the behavior of a parent's
                validator callable, delegate and call it directly in
                the new validator function by retrieving it using
                ``OldValidator.VALIDATORS["validation_keyword_name"]``.

        type_checker (jsonschema.TypeChecker):

            a type checker, used when applying the :kw:`type` keyword.

            If unprovided, the type checker of the extended
            `jsonschema.protocols.Validator` will be carried along.

        format_checker (jsonschema.FormatChecker):

            a format checker, used when applying the :kw:`format` keyword.

            If unprovided, the format checker of the extended
            `jsonschema.protocols.Validator` will be carried along.

    Returns:

        a new `jsonschema.protocols.Validator` class extending the one
        provided

    .. note:: Meta Schemas

        The new validator class will have its parent's meta schema.

        If you wish to change or extend the meta schema in the new
        validator class, modify ``META_SCHEMA`` directly on the returned
        class. Note that no implicit copying is done, so a copy should
        likely be made before modifying it, in order to not affect the
        old validator.
    """
    all_validators = dict(validator.VALIDATORS)
    all_validators.update(validators)

    if type_checker is None:
        type_checker = validator.TYPE_CHECKER
    if format_checker is None:
        format_checker = validator.FORMAT_CHECKER
    return create(
        meta_schema=referencing.jsonschema.DRAFT3,
        validators=all_validators,
        type_checker=type_checker,
        format_checker=format_checker,
        id_of=validator.ID_OF,
        applicable_validators=validator._APPLICABLE_VALIDATORS,
    )


Validator_ = create(
    meta_schema=draft3string,
    validators={
        "$ref": bsonschema._keywords.ref,
        "additionalItems": bsonschema._legacy_keywords.additionalItems,
        "additionalProperties": bsonschema._keywords.additionalProperties,
        "dependencies": bsonschema._legacy_keywords.dependencies_draft3,
        "disallow": bsonschema._legacy_keywords.disallow_draft3,
        "divisibleBy": bsonschema._keywords.multipleOf,
        "enum": bsonschema._keywords.enum,
        "extends": bsonschema._legacy_keywords.extends_draft3,
        "format": bsonschema._keywords.format,
        "items": bsonschema._legacy_keywords.items_draft3_draft4,
        "maxItems": bsonschema._keywords.maxItems,
        "maxLength": bsonschema._keywords.maxLength,
        "maximum": bsonschema._legacy_keywords.maximum_draft3_draft4,
        "minItems": bsonschema._keywords.minItems,
        "minLength": bsonschema._keywords.minLength,
        "minimum": bsonschema._legacy_keywords.minimum_draft3_draft4,
        "pattern": bsonschema._keywords.pattern,
        "patternProperties": bsonschema._keywords.patternProperties,
        "properties": bsonschema._legacy_keywords.properties_draft3,
        "type": bsonschema._legacy_keywords.type_draft3,
        "uniqueItems": bsonschema._keywords.uniqueItems,
    },
    type_checker=bsonschema._types.draft3_type_checker,
    format_checker=bsonschema._format.draft3_format_checker,
    id_of=referencing.jsonschema.DRAFT3.id_of,
    applicable_validators=bsonschema._legacy_keywords.ignore_ref_siblings,
)


class _RefResolver:
    """
    Resolve JSON References.

    Arguments:

        base_uri (str):

            The URI of the referring document

        referrer:

            The actual referring document

        store (dict):

            A mapping from URIs to documents to cache

        cache_remote (bool):

            Whether remote refs should be cached after first resolution

        handlers (dict):

            A mapping from URI schemes to functions that should be used
            to retrieve them

        urljoin_cache (:func:`functools.lru_cache`):

            A cache that will be used for caching the results of joining
            the resolution scope to subscopes.

        remote_cache (:func:`functools.lru_cache`):

            A cache that will be used for caching the results of
            resolved remote URLs.

    Attributes:

        cache_remote (bool):

            Whether remote refs should be cached after first resolution

    .. deprecated:: v4.18.0

        ``RefResolver`` has been deprecated in favor of `referencing`.
    """

    _DEPRECATION_MESSAGE = (
        "jsonschema.RefResolver is deprecated as of v4.18.0, in favor of the "
        "https://github.com/python-jsonschema/referencing library, which "
        "provides more compliant referencing behavior as well as more "
        "flexible APIs for customization. A future release will remove "
        "RefResolver. Please file a feature request (on referencing) if you "
        "are missing an API for the kind of customization you need."
    )

    def __init__(
        self,
        base_uri,
        referrer,
        store=HashTrieMap(),
        cache_remote=True,
        handlers=(),
        urljoin_cache=None,
        remote_cache=None,
    ):
        if urljoin_cache is None:
            urljoin_cache = lru_cache(1024)(urljoin)
        if remote_cache is None:
            remote_cache = lru_cache(1024)(self.resolve_from_url)

        self.referrer = referrer
        self.cache_remote = cache_remote
        self.handlers = dict(handlers)

        self._scopes_stack = [base_uri]

        self.store = bsonschema._utils.URIDict(
            (uri, each.contents) for uri, each in SPECIFICATIONS.items()
        )
        self.store.update(
            (id, each.META_SCHEMA) for id, each in _META_SCHEMAS.items()
        )
        self.store.update(store)
        self.store.update(
            (schema["$id"], schema)
            for schema in store.values()
            if isinstance(schema, Mapping) and "$id" in schema
        )
        self.store[base_uri] = referrer

        self._urljoin_cache = urljoin_cache
        self._remote_cache = remote_cache

    @classmethod
    def from_schema(  # noqa: D417
        cls,
        schema,
        id_of=referencing.jsonschema.DRAFT3.id_of,
        *args,
        **kwargs,
    ):
        """
        Construct a resolver from a JSON schema object.

        Arguments:

            schema:

                the referring schema

        Returns:

            `_RefResolver`
        """
        return cls(base_uri=id_of(schema) or "", referrer=schema, *args, **kwargs)  # noqa: B026, E501

    def push_scope(self, scope):
        """
        Enter a given sub-scope.

        Treats further dereferences as being performed underneath the
        given scope.
        """
        self._scopes_stack.append(
            self._urljoin_cache(self.resolution_scope, scope),
        )

    def pop_scope(self):
        """
        Exit the most recent entered scope.

        Treats further dereferences as being performed underneath the
        original scope.

        Don't call this method more times than `push_scope` has been
        called.
        """
        try:
            self._scopes_stack.pop()
        except IndexError:
            raise bsonschema.exceptions._RefResolutionError(
                "Failed to pop the scope from an empty stack. "
                "`pop_scope()` should only be called once for every "
                "`push_scope()`",
            )

    @property
    def resolution_scope(self):
        """
        Retrieve the current resolution scope.
        """
        return self._scopes_stack[-1]

    @property
    def base_uri(self):
        """
        Retrieve the current base URI, not including any fragment.
        """
        uri, _ = urldefrag(self.resolution_scope)
        return uri

    @contextlib.contextmanager
    def in_scope(self, scope):
        """
        Temporarily enter the given scope for the duration of the context.

        .. deprecated:: v4.0.0
        """
        warnings.warn(
            "jsonschema.RefResolver.in_scope is deprecated and will be "
            "removed in a future release.",
            DeprecationWarning,
            stacklevel=3,
        )
        self.push_scope(scope)
        try:
            yield
        finally:
            self.pop_scope()

    @contextlib.contextmanager
    def resolving(self, ref):
        """
        Resolve the given ``ref`` and enter its resolution scope.

        Exits the scope on exit of this context manager.

        Arguments:

            ref (str):

                The reference to resolve
        """
        url, resolved = self.resolve(ref)
        self.push_scope(url)
        try:
            yield resolved
        finally:
            self.pop_scope()

    def _find_in_referrer(self, key):
        return self._get_subschemas_cache()[key]

    @lru_cache  # noqa: B019
    def _get_subschemas_cache(self):
        cache = {key: [] for key in _SUBSCHEMAS_KEYWORDS}
        for keyword, subschema in _search_schema(
            self.referrer, _match_subschema_keywords,
        ):
            cache[keyword].append(subschema)
        return cache

    @lru_cache  # noqa: B019
    def _find_in_subschemas(self, url):
        subschemas = self._get_subschemas_cache()["$id"]
        if not subschemas:
            return None
        uri, fragment = urldefrag(url)
        for subschema in subschemas:
            id = subschema["$id"]
            if not isinstance(id, str):
                continue
            target_uri = self._urljoin_cache(self.resolution_scope, id)
            if target_uri.rstrip("/") == uri.rstrip("/"):
                if fragment:
                    subschema = self.resolve_fragment(subschema, fragment)
                self.store[url] = subschema
                return url, subschema
        return None

    def resolve(self, ref):
        """
        Resolve the given reference.
        """
        url = self._urljoin_cache(self.resolution_scope, ref).rstrip("/")

        match = self._find_in_subschemas(url)
        if match is not None:
            return match

        return url, self._remote_cache(url)

    def resolve_from_url(self, url):
        """
        Resolve the given URL.
        """
        url, fragment = urldefrag(url)
        if not url:
            url = self.base_uri

        try:
            document = self.store[url]
        except KeyError:
            try:
                document = self.resolve_remote(url)
            except Exception as exc:
                raise bsonschema.exceptions._RefResolutionError(exc)

        return self.resolve_fragment(document, fragment)

    def resolve_fragment(self, document, fragment):
        """
        Resolve a ``fragment`` within the referenced ``document``.

        Arguments:

            document:

                The referent document

            fragment (str):

                a URI fragment to resolve within it
        """
        fragment = fragment.lstrip("/")

        if not fragment:
            return document

        if document is self.referrer:
            find = self._find_in_referrer
        else:

            def find(key):
                yield from _search_schema(document, _match_keyword(key))

        for keyword in ["$anchor", "$dynamicAnchor"]:
            for subschema in find(keyword):
                if fragment == subschema[keyword]:
                    return subschema
        for keyword in ["id", "$id"]:
            for subschema in find(keyword):
                if "#" + fragment == subschema[keyword]:
                    return subschema

        # Resolve via path
        parts = unquote(fragment).split("/") if fragment else []
        for part in parts:
            part = part.replace("~1", "/").replace("~0", "~")

            if isinstance(document, Sequence):
                try:  # noqa: SIM105
                    part = int(part)
                except ValueError:
                    pass
            try:
                document = document[part]
            except (TypeError, LookupError):
                raise bsonschema.exceptions._RefResolutionError(
                    f"Unresolvable JSON pointer: {fragment!r}",
                )

        return document

    def resolve_remote(self, uri):
        """
        Resolve a remote ``uri``.

        If called directly, does not check the store first, but after
        retrieving the document at the specified URI it will be saved in
        the store if :attr:`cache_remote` is True.

        .. note::

            If the requests_ library is present, ``jsonschema`` will use it to
            request the remote ``uri``, so that the correct encoding is
            detected and used.

            If it isn't, or if the scheme of the ``uri`` is not ``http`` or
            ``https``, UTF-8 is assumed.

        Arguments:

            uri (str):

                The URI to resolve

        Returns:

            The retrieved document

        .. _requests: https://pypi.org/project/requests/
        """
        try:
            import requests
        except ImportError:
            requests = None

        scheme = urlsplit(uri).scheme

        if scheme in self.handlers:
            result = self.handlers[scheme](uri)
        elif scheme in ["http", "https"] and requests:
            # Requests has support for detecting the correct encoding of
            # json over http
            result = requests.get(uri).json()
        else:
            # Otherwise, pass off to urllib and assume utf-8
            with urlopen(uri) as url:
                result = json.loads(url.read().decode("utf-8"))

        if self.cache_remote:
            self.store[uri] = result
        return result


_SUBSCHEMAS_KEYWORDS = ("$id", "id", "$anchor", "$dynamicAnchor")


def _match_keyword(keyword):

    def matcher(value):
        if keyword in value:
            yield value

    return matcher


def _match_subschema_keywords(value):
    for keyword in _SUBSCHEMAS_KEYWORDS:
        if keyword in value:
            yield keyword, value


def _search_schema(schema, matcher):
    """Breadth-first search routine."""
    values = deque([schema])
    while values:
        value = values.pop()
        if not isinstance(value, dict):
            continue
        yield from matcher(value)
        values.extendleft(value.values())


def validate(instance, schema, cls=None, *args, **kwargs):  # noqa: D417
    cls.check_schema(schema)
    validator = cls(schema, *args, **kwargs)
    error = bsonschema.exceptions.best_match(validator.iter_errors(instance))
    if error is not None:
        raise error


def validator_for(schema, default=_UNSET):
    return Validator_

