FROM 3arthqu4ke/headlessmc:latest

# Install Python 3 (required by Minescript for .py scripts) and screen (for CLI interaction)
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
    python3 python3-pip screen curl \
    xvfb mesa-utils libgl1-mesa-dri libgl1 ffmpeg \
    > /dev/null 2>&1 && \
    rm -rf /var/lib/apt/lists/* && \
    ln -sf /usr/bin/python3 /usr/bin/python

# Configure HeadlessMC for headless operation
RUN mkdir -p /headlessmc/HeadlessMC && \
    echo "hmc.offline=true" >> /headlessmc/HeadlessMC/config.properties && \
    echo "hmc.offline.username=Claude" >> /headlessmc/HeadlessMC/config.properties && \
    echo "hmc.check.xvfb=true" >> /headlessmc/HeadlessMC/config.properties && \
    echo "hmc.assets.dummy=false" >> /headlessmc/HeadlessMC/config.properties && \
    echo "hmc.always.lwjgl.flag=false" >> /headlessmc/HeadlessMC/config.properties

# Download and install Fabric for MC 1.21.5
RUN hmc download fabric:1.21.5

# Copy mod JARs into the Fabric mods directory
# The mods dir location depends on the MC instance path; we copy after launch dir is created
COPY mc-client/mods/*.jar /tmp/mods/

# Install aiohttp for bridge HTTP+WS server, mcrcon for server commands
RUN pip3 install --break-system-packages aiohttp mcrcon Pillow

# Copy test scripts
COPY mc-client/scripts/ /tmp/scripts/

# Copy MC render options
COPY mc-client/options.txt /tmp/options.txt

# Copy bridge package
COPY bridge/ /tmp/bridge/

# Startup script that:
# 1. Copies mods into the correct mods folder
# 2. Launches the headless client
COPY mc-client/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
