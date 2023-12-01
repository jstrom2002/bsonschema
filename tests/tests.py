import os
import sys

sys.path.append(os.path.realpath(os.curdir))
sys.path.append(os.path.dirname(os.path.realpath(os.curdir)))

import bsonschema
import bson 
import json

# Parse BSON file to dict.
# returns: a decoded 'dict' parsed from the file.
def read_bson_from_disk(filepath: str) -> dict:
    contents = open(filepath, "rb").read()

    # Handle null case
    if len(contents) == 0:
        return dict()

    # Determine if the document is binary encoded. This is possible because ASCII encoded
    # docs will typically not have null chars (or any chars < 10 on the ASCII table).
    for c in contents:
        if c < 10:
            return bson.BSON.decode(
                contents, bson.CodecOptions(unicode_decode_error_handler="ignore")
            )

    # Else, this doc is ASCII-encoded (an older format), and should be
    # parsed using 'json.loads()' and converted to BSON.
    return bson.BSON.decode(
        bson.BSON.encode(json.loads(contents)),
        bson.CodecOptions(unicode_decode_error_handler="ignore"),
    )

if __name__ == "__main__":
    try:
        validator = bsonschema.Draft3Validator(json.load(open("./mdb_schema.json", "r")))
        inst = read_bson_from_disk("./test_bson_for_validation.bson")
        validator.validate(inst)
    except Exception as e:
        print(str(e))
