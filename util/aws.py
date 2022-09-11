
import base64
import boto3
import datetime
import fabric
import os
import pprint
import re
import requests
import subprocess
import time

from typing import Dict
from typing import List
from typing import Optional

from util.prof import prof
from util.simple_utc import simple_utc

envname = "arclight"
version = 2

label = f"{envname}-v{version}"

class Aws:
    @prof
    def __init__(self, region: str, zone: str, aws_access_key_id: str, aws_secret_access_key: str):
        self.region = region
        self.zone = zone
        
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        
        if "BUILD-NUMBER" in os.environ:
            self.owner = f"jenkins-{os.environ['BUILD-NUMBER']}"
        else:
            self.owner = os.getlogin()
        
        # ECR setup!
        ecr = boto3.client('ecr',
            region_name = self.region,
            aws_access_key_id = self.aws_access_key_id,
            aws_secret_access_key = self.aws_secret_access_key)
        
        # Set up our repository if we need one
        try:
            # we don't want this versioned, that's just unnecessary cost
            ecr.create_repository(
                repositoryName = envname,
            )
            print("ECR repository: created")
        except ecr.exceptions.RepositoryAlreadyExistsException:
            print("ECR repository: already exists")
            
        # get our ECR auth token
        auth = ecr.get_authorization_token()["authorizationData"][0]
        self.ecsendpoint = auth["proxyEndpoint"]
        self.ecsuser, self.ecspassword = base64.b64decode(auth["authorizationToken"]).decode("utf-8").split(":")
        subprocess.check_call([
                'docker', 'login',
                '-u', self.ecsuser,
                '-p', self.ecspassword,
                self.ecsendpoint,
            ])

        # generate our repo prefix now that we have ECR information
        self.repo = self.ecsendpoint.removeprefix("https://")
        
        # EC2 setup!
        ec2 = boto3.client('ec2',
            region_name = self.region,
            aws_access_key_id = self.aws_access_key_id,
            aws_secret_access_key = self.aws_secret_access_key)
        
        # Find/make VPC
        vpcs = ec2.describe_vpcs(Filters = [{'Name':'tag:Name', 'Values':[label]}])["Vpcs"]
        if len(vpcs) == 1:
            vpc = vpcs[0]["VpcId"]
            print(f"VPC: already exists ({vpc})")
        elif len(vpcs) > 1:
            raise Exception("too many vpcs!")
        else:
            vpc = ec2.create_vpc(
                CidrBlock = "10.42.0.0/16", # we don't actually care about this CIDR block, but we have to specify something
                TagSpecifications = [
                    {
                        "ResourceType": "vpc",
                        "Tags": self.generate_tags(name = label, owner = "arclight-core"),
                    },
                ],
            )["Vpc"]["VpcId"]
            
            # it doesn't start up instantly so we need to wait for it to be ready
            while True:
                try:
                    vpcs = ec2.describe_vpcs(VpcIds = [vpc])["Vpcs"]
                except ec2.exceptions.ClientError:
                    print("VPC: Waiting for VPC to be found")
                    time.sleep(1)
                    continue
                
                break
                
            print(f"VPC: created {vpc}")
                
        # Find/make subnet
        subnets = ec2.describe_subnets(Filters = [{'Name':'tag:Name', 'Values':[label]}])["Subnets"]
        if len(subnets) == 1:
            self.subnet = subnets[0]["SubnetId"]
            print(f"SUBNET: already exists ({self.subnet})")
        elif len(subnets) > 1:
            raise Exception("too many subnets!")
        else:
            self.subnet = ec2.create_subnet(
                VpcId = vpc,
                CidrBlock = "10.42.0.0/16", # again, we don't actually care about this CIDR block, but we have to specify something
                
                AvailabilityZone = self.zone,
                
                TagSpecifications = [
                    {
                        "ResourceType": "subnet",
                        "Tags": self.generate_tags(name = label, owner = "arclight-core"),
                    },
                ],
            )["Subnet"]["SubnetId"]
            
            # it doesn't start up instantly so we need to wait for it to be ready
            while True:
                try:
                    subnets = ec2.describe_subnets(SubnetIds = [self.subnet])["Subnets"]
                except ec2.exceptions.ClientError:
                    print("SUBNET: waiting for creation")
                    time.sleep(1)
                    continue
                
                break
            
            try:
                # Attach internet gateway
                gateways = ec2.describe_internet_gateways(Filters = [{'Name':'tag:Name', 'Values':[label]}])["InternetGateways"]
                if len(gateways) == 1:
                    gateway = gateways[0]["InternetGatewayId"]
                    print(f"GATEWAY: already exists ({gateway})")
                elif len(gateways) > 1:
                    raise Exception("too many gateways!")
                else:
                    gateway = ec2.create_internet_gateway(
                        TagSpecifications = [
                            {
                                "ResourceType": "internet-gateway",
                                "Tags": self.generate_tags(name = label, owner = "arclight-core"),
                            },
                        ],
                    )["InternetGateway"]["InternetGatewayId"]
                    
                    ec2.attach_internet_gateway(
                        InternetGatewayId = gateway,
                        VpcId = vpc,
                    )
                    
                    print(f"GATEWAY: created ({gateway})")
                
                # Create a route table
                routetable = ec2.create_route_table(VpcId = vpc)["RouteTable"]["RouteTableId"];
                
                ec2.create_route(
                    DestinationCidrBlock = "0.0.0.0/0",
                    GatewayId = gateway,
                    RouteTableId = routetable
                )
                
                # Attach this whole mess to the subnet
                ec2.associate_route_table(
                    RouteTableId = routetable,
                    SubnetId = self.subnet,
                )
            except:
                ec2.delete_subnet(SubnetId = self.subnet);
                print(f"SUBNET: cleaned up ({self.subnet})")
                raise;
                
            print(f"SUBNET: created ({self.subnet})")
        
        # Need my own IP here
        myip = requests.get('https://checkip.amazonaws.com').content.decode('utf8').strip()
        print(f"IP: {myip}")
        
        # Find/make a security group from our current public IP so we can connect to the server
        securitygroupname = f"{label}-{myip}"
        securitys = ec2.describe_security_groups(Filters = [{'Name':'tag:Name', 'Values':[securitygroupname]}])["SecurityGroups"]
        if len(securitys) == 1:
            self.security = securitys[0]["GroupId"]
            print(f"SECURITY: already exists ({self.security})")
        elif len(securitys) > 1:
            raise Exception("too many security groups!")
        else:
            self.security = ec2.create_security_group(
                Description = f"Arclight SSH-to-IP security group for {myip}",
                GroupName = securitygroupname,
                VpcId = vpc,
                
                TagSpecifications = [
                    {
                        "ResourceType": "security-group",
                        "Tags": self.generate_tags(name = securitygroupname, owner = "arclight-core"),
                    },
                ],
            )["GroupId"]
            
            try:
                ec2.authorize_security_group_ingress(
                    GroupId = self.security,
                    IpPermissions = [{
                        'FromPort': 22,
                        'ToPort': 22,
                        'IpProtocol': 'tcp',
                        'IpRanges': [{
                            "CidrIp": f"{myip}/32",
                        }],
                    }],
                )
                
            except:
                # something failed, clean up
                ec2.delete_security_group(
                    GroupId=self.security,
                )
                print(f"SECURITY: failure on creation, cleaned up ({self.security})")
                raise
            
            print(f"SECURITY: created ({self.security})")
        
        # S3 setup
        s3 = boto3.client("s3",
            aws_access_key_id = self.aws_access_key_id,
            aws_secret_access_key = self.aws_secret_access_key)
        
        if self.region == "us-east-1":
            # why can't you just pass us-east-1 in and have the system deal with it
            s3.create_bucket(
                Bucket = "arclight",
            )
        else:
            s3.create_bucket(
                Bucket = "arclight",
                CreateBucketConfiguration = {
                    "LocationConstraint": self.region,
                },
            )
        
        print(f"S3: initialized")

    @prof
    def push_container(self, containername: str) -> str:
    
        # assemble a full container name
        fullcontainername = f"{self.repo}/{containername}"
        
        # tag our generated image with that name
        # (yes, we could have just generated it with this name to begin with, but doing this is fast and it makes the dataflow easier)
        subprocess.check_call([
                'docker', 'tag',
                containername,
                fullcontainername,
            ])

        # push the whole shebang up to ECR
        subprocess.check_call([
                'docker', 'push',
                fullcontainername
            ])
        
        return fullcontainername
    
    @prof
    def run_instance_prepped(self, ami: str, instanceType: str, blockDeviceMappings: Dict, workingVolume: Dict = None) -> 'AwsInstance':
        ec2 = boto3.client('ec2',
            region_name = self.region,
            aws_access_key_id = self.aws_access_key_id,
            aws_secret_access_key = self.aws_secret_access_key)
        
        # Spawn the server itself using our AMI, subnet, and security group
        instance = ec2.run_instances(
            ImageId = ami,
            InstanceType = instanceType,
            
            BlockDeviceMappings = blockDeviceMappings,
            
            # get a public IP
            NetworkInterfaces = [{
                "DeviceIndex": 0,
                "AssociatePublicIpAddress": True,
                "SubnetId": self.subnet,
                "Groups": [self.security],
            }],
            
            # why are these not defaults
            MinCount = 1,
            MaxCount = 1,
            
            TagSpecifications = [
                {
                    "ResourceType": "instance",
                    "Tags": self.generate_tags(name = f"{label}-{self.owner}", owner = self.owner, timeout = datetime.timedelta(days = 1)),
                },
            ],
        )["Instances"][0]["InstanceId"]
        print(f"INSTANCE: Initializing instance ({instance})")
        
        # Now that it's running, we really want to kill that server if something goes wrong.
        try:
            # Wait for running and public-IP
            while True:
                try:
                    instanceInfo = ec2.describe_instances(InstanceIds = [instance])["Reservations"][0]["Instances"][0]
                except ec2.exceptions.ClientError:
                    print("INSTANCE: Waiting for instance to be found")
                    time.sleep(1)
                    continue
                
                if instanceInfo["State"]["Name"] == 'pending':
                    print("INSTANCE: Waiting for startup")
                    time.sleep(1)
                    continue
                
                if instanceInfo["State"]["Name"] != 'running':
                    raise Exception(f"INSTANCE: Has entered state {instanceInfo['State']['Name']}, something is wrong, aborting")
                
                if "PublicIpAddress" not in instanceInfo or instanceInfo["PublicIpAddress"] == "":
                    print("INSTANCE: Waiting for public IP")
                    time.sleep(1)
                    continue
                
                instanceip = instanceInfo["PublicIpAddress"];
                print(f"INSTANCE: Startup at {instanceip} successful!")
                break
            
            # Attach a working volume if we have one
            if workingVolume is not None:
                ec2.attach_volume(
                    InstanceId = instance,
                    VolumeId = workingVolume["VolumeId"],
                    Device = workingVolume["Device"],
                )
            
            # Wait for the SSH server to come up
            while True:
                import socket
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.settimeout(5)
                    if sock.connect_ex((instanceip, 22)) != 0:
                        print(f"INSTANCE: Waiting for SSH server")
                        time.sleep(1)
                        continue
                    
                    break
            print("INSTANCE: SSH server now responding!")
            
            # SSH call prefix
            sshcallstartup = [
                'ssh',
                '-o', 'StrictHostKeyChecking no', # ensure we accept the key
                '-i', 'config/id_rsa', # use our generic ssh key
                '-o', 'ConnectionAttempts 10', # seriously I don't know why this is so intermittent
                f"Administrator@{instanceip}",
            ]
            sshcalltermcheckless = [
                'ssh',
                '-tt', # this allows for better terminal output, but also breaks returning error codes
                '-i', 'config/id_rsa', # use our generic ssh key
                '-o', 'ConnectionAttempts 10', # seriously I don't know why this is so intermittent
                f"Administrator@{instanceip}",
            ]
            sshcall = [
                'ssh',
                '-i', 'config/id_rsa', # use our generic ssh key
                '-o', 'ConnectionAttempts 10', # seriously I don't know why this is so intermittent
                f"Administrator@{instanceip}",
            ]
            scpcall = [
                'scp',
                '-i', 'config/id_rsa', # use our generic ssh key
                f"Administrator@{instanceip}:",
            ]
            

            # First, just log in to register the keys. The first login seems to be sketchy in general so we try multiple times.
            # `capture_output = True` is really important here because otherwise it breaks the Windows console (why?!)
            tries = 0
            while subprocess.run(sshcallstartup + ['echo', 'hello!'], capture_output = True).returncode != 0:
                tries = tries + 1
                if tries == 10:
                    raise Exception("INSTANCE: Can't complete initial connection")
                
                time.sleep(2)
            print("INSTANCE: SSH keys registered")
            
            handle = AwsInstance()
            handle.instanceid = instance
            handle.instanceip = instanceip
            handle.scpcall = scpcall
            handle.ec2 = ec2
            
            # Extend the primary drive
            # This is subject to the same weird console quality problems that subprocess.run is (see the comment down in AwsInstance.ssh)
            # However, fabric doesn't have easy support for input strings
            # Thankfully we really don't care about realtime results here, so we just capture the output and reprint it ourselves.
            print("INSTANCE: Initializing primary drive")
            output = subprocess.run(sshcall + [
                    'diskpart',
                ], input = """
                    select volume 0
                    extend
                    exit
                """, text = True, check = True, stdout = subprocess.PIPE, stderr = subprocess.STDOUT).stdout
            print(re.sub(r'[^\x0a\x0d\x20-\x7f]', r'', output))
            
            # Initialize the working drive, if we have one and it needs init (only if it's a fresh volume, otherwise it gets automounted in D:)
            if workingVolume is not None and workingVolume["Init"]:
                print("INSTANCE: Initializing working drive")
                output = subprocess.run(sshcall + [
                        'diskpart',
                    ], input = """
                        select disk 1
                        create partition primary
                        format quick fs=ntfs
                        assign letter=D
                        exit
                    """, text = True, check = True, stdout = subprocess.PIPE, stderr = subprocess.STDOUT).stdout
                print(re.sub(r'[^\x0a\x0d\x20-\x7f]', r'', output))

            print("INSTANCE: Drive initialization complete")

            # Log in with Docker
            handle.ssh([
                'docker', 'login',
                '-u', self.ecsuser,
                '-p', self.ecspassword,
                self.ecsendpoint,
            ])
            
        except:
            ec2.terminate_instances(InstanceIds = [instance])
            print(f"INSTANCE: Terminated {instance} due to failure on startup!")
            raise
        
        return handle
    
    def generate_tags(self, name: str, owner: str, timeout: Optional[datetime.timedelta] = None) -> None:
        tags = [
            {"Key": "Name", "Value": name},
            {"Key": "arclight-owner", "Value": owner},
            {"Key": "arclight-version", "Value": str(version)},
        ]
        
        if timeout is not None:
            tags += [{"Key": "arclight-timeout", "Value": (datetime.datetime.now() + timeout).replace(tzinfo=simple_utc()).isoformat()}]

        return tags
    
    def update_timeout(self, resources: List[str], timeout: datetime.timedelta) -> None:
        ec2 = boto3.client('ec2',
            region_name = self.region,
            aws_access_key_id = self.aws_access_key_id,
            aws_secret_access_key = self.aws_secret_access_key)
        ec2.create_tags(
            Resources = resources,
            Tags = [{
                "Key": "arclight-timeout",
                "Value": (datetime.datetime.now() + timeout).replace(tzinfo=simple_utc()).isoformat(),
            }],
        )

