FROM airship/ue4-build-prerequisites

# Right now ue4-docker installs 3.7 and we want to use some 3.9 features
RUN choco install -y python --version=3.9.7

# Used for compressing results
RUN choco install -y 7zip

# p4python is needed for p4 sync support
# boto3 is needed to copy results to s3
# psutil is needed to figure out how much memory the host system has in build.py
# requests and scrypt and rauth are used for something in our build scripts
RUN pip install p4python boto3 psutil requests scrypt rauth

# Install necessary binary packages!
WORKDIR C:\\installers

# p4
COPY p4vinst64.msi .
RUN msiexec /i p4vinst64.msi /quiet /norestart ADDLOCAL=ALL

# enable p4 unicode mode globally
ENV P4CHARSET=utf8-bom

# UE4 Linux build support
# COPY v19_clang-11.0.1-centos7.exe .
# RUN v19_clang-11.0.1-centos7.exe /S

# (add console SDKs in here)

# Clean up
WORKDIR C:\\
RUN rmdir C:\\installers /s /q

# Get ready to actually run
# Adding bootstrap is intentionally last because we change it *all the time*
WORKDIR C:\\bootstrap
COPY bootstrap.py .
ENTRYPOINT python -u c:\\bootstrap\\bootstrap.py
