import re

from referencing.jsonschema import lookup_recursive_ref

import bsonschema._utils
from bsonschema.exceptions import ValidationError


def ignore_ref_siblings(schema):
    """
    Ignore siblings of ``$ref`` if it is present.

    Otherwise, return all keywords.

    Suitable for use with `create`'s ``applicable_validators`` argument.
    """
    ref = schema.get("$ref")
    if ref is not None:
        return [("$ref", ref)]
    else:
        return schema.items()


def dependencies_draft3(validator, dependencies, instance, schema):
    if not validator.is_type(instance, "object"):
        return

    for property, dependency in dependencies.items():
        if property not in instance:
            continue

        if validator.is_type(dependency, "object"):
            yield from validator.descend(
                instance, dependency, schema_path=property,
            )
        elif validator.is_type(dependency, "string"):
            if dependency not in instance:
                message = f"{dependency!r} is a dependency of {property!r}"
                yield ValidationError(message)
        else:
            for each in dependency:
                if each not in instance:
                    message = f"{each!r} is a dependency of {property!r}"
                    yield ValidationError(message)



def disallow_draft3(validator, disallow, instance, schema):
    for disallowed in bsonschema._utils.ensure_list(disallow):
        if validator.evolve(schema={"type": [disallowed]}).is_valid(instance):
            message = f"{disallowed!r} is disallowed for {instance!r}"
            yield ValidationError(message)


def extends_draft3(validator, extends, instance, schema):
    if validator.is_type(extends, "object"):
        yield from validator.descend(instance, extends)
        return
    for index, subschema in enumerate(extends):
        yield from validator.descend(instance, subschema, schema_path=index)


def items_draft3_draft4(validator, items, instance, schema):
    if not validator.is_type(instance, "array"):
        return

    if validator.is_type(items, "object"):
        for index, item in enumerate(instance):
            yield from validator.descend(item, items, path=index)
    else:
        for (index, item), subschema in zip(enumerate(instance), items):
            yield from validator.descend(
                item, subschema, path=index, schema_path=index,
            )


def additionalItems(validator, aI, instance, schema):
    if (
        not validator.is_type(instance, "array")
        or validator.is_type(schema.get("items", {}), "object")
    ):
        return

    len_items = len(schema.get("items", []))
    if validator.is_type(aI, "object"):
        for index, item in enumerate(instance[len_items:], start=len_items):
            yield from validator.descend(item, aI, path=index)
    elif not aI and len(instance) > len(schema.get("items", [])):
        error = "Additional items are not allowed (%s %s unexpected)"
        yield ValidationError(
            error % bsonschema._utils.extras_msg(instance[len(schema.get("items", [])):]),
        )


def minimum_draft3_draft4(validator, minimum, instance, schema):
    if not validator.is_type(instance, "number"):
        return

    if schema.get("exclusiveMinimum", False):
        failed = instance <= minimum
        cmp = "less than or equal to"
    else:
        failed = instance < minimum
        cmp = "less than"

    if failed:
        message = f"{instance!r} is {cmp} the minimum of {minimum!r}"
        yield ValidationError(message)


def maximum_draft3_draft4(validator, maximum, instance, schema):
    if not validator.is_type(instance, "number"):
        return

    if schema.get("exclusiveMaximum", False):
        failed = instance >= maximum
        cmp = "greater than or equal to"
    else:
        failed = instance > maximum
        cmp = "greater than"

    if failed:
        message = f"{instance!r} is {cmp} the maximum of {maximum!r}"
        yield ValidationError(message)


def properties_draft3(validator, properties, instance, schema):
    if not validator.is_type(instance, "object"):
        return

    for property, subschema in properties.items():
        if property in instance:
            yield from validator.descend(
                instance[property],
                subschema,
                path=property,
                schema_path=property,
            )
        elif subschema.get("required", False):
            error = ValidationError(f"{property!r} is a required property")
            error._set(
                validator="required",
                validator_value=subschema["required"],
                instance=instance,
                schema=schema,
            )
            error.path.appendleft(property)
            error.schema_path.extend([property, "required"])
            yield error


def type_draft3(validator, types, instance, schema):
    types = bsonschema._utils.ensure_list(types)

    all_errors = []
    for index, type in enumerate(types):
        if validator.is_type(type, "object"):
            errors = list(validator.descend(instance, type, schema_path=index))
            if not errors:
                return
            all_errors.extend(errors)
        else:
            if validator.is_type(instance, type):
                return
    else:
        reprs = []
        for type in types:
            try:
                reprs.append(repr(type["name"]))
            except Exception:
                reprs.append(repr(type))
        yield ValidationError(
            f"{instance!r} is not of type {', '.join(reprs)}",
            context=all_errors,
        )

def recursiveRef(validator, recursiveRef, instance, schema):
    resolved = lookup_recursive_ref(validator._resolver)
    yield from validator.descend(
        instance,
        resolved.contents,
        resolver=resolved.resolver,
    )


