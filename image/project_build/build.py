
import argparse
import os
import pathlib
import subprocess

parser = argparse.ArgumentParser()
required = parser.add_argument_group('required arguments')
required.add_argument("--name", help=f"Container name used for the final image", required=True)
required.add_argument("--isolation", help=f"Docker build isolation mode (either `process` or `hyperv`)", required=True)
required.add_argument("--baseimage", help=f"Windows image to use", required=True)
required.add_argument("--dllsrcimage", help=f"Windows image to copy files from", required=True)
args = parser.parse_args()

# Get paths
rootdir = pathlib.Path(__file__).parent
ue4dockerdir = rootdir.joinpath('ue4-docker').resolve()
    
# First we need to get ue4-docker up and running and generating output
# Install pipenv
subprocess.check_call(['pip', 'install', 'pipenv'])

# Make the environment
subprocess.check_call(['pipenv', 'run', 'pip', 'install', '.'], cwd = ue4dockerdir)

# Actually kick off the ue4-docker build-prerequisites Dockerfile generation
ue4dockerbuilddir = rootdir.joinpath('ue4-docker-build').resolve()
subprocess.check_call([
        'pipenv', 'run', 'ue4-docker', 'build',
        '-layout', ue4dockerbuilddir,
        '--target', 'build-prerequisites',
        '-isolation', args.isolation,
        '-basetag', 'abba', # this is basically ignored, we have to specify it when doing the docker build manually
    ],
    cwd = ue4dockerdir)

# Build the setup Dockerfile, which contains things that need to happen before build-prerequisites
subprocess.check_call([
        'docker', 'build',
        '-t', 'airship/setup',
        'setup',
        '--build-arg', f'BASEIMAGE=mcr.microsoft.com/windows/servercore:{args.baseimage}',
        f'--isolation={args.isolation}',
    ],
    cwd = rootdir)

# Build the build-prerequisites Dockerfile
print("Building build-prerequisites Dockerfile, this takes forever and there's no easy way to get realtime output in this script, sorry . . .")
# see https://stackoverflow.com/questions/11516258/what-is-the-equivalent-of-unbuffer-program-on-windows/44531837#44531837
subprocess.check_call([
        'docker', 'build',
        '-t', 'airship/ue4-build-prerequisites',
        'ue4-docker-build/ue4-build-prerequisites',
        '--build-arg', f'BASEIMAGE=airship/setup',
        '--build-arg', f'DLLSRCIMAGE=mcr.microsoft.com/windows:{args.dllsrcimage}',
        '--build-arg', 'VISUAL_STUDIO_BUILD_NUMBER=16', # VS 2019; 2017 or earlier has a difficult-to-work-around bug in hyperv-isolated builds with debug info (https://github.com/docker/for-win/issues/829)
        f'--isolation={args.isolation}',
    ],
    cwd = rootdir)

# Build our arclight environment
subprocess.check_call([
        'docker', 'build',
        '-t', args.name,
        'environment',
        f'--isolation={args.isolation}',
    ],
    cwd = rootdir)
