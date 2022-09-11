
# Build step for Arclight.
# This writes a given message to a file.
# This is fast and useful for debugging.

import argparse
import json
import os

parser = argparse.ArgumentParser()

mode = parser.add_mutually_exclusive_group(required = True)
mode.add_argument("--validate", help="Validate settings and return configuration", action="store_true")
mode.add_argument("--output", help="Output target")

parser.add_argument("--message", help="Message to print", required=True)

args = parser.parse_args()

if args.validate:
    print(json.dumps({
        "image": "project_build",
    }))
    exit()

os.mkdir(args.output)
text_file = open("results.txt", "w")
n = text_file.write(args.message)
text_file.close()
