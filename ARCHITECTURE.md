Welcome to Arclight!

This is currently not well-documented, but I've started to braindump information here just to keep it collated.

Arclight is a build system designed to build Unreal projects via Docker images run through AWS. It is intended for most of this to live inside the project repository, which means it needs to jump through some wacky hoops in order to function properly.

The general path works like this:

* Get script configuration info by running `arclight/script/{SCRIPTNAME}.py --validate`
* Build the image if necessary, by running `arclight/image/{IMAGENAME}/build.py`
* Run the image with the desired script name and parameters
* Sync up p4 to the appropriate version
* Run `arclight/script/{SCRIPTNAME}.py` to actually do the thing

In theory, this is designed to allow multiple images, in multiple branches, with multiple scripts. In practice these are currently rather tightly coupled. Someday this will be fixed if we ever need it.

----

# Initial Setup

Install Python 3.9 and the current version of Docker.

Open a command prompt, and type `pip install --user pipenv`

Change to the Arclight directory, and type `pipenv sync`

If you want to use AWS, get AWS keys from whoever in your organization gives them out, copy `config/credentials.json.example` to `config/credentials.json`, and fill out the obvious text fields. If you're setting up specific user permissions, right now this requires EC2, ECR, and S3 permissions; I've just been granting full permissions in those categories because the most expensive and exploitable thing here is EC2's Start Instance, and that's just necessary for functionality, so I kinda haven't cared about the rest.

----

# In-Place Testing

The standard way of doing local testing is to base it on an existing repository on your hard drive. For complicated reasons, this is more complicated than I'd like. Here's a commandline:

pipenv run python arclight.py --inplace --p4_username $P4_USERNAME --p4_password $P4_PASSWORD --p4_server ssl:ultragame.mygamestudio.com --p4_workspace $P4_WORKSPACE --smb_username $WINDOWS_USERNAME --smb_password $WINDOWS_PASSWORD build --target_platform=Win64 --client_config=Development

The final result should look like this:

pipenv run python arclight.py --inplace --p4_username zorbathut --p4_password Swordfish --p4_server ssl:ultragame.mygamestudio.com --p4_workspace Zorba-Main-Dev --smb_username zorbathut --smb_password Swordfish build --target_platform=Win64 --client_config=Development

(no, my password is not actually Swordfish)

The SMB share stuff is necessary; you will *need* to share that directory with full read/write access. The script will help configure this, but it will still need your username and password. Yes, I know this is terrible. You can avoid it if you're using one of:

* Windows 10 20H2 or earlier
* Windows 11
* Windows Server

But the scripts don't yet support this, so if you want this functionality, come talk to me and I'll implement it.

# AWS Remote Execution

If you want to run the entire pathway on AWS, you will first need to get AWS certs (ask Chris), then use a different commandline:

pipenv run python arclight.py --aws --p4_username $P4_USERNAME --p4_password $P4_PASSWORD --p4_server ssl:ultragame.mygamestudio.com --p4_stream Ultragame_Mainline --p4_sync head [--p4_patch PATCHID] [--aws_allow_new_ami] build --target_platform=Win64 --client_config=Development

This will sync and build everything from scratch remotely. It also takes a while. Note that the image built will be built based on your local code, but the scripts run will be based on the current head version on the repository; if you want to modify code serverside, use the `--p4_patch` option to merge in code at runtime.

If you want to build a new AWS AMI, use the `--aws_allow_new_ami` option. This will take roughly an extra half an hour; it will also cache your current Docker image so you can run it rapidly in the future. I recommend doing this if you've made and tested Dockerfile changes that are bigger than a hundred megabytes. Later this option will vanish and it will handle this more intelligently; if you need this, let me know and I'll prioritize it. This option has little to do with the actual release-mode behavior, that has its own handling.

## Local Managed-Repo Testing

If you're trying to test the p4 sync process you'll want to use the Managed option. The first time this is used, it creates a new workspace and syncs up a project from scratch. Obviously this takes a while!

You will need to keep track, *by hand*, of what version you're on so it can update deltas. If you forget, delete the directory and start from zero.

