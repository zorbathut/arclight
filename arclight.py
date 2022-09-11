
import atexit
import argparse
import boto3
import datetime
import dateutil
import docker
import math
import multiprocessing
import json
import os
import pathlib
import platform
import pprint
import psutil
import re
import subprocess
import sys
import time

import util.aws
from util.prof import prof
from util.prof import Context
from util.simple_utc import simple_utc

@prof
def main() -> None:
    parser = argparse.ArgumentParser(
        prog = "Arclight",
        epilog = "Read ARCHITECTURE.md for more info, including sample executions!")
    mode = parser.add_mutually_exclusive_group(required = True)
    mode.add_argument("--inplace", help="Build locally with an inplace directory", action="store_true")
    mode.add_argument("--managed", help="Build locally with a fresh repository from p4", action="store_true")
    mode.add_argument("--aws", help="Build on AWS", action="store_true")

    p4info = parser.add_argument_group('p4 configuration')
    p4info.add_argument("--p4_username", help="Username for p4", required = True)
    p4info.add_argument("--p4_password", help="Password for p4", required = True)
    p4info.add_argument("--p4_server", help="Server name and port for p4", required = True)
    p4info.add_argument("--p4_workspace", help="Workspace name for p4 (required for `inplace`)")
    p4info.add_argument("--p4_stream", help="Stream name for p4 (required for `managed, aws`)")
    p4info.add_argument("--p4_sync", help="Changelist number to sync to, `head` is valid (required for `managed`, `aws`, valid for `inplace`)")
    p4info.add_argument("--p4_sync_from", help="Changelist number to sync from (required for `managed`)")
    p4info.add_argument("--p4_patch", help="Comma-separated list of changelists to unshelve after sync (optional, `managed, aws` only, prevents snapshot)")
    p4info.add_argument("--p4_patch_allow_preserve_DO_NOT_USE", action="store_true") # no stop

    smb = parser.add_argument_group('smb mount configuration (required for non-AWS modes)')
    smb.add_argument("--smb_username", help="Username for SMB mounting")
    smb.add_argument("--smb_password", help="Password for SMB mounting")

    aws = parser.add_argument_group('aws configuration')
    aws.add_argument("--aws_allow_new_ami", help="Allow creating a new AMI", action="store_true")

    parser.add_argument("--working", help="Working directory to use (required for `managed`)")
    parser.add_argument("--memory", help="Maximum memory to use (in gigabytes)", type=int)
    parser.add_argument("script", help="Name of the script to run")
    parser.add_argument("script_args", help="Options to be fed to the script verbatim", nargs=argparse.REMAINDER)

    args = parser.parse_args()
    
    # Verify that we have a functional environment
    if platform.system() != "Windows":
        raise Exception("Arclight currently requires Windows. Sorry. Pull requests welcome!")
    
    dockerenv = docker.from_env()
    if dockerenv.info()["OSType"].lower() != "windows":
        print("Docker isn't set to Windows mode. Attempting to switch it via command line.")
        
        # This returns an error code for some godforsaken reason.
        subprocess.call([
            "c:\Program Files\Docker\Docker\DockerCli.exe",
            "-SwitchDaemon"])
        
        print("Pausing to give it time to sort itself out.")
        # This actually seems to take under a second, but I'm adding an extra delay to be safe and because this should happen only once.
        time.sleep(5)

        # Recreate or we get a broken pipe.
        dockerenv = docker.from_env()
        if dockerenv.info()["OSType"].lower() != "windows":
            raise Exception("Docker isn't set to Windows mode and something went wrong with switching it over. Try doing it manually; perhaps your Docker installation is incomplete?")
    
    if "BUILD-NUMBER" in os.environ:
        buildid = f"jenkins-{os.environ['BUILD-NUMBER']}"
    else:
        buildid = f'custom.{os.getlogin()}.{platform.node()}.{os.getpid()}'
    
    # Get data from the script runner
    print(f"Shelling out to {args.script}.py to gather image information . . .")
    scriptsettings = json.loads(subprocess.check_output([
                sys.executable, # necessary to use this to stick with the pipenv
                f"{args.script}.py",
                "--validate",
            ] + args.script_args,
            cwd = str(pathlib.Path(__file__).parent.joinpath("script"))))
    imagename = scriptsettings["image"]

    # Get paths and variables
    arclightdir = pathlib.Path(__file__).parent
    rootdir = arclightdir.parent
    imagebuilddir = arclightdir.joinpath(f'image/{imagename}').resolve()
    containername = f"arclight:{imagename}_{buildid}" # This *should* be chosen based on the branch and image changelist, if there are no patches yet
    outputprefix = "arclight_output" # this is here just so it's centralized, I don't expect it'll get changed
    
    # AWS variables
    awscontainername = None # filled out by the AWS systems
    awscredentials = None # filled out by the AWS systems
    awsregion = "us-east-1"
    # I ran this once without explicitly specifying an availability zone and it ended up in us-east-1e which was literally the only availability zone without the instance type we needed
    # there's probably a better way to do this but for now I'm kinda just guessing
    awsavailabilityzone = "us-east-1c"

    # containersettings = util.wincontainer_version's results
    # mountingMode = "docker" or "aws" or "smb"
    # targetdir = [directory]
    if args.inplace or args.managed:
        import util.wincontainer_version
        containersettings = util.wincontainer_version.local()
        
        if containersettings["runisolation"] == "hyperv":
            # docker mounts don't work with hyperv isolation :(
            mountingMode = "smb"
        else:
            mountingMode = "docker"
        
    elif args.aws:
        import util.wincontainer_version
        containersettings = util.wincontainer_version.aws_2019()
        
        mountingMode = "aws"

        with open("config/credentials.json", "r") as f:
            awscredentials = json.load(f)
    else:
        raise Exception("no valid runtime mode?")
    
    # p4_sync_from validation and setup
    if args.inplace:
        # p4_sync is optional
        
        if args.p4_sync_from is not None:
            raise Exception("`--p4_sync_from` is not permitted with `--inplace`")
    elif args.managed:
        if args.p4_sync is None:
            raise Exception("`--p4_sync` is required with `--managed`")
            
        if args.p4_sync_from is None:
            raise Exception("`--p4_sync_from` is required with `--managed`")
    elif args.aws:
        if args.p4_sync is None:
            raise Exception("`--p4_sync` is required with `--aws`")

        if args.p4_sync_from is not None:
            raise Exception("`--p4_sync_from` is not permitted with `--aws`")
    else:
        raise Exception("no valid runtime mode?")
    
    if args.p4_sync_from is not None:
        if not args.p4_sync_from.isdigit():
            raise Exception(f"{args.p4_sync_from} is not a valid changelist for `--p4_sync_from` (must be numeric)")
            
    if args.p4_sync is not None:
        if args.p4_sync != "head" and not args.p4_sync.isdigit():
            raise Exception(f"{args.p4_sync} is not a valid changelist for `--p4_sync` (must be numeric or `head`)")

    # Figure out mounting mode and target directory
    if mountingMode == "docker":
        # docker mounting mode needs to put it in a subdirectory
        targetDir = "c:\\work"
    elif mountingMode == "aws":
        # aws also needs to put it in a subdirectory, which surprises me
        targetDir = "c:\\work"
    elif mountingMode == "smb":
        # samba needs its own drive entirely
        # I'm picking r: because who has an r:?
        # but we really should be searching for something that's available
        targetDir = "r:\\"
    else:
        raise Exception("no valid mounting mode?")
    outputDir = os.path.join(targetDir, outputprefix)

    # Validate options
    smb_share = None
    if mountingMode == "smb":
        if args.smb_username is None or args.smb_password is None:
            raise Exception("Missing smb share configuration while using smb-share mode; please provide smb_username, smb_password, and smb_share")
        
        # figure out our SMB share, make one if we need to
        import win32net
        

        # magic number 2 is the "level of detail"; this one lists paths
        shares, _, _ = win32net.NetShareEnum(None, 2)
        share = next((x for x in shares if os.path.realpath(x["path"]) == os.path.realpath(rootdir)), None)

        if share is not None:
            smb_share = f"\\\\{platform.node()}\\{share['netname']}"
            print(f"Found SMB share {smb_share}")
        else:
            if False:
                import pywintypes

                # This would technically allow it to create the share automatically.
                # I am leery of this and don't plan to enable it unless we need it for integration with the build farm.
                target_share_name = os.path.basename(rootdir)
                smb_share = f"\\\\{platform.node()}\\{target_share_name}"
                print(f"Can't find a valid SMB share! Attempting to make {smb_share} pointing to {os.path.realpath(rootdir)} . . .")

                shinfo={} # shinfo struct
                shinfo['netname'] = target_share_name
                shinfo['type'] = 0
                shinfo['remark'] = ""
                shinfo['permissions'] = 0 # This is probably wrong and needs to be changed.
                shinfo['max_uses'] = -1
                shinfo['current_uses'] = 0
                shinfo['path'] = os.path.realpath(rootdir)
                shinfo['passwd'] = None
                try:
                    win32net.NetShareAdd(None, 2, shinfo)
                except pywintypes.error:
                    print("Creation failed! You probably don't have permission.")
                    print()
                    print("Please run this command in an elevated prompt:")
                    print()
                    print(f"net share {target_share_name}={os.path.realpath(rootdir)} /grant:{os.getlogin()},FULL")
                    print()
                    raise
            else:
                target_share_name = os.path.basename(rootdir)

                print("Couldn't find appropriate SMB share!")
                print()
                print("Please run this command in an elevated prompt:")
                print()
                print(f"net share {target_share_name}={os.path.realpath(rootdir)} /grant:{os.getlogin()},FULL")
                print()

                raise Exception("no SMB share")

    # Build the docker image
    with Context("docker build"):
        subprocess.check_call([
                'python', 'build.py',
                '--name', containername,
                '--baseimage', containersettings["baseimage"],
                '--dllsrcimage', containersettings["dllsrcimage"],
                '--isolation', containersettings["buildisolation"],
            ], cwd=imagebuilddir)

    # Upload docker image if we're going to AWS
    if args.aws:
        aws = util.aws.Aws(
            region = awsregion,
            zone = awsavailabilityzone,
            aws_access_key_id = awscredentials["aws_access_key_id"],
            aws_secret_access_key = awscredentials["aws_secret_access_key"])

        # Push our container and get our fully-specified container name
        fullcontainername = aws.push_container(containername)
        
        # Long-term, this is going to be the unique ID for locally-built test images, and the branch/type/cl triple - same as the tag - for long-term images
        # This is because the unique ID may not even be available when we're doing the build.
        # Right now it's just the unique ID though because it's always available.
        dockerimageid = subprocess.check_output([
                "docker",
                "images", fullcontainername,
                "--format", "{{.ID}}",
            ]).decode("utf-8").strip()
            
    # figure out all the args we need for bootstrap.py
    bootstrap_args = [
        # build script arguments
        "--no-init-drive",
        "--workdir", targetDir,
        "--script", args.script,
        "--output", outputDir,
    ]

    if mountingMode == "smb":
        bootstrap_args += [
            "--smb_username", args.smb_username,
            "--smb_password", args.smb_password,
            "--smb_share", smb_share,
        ]

    with Context("p4 setup"):
        # Connect to p4 and gather necessary info
        import P4
        p4 = P4.P4()
        p4.user = args.p4_username
        p4.password = args.p4_password
        p4.port = args.p4_server
        p4.connect()
        p4.run_login()
        
        # retrieve the fingerprint
        trusts = p4.run_trust("-l")
        # this format is really janky
        
        # it's a list containing a single giant multiline string in a format that looks like
        # "12.34.56.78:1234 de:ad:be:ef:et:c"
        # repeated once for every server the computer is aware of, but with a fully-resolved hostname
        
        # I don't want to go to the trouble of resolving the IP right now, and all the server keys are the same at the moment
        # so we're just scooping the fingerprint out of the first line and using it
        fingerprint = trusts[0].split("\n")[0].split(" ")[1]
        
        if args.p4_sync == "head":
            # We absolutely need a consistent p4-sync value for the rest of this
            # if we're syncing to "head" as a concept, and resolving it multiple times during runtime, then things like saving AWS snapshots might end up with the wrong version number
            
            # Resolve "head" here so we have a consistent view of what we're doing
            
            # This is a repo-wide number, which is technically OK because it will work everywhere
            # In theory we should be getting the last change submitted only to the branch we're looking at.
            # But that's tougher and I'm currently not worrying about it.
            args.p4_sync = p4.run("counter", "change")[0]["value"]
            print(f"P4: Resolved p4_sync to {args.p4_sync}")
        
        bootstrap_args += [
            "--p4_username", args.p4_username,
            "--p4_password", args.p4_password,
            "--p4_server", args.p4_server,
            "--p4_fingerprint", fingerprint,
        ]
            
        if args.managed or args.aws:
            if args.p4_workspace is not None:
                raise Exception("p4 workspace specified for managed or fresh checkout; this is currently not supported")
            
            p4host = "AIRSHIP-DUJA"
            
            # make a new fresh workspace
            # this dataflow is currently messy and desperately needs to be improved
            # if we're running in AWS mode, this needs to happen *after* we get the snapshot, which, itself, needs to happen after we resolve `head`, which needs to happen after the basic p4
            # if we're running in managed mode it just needs to happen now
            # part of me thinks this should be moved into some kind of mode-specific function . . . but then, the order is important, and all the modes basically do the same thing in roughly the same order, and splitting this apart is absolutely begging for bugs
            # to make this worse, *when* this is called in AWS mode, it already has some resources allocated that it needs to be able to tear down on error
            # which means either doing a whole ton of manual error handling or wrapping it in a `with` block or doing something gnarlier with our own error-handling code
            # so tl;dr this design sucks and should be fixed but right now I'm too familiar with the code to do it properly.
            def p4_create_workspace():
                if args.working is not None:
                    sanitizedWorkingDir = re.sub(r'\W+', '_', args.working)
                    workspaceName = f"{args.p4_username}_arclight_{platform.node()}_{sanitizedWorkingDir}"
                else:
                    workspaceName = f"{args.p4_username}_arclight_{buildid}"
                
                # Clear out this client if it already exists
                try:
                    p4.run_client("-d", "-f", workspaceName)
                except P4.P4Exception:
                    pass    # this is probably "this workspace doesn't exist" and we're fine with that
                
                client = p4.run_client("-S", f"//depot/{args.p4_stream}", "-o", workspaceName)
                client[0]["Root"] = targetDir
                client[0]["Host"] = p4host  # we'll use P4HOST to fake this
                p4.save_client(client[0])
                print(f"P4: created workspace {workspaceName}")
                
                # this isn't a great cleanup but it'll work for now
                @atexit.register
                def p4_cleanup_workspace():
                    print(f"P4: deleted workspace {workspaceName}")
                    p4.run_client('-d', workspaceName)
                
                # switch client!
                # and, uh, fake stuff!
                p4.host = p4host
                p4.client = workspaceName
                
                # Pretend we're at sync_from, if we have one
                if args.p4_sync_from is not None:
                    p4.exception_level = 1  # "up-to-date" is a warning for some godforsaken reason
                    p4.run_flush(f"@{args.p4_sync_from}")
                    p4.exception_level = 2  # back to warning us about everything, which is generally useful
                
                nonlocal bootstrap_args
                bootstrap_args += [
                    "--p4_workspace", workspaceName,
                ]
            
            # just do it immediately
            if args.managed:
                p4_create_workspace()
            
            if args.p4_patch is not None:
                bootstrap_args += [
                    "--p4_patch", args.p4_patch,
                ]
        else:
            if args.p4_workspace is None:
                raise Exception("no p4 workspace specified for in-place work; this is currently not supported")
            
            client = p4.run_client("-o", args.p4_workspace)
            
            # We can just yank the root out here and use it
            args.working = client[0]["Root"]
            
            bootstrap_args += [
                "--p4_workspace", args.p4_workspace,
            ]
    
    # add this in now that it's resolved to a number
    if args.p4_sync is not None:
        bootstrap_args += [
            "--p4_sync", args.p4_sync,
        ]

    if args.aws:
        ec2 = boto3.client('ec2',
            region_name = awsregion,
            aws_access_key_id = awscredentials["aws_access_key_id"],
            aws_secret_access_key = awscredentials["aws_secret_access_key"])
        
        # as of this writing, these are the instance types that arguably make sense
        # (cores, memory, network, cost/hr)
        instanceTypes = {
            # 0.0845/core/hr
            # "c5a.2xlarge":      (  8,   16,   5,  0.676), # does not work, OOM
            # "c5a.4xlarge":      ( 16,   32,   5,  1.352), # does not work, OOM
            "c5a.8xlarge":      ( 32,   64,  10,  2.704),
            "c5a.12xlarge":     ( 48,   96,  12,  4.056),
            "c5a.16xlarge":     ( 64,  128,  20,  5.408),
            "c5a.24xlarge":     ( 96,  192,  20,  8.112),
            
            # 0.0885/core/hr
            "c6i.32xlarge":     (128,  256,  50, 11.328),
            
            # 0.0892/core/hr
            "m6a.48xlarge":     (192,  768,  50, 17.127),
            
            # 0.1679/core/hr
            "u-6tb1.112xlarge": (448, 6144, 100, 75.208),
        }
        
        # We're going to try to find, and perhaps even make, an AMI for ourselves
        aminame = f"arclight-{dockerimageid}"
        amis = ec2.describe_images(Filters = [{'Name':'tag:Name', 'Values':[aminame]}])["Images"]
        if len(amis) == 1:
            ami = amis[0]["ImageId"]
            print(f"AMI: found ({aminame}, {ami})")
        elif len(amis) > 1:
            raise Exception("too many AMIs!")
        elif not args.aws_allow_new_ami:
            # this is the same "find a base AMI" logic listed below
            amis = ec2.describe_images(Filters = [{'Name':'tag:Name', 'Values':['arclight-*']}])["Images"]
            ami = max(amis, key = lambda img: dateutil.parser.parse(img["CreationDate"]))['ImageId']
            print(f"AMI: found slightly-old ({aminame}, {ami})")
        else:
            # aww dangit
            with Context("ami build"):
                print(f"AMI: building new AMI ({aminame})")
                
                # Find the most recent arclight AMI
                # Note: This has to be created (once) by hand, see ARCHITECTURE.md
                amis = ec2.describe_images(Filters = [{'Name':'tag:Name', 'Values':['arclight-*']}])["Images"]
                if len(amis) == 0:
                    raise Exception("can't find a base AMI!")
                
                # try to find the most recent one
                # this could be a lot better because "most recent one" doesn't actually imply "closest to this image"
                # in addition, we shouldn't even be doing this if we have an AMI that's sufficiently close
                # but all of this is complicated and I am currently not planning to worry about it
                baseami = max(amis, key = lambda img: dateutil.parser.parse(img["CreationDate"]))['ImageId']
                print(f"AMI: chose base image ({baseami})");
                
                # spawn that AMI
                with aws.run_instance_prepped(
                        ami = baseami,
                        instanceType = "m5a.large", # 8gb RAM, 10gbit network; we don't care about much else here
                        blockDeviceMappings = [
                            # primary drive is not big enough
                            {
                                "DeviceName": "/dev/sda1",
                                "Ebs": {
                                    "VolumeType": "gp3",
                                    "VolumeSize": 50,
                                    
                                    "Iops": 3000,
                                    "Throughput": 125,
                                
                                    "DeleteOnTermination": True,
                                },
                            },
                        ]) as instance:
                
                    # Pull the image
                    with Context("docker pull"):
                        instance.ssh([
                            'docker', 'pull',
                            fullcontainername,
                        ])
                    
                    # Get all the current known images
                    knownimages = instance.ssh([
                        'docker', 'images',
                        '--format', '{{.Repository}}:{{.Tag}}',
                    ]).strip().splitlines()
                    
                    # Wipe out the ones we don't want
                    for img in knownimages:
                        if img != fullcontainername and img != "":
                            print(f"AMI: removing stale container {img}")
                            instance.ssh([
                                'docker', 'rmi',
                                img.strip(),
                            ])
                    
                    # Clean up remaining images
                    instance.ssh([
                            'docker', 'image', 'prune', '-f',
                        ])
                    
                    # Stop so we can snapshot it
                    ec2.stop_instances(InstanceIds = [instance.instanceid])
                    
                    # Wait for it to really be stopped
                    while True:
                        instanceInfo = ec2.describe_instances(InstanceIds = [instance.instanceid])["Reservations"][0]["Instances"][0]
                        
                        if instanceInfo["State"]["Name"] == 'running' or instanceInfo["State"]["Name"] == 'stopping':
                            print("AMI: waiting for stop")
                            time.sleep(1)
                            continue
                            
                        if instanceInfo["State"]["Name"] != 'stopped':
                            raise Exception(f"AMI: stopping process failed! {instanceInfo['State']['Name']}")
                        
                        break
                    
                    # Make an AMI off it
                    print("AMI: building . . .")
                    ami = ec2.create_image(
                        InstanceId = instance.instanceid,
                        Name = aminame,
                        TagSpecifications = [
                            {
                                "ResourceType": "image",
                                "Tags": aws.generate_tags(name = aminame, owner = "arclight-core", timeout = datetime.timedelta(days = 7)),
                            },
                            {
                                "ResourceType": "snapshot",
                                "Tags": aws.generate_tags(name = aminame, owner = "arclight-core", timeout = datetime.timedelta(days = 7)),
                            },
                        ]
                    )["ImageId"]
                    
                    print(f"AMI: building ({ami})")
                
                # Deallocate the instance, now just wait for it to be finished building (this takes a while . . .)
                with Context("ami finish"):
                    while True:
                        amis = ec2.describe_images(ImageIds = [ami])["Images"]
                        if amis[0]["State"] == "pending":
                            print("AMI: waiting for completion")
                            time.sleep(5)
                            continue
                            
                        if amis[0]["State"] != "available":
                            raise Exception(f"AMI: construction process failed! {amis[0]['State']}")
                        
                        break

        aws.update_timeout([ami] + [dev['Ebs']['SnapshotId'] for dev in amis[0]["BlockDeviceMappings"]], datetime.timedelta(days = 7))
        
        instanceType = "c5a.8xlarge"
        cpus = instanceTypes[instanceType][0]
        memory = instanceTypes[instanceType][1] # it's fine to use *all* the memory because this is process isolation
        
        # but if we have something specified, cut it down
        if args.memory:
            memory = min(memory, int(args.memory))
        
        # s3 filename that we'll be writing to
        s3filename = f"arclight-{aws.owner}.{(datetime.datetime.now() + datetime.timedelta(days = 1)).replace(tzinfo=simple_utc()).isoformat()}.7z"
        
        # Find our working volume . . .
        # There's honestly a lot of race conditions in here. Right now I'm hoping we just don't run into trouble, but this ideally should be fixed one way or another.
        # In a world without atomic operations we're going to need to either implement our own mutexes, or just recover from every imaginable error condition, which sounds like a bit of a headache.
        volumeInit = False # assume for now we'll find something sensible!
        volumePreserveOnSuccess = args.p4_patch is None or args.p4_patch_allow_preserve_DO_NOT_USE # we don't want to save this if we have a patch, because that might result in a weird unexpected state
        volume = None   # we'll fill this one way or another!
        
        # first, look for something appropriate in volume form; if we find it, we'll just use that
        # ugh, python, why don't you have manual scoping allowed
        if volume is None:
            volumes = ec2.describe_volumes(
                # Looking for something with the same version and the same branch
                # Also, must be available
                Filters = [
                    { 'Name': 'tag:arclight-version', 'Values': [str(util.aws.version)] },
                    { 'Name': 'tag:arclight-sig-stream', 'Values': [args.p4_stream] },
                    { 'Name': 'status', 'Values': ["available"] },
                ])["Volumes"]
            
            # Find the largest changelist that isn't larger than our sync target (Price is Right rules)
            volumeObj = max([volume for volume in volumes if int(util.aws.get_tag(volume["Tags"], "arclight-sig-cl")) <= int(args.p4_sync)], key = lambda volume: int(util.aws.get_tag(volume["Tags"], "arclight-sig-cl")), default = None)
            
            if volumeObj is not None:
                volume = volumeObj["VolumeId"]
                
                # Set our syncfrom info
                args.p4_sync_from = util.aws.get_tag(volumeObj["Tags"], "arclight-sig-cl")
                
                print(f"VOLUME: Reusing live volume {volume}@{args.p4_sync_from}")
                
                # Strip out the tags to reduce the chance of someone trying to use it out from under us
                ec2.delete_tags(
                    Resources = [volume],
                    Tags = [
                        { 'Key': 'arclight-sig-stream' },
                        { 'Key': 'arclight-sig-cl' },
                    ]
                )
                
                # TODO: Ideally we'd set this up to put the tags back if an error happens before the instance is started, so we could reuse the volume again
                # Right now it'll just get deleted
        
        # If we have an existing volume, we're good! Otherwise, try making one, ideally from a snapshot
        if volume is None:
            snapshots = ec2.describe_snapshots(
                # Looking for something with the same version and the same branch
                # Also, must be available
                Filters = [
                    { 'Name': 'tag:arclight-version', 'Values': [str(util.aws.version)] },
                    { 'Name': 'tag:arclight-sig-stream', 'Values': [args.p4_stream] },
                    { 'Name': 'status', 'Values': ["completed"] },
                ])["Snapshots"]
            
            snapshotObj = max([snapshot for snapshot in snapshots if (int(util.aws.get_tag(snapshot["Tags"], "arclight-sig-cl")) <= int(args.p4_sync))], key = lambda snapshot: int(util.aws.get_tag(snapshot["Tags"], "arclight-sig-cl")), default = None)
            
            if snapshotObj is not None:
                # Set our syncfrom info
                args.p4_sync_from = util.aws.get_tag(snapshotObj["Tags"], "arclight-sig-cl")
                
                snapshotId = snapshotObj["SnapshotId"]
            else:
                snapshotId = None # welp
                
                # p4_sync_from will remain None because we're starting from scratch
            
            createVolumeParams = {
                "AvailabilityZone": awsavailabilityzone,
                
                # enough for now! TODO figure out if we can shrink this down a bit to save cash (and maybe detect if it's getting too low and start moving it up automatically?)
                "Size": 500,
                
                # slightly above GP3 lowest-level, to allow saturating large p4 syncs
                "VolumeType": "gp3",
                "Iops": 3000,
                "Throughput": 250,
                
                "TagSpecifications": [
                    {
                        "ResourceType": "volume",
                        "Tags": aws.generate_tags(name = f"{util.aws.label}-{aws.owner}-working", owner = aws.owner, timeout = datetime.timedelta(days = 1)),
                    },
                ],
            }
            
            if snapshotId is not None:
                createVolumeParams["SnapshotId"] = snapshotId
            
            # Go ahead and make a volume!
            volume = ec2.create_volume(**createVolumeParams)["VolumeId"]
            
            if snapshotId is not None:
                print(f"VOLUME: Cloning snapshot {snapshotId} -> {volume}@{args.p4_sync_from}")
            else:
                print(f"VOLUME: Creating fresh volume {volume}")
                
                # We actually *do* need to initialize this :(
                volumeInit = True
                
        # prepare to kill this on failure
        with util.aws.AwsVolume(ec2, volume) as volumeHandle:
                        
            # now that args.p4_sync_from has been filled in we can create our workspace!
            p4_create_workspace()
        
            # Spawn the server itself using our fancy new AMI, subnet, security group, and p4 workspace
            with aws.run_instance_prepped(
                    ami = ami,
                    instanceType = instanceType,
                    blockDeviceMappings = [
                        # primary drive is not big enough by default so we size it up a bit
                        {
                            "DeviceName": "/dev/sda1",
                            "Ebs": {
                                "VolumeType": "gp3",
                                "VolumeSize": 50,
                                
                                "Iops": 3000,
                                "Throughput": 125,
                            
                                "DeleteOnTermination": True,
                            },
                        },
                    ],
                    workingVolume = {
                        "VolumeId": volume,
                        "Device": "xvdb",
                        "Init": volumeInit,
                    }) as instance:
                
                # Pull the image
                print("BUILD: pulling image")
                instance.ssh([
                    'docker', 'pull',
                    fullcontainername,
                ])
                
                bootstrap_args += [
                    "--output_compress", # makes it easier and faster (and cheaper) to download
                    "--output_s3", s3filename,
                    "--aws_access_key_id", awscredentials["aws_access_key_id"],
                    "--aws_secret_access_key", awscredentials["aws_secret_access_key"],
                ]
                
                # Run the build script!
                print("BUILD: starting image")
                with Context("run"):
                    instance.ssh([
                        'docker', 'run',
                        '-v', f'd:\:{targetDir}',
                        f"--cpus={cpus}",
                        f"--memory={memory}GB",
                        f"--isolation={containersettings['runisolation']}",
                        # image name
                        fullcontainername,
                    ] + bootstrap_args + ["--"] + args.script_args)
                
                # Success!
                if volumePreserveOnSuccess:
                
                    # Stop the instance for a clean snapshot
                    print("INSTANCE: stopping instance")
                    ec2.stop_instances(InstanceIds = [instance.instanceid])
                    
                    # Wait for it to really be stopped
                    while True:
                        instanceInfo = ec2.describe_instances(InstanceIds = [instance.instanceid])["Reservations"][0]["Instances"][0]
                        
                        if instanceInfo["State"]["Name"] == 'running' or instanceInfo["State"]["Name"] == 'stopping':
                            print("INSTANCE: waiting for stop")
                            time.sleep(1)
                            continue
                            
                        if instanceInfo["State"]["Name"] != 'stopped':
                            raise Exception(f"INSTANCE: stopping process failed! {instanceInfo['State']['Name']}")
                        
                        break
                    
                    # Put together the tags we'll be attaching to stuff
                    imageName = f"{util.aws.label}-{args.p4_stream}-{args.p4_sync}"
                    extraTags = [
                        {"Key": "arclight-sig-stream", "Value": args.p4_stream},
                        {"Key": "arclight-sig-cl", "Value": args.p4_sync},
                    ]
                    
                    # Snapshot
                    snapshot = ec2.create_snapshot(
                        VolumeId = volume,
                        TagSpecifications = [
                            {
                                "ResourceType": "snapshot",
                                "Tags": aws.generate_tags(name = imageName, owner = "arclight-core", timeout = datetime.timedelta(days = 7)) + extraTags,
                            },
                        ],
                    )["SnapshotId"]
                    print(f"VOLUME: snapshotted to {snapshot}")
                    
                    # Re-tag volume so it can be reused on short notice
                    ec2.create_tags(
                        Resources = [volume],
                        Tags = aws.generate_tags(name = imageName, owner = "arclight-core", timeout = datetime.timedelta(days = 1)) + extraTags,
                    )
                    print(f"VOLUME: retagged {volume} for reuse")
                    
                    # And keep it around
                    volumeHandle.preserve = True
            
        # Instance and volume terminate here
            
        print("BUILD: downloading result")
        with Context("download"):
            s3 = boto3.client('s3',
                aws_access_key_id = awscredentials["aws_access_key_id"],
                aws_secret_access_key = awscredentials["aws_secret_access_key"])
            with open("arclight_output.7z", "wb") as f:
                s3.download_fileobj("arclight", s3filename, f)
            s3.delete_object(Bucket = "arclight", Key = s3filename)
        
    elif args.inplace or args.managed:
        # Local Docker execution
        # Assemble the execution command

        cpus = multiprocessing.cpu_count()
        
        # use 75% of the computer's memory at most
        memory = round(psutil.virtual_memory().total / (1 << 30) / 4 * 3)
        
        # but if we have something specified, cut it down
        if args.memory:
            memory = min(memory, int(args.memory))
            
        print(f"Using {memory}GB of RAM")

        # chop off 8gb, then assume 2gb per CPU
        # on a 64gb machine, this uses 48gb and gets 20 threads, which works
        # on a 32gb machine, this uses 24gb and gets 8 threads, which doesn't work
        # some further adjustments might be needed?
        max_cpus = math.floor((memory - 8) / 2)

        if cpus > max_cpus:
            print(f"Reducing CPU count to deal with limited memory; maxing out at {max_cpus} CPUs")
            cpus = max_cpus

        command = [
            'docker', 'run',
            
            # This used to be needed to solve a networking error
            # See https://forums.docker.com/t/dns-mechanism-with-windows-containers/104542
            # It may have been fixed, and is incompatible with Stevedore.
            # If you see this in 2023, please just remove it.
            # "--network", "Default Switch",
        ]

        if mountingMode == "docker":
            command += [
                '-v', f'{args.working}:{targetDir}',
            ]
         
        command += [
            f"--cpus={cpus}",
            f"--memory={memory}GB",
            f"--isolation={containersettings['runisolation']}",
            containername,
        ]
        
        command += bootstrap_args
        command += ["--"]
        command += args.script_args

        # see https://stackoverflow.com/questions/11516258/what-is-the-equivalent-of-unbuffer-program-on-windows/44531837#44531837
        # for now, we just permit gnarly buffered output
        #print("For complicated reasons, it is impossible to run this command for you and still show realtime (or even complete!) log output.")
        #print("Please run the following command:")
        #print(' '.join(quote(c) for c in command))

        # cwd doesn't really matter here
        with Context("run"):
            subprocess.check_call(command)

    print("SUCCESS!")

if __name__ == "__main__":
    main()
