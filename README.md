Arclight is a Docker-based distributed build and test system designed for games and intended to be run through a build harness such as Jenkins. I plan to clean this up and make it more human-usable at some point, but right now I'm just uploading this for a friend.

This is not as documented as it should be; there be dragons here.

Various notes:

Download the latest version of p4v and put the installer, which should be named "p4vinst64.msi", in `image/project_build/environment`.

If you're running Unreal Engine and need Linux build support, download the appropriate Linux Clang cross-compliation package, put it in `image/project_build/environment`, and edit the Dockerfile to uncomment-out the installation (maybe changing the filename based on the version you need.)

This includes a complete copy of ue4-docker. You can probably use the latest version, I don't think the included version is customized.

You'll need to fill in the bottom of `build.py`; our inhouse version currently relies on scripts that we don't have the ability to redistribute. There's also a bit near the bottom of `bootstrap.py`, search for `SORRY, CAN'T MAKE THIS PART PUBLIC`.

This was developed under Airship Syndicate, who graciously allowed me to open-source it. Thanks, guys! Buy their games!

If you have trouble with this, feel free to pester me on Discord (ZorbaTHut@4936); I don't guarantee I can solve all your problems but I do want to gradually shape this up.