import bson 
import json
import bsonschema

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

    validator = bsonschema.Validator(json.load(open("./tests/mdb_schema.json", "r")))        

    # Test that a valid BSON will pass.
    try:
        bson_will_pass = read_bson_from_disk("./tests/test_bson_will_pass_validation.bson")
        validator.validate(bson_will_pass)
        print('Valid BSON file passed validation.')        
    except Exception as e:
        print(str(e))

    # Test that an invalid BSON will throw an exception.
    try:
        bson_will_fail = read_bson_from_disk("./tests/test_bson_will_fail_validation.bson")
        validator.validate(bson_will_fail)  
        print("ERROR! Invalid BSON did not throw an exception during validation.")
        exit(-1) 
    except Exception as e:
        print('Invalid BSON successfully failed validation.')    
