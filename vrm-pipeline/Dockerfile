# --- Stage 1: Rust builder ---
FROM rust:1.79-slim AS rust-builder
WORKDIR /build
COPY Cargo.toml Cargo.lock* ./
COPY crates/ ./crates/
RUN cargo build --release

# --- Stage 2: Blender runtime ---
FROM ubuntu:22.04 AS runtime

# Avoid interactive prompts during apt
ENV DEBIAN_FRONTEND=noninteractive

# Install dependencies for Blender headless
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglx0 \
    libglu1-mesa \
    libxi6 \
    libxrender1 \
    libxfixes3 \
    libxxf86vm1 \
    libfontconfig1 \
    libfreetype6 \
    wget \
    ca-certificates \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Blender 4.2.0 LTS
RUN wget -q "https://download.blender.org/release/Blender4.2/blender-4.2.0-linux-x64.tar.xz" -O /tmp/blender.tar.xz \
    && tar -xJf /tmp/blender.tar.xz -C /opt \
    && mv /opt/blender-4.2.0-linux-x64 /opt/blender \
    && rm /tmp/blender.tar.xz

ENV PATH="/opt/blender:${PATH}"

# Install VRM Add-on for Blender 2.20.77
RUN wget -q "https://github.com/saturday06/VRM-Addon-for-Blender/releases/download/2_20_77/VRM_Addon_for_Blender-2_20_77.zip" \
        -O /tmp/vrm_addon.zip \
    && mkdir -p /opt/blender/4.2/scripts/addons/VRM_Addon_for_Blender \
    && unzip -q /tmp/vrm_addon.zip -d /opt/blender/4.2/scripts/addons/VRM_Addon_for_Blender \
    && rm /tmp/vrm_addon.zip

# Copy Rust binaries
COPY --from=rust-builder /build/target/release/ledger /usr/local/bin/
COPY --from=rust-builder /build/target/release/dist   /usr/local/bin/
COPY --from=rust-builder /build/target/release/vrm-watch /usr/local/bin/

# Copy render script
COPY render.py /opt/vrm-pipeline/render.py

WORKDIR /opt/vrm-pipeline
