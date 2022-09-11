
import argparse
import boto3
import multiprocessing
import os
import pathlib
import pprint
import re
import shutil
import socket
import subprocess
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--smb_username", help="Username for SMB mounting")
parser.add_argument("--smb_password", help="Password for SMB mounting")
parser.add_argument("--smb_share", help="Share for SMB mounting")

parser.add_argument("--p4_sync", help=f"Sync via p4 to this changelist")
parser.add_argument("--p4_patch", help="Comma-separated list of changelists to unshelve after sync (optional, prevents snapshot)")

parser.add_argument("script_args", help="Options to be fed to the script verbatim", nargs=argparse.REMAINDER)

required = parser.add_argument_group('required arguments')
required.add_argument("--init-drive", help=f"Whether to initialize the drive from nothing", action=argparse.BooleanOptionalAction, required=True)
required.add_argument("--workdir", help=f"Working directory to use", required=True)
required.add_argument("--output", help=f"Output directory to use within workdir", required=True)
required.add_argument("--output_compress", help=f"Whether to compress the output file", action="store_true")
required.add_argument("--output_s3", help=f"Path to upload the file to on s3 (requires output_compress, requires aws config)")
required.add_argument("--script", help=f"Target script name to run", required=True)

p4info = parser.add_argument_group('p4 configuration')
p4info.add_argument("--p4_username", help="Username for p4", required=True)
p4info.add_argument("--p4_password", help="Password for p4", required=True)
p4info.add_argument("--p4_server", help="Server name and port for p4", required=True)
p4info.add_argument("--p4_fingerprint", help="Fingerprint for p4", required=True)
p4info.add_argument("--p4_workspace", help="Workspace name for p4", required=True)

awsinfo = parser.add_argument_group('aws configuration')
awsinfo.add_argument("--aws_access_key_id")
awsinfo.add_argument("--aws_secret_access_key")

args = parser.parse_args()

if args.init_drive:
    print("init_drive specified but not yet supported")
    raise Exception(1)

# Check to see if we have Internet and DNS access - it's apparently common for Docker's networking to throw a cog and I'd rather get a clean error message here
try:
	s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	s.settimeout(3)
	s.connect(("8.8.8.8", 53)) # using Google's DNS server to check connectivity
	s.close()
except socket.error as ex:
	print(ex)
	s.close()
	raise Exception("No Internet access; is your Docker network configured properly?")
try:
    socket.getaddrinfo("google.com", 80)
except socket.error as ex:
    raise Exception("No DNS access; is your Docker network configured properly?")

if args.smb_username:
    print(f"Mounting SMB share {args.smb_share} in {args.workdir} . . .")
    subprocess.check_call([
            'net', 'use',
            args.workdir[0:2],
            args.smb_share,
            f'/user:{args.smb_username}', args.smb_password,
        ])

# chop off the -- prefix if we have one
if args.script_args is not None and len(args.script_args) >= 1 and args.script_args[0] == "--":
    args.script_args = args.script_args[1:]

os.chdir(args.workdir)

# We'll be modifying this env as we go
env = os.environ.copy()

# Right now this should always be the case
if args.p4_username is not None:
    import P4
    
    print(f"Connecting to p4 server {args.p4_server} as {args.p4_username} with {args.p4_workspace}")
    
    # Put together the p4 workspace
    p4 = P4.P4()
    p4.user = args.p4_username
    p4.password = args.p4_password
    p4.port = args.p4_server
    p4.client = args.p4_workspace
    
    pprint.pprint(p4)
    
    p4.connect()
    
    # Trust up; we got the ID from upstream
    p4.run_trust("-i", args.p4_fingerprint)
    
    # Now we can actually login (normally this is implicit, but trust failures break that pathway)
    p4.run_login()

    # This is a little gnarly. `args.workdir` tells us where we should expect our data to show up.
    # Unfortunately, p4 is very opinionated about what directory it's willing to access.
    # What we actually want to do is map our working directory onto whatever p4 is expecting
    # This can be done with a combination of subst and symlinks, but it's gnarly no matter what we do
    # I expect we'll be dealing with new issues here for a while.
    client = p4.run_client("-o", args.p4_workspace)
    
    # Right now, we assume the client root doesn't already exist as a directory.
    # If you've put your Perforce repo in `c:\windows` and remapped Windows to D:,
    # all so the Docker container gets confused when the p4 repo conflicts with its system directory, then, uh . . .
    # . . . don't do that, I guess?
    # I'm not sure what fix there could be for this.
    clientrootdir = client[0]["Root"]
    if clientrootdir != args.workdir:
        print(f"Client root currently located in {args.workdir}, should be {clientrootdir}; generating symlink")
        if os.path.isdir(clientrootdir) or os.path.isfile(clientrootdir):
            raise Exception("Directory {clientrootdir} already exists in Docker! Not sure how to handle this, aborting.")

        drive = clientrootdir[0:2]
        if not os.path.isdir(drive):
            print(f"  Creating fake drive {drive}")
            stubdir = "c:\\mnt"
            os.mkdir(stubdir)
            subprocess.check_call([
                'subst', drive[0:2], stubdir,
            ])
        
        # Make directories up to right before our target
        clientrootdirparent = str(pathlib.Path(clientrootdir).parent)
        if not os.path.isdir(clientrootdirparent):
            os.makedirs(clientrootdirparent)

        print(f"  Making symlink")
        # not utils.run because we need `shell = True`
        subprocess.check_call([
                'mklink', '/d',
                clientrootdir,
                args.workdir,
            ], shell = True)

        print("  Directory masquerade successful!")
    
    # And now we just travel to the workdir and everything is fine, yay
    args.workdir = clientrootdir
    os.chdir(args.workdir)

    # Set up our env variables for child processes; other p4-users will expect these
    env["P4USER"] = args.p4_username
    env["P4PASSWORD"] = args.p4_password
    env["P4PORT"] = args.p4_server
    env["P4CLIENT"] = args.p4_workspace
    
    # Fake this so we can use people's existing clients without requiring them to mess with their host values.
    # (p4, thank you for having this feature. sincerely, me)
    # You might ask why we're setting it in three different ways. It's because *something* always breaks unless I do.
    # (I am less enthused about this feature.)
    # Not necessary if it doesn't have a Host field, of course!
    if "Host" in client[0]:
        p4.set_env("P4HOST", client[0]["Host"])
        p4.host = client[0]["Host"]
        
        # This propagates to children (like the ue4 build script) so they work properly.
        env["P4HOST"] = client[0]["Host"]

