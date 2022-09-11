
# Returns a relatively simple dict with four members
# baseimage: The tag of the `windows/servercore` image that we'll be using
# dllsrcimage: The tag of the `windows` image that we'll be cloning DLLs from
# buildisolation: Either `process` or `hyperv`; the isolation mode that we generate the image with
# runisolation: Either `process` or `hyperv`; the isolation mode that we run the image with

def local():
    # We *should* be querying the OS version in order to figure out the best version.
    # Right now we aren't! This was written when I was on Windows 10 21H1 and I haven't needed to change the settings yet.
    # This means we can't use process isolation anyway (it's complicated okay) so we just fall back on the Windows Server Core settings.
    return {
        "baseimage": "ltsc2019",
        "dllsrcimage": "1809",      # yes I know these aren't consistent; this is the compatible windows version for ltsc2019 images (see ue4docker's WindowsUtils.py)
        "buildisolation": "hyperv",
        "runisolation": "hyperv",
    }

def aws_2019():
    # Settings for running on AWS Windows Server Core 2019
    return {
        "baseimage": "ltsc2019",
        "dllsrcimage": "1809", # yes I know these aren't consistent; this is the compatible windows version for ltsc2019 images (see ue4docker's WindowsUtils.py)
        "buildisolation": "hyperv", # We chose 2019 over 2022 because it *can* be built locally with hyperv on Win10; 2022 can't be built on windows 10 at all.
        "runisolation": "process", # Of course, this is the entire point of choosing carefully; being able to do the faster process isolation at runtime.
    }
