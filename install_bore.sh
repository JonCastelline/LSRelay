#!/usr/bin/env bash
set -e

echo "=== Installing Bore (TCP Tunnel) ==="

ARCH=$(uname -m)
if [ "$ARCH" = "aarch64" ]; then
    URL="https://github.com/ekzhang/bore/releases/download/v0.6.0/bore-v0.6.0-aarch64-unknown-linux-musl.tar.gz"
elif [ "$ARCH" = "armv7l" ]; then
    URL="https://github.com/ekzhang/bore/releases/download/v0.6.0/bore-v0.6.0-armv7-unknown-linux-musleabihf.tar.gz"
elif [ "$ARCH" = "x86_64" ]; then
    URL="https://github.com/ekzhang/bore/releases/download/v0.6.0/bore-v0.6.0-x86_64-unknown-linux-musl.tar.gz"
else
    echo "Unsupported architecture: $ARCH"
    exit 1
fi

echo "Downloading bore for $ARCH..."
curl -SsL "$URL" -o /tmp/bore.tar.gz
tar -xzf /tmp/bore.tar.gz -C /tmp/
sudo mv /tmp/bore /usr/local/bin/bore
sudo chmod +x /usr/local/bin/bore
rm -f /tmp/bore.tar.gz

echo "Bore installed successfully: $(bore --version)"
echo ""
echo "Starting test tunnel for port 45124..."
bore local 45124 --to bore.pub --port 45124
