
import boto3
import datetime
import dateutil
import itertools
import json
import pprint

import util.aws

from typing import List
from typing import Optional

from util.simple_utc import simple_utc

with open("config/credentials.json", "r") as f:
    awscredentials = json.load(f)

awsregion = "us-east-1"
cleanup_version = 1
current_version = 2

ec2 = boto3.client('ec2',
    region_name=awsregion,
    aws_access_key_id = awscredentials["aws_access_key_id"],
    aws_secret_access_key = awscredentials["aws_secret_access_key"])

def is_expired(taglist: List) -> bool:
    version = util.aws.get_tag(taglist, "arclight-version")
    if version is None:
        return True
    
    if int(version) <= cleanup_version:
        return True
    
    timestamp = util.aws.get_tag(taglist, "arclight-timeout")
    if timestamp is None:
        return False    # never expires
    
    timestamp = dateutil.parser.parse(timestamp)
    if timestamp < datetime.datetime.now().replace(tzinfo=simple_utc()):
        return True

def cleanup(category: str, extract = None, category_id: str = None, delete_name: str = None, destroy = None):
    print()
    print(f"{category}:")
    
    # set up defaults
    # category is "internet_gateways"
    category_singular = category[:-1] # internet_gateway
    category_cap = "".join([word.capitalize() for word in category.split("_")]) # InternetGateways
    category_cap_singular = category_cap[:-1] # InternetGateway
    if category_id is None:
        category_id = f"{category_cap_singular}Id" # InternetGatewayId
    
    if extract is None:
        extract = lambda result: result[category_cap]

    if delete_name is None:
        delete_name = f'delete_{category_singular}'
    
    # get functions
    descr = getattr(ec2, f'describe_{category}')
    if destroy is None: # otherwise we'll just be using destroy anyway
        delete = getattr(ec2, delete_name)
    
    # list everything
    # we're not providing a MaxResults so this should get us literally everything
    results = [descr(Filters = [{'Name':'tag:Name', 'Values':['arclight-*']}])]
    
    # extract actual objects
    items = list(itertools.chain.from_iterable([extract(result) for result in results]))
    
    for item in items:
        name = util.aws.get_tag(item["Tags"], "Name")
        if name is None:
            name = item[category_id]
        else:
            name = f"{name} {item[category_id]}"
        
        if util.aws.get_tag(item["Tags"], "Name") == "arclight-base":
            print(f"  Skipping {name}")
            continue
        
        if is_expired(item["Tags"]):
            print(f"  Cleaning up {name}")
            if destroy is not None:
                destroy(item[category_id])
            else:
                delete(**{category_id: item[category_id]})
            continue

        print(f"  Keeping {name}")

cleanup("images", delete_name = "deregister_image")
cleanup("instances",
    extract = lambda result: itertools.chain.from_iterable([reservation["Instances"] for reservation in result["Reservations"]]),
    destroy = lambda instid: ec2.terminate_instances(InstanceIds = [instid]))
cleanup("snapshots")
cleanup("volumes")
cleanup("subnets")
cleanup("security_groups", category_id = "GroupId")
cleanup("route_tables")
def igw_cleanup(igwid):
    ec2.detach_internet_gateway(InternetGatewayId = igwid)
    ec2.delete_internet_gateway(InternetGatewayId = igwid)
cleanup("internet_gateways", destroy = igw_cleanup) # must be after route_tables
cleanup("vpcs") # must be after subnets, security_groups, route_tables, internet_gateways

# Along with just killing straight-up expired snapshots, we want to wipe all snapshots that are older than the oldest complete snapshot in each (version, stream)
print("")
print("obsolete snapshot processing:")
for v in range(cleanup_version + 1, current_version + 1):
    snapshots = ec2.describe_snapshots(Filters = [
            {'Name': 'tag:Name', 'Values': ['arclight-*']},
            {'Name': 'tag:arclight-version', 'Values': [str(v)]},
        ])["Snapshots"]
    
    streams = {}
    for snapshot in snapshots:
        stream = util.aws.get_tag(snapshot["Tags"], "arclight-sig-stream")
        if stream is None:
            continue
        
        streams[stream] = True
    
    for stream in streams.keys():
        print(f"  Processing v{v}-{stream}")
        bestcl = 0
        bestsnapshotid = None
        
        consideration = []
        
        for snapshot in snapshots:
            if snapshot["State"] != "completed":
                continue
            
            if util.aws.get_tag(snapshot["Tags"], "arclight-sig-stream") != stream:
                continue
            
            thiscl = int(util.aws.get_tag(snapshot["Tags"], "arclight-sig-cl"))
            if thiscl > bestcl:
                bestcl = thiscl
                bestsnapshotid = snapshot["SnapshotId"]

        for snapshot in snapshots:
            if util.aws.get_tag(snapshot["Tags"], "arclight-sig-stream") != stream:
                continue
            
            sid = snapshot['SnapshotId']
            if sid == bestsnapshotid:
                print(f"    Keeping {sid} (@{bestcl} and complete)")
                continue
            
            thiscl = int(util.aws.get_tag(snapshot["Tags"], "arclight-sig-cl"))
            if thiscl > bestcl:
                print(f"    Keeping {sid} (@{thiscl} > @{bestcl})")
                continue
                
            if thiscl == bestcl:
                print(f"    Cleaning up {sid} (@{thiscl} == @{bestcl} but not chosen)")
            else:
                print(f"    Cleaning up {sid} (@{thiscl} <= @{bestcl})")
        
            ec2.delete_snapshot(SnapshotId = sid)

        print()

# TODO: ecr cleanup
# TODO: s3 cleanup
# TODO: p4 cleanup