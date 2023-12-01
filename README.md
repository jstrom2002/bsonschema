# bsonschema

Simple bson validator written similarly to [jsonschema](https://github.com/python-jsonschema/jsonschema) but for BSON types.

### Example of Use

```
import bsonschema;

inst = read_bson_from_disk("./tests/test_bson_for_validation.bson")
valid = bsonschema.Validator(json.load(open("./tests/mdb_schema.json", "r")))
valid.validate(inst)
print('Test file passed validation.')
```