pipenv run python arclight.py --managed --p4_username $P4_USERNAME --p4_password $P4_PASSWORD --p4_server ssl:ultragame.mygamestudio.com --p4_stream Ultragame_Mainline --working $WORKING_DIRECTORY --smb_username $WINDOWS_USERNAME --smb_password $WINDOWS_PASSWORD --smb_share $WINDOWS_SHARE_ID --p4_sync_from $ORIGIN --p4_sync $DESTINATION [--p4_patch PATCHID] build --target_platform=Win64 --client_config=Development

This is similar to the `--inplace` option, with a few changes. First, you must specify a working directory and desired stream instead of a p4 workspace. Second, you must specify what patch it's syncing from and to; the next time you run it, "from" must match last usage's "to". It also supports `--p4_patch`; the patch will be reverted once it's done or if it fails in a normal way, but if you bypass this, you might end up with your working directory in an inconsistent state. (The AWS version solves this by simply discarding that branch of the working disk.)

# Initial Setup

The AMI we use to bootstrap this on Amazon is difficult to make.

There is unfortunately no default Windows container with SSH access, or *any* programatic-capable remote access. We have to make this by hand. (Why?)

In theory there's an AMI Creator that can do this for us. In practice I have not gotten it to work. This should be worked on more.

Right now, the AMI is created by hand. First, start with Windows_Server-2019-English-Full-ContainersLatest-2021.11.10; and this isn't necessary but I recommend running with at least 8GB RAM just because it's a slow nightmare otherwise.

SSHD setup:
* Open Powershell
* `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0` (this takes a surprisingly long time)
* `Start-Service sshd`
* `Set-Service -Name sshd -StartupType 'Automatic'`
* Open c:\ProgramData\ssh
* Create a file called `administrators_authorized_keys` with your SSH public key in it (note: explorer by default hides extensions, and administrators_authorized_keys.txt won't work!)
* Open cmd
* `icacls.exe "C:\ProgramData\ssh\administrators_authorized_keys" /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"`

Python setup (NOTE: I don't think we use this anymore and it can probably be removed!):
* Open Cmd
* @"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -InputFormat None -ExecutionPolicy Bypass -Command "[System.Net.ServicePointManager]::SecurityProtocol = 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))" && SET "PATH=%PATH%;%ALLUSERSPROFILE%\chocolatey\bin"
* choco install -y python

Close (not terminate) the instance, then save it as a new AMI named `arclight-base`. (Then terminate it to clean it up.)

----

# Filesystem layout

`script`: Contains the actual work scripts as `script/SCRIPTNAME.py`. Each one of these is a command that can be run in Arclight. Each script also returns an image that it's meant to work with.
`image`: Contains the dockerfile image build scripts as `image/IMAGENAME/build.py`. 
`id_rsa/id_rsa.pub`: Contains the private key used to communicate with EC2 instances.

Everything else is `arclight` scripts and utilities.

----

# AWS tags used

`name`: Human-readable name attempting to convey information. Usually a bunch of the following tags concatenated together; must start with "arclight-". There shouldn't be any meaning to the arclight system itself, but right now that is totally not true.
`arclight-owner`: Lists the "owner" of a resource. In general, this is `arclight-core` indicating that it's not owned by anything in particular besides the arclight environment itself, `jenkins-[buildnumber]` indicating that it's owned by that specific build number and can be cleaned up as soon as that job is complete, or `[username]` indicating that it's owned by that user and you should go talk to them before cleaning it up.
`arclight-version`: Lists the version of arclight used to generate something. Don't touch things of a newer version than the running process. If an asset is old enough that we don't need to preserve any data from it anymore, it can be cleaned up.
`arclight-timeout`: An ISO8601 timestamp indicating when this should be deleted due to being old.
`arclight-sig-stream`: Used for volumes and snapshots storing results, indicates that it was built off a specific branch.
`arclight-sig-cl`: Used for volumes and snapshots storing results, indicates that it's the result of a build finishing on a specific changelist.