#!/usr/bin/env bash
set -e

echo "=== Installing Playit.gg Repository and Package ==="

# Download and install Playit GPG signing key
curl -SsL https://playit-cloud.github.io/ppa/key.gpg | gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/playit.gpg >/dev/null

# Add Playit APT repository
echo "deb [signed-by=/etc/apt/trusted.gpg.d/playit.gpg] https://playit-cloud.github.io/ppa/data ./" | sudo tee /etc/apt/sources.list.d/playit.list

# Update package lists and install playit
sudo apt update
sudo apt install playit -y

echo "=== Installation Complete ==="
echo "Now running 'playit' to generate your claim link."
echo "Visit the URL shown below in your browser, then press Ctrl+C when claimed."
echo ""
playit