def find_evaluated_item_indexes_by_schema(validator, instance, schema):
    """
    Get all indexes of items that get evaluated under the current schema.

    Covers all keywords related to unevaluatedItems: items, prefixItems, if,
    then, else, contains, unevaluatedItems, allOf, oneOf, anyOf
    """
    if validator.is_type(schema, "boolean"):
        return []
    evaluated_indexes = []

    ref = schema.get("$ref")
    if ref is not None:
        resolved = validator._resolver.lookup(ref)
        evaluated_indexes.extend(
            find_evaluated_item_indexes_by_schema(
                validator.evolve(
                    schema=resolved.contents,
                    _resolver=resolved.resolver,
                ),
                instance,
                resolved.contents,
            ),
        )

    if "$recursiveRef" in schema:
        resolved = lookup_recursive_ref(validator._resolver)
        evaluated_indexes.extend(
            find_evaluated_item_indexes_by_schema(
                validator.evolve(
                    schema=resolved.contents,
                    _resolver=resolved.resolver,
                ),
                instance,
                resolved.contents,
            ),
        )

    if "items" in schema:
        if "additionalItems" in schema:
            return list(range(0, len(instance)))

        if validator.is_type(schema["items"], "object"):
            return list(range(0, len(instance)))
        evaluated_indexes += list(range(0, len(schema["items"])))

    if "if" in schema:
        if validator.evolve(schema=schema["if"]).is_valid(instance):
            evaluated_indexes += find_evaluated_item_indexes_by_schema(
                validator, instance, schema["if"],
            )
            if "then" in schema:
                evaluated_indexes += find_evaluated_item_indexes_by_schema(
                    validator, instance, schema["then"],
                )
        else:
            if "else" in schema:
                evaluated_indexes += find_evaluated_item_indexes_by_schema(
                    validator, instance, schema["else"],
                )

    for keyword in ["contains", "unevaluatedItems"]:
        if keyword in schema:
            for k, v in enumerate(instance):
                if validator.evolve(schema=schema[keyword]).is_valid(v):
                    evaluated_indexes.append(k)

    for keyword in ["allOf", "oneOf", "anyOf"]:
        if keyword in schema:
            for subschema in schema[keyword]:
                errs = next(validator.descend(instance, subschema), None)
                if errs is None:
                    evaluated_indexes += find_evaluated_item_indexes_by_schema(
                        validator, instance, subschema,
                    )

    return evaluated_indexes


def unevaluatedItems_draft2019(validator, unevaluatedItems, instance, schema):
    if not validator.is_type(instance, "array"):
        return
    evaluated_item_indexes = find_evaluated_item_indexes_by_schema(
        validator, instance, schema,
    )
    unevaluated_items = [
        item for index, item in enumerate(instance)
        if index not in evaluated_item_indexes
    ]
    if unevaluated_items:
        error = "Unevaluated items are not allowed (%s %s unexpected)"
        yield ValidationError(error % bsonschema._utils.extras_msg(unevaluated_items))


def find_evaluated_property_keys_by_schema(validator, instance, schema):
    if validator.is_type(schema, "boolean"):
        return []
    evaluated_keys = []

    ref = schema.get("$ref")
    if ref is not None:
        resolved = validator._resolver.lookup(ref)
        evaluated_keys.extend(
            find_evaluated_property_keys_by_schema(
                validator.evolve(
                    schema=resolved.contents,
                    _resolver=resolved.resolver,
                ),
                instance,
                resolved.contents,
            ),
        )

    if "$recursiveRef" in schema:
        resolved = lookup_recursive_ref(validator._resolver)
        evaluated_keys.extend(
            find_evaluated_property_keys_by_schema(
                validator.evolve(
                    schema=resolved.contents,
                    _resolver=resolved.resolver,
                ),
                instance,
                resolved.contents,
            ),
        )

    for keyword in [
        "properties", "additionalProperties", "unevaluatedProperties",
    ]:
        if keyword in schema:
            schema_value = schema[keyword]
            if validator.is_type(schema_value, "boolean") and schema_value:
                evaluated_keys += instance.keys()

            elif validator.is_type(schema_value, "object"):
                for property in schema_value:
                    if property in instance:
                        evaluated_keys.append(property)

    if "patternProperties" in schema:
        for property in instance:
            for pattern in schema["patternProperties"]:
                if re.search(pattern, property):
                    evaluated_keys.append(property)

    if "dependentSchemas" in schema:
        for property, subschema in schema["dependentSchemas"].items():
            if property not in instance:
                continue
            evaluated_keys += find_evaluated_property_keys_by_schema(
                validator, instance, subschema,
            )

    for keyword in ["allOf", "oneOf", "anyOf"]:
        if keyword in schema:
            for subschema in schema[keyword]:
                errs = next(validator.descend(instance, subschema), None)
                if errs is None:
                    evaluated_keys += find_evaluated_property_keys_by_schema(
                        validator, instance, subschema,
                    )

    if "if" in schema:
        if validator.evolve(schema=schema["if"]).is_valid(instance):
            evaluated_keys += find_evaluated_property_keys_by_schema(
                validator, instance, schema["if"],
            )
            if "then" in schema:
                evaluated_keys += find_evaluated_property_keys_by_schema(
                    validator, instance, schema["then"],
                )
        else:
            if "else" in schema:
                evaluated_keys += find_evaluated_property_keys_by_schema(
                    validator, instance, schema["else"],
                )

    return evaluated_keys