def cli_quote(s: str) -> str:
    if s.isalnum():
        return s
        
    # So if we have a trailing \ inside quotes, the commandline interprets it as escaping the quote
    # We fix this specific special case, but there's probably more cases
    # This is overall very ugly :(
    if s[-1] == '\\':
        s = s[0:-1] + '\\\\'
    
    return f'"{s}"'
        
class AwsInstance:
    instanceid = None
    instanceip = None
    
    scpcall = None
    ec2 = None
    
    connection = None
    
    def __enter__(self):
        return self
  
    def __exit__(self, exception_type, exception_value, exception_traceback):
        # Disconnect
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        
        # Kill the server; it should be gone by now anyway!
        self.ec2.terminate_instances(InstanceIds = [self.instanceid])
        print(f"INSTANCE: Terminated {self.instanceid} during cleanup!")
        
    def ssh(self, command: List[str]) -> str:
        # I really shouldn't be using something this heavyweight here
        # Unfortunately, something *really weird* is going on with executing `ssh` via subprocess
        # Almost no matter what I do, it completely breaks the terminal
        # One alternative is to use -tt to create an artificial pty . . . but then you lose error check detection
        # Another option is to capture the output and print it manually after the command has finished
        # But then you don't get realtime output, which is actually really important for us
        # Maybe I could make it work with popen, but this is honestly easier than that.
        # Also, seriously, how is this so hard?
        if self.connection is None:
            self.connection = fabric.Connection(
                f"Administrator@{self.instanceip}",
                connect_kwargs={
                    "key_filename": "config/id_rsa",
                })
            self.connection.open() # needed so we can do the keepalive thing ;.;
            self.connection.transport.set_keepalive(60) # sometimes the SSH connection dies and this helps that not happen
        
        return self.connection.run(' '.join(cli_quote(c) for c in command)).stdout
    
    def scpFrom(self, src: str, dst: str) -> None:
        params = self.scpcall.copy()
        params[-1] += src   # we need to splice the last parameter together to form a valid SCP commandline
        params += [dst]
        subprocess.check_call(params)

class AwsVolume:
    ec2 = None
    volumeid = None
    preserve = False
    
    def __init__(self, ec2, volumeid: str):
        self.ec2 = ec2
        self.volumeid = volumeid
      
    def __enter__(self):
        return self
  
    def __exit__(self, exception_type, exception_value, exception_traceback):
        if not self.preserve:
            # Wipe the volume
            # This is tricky because it might still be attached to a shutting-down instance
            # Maybe we should just update_timeout() it to an immediate timeout and let the cleanup procedure kill it?
            while True:
                try:
                    self.ec2.delete_volume(VolumeId = self.volumeid)
                    print(f"VOLUME: Deleted {self.volumeid} during cleanup!")
                    break
                except self.ec2.exceptions.ClientError:
                    print(f"VOLUME: Trying to clean up {self.volumeid} . . .")
                    time.sleep(1)
                    continue

def get_tag(taglist: List, key: str) -> Optional[str]:
    for tag in taglist:
        if tag["Key"] == key:
            return tag["Value"]
    
    return None
