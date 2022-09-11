
# Build step for Arclight.
# This builds and cooks the game and server for the chosen target platform and with the target config.

import argparse
import json
import os
import pathlib
import psutil
import shutil
import subprocess

parser = argparse.ArgumentParser()

mode = parser.add_mutually_exclusive_group(required = True)
mode.add_argument("--validate", help="Validate settings and return configuration", action="store_true")
mode.add_argument("--output", help="Output target")

parser.add_argument("--target_platform", help="Target platform (Win64, PS4, PS5, XboxOneGDK, XSX)", required = True)
parser.add_argument("--client_config", help="Client config (Development, DebugGame, Test, Shipping)", required = True)
parser.add_argument("--sentry_auth_token", help="Sentry auth token")
args = parser.parse_args()

if args.validate:
    print(json.dumps({
        "image": "project_build",
    }))
    exit()

root_dir = os.getcwd()

# DO THE ACTUAL BUILD HERE

# SORRY, CAN'T MAKE THIS PART PUBLIC