if args.p4_sync is not None:
    # Do the big sync! (yes this takes forever)
    # Sometimes we have minor network hiccups. We "solve" this by retrying up to ten times.
    # Yes, I know this is ghastly.
    maxTries = 10
    for attempt in range(maxTries):
        print(f"Syncing (try {attempt + 1}) . . .")
        success = False
        try:
            p4.exception_level = 1  # "up-to-date" is a warning for some godforsaken reason
            p4.run_sync(f"@{args.p4_sync}")
            success = True
        except P4.P4Exception as e:
            print(e)
        p4.exception_level = 2  # back to warning us about everything, which is generally useful
        
        if success:
            break
        
        if attempt == maxTries - 1:
            print("Failed! Aborting :(")
            sys.exit(1)
        
        print("Waiting ten seconds . . .")
        time.sleep(10)
        p4.connect()

# Clean our output directory and output file, just in case
archiveoutput = args.output + ".7z"
if os.path.isdir(args.output):
    shutil.rmtree(args.output)
if os.path.isfile(archiveoutput):
    os.remove(archiveoutput)
    
# Patch up! We do this as late as possible so there's a small surface for us needing to revert the patch
p4change = None
if args.p4_patch is not None:
    p4change = p4.save_change({'Change': 'new', 'Description': 'arclight build patches'})[0]
    # extract the actual number out (come on, p4)
    p4change = re.search(r'Change (\d+) created.', p4change).group(1)
    print(f"Patching into changelist {p4change}")
    
    for patch in args.p4_patch.split(","):
        p4.run_unshelve("-s", patch, "-c", p4change)

try:
    # SORRY, CAN'T MAKE THIS PART PUBLIC
    # You'll need to fix this, I can't distribute `utils.run`. I'm pretty sure the easiest way to do this is to just change this to a `subprocess` call.
    # Please pull request it once you do, thanks!
    
    # Build the thing (with appropriate data)
    utils.run([
            'python',
            '-u', # unbuffered so we actually get realtime output
            f'arclight/script/{args.script}.py',
            '--output', args.output,
        ] + args.script_args,
        cwd = args.workdir,
        env = env)
finally:
    if p4change is not None:
        # Revert our changes; this makes it a lot easier to keep iterating on a single image, if we want to
        print(f"Cleaning up p4 patch changelist {p4change}")
        p4.run_revert("-w", "-c", p4change, "...")
        p4.run_change("-d", p4change)

# Compress if requested (here so we can keep 7z in the Docker image)
if args.output_compress:
    print(f"Compressing to {archiveoutput}")
    subprocess.check_call([
        '7z', 'a',
        '-bb1',      # detailed logging
        '-mx1',      # low compression
        # We'd like to do .zip compression, but 7zip does not do parallel compression into .zip files very well.
        archiveoutput,
        args.output,
    ])
    
    # And now wipe, because we know where this is and can do it easily
    shutil.rmtree(args.output)

if args.output_s3 is not None:
    if not args.output_compress:
        raise Exception("Attempting to S3 without zipping!")
    
    if args.aws_access_key_id is None or args.aws_secret_access_key is None:
        raise Exception("Missing AWS keys!")
    
    s3 = boto3.client('s3', aws_access_key_id = args.aws_access_key_id, aws_secret_access_key = args.aws_secret_access_key)
    with open(archiveoutput, "rb") as f:
        s3.upload_fileobj(f, "arclight", args.output_s3)
    
    # final cleanup
    os.remove(archiveoutput)
